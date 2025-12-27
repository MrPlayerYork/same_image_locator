from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageOps
from rich.live import Live

from modules.ui_console import UISplit
from modules.web_review import ReviewServer, serve_review_ui

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
GROUP_META_NAME = "_group_meta.json"


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
    mode: str = "exact"
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HashedImage:
    path: Path
    value: int


def hamming_distance(a: int, b: int) -> int:
    return int(a ^ b).bit_count()


def average_hash_64(path: Path, hash_size: int = 8) -> int:
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img).convert("L")
        img = img.resize((hash_size, hash_size), Image.Resampling.LANCZOS)
        pixels = list(img.getdata())  # type: ignore

    avg = sum(pixels) / len(pixels)
    bits = 0
    for px in pixels:
        bits = (bits << 1) | (1 if px >= avg else 0)
    return bits


@functools.lru_cache(maxsize=None)
def _dct_basis(n: int) -> np.ndarray:
    """
    Precompute the DCT-II transform matrix for an n x n block.
    """
    x = np.arange(n)
    mat = np.cos((math.pi / n) * (x[:, None] + 0.5) * x[None, :])
    mat[0, :] *= 1 / math.sqrt(n)
    mat[1:, :] *= math.sqrt(2 / n)
    return mat


def _dct_2d(block: np.ndarray) -> np.ndarray:
    """
    Lightweight 2D DCT-II using a precomputed basis matrix.
    """
    n, m = block.shape
    if n != m:
        raise ValueError("DCT block must be square")
    basis = _dct_basis(n)
    return basis @ block @ basis.T


def phash_64(path: Path, hash_size: int = 8, highfreq_factor: int = 4) -> int:
    dim = hash_size * highfreq_factor
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img).convert("L")
        img = img.resize((dim, dim), Image.Resampling.LANCZOS)
        block = np.asarray(img, dtype=float)

    dct = _dct_2d(block)
    low_freq = dct[1 : hash_size + 1, 1 : hash_size + 1]
    flat = low_freq.flatten()
    avg = flat.mean()

    bits = 0
    for val in flat:
        bits = (bits << 1) | (1 if val >= avg else 0)
    return bits


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
        DupeGroup(digest=k, files=tuple(sorted(v)), mode="exact")
        for k, v in by_hash.items()
        if len(v) > 1
    ]
    groups.sort(key=lambda g: (-len(g.files), g.digest))
    return groups


def _hash_file_safe(
    path: Path,
    fn: Callable[[Path], int],
    _render: UISplit,
) -> int | None:
    try:
        return fn(path)
    except Exception as exc:
        _render.log_main(f"?? Skipping (cannot hash): {path} ({exc})")
        return None


def _perceptual_groups(
    files: list[Path],
    mode: str,
    threshold: int,
    _render: UISplit,
    hash_size: int = 8,
) -> list[DupeGroup]:
    assert mode in {"ahash", "phash"}
    hasher: Callable[[Path], int]
    if mode == "ahash":
        hasher = functools.partial(average_hash_64, hash_size=hash_size)
    else:
        hasher = functools.partial(phash_64, hash_size=hash_size)

    hashed: list[HashedImage] = []
    for p in sorted(files):
        hv = _hash_file_safe(p, hasher, _render=_render)
        if hv is not None:
            hashed.append(HashedImage(path=p, value=hv))

    _render.log_main(
        f"Hashed {len(hashed)} file(s) with {mode.upper()} (skipped {len(files) - len(hashed)})."
    )

    groups: list[DupeGroup] = []
    remaining = hashed
    while remaining:
        seed = remaining[0]
        rest = remaining[1:]
        cluster = [seed]
        survivors: list[HashedImage] = []
        for candidate in rest:
            if hamming_distance(seed.value, candidate.value) <= threshold:
                cluster.append(candidate)
            else:
                survivors.append(candidate)
        remaining = survivors

        if len(cluster) > 1:
            digest = f"{seed.value:016x}"
            paths = tuple(sorted([c.path for c in cluster]))
            groups.append(
                DupeGroup(
                    digest=digest,
                    files=paths,
                    mode=mode,
                    meta={"threshold": threshold, "hash_size": hash_size},
                )
            )

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


def write_group_meta(group_dir: Path, group: DupeGroup) -> None:
    meta = {
        "mode": group.mode,
        "digest": group.digest,
        "count": len(group.files),
    }
    meta.update(group.meta)
    (group_dir / GROUP_META_NAME).write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


def read_group_meta(group_dir: Path) -> dict[str, Any]:
    path = group_dir / GROUP_META_NAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


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


def restore_selected_and_delete_rest(
    group_dir: Path, keep_names: set[str], _render: UISplit
) -> None:
    mapping = read_manifest(group_dir)

    ignore = {MANIFEST_NAME.lower(), GROUP_META_NAME.lower(), "_review_state.json"}
    present = [
        p for p in group_dir.iterdir() if p.is_file() and p.name.lower() not in ignore
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
                    _render.log_main(f"[WARN] Could not delete (locked): {moved_path}")
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
                _render.log_main(f"[WARN] Source locked, left behind as: {stuck}")
            except Exception:
                _render.log_main(
                    f"[WARN] Source locked and couldn't rename: {moved_path}"
                )


def stage_all_groups_into_decision_folder(
    groups: list[DupeGroup], decision_root: Path, dry_run: bool, _render: UISplit
) -> list[Path]:
    """
    Moves ALL groups into decision folders first, writes manifests.
    Returns list of created group directories, in processing order.
    """
    created: list[Path] = []

    for i, grp in enumerate(groups, 1):
        if grp.mode == "exact":
            suffix = f"sha_{grp.digest[:10]}"
        else:
            threshold = grp.meta.get("threshold")
            th_s = f"t{int(threshold):02d}_" if isinstance(threshold, int) else ""
            suffix = f"{grp.mode}_{th_s}{grp.digest[:12]}"

        group_dir = decision_root / f"group_{i:04d}_{suffix}"
        if group_dir.exists():
            # If it already exists, treat it as staged and include it.
            if not dry_run and not (group_dir / GROUP_META_NAME).exists():
                write_group_meta(group_dir, grp)
            created.append(group_dir)
            continue

        if dry_run:
            _render.log_main(f"(dry-run) Would stage group into: {group_dir}")
            continue

        group_dir.mkdir(parents=True, exist_ok=False)
        moved_to_original: list[tuple[Path, Path]] = []

        for original in grp.files:
            dst = safe_unique_name(group_dir, original.name)
            shutil.move(str(original), str(dst))
            moved_to_original.append((dst, original))

        write_manifest(group_dir, moved_to_original)
        write_group_meta(group_dir, grp)
        created.append(group_dir)

    return created


def find_pending_group_dirs(decision_root: Path) -> list[Path]:
    if not decision_root.exists():
        return []
    group_dirs = [p for p in decision_root.glob("group_*") if p.is_dir()]
    # sort by name so group_0001, group_0002...
    return sorted(group_dirs, key=lambda p: p.name)


def group_mode_from_dir(group_dir: Path) -> str:
    meta = read_group_meta(group_dir)
    mode = str(meta.get("mode", "exact")).lower()
    if mode not in {"exact", "ahash", "phash"}:
        return "exact"
    return mode


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage duplicate images, then review via local Flask UI."
    )
    ap.add_argument("root", type=Path, help="Root folder to scan")
    ap.add_argument("--decision-folder", type=Path, default=Path("_DECISION_DUPES"))
    ap.add_argument("--include-all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--mode",
        choices=["exact", "ahash", "phash"],
        default="exact",
        help="Duplicate detection mode.",
    )
    ap.add_argument(
        "--threshold",
        type=int,
        default=5,
        help="Hamming distance threshold for aHash/pHash grouping (0-64).",
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5173)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    root = args.root.expanduser().resolve()
    decision_root = args.decision_folder.expanduser().resolve()
    threshold = max(0, min(64, int(args.threshold)))

    ui = UISplit()
    with Live(ui.layout, console=ui.console, refresh_per_second=10, screen=True):
        if not root.exists():
            ui.log_main(f"Root does not exist: {root}")
            return 2

        decision_root.mkdir(parents=True, exist_ok=True)
        ui.log_main(f"Decision folder: {decision_root}")
        ui.log_main(
            f"Detection mode: {args.mode} "
            f"(threshold={threshold if args.mode != 'exact' else 'n/a'})"
        )

        pending = find_pending_group_dirs(decision_root)
        if pending:
            ui.log_main(
                f"[WARN] Found {len(pending)} existing group folder(s) in {decision_root}."
            )
            ui.log_main("Resuming review from decision folder (skipping rescan/hash).")
            group_dirs = pending
        else:
            files = list(iter_files(root, include_all=args.include_all))
            ui.log_main(f"Scanned: {len(files)} files under {root}")

            if args.mode == "exact":
                groups = find_exact_groups(files)
                ui.log_main(f"Exact duplicate groups found: {len(groups)}")
            else:
                groups = _perceptual_groups(
                    files=files,
                    mode=args.mode,
                    threshold=threshold,
                    _render=ui,
                )
                ui.log_main(
                    f"{args.mode.upper()} near-duplicate groups found: {len(groups)} "
                    f"(threshold={threshold})"
                )
            if not groups:
                return 0

            ui.log_main("\nStaging all groups into the decision folder...")
            group_dirs = stage_all_groups_into_decision_folder(
                groups, decision_root, dry_run=args.dry_run, _render=ui
            )

        if args.dry_run:
            ui.log_main("\nDry-run complete.")
            return 0

        ui.log_main(f"Staged groups ready to review: {len(group_dirs)}")

        server = ReviewServer(host=args.host, port=args.port)

        from modules.web_review import attach_flask_logger

        attach_flask_logger(server._app, ui)

        for idx, group_dir in enumerate(group_dirs, 1):
            if not (group_dir / MANIFEST_NAME).exists():
                ui.log_main(f"Skipping (no manifest): {group_dir}")
                continue

            ui.log_main("\n" + "=" * 72)
            ui.log_main(f"Review {idx}/{len(group_dirs)}: {group_dir.name}")
            meta = read_group_meta(group_dir)
            mode = group_mode_from_dir(group_dir)
            if mode != "exact" and "threshold" in meta:
                ui.log_main(f" Mode: {mode} (threshold={meta.get('threshold')})")
            else:
                ui.log_main(f" Mode: {mode}")
            result = serve_review_ui(
                server, group_dir, open_browser=(not args.no_open), mode=mode
            )

            restore_selected_and_delete_rest(group_dir, result.keep_names, _render=ui)
            if not rmtree_with_retry(group_dir):
                ui.log_main(
                    f"[WARN] Could not remove group folder (still locked): {group_dir}"
                )
            ui.log_main("Applied selection. Next group...")

        # Final cleanup pass: remove any leftover group folders from this run or older runs
        ui.log_main("\nFinal cleanup: removing leftover group folders...")
        for p in sorted(decision_root.glob("group_*")):
            if p.is_dir():
                if not rmtree_with_retry(p):
                    ui.log_main(f"[WARN] Still locked, couldn't remove: {p}")

        # If decision_root is empty now, remove it too
        try:
            decision_root.rmdir()
            ui.log_main("Removed empty decision folder.")
        except OSError:
            pass

    print("\nAll groups processed. Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
