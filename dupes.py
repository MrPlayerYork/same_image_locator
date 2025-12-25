#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

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
MANIFEST_NAME = "_manifest.tsv"  # moved_path<TAB>original_path
PREVIEW_HTML = "_preview.html"

# Pillow is optional unless using --mode phash
try:
    from PIL import Image  # type: ignore
except Exception:
    Image = None  # noqa: N816


# ----------------------------
# File discovery + exact hash
# ----------------------------
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


# ----------------------------
# Perceptual hash (aHash 64)
# ----------------------------
def ahash_64(path: Path, size: int = 8) -> int:
    if Image is None:
        raise RuntimeError("Pillow not installed. Install with: pip install pillow")
    with Image.open(path) as img:
        img = img.convert("L").resize((size, size), Image.Resampling.LANCZOS)
        pixels = list(img.getdata())
    avg = sum(pixels) / len(pixels)
    bits = 0
    for i, px in enumerate(pixels):
        if px >= avg:
            bits |= 1 << i
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def img_meta(path: Path) -> tuple[int | None, int | None]:
    """Return (w, h) or (None, None) if unreadable."""
    if Image is None:
        return (None, None)
    try:
        with Image.open(path) as img:
            return img.size[0], img.size[1]
    except Exception:
        return (None, None)


# ----------------------------
# Group definitions
# ----------------------------
@dataclass(frozen=True)
class DupeGroup:
    key: str
    mode: str  # "exact" or "phash"
    files: tuple[Path, ...]
    # phash diagnostics
    seed: Path | None = None
    threshold: int | None = None
    distances: tuple[tuple[Path, int], ...] = ()


# ----------------------------
# Exact duplicate grouping
# ----------------------------
def find_exact_groups(files: list[Path]) -> list[DupeGroup]:
    by_size: dict[int, list[Path]] = defaultdict(list)
    for f in files:
        try:
            by_size[f.stat().st_size].append(f)
        except OSError:
            continue

    by_hash: dict[str, list[Path]] = defaultdict(list)
    for _, bucket in by_size.items():
        if len(bucket) < 2:
            continue
        for f in bucket:
            try:
                digest = sha256_file(f)
                by_hash[digest].append(f)
            except OSError:
                continue

    groups: list[DupeGroup] = []
    for digest, bucket in by_hash.items():
        if len(bucket) > 1:
            groups.append(
                DupeGroup(key=digest, mode="exact", files=tuple(sorted(bucket)))
            )
    groups.sort(key=lambda g: (-len(g.files), g.key))
    return groups


# ----------------------------
# Perceptual duplicate grouping
# ----------------------------
def find_phash_groups(files: list[Path], threshold: int = 2) -> list[DupeGroup]:
    if Image is None:
        raise RuntimeError("Pillow not installed. Install with: pip install pillow")

    # Compute hashes (skip unreadable)
    hashed: list[tuple[Path, int]] = []
    for f in files:
        try:
            hashed.append((f, ahash_64(f)))
        except Exception:
            continue

    used: set[Path] = set()
    groups: list[DupeGroup] = []

    # Greedy clustering by seed
    for i, (seed_path, seed_hash) in enumerate(hashed):
        if seed_path in used:
            continue

        cluster: list[Path] = [seed_path]
        dists: list[tuple[Path, int]] = [(seed_path, 0)]
        used.add(seed_path)

        for j in range(i + 1, len(hashed)):
            p, h = hashed[j]
            if p in used:
                continue
            dist = hamming(seed_hash, h)
            if dist <= threshold:
                cluster.append(p)
                dists.append((p, dist))
                used.add(p)

        if len(cluster) > 1:
            # Sort cluster and distances for nicer printing
            cluster_sorted = tuple(sorted(cluster))
            dists_sorted = tuple(sorted(dists, key=lambda x: (x[1], str(x[0]))))
            groups.append(
                DupeGroup(
                    key=f"ahash<= {threshold}",
                    mode="phash",
                    files=cluster_sorted,
                    seed=seed_path,
                    threshold=threshold,
                    distances=dists_sorted,
                )
            )

    # Larger groups first
    groups.sort(key=lambda g: (-len(g.files), str(g.seed) if g.seed else ""))
    return groups


# ----------------------------
# Decision folder helpers
# ----------------------------
def safe_unique_name(dst_dir: Path, filename: str) -> Path:
    candidate = dst_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suf = candidate.suffix
    i = 1
    while True:
        c = dst_dir / f"{stem}__{i}{suf}"
        if not c.exists():
            return c
        i += 1


def write_manifest(group_dir: Path, moved_to_original: list[tuple[Path, Path]]) -> None:
    lines = [
        f"{moved.as_posix()}\t{original.as_posix()}"
        for moved, original in moved_to_original
    ]
    (group_dir / MANIFEST_NAME).write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
    )


def read_manifest(group_dir: Path) -> dict[Path, Path]:
    mapping: dict[Path, Path] = {}
    mpath = group_dir / MANIFEST_NAME
    if not mpath.exists():
        return mapping
    for line in mpath.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        moved_s, original_s = line.split("\t", 1)
        mapping[Path(moved_s)] = Path(original_s)
    return mapping


def wait_for_enter_or_single_file(group_dir: Path) -> None:
    enter_event = threading.Event()

    def _enter_listener():
        try:
            input(
                "\nPress Enter to finish this group (or delete until only one remains)... "
            )
        except EOFError:
            pass
        enter_event.set()

    threading.Thread(target=_enter_listener, daemon=True).start()

    while True:
        if enter_event.is_set():
            return

        remaining = [
            p
            for p in group_dir.iterdir()
            if p.is_file() and p.name not in {MANIFEST_NAME, PREVIEW_HTML}
        ]
        if len(remaining) <= 1:
            return

        time.sleep(0.75)


def move_back_survivors(group_dir: Path) -> None:
    mapping = read_manifest(group_dir)
    remaining = [
        p
        for p in group_dir.iterdir()
        if p.is_file() and p.name not in {MANIFEST_NAME, PREVIEW_HTML}
    ]

    if not remaining:
        print(
            "  No files left in decision folder (you deleted them all). Nothing to restore."
        )
        return

    for moved_path in remaining:
        original = mapping.get(moved_path)
        if original is None:
            # fallback: match by name
            for k, v in mapping.items():
                if k.name == moved_path.name:
                    original = v
                    break

        if original is None:
            print(
                f"  ⚠️  No manifest entry for {moved_path.name}; leaving it in decision folder."
            )
            continue

        original = original.expanduser()
        original.parent.mkdir(parents=True, exist_ok=True)

        if original.exists():
            alt = safe_unique_name(original.parent, original.name)
            print(f"  ⚠️  Original occupied: {original}")
            print(f"     Restoring survivor to: {alt}")
            original = alt

        shutil.move(str(moved_path), str(original))
        print(f"  Restored: {original}")


# ----------------------------
# Confidence report + preview
# ----------------------------
def confidence_label(avg_dist: float, max_dist: int, threshold: int) -> str:
    # Simple, human-friendly labeling (opinionated, but practical)
    if max_dist == 0:
        return "EXTREMELY likely (visually identical)"
    if max_dist <= max(1, threshold // 2):
        return "VERY likely"
    if avg_dist <= threshold * 0.75:
        return "LIKELY"
    return "MAYBE (double-check)"


def print_group_report(group: DupeGroup) -> None:
    if group.mode == "exact":
        print("Confidence: 100% (byte-for-byte identical)")
        return

    # phash mode
    dists = list(group.distances)
    # distances includes seed at 0
    only = [d for _, d in dists]
    avg = sum(only) / len(only) if only else 0.0
    mx = max(only) if only else 0
    thr = group.threshold or 0
    label = confidence_label(avg, mx, thr)

    print(f"Confidence: {label}")
    print(f"  Threshold: {thr}")
    print(f"  Distances: min={min(only) if only else 0}, avg={avg:.2f}, max={mx}")
    print("  Per-file distance to seed:")
    for p, d in dists:
        try:
            sz = p.stat().st_size
        except OSError:
            sz = -1
        w, h = img_meta(p)
        dims = f"{w}x{h}" if w and h else "?"
        print(f"    - d={d:2d}  {dims:>9}  {sz:>10} bytes  {p}")


def write_preview_html(group_dir: Path, title: str) -> None:
    # A lightweight local preview page. Browser will render images directly from the folder.
    imgs = [
        p
        for p in sorted(group_dir.iterdir())
        if p.is_file() and p.name not in {MANIFEST_NAME, PREVIEW_HTML}
    ]
    rows = []
    for p in imgs:
        # Use relative filenames so it stays portable
        name = p.name
        rows.append(
            f"""
            <div class="card">
              <div class="name">{name}</div>
              <img src="{name}" alt="{name}" loading="lazy"/>
            </div>
            """
        )

    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, Segoe UI, Arial; margin: 16px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 14px; }}
    .card {{ border: 1px solid #ddd; border-radius: 10px; padding: 10px; }}
    .name {{ font-size: 12px; color: #333; margin-bottom: 8px; word-break: break-all; }}
    img {{ width: 100%; height: auto; border-radius: 8px; }}
    .hint {{ color: #666; font-size: 13px; margin-bottom: 14px; }}
    code {{ background: #f3f3f3; padding: 2px 6px; border-radius: 6px; }}
  </style>
</head>
<body>
  <h2>{title}</h2>
  <div class="hint">
    Delete files in this folder. Finish by pressing <code>Enter</code> in the terminal, or leave only 1 file.
  </div>
  <div class="grid">
    {"".join(rows)}
  </div>
</body>
</html>
"""
    (group_dir / PREVIEW_HTML).write_text(html, encoding="utf-8")


def open_in_explorer(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}"')
    except Exception:
        pass


# ----------------------------
# Main
# ----------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Interactive duplicate image triage (exact + phash)."
    )
    ap.add_argument("root", type=Path, help="Root folder to scan")
    ap.add_argument(
        "--decision-folder",
        type=Path,
        default=Path("_DECISION_DUPES"),
        help="Folder where duplicate groups are moved for manual deletion",
    )
    ap.add_argument(
        "--include-all",
        action="store_true",
        help="Scan all files, not just image extensions",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Show what would happen, move nothing"
    )

    ap.add_argument(
        "--mode",
        choices=["exact", "phash"],
        default="exact",
        help="exact = byte-identical; phash = visually-similar candidates (requires Pillow)",
    )
    ap.add_argument(
        "--phash-threshold",
        type=int,
        default=2,
        help="Hamming distance for perceptual matches (0-10 typical). Lower = stricter.",
    )
    ap.add_argument(
        "--open",
        action="store_true",
        help="Auto-open each group folder in your file explorer",
    )
    ap.add_argument(
        "--preview",
        action="store_true",
        help="Write a local _preview.html in each group folder and open it (nice for phash)",
    )

    args = ap.parse_args()

    root = args.root.expanduser().resolve()
    decision_root = args.decision_folder.expanduser().resolve()

    if not root.exists():
        print(f"Root does not exist: {root}")
        return 2

    if args.mode == "phash" and Image is None:
        print("phash mode requires Pillow. Install with: pip install pillow")
        return 3

    files = list(iter_files(root, include_all=args.include_all))
    print(f"Scanned: {len(files)} files under {root}")

    if args.mode == "exact":
        groups = find_exact_groups(files)
    else:
        groups = find_phash_groups(files, threshold=args.phash_threshold)

    print(f"Groups found ({args.mode}): {len(groups)}")
    if not groups:
        return 0

    decision_root.mkdir(parents=True, exist_ok=True)
    print(f"Decision folder: {decision_root}")

    for gi, grp in enumerate(groups, 1):
        print("\n" + "=" * 78)
        if grp.mode == "exact":
            print(
                f"Group {gi}/{len(groups)} | {len(grp.files)} exact dupes | SHA256 {grp.key[:12]}..."
            )
        else:
            print(
                f"Group {gi}/{len(groups)} | {len(grp.files)} phash candidates | {grp.key}"
            )

        for p in grp.files:
            print(f"  - {p}")

        print_group_report(grp)

        group_dir = decision_root / f"group_{gi:04d}_{grp.mode}"
        if grp.mode == "exact":
            group_dir = decision_root / f"group_{gi:04d}_sha_{grp.key[:10]}"
        else:
            seed_tag = grp.seed.name[:20] if grp.seed else "seed"
            group_dir = decision_root / f"group_{gi:04d}_ph_{seed_tag}"

        if group_dir.exists():
            print(f"\n⚠️  Group folder already exists: {group_dir}")
            print("    Skipping to avoid messing with an in-progress group.")
            continue

        if args.dry_run:
            print("\n(dry-run) Would create:", group_dir)
            print("(dry-run) Would move files into it, then wait for your deletions.")
            continue

        group_dir.mkdir(parents=True, exist_ok=False)

        moved_to_original: list[tuple[Path, Path]] = []
        for original in grp.files:
            dst = safe_unique_name(group_dir, original.name)
            shutil.move(str(original), str(dst))
            moved_to_original.append((dst, original))

        write_manifest(group_dir, moved_to_original)

        if args.preview:
            title = f"{grp.mode.upper()} group {gi} ({len(grp.files)} files)"
            write_preview_html(group_dir, title)
            try:
                webbrowser.open((group_dir / PREVIEW_HTML).as_uri())
            except Exception:
                pass

        if args.open:
            open_in_explorer(group_dir)

        print(f"\nMoved {len(moved_to_original)} files into: {group_dir}")
        print("Delete the ones you don't want inside that folder.")
        print("Finish: press Enter OR leave only 1 file remaining there. ✅")

        wait_for_enter_or_single_file(group_dir)

        print("\nRestoring remaining file(s) to original locations...")
        move_back_survivors(group_dir)

        print(f"Done with group {gi}. (Group folder kept at {group_dir} for audit.)")

    print("\nAll groups processed. 🎉")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
