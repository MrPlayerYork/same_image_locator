from __future__ import annotations

import argparse
import hashlib
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from web_review import ReviewServer, serve_review_ui

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
    for bucket in by_size.values():
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


def rmtree_with_retry(dir_path: Path, retries: int = 40, delay: float = 0.15) -> bool:
    """
    Delete a directory tree, retrying on WinError 32 / PermissionError.
    Returns True if removed, False if still present after retries.
    """
    for _ in range(retries):
        try:
            if dir_path.exists():
                shutil.rmtree(dir_path)
            return True
        except PermissionError:
            time.sleep(delay)
        except FileNotFoundError:
            return True
    return not dir_path.exists()


def delete_with_retry(path: Path, retries: int = 12, delay: float = 0.15) -> bool:
    """
    Windows can lock files if they're being served/viewed.
    Retry a few times before giving up.
    Returns True if deleted, False otherwise.
    """
    for i in range(retries):
        try:
            path.unlink(missing_ok=True)
            return True
        except PermissionError:
            time.sleep(delay)
        except FileNotFoundError:
            return True
    return False


def copy_with_retry(
    src: Path, dst: Path, retries: int = 40, delay: float = 0.10
) -> None:
    for _ in range(retries):
        try:
            shutil.copy2(src, dst)
            return
        except PermissionError:
            time.sleep(delay)
    shutil.copy2(src, dst)  # last try


def restore_selected_and_delete_rest(group_dir: Path, keep_names: set[str]) -> None:
    mapping = read_manifest(group_dir)

    present = [
        p
        for p in group_dir.iterdir()
        if p.is_file()
        and p.name.lower() not in {MANIFEST_NAME.lower(), "_review_state.json"}
    ]

    for moved_path in present:
        name = moved_path.name
        if name not in keep_names:
            if not delete_with_retry(moved_path):
                pending = moved_path.with_name(moved_path.name + ".pending_delete")
                try:
                    moved_path.rename(pending)
                    delete_with_retry(pending)
                except Exception:
                    print(f"⚠️ Could not delete (locked): {moved_path}")
            continue

        original = mapping.get(moved_path)
        if original is None:
            for k, v in mapping.items():
                if k.name == name:
                    original = v
                    break

        if original is None:
            # safer: leave it there if we can't map
            continue

        original = original.expanduser()
        original.parent.mkdir(parents=True, exist_ok=True)

        if original.exists():
            original = safe_unique_name(original.parent, original.name)

        copy_with_retry(moved_path, original)

        if not delete_with_retry(moved_path):
            # can't delete because it's locked -> don't crash, quarantine it
            stuck = moved_path.with_name(moved_path.name + ".stuck")
            try:
                moved_path.rename(stuck)
                print(f"⚠️ Source locked, left behind as: {stuck}")
            except Exception:
                print(f"⚠️ Source locked and couldn't rename: {moved_path}")


def stage_all_groups_into_decision_folder(
    groups: list[DupeGroup], decision_root: Path, dry_run: bool
) -> list[Path]:
    """
    Moves ALL groups into decision folders first, writes manifests.
    Returns list of created group directories, in processing order.
    """
    created: list[Path] = []

    for i, grp in enumerate(groups, 1):
        group_dir = decision_root / f"group_{i:04d}_sha_{grp.digest[:10]}"
        if group_dir.exists():
            # If it already exists, treat it as staged and include it.
            created.append(group_dir)
            continue

        if dry_run:
            print(f"(dry-run) Would stage group into: {group_dir}")
            continue

        group_dir.mkdir(parents=True, exist_ok=False)
        moved_to_original: list[tuple[Path, Path]] = []

        for original in grp.files:
            dst = safe_unique_name(group_dir, original.name)
            shutil.move(str(original), str(dst))
            moved_to_original.append((dst, original))

        write_manifest(group_dir, moved_to_original)
        created.append(group_dir)

    return created


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage all exact dupes, then review via local Flask UI."
    )
    ap.add_argument("root", type=Path, help="Root folder to scan")
    ap.add_argument("--decision-folder", type=Path, default=Path("_DECISION_DUPES"))
    ap.add_argument("--include-all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5173)
    ap.add_argument("--no-open", action="store_true")
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

    print("\nStaging all groups into the decision folder...")
    group_dirs = stage_all_groups_into_decision_folder(
        groups, decision_root, dry_run=args.dry_run
    )

    if args.dry_run:
        print("\nDry-run complete.")
        return 0

    print(f"Staged groups ready to review: {len(group_dirs)}")

    server = ReviewServer(host=args.host, port=args.port)

    for idx, group_dir in enumerate(group_dirs, 1):
        if not (group_dir / MANIFEST_NAME).exists():
            print(f"Skipping (no manifest): {group_dir}")
            continue

        print("\n" + "=" * 72)
        print(f"Review {idx}/{len(group_dirs)}: {group_dir.name}")
        result = serve_review_ui(
            server, group_dir, open_browser=(not args.no_open), mode="exact"
        )

        restore_selected_and_delete_rest(group_dir, result.keep_names)
        if not rmtree_with_retry(group_dir):
            print(f"⚠️ Could not remove group folder (still locked): {group_dir}")
        print("Applied selection. Next group...")

    print("\nAll groups processed. 🎉")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
