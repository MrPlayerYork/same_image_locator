#!/usr/bin/env python3
"""
Find duplicate images in a folder tree.

- Exact duplicates: byte-for-byte identical (fast, reliable)
- Perceptual duplicates (--phash): same image content even if re-encoded/resized slightly

Usage:
  python find_dupe_images.py "D:/Pictures"
  python find_dupe_images.py "D:/Pictures" --phash
  python find_dupe_images.py "D:/Pictures" --phash --phash-threshold 2
  python find_dupe_images.py "D:/Pictures" --delete-mode print
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable
from rich import print
from rich.traceback import install

install(show_locals=True)

# Optional dependency for perceptual hashing.
# Install: pip install pillow
try:
    from PIL import Image  # type: ignore
except Exception:
    Image = None  # noqa: N816


IMG_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tiff",
    ".tif",
    ".webp",
    ".heic",
    ".heif",
}


def iter_files(root: Path, include_all: bool = False) -> Iterable[Path]:
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if include_all:
            yield p
        else:
            if p.suffix.lower() in IMG_EXTS:
                yield p


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def ahash_64(path: Path, size: int = 8) -> int:
    """
    Average hash (aHash), 64-bit, quick perceptual hash.
    Good enough for "looks identical" hunting without extra libs.

    Returns an integer bitmask of length 64.
    """
    if Image is None:
        raise RuntimeError("Pillow not installed. Install with: pip install pillow")

    with Image.open(path) as img:
        img = img.convert("L").resize((size, size), Image.Resampling.LANCZOS)
        pixels = list(img.getdata())  # type: ignore
    avg = sum(pixels) / len(pixels)
    bits = 0
    for i, px in enumerate(pixels):
        if px >= avg:
            bits |= 1 << i
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


@dataclass(frozen=True)
class Group:
    key: str
    files: tuple[Path, ...]


def group_exact_duplicates(files: list[Path]) -> list[Group]:
    # 1) size filter
    by_size: dict[int, list[Path]] = defaultdict(list)
    for f in files:
        try:
            by_size[f.stat().st_size].append(f)
        except OSError:
            continue

    # 2) hash only within same-size buckets
    by_hash: dict[str, list[Path]] = defaultdict(list)
    for size, bucket in by_size.items():
        if len(bucket) < 2:
            continue
        for f in bucket:
            try:
                digest = sha256_file(f)
                by_hash[digest].append(f)
            except OSError:
                continue

    groups = []
    for digest, bucket in by_hash.items():
        if len(bucket) > 1:
            groups.append(Group(key=digest, files=tuple(sorted(bucket))))
    return groups


def group_perceptual_duplicates(files: list[Path], threshold: int = 2) -> list[Group]:
    """
    Groups images whose aHash differs by <= threshold.
    Note: This is a greedy clustering pass (fast, practical).
    """
    if Image is None:
        raise RuntimeError("Pillow not installed. Install with: pip install pillow")

    # size prefilter still helps; perceptual hash can be computed for all though
    hashes: list[tuple[Path, int]] = []
    for f in files:
        try:
            hashes.append((f, ahash_64(f)))
        except Exception:
            # skip unreadable/corrupt images
            continue

    used: set[Path] = set()
    groups: list[Group] = []

    # Greedy: pick a seed, pull in everything close to it.
    for i, (f, h) in enumerate(hashes):
        if f in used:
            continue
        cluster = [f]
        used.add(f)
        for j in range(i + 1, len(hashes)):
            g, hg = hashes[j]
            if g in used:
                continue
            if hamming(h, hg) <= threshold:
                cluster.append(g)
                used.add(g)
        if len(cluster) > 1:
            groups.append(
                Group(key=f"ahash<= {threshold}", files=tuple(sorted(cluster)))
            )
    return groups


def choose_keep_file(paths: list[Path], prefer: str = "largest") -> Path:
    if prefer == "largest":
        return max(paths, key=lambda p: p.stat().st_size)
    if prefer == "smallest":
        return min(paths, key=lambda p: p.stat().st_size)
    # default: keep the first sorted path
    return sorted(paths)[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Find duplicate images in a folder tree.")
    ap.add_argument("root", type=Path, help="Root folder to scan")
    ap.add_argument(
        "--include-all",
        action="store_true",
        help="Scan all files, not just image extensions",
    )
    ap.add_argument(
        "--phash",
        action="store_true",
        help="Also find perceptual duplicates (requires Pillow)",
    )
    ap.add_argument(
        "--phash-threshold",
        type=int,
        default=2,
        help="Hamming distance for perceptual matches (0-10 typical)",
    )
    ap.add_argument(
        "--keep",
        choices=["largest", "smallest", "first"],
        default="largest",
        help="When printing suggestions, which file would you keep?",
    )
    ap.add_argument(
        "--delete-mode",
        choices=["off", "print"],
        default="off",
        help="Never deletes. 'print' will print rm/del commands you can run yourself.",
    )
    args = ap.parse_args()

    root = args.root.expanduser().resolve()
    if not root.exists():
        print(f"Root does not exist: {root}")
        return 2

    files = list(iter_files(root, include_all=args.include_all))
    print(f"Scanned files: {len(files)} under {root}")

    exact = group_exact_duplicates(files)
    print(f"\nExact duplicate groups: {len(exact)}")
    for idx, grp in enumerate(exact, 1):
        paths = list(grp.files)
        keep = choose_keep_file(
            paths, prefer=args.keep if args.keep != "first" else "first"
        )
        print(f"\n[{idx}] SHA256 {grp.key[:12]}... ({len(paths)} files)")
        print(f"  Keep: {keep}")
        for p in paths:
            if p == keep:
                continue
            print(f"  Dupe: {p}")
        if args.delete_mode == "print":
            for p in paths:
                if p == keep:
                    continue
                # Cross-platform-ish suggestion
                if os.name == "nt":
                    print(f'  del "{p}"')
                else:
                    print(f'  rm "{p}"')

    if args.phash:
        if Image is None:
            print(
                "\n--phash requested but Pillow isn't installed. Install with: pip install pillow"
            )
            return 3
        ph = group_perceptual_duplicates(files, threshold=args.phash_threshold)
        print(
            f"\nPerceptual duplicate groups (aHash): {len(ph)} (threshold={args.phash_threshold})"
        )
        for idx, grp in enumerate(ph, 1):
            print(f"\n[P{idx}] {grp.key} ({len(grp.files)} files)")
            for p in grp.files:
                print(f"  {p}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
