from __future__ import annotations

import argparse
import hashlib
import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from rich import print
from rich.traceback import install

from web_review import ReviewServer, serve_review_ui

install(show_locals=True)

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
MANIFEST_NAME = "_manifest.tsv"


def iter_files(root: Path, include_all: bool = False):
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


@dataclass(frozen=True)
class DupeGroup:
    digest: str
    files: tuple[Path, ...]


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

    groups = [
        DupeGroup(digest=k, files=tuple(sorted(v)))
        for k, v in by_hash.items()
        if len(v) > 1
    ]
    groups.sort(key=lambda g: (-len(g.files), g.digest))
    return groups


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
    (group_dir / MANIFEST_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def restore_selected_and_delete_rest(group_dir: Path, keep_names: set[str]) -> None:
    mapping = read_manifest(group_dir)

    # Files currently in decision group dir (excluding manifest + state)
    present = [
        p
        for p in group_dir.iterdir()
        if p.is_file()
        and p.name.lower() not in {MANIFEST_NAME.lower(), "_review_state.json"}
    ]

    # If user selected nothing, we interpret as "keep nothing" (and delete all).
    # You can change this behavior if you want.
    for moved_path in present:
        name = moved_path.name
        if name not in keep_names:
            moved_path.unlink(missing_ok=True)
            continue

        original = mapping.get(moved_path)
        if original is None:
            # fallback: match by filename
            for k, v in mapping.items():
                if k.name == name:
                    original = v
                    break

        if original is None:
            # Can't restore without mapping; leave it there (safer)
            continue

        original = original.expanduser()
        original.parent.mkdir(parents=True, exist_ok=True)

        if original.exists():
            # avoid overwrite
            original = safe_unique_name(original.parent, original.name)

        shutil.move(str(moved_path), str(original))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Interactive dupe cleanup with a local Flask UI (exact dupes)."
    )
    ap.add_argument("root", type=Path, help="Root folder to scan")
    ap.add_argument(
        "--decision-folder",
        type=Path,
        default=Path("_DECISION_DUPES"),
        help="Folder where duplicate groups are moved for review",
    )
    ap.add_argument(
        "--include-all",
        action="store_true",
        help="Scan all files, not just image extensions",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Find groups, but don't move/delete"
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5173)
    ap.add_argument(
        "--no-open", action="store_true", help="Don't auto-open the browser tab"
    )

    args = ap.parse_args()
    root = args.root.expanduser().resolve()
    decision_root = args.decision_folder.expanduser().resolve()

    if not root.exists():
        print(f"Root does not exist: {root}")
        return 2

    files = list(iter_files(root, include_all=args.include_all))
    print(f"Scanned: {len(files)} files under {root}")

    groups = find_exact_groups(files)
    print(f"Exact duplicate groups found: {len(groups)}")
    if not groups:
        return 0

    decision_root.mkdir(parents=True, exist_ok=True)
    print(f"Decision folder: {decision_root}")

    server = ReviewServer(host=args.host, port=args.port)

    for i, grp in enumerate(groups, 1):
        print("\n" + "=" * 72)
        print(
            f"Group {i}/{len(groups)} | {len(grp.files)} duplicates | SHA256 {grp.digest[:12]}..."
        )
        for p in grp.files:
            print(f"  - {p}")

        group_dir = decision_root / f"group_{i:04d}_sha_{grp.digest[:10]}"
        if group_dir.exists():
            print(f"⚠️  Group folder already exists, skipping: {group_dir}")
            continue

        if args.dry_run:
            print(f"(dry-run) Would move into: {group_dir}")
            continue

        group_dir.mkdir(parents=True, exist_ok=False)

        moved_to_original: list[tuple[Path, Path]] = []
        for original in grp.files:
            dst = safe_unique_name(group_dir, original.name)
            shutil.move(str(original), str(dst))
            moved_to_original.append((dst, original))

        write_manifest(group_dir, moved_to_original)

        print(f"\nReview this group in your browser: http://{args.host}:{args.port}")
        result = serve_review_ui(server, group_dir, open_browser=(not args.no_open))

        print(f"Confirmed keep count: {len(result.keep_names)}")

        restore_selected_and_delete_rest(group_dir, result.keep_names)
        print("Done. Moving to next group...")

    print("\nAll groups processed. 🎉")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
