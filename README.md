# Same Image Locator

A CLI tool for staging and reviewing duplicate or near-duplicate image files. It can scan by exact SHA-256 as well as perceptual hashes (aHash and pHash), move candidate sets into numbered decision folders, and serve a local Flask UI so you can pick which copies to keep before the script copies them back to their originals and cleans up.

__Recommended that you save a copy of your files before use as to not accidentally loose any files!__

## Project structure

- `find_dupe_images.py` . orchestrates the scan, staging, review loop, and cleanup. It uses `modules.ui_console.UISplit` to show Rich-powered logs while the Flask server is running and can build groups with SHA-256, aHash, or pHash.
- `modules/web_review.py` . hosts the `/api` endpoints, maintains per-group state, exposes global folder preferences, and launches the browser-based review grid.
- `modules/ui_console.py` . keeps a split-pane terminal layout so Flask logs and the main script progress can be observed at once.
- `_manifest.tsv` files (one per duplicate group) map every staged file back to its original path. `_group_meta.json` records the detection mode/threshold used for that group so resumed runs stay consistent.

## Requirements

- Python 3.13+
- Install dependencies with `pip install -r requirements.txt` (you can generate one from `pyproject.toml` if needed) or `pip install flask pillow python-dotenv rich numpy`.

## How it works

1. `find_dupe_images.py` scans the provided root directory (image extensions in `IMG_EXTS`, unless `--include-all` is supplied).
2. Depending on `--mode`:
   - `exact`: files are bucketed by size first, then hashed with SHA-256 to find byte-for-byte duplicates.
   - `ahash` / `phash`: each image is reduced to a 64-bit perceptual fingerprint; groups are formed greedily by Hamming distance, using `--threshold` as the cutoff for "looks similar enough."
3. Each candidate set is moved atomically into a decision folder (`_DECISION_DUPES` by default) and annotated with `_manifest.tsv` plus `_group_meta.json`.
4. A Flask server from `modules.web_review` serves the grouped files and remembers what you selected via `_review_state.json`.
5. After you pick which files to keep from each group in the browser, the script copies the kept files back (with collision-safe names) and deletes the rest, cleaning up the decision folder afterward.

## Usage

```bash
python find_dupe_images.py /path/to/photos
```

### Common options

- `--decision-folder PATH` - where staged duplicate folders live (default `_DECISION_DUPES`).
- `--mode {exact,ahash,phash}` - pick byte-for-byte (`exact`) or perceptual matching (`ahash` or `phash`).
- `--threshold N` - Hamming distance cutoff for perceptual modes (0-64). Lower = stricter. Ignored for exact mode.
- `--include-all` - include every regular file instead of just common image extensions.
- `--dry-run` - stage groups and log actions without moving files or starting the review UI.
- `--host` / `--port` - control where Flask listens (`127.0.0.1` and `5173` by default).
- `--no-open` - skip opening the browser automatically if you prefer to navigate to the UI yourself.

## How to use

1. Run the script pointing at a directory tree. If you want to test without touching files, add `--dry-run`.
2. When groups are found, the script will either resume an existing `_DECISION_DUPES` folder or stage new groups. Keep an eye on the terminal logging pane for progress messages.
3. A browser window opens (unless `--no-open`) with thumbnails grouped by the selected mode. Use the UI to mark which files to keep and then finish the group.
4. Back in the terminal, `find_dupe_images.py` copies the kept files back to safe names, deletes the rest (with retries for locked files), and removes the group folder.
5. Repeat the browser review for each group until none remain. The script will clean up `_DECISION_DUPES` when it becomes empty.

Auto-finish (double-click automation when exactly one image is selected) is only available in `exact` mode; it is automatically disabled for perceptual modes where human review matters more.

If you have a workflow where one folder should always win, the UI remembers your preferred folders and auto-selects their files in later groups (see `modules/web_review.py` for preference handling).

<div align="center">
  <a href="https://creativecommons.org/licenses/by-nc-sa/4.0/">
    <img src="https://mirrors.creativecommons.org/presskit/icons/cc.svg" alt="CC" width="25">
    <img src="https://mirrors.creativecommons.org/presskit/icons/by.svg" alt="BY" width="25">
    <img src="https://mirrors.creativecommons.org/presskit/icons/nc.svg" alt="NC" width="25">
    <img src="https://mirrors.creativecommons.org/presskit/icons/sa.svg" alt="SA" width="25">
  </a>
</div>
