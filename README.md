# Same Image Locator

A CLI tool for staging and reviewing exact duplicate image files from a directory tree. It scans files by size and SHA-256, moves duplicate sets into numbered decision folders, and serves a local Flask UI so you can pick which copies to keep before the script copies them back to their originals and cleans up.

## Project structure

- `find_dupe_images.py` … orchestrates the scan, staging, review loop, and cleanup. It uses `modules.ui_console.UISplit` to show Rich-powered logs while the Flask server is running.
- `modules/web_review.py` … hosts the `/api` endpoints, maintains per-group state, exposes global folder preferences, and launches the browser-based review grid.
- `modules/ui_console.py` … keeps a split-pane terminal layout so Flask logs and the main script progress can be observed at once.
- `_manifest.tsv` files (one per duplicate group) map every staged file back to its original path, which lets the review UI show folder information and allows `find_dupe_images.py` to restore the chosen files after review.

## Requirements

- Python 3.13+
- Install dependencies with `pip install -r requirements.txt` (you can generate one from `pyproject.toml` if needed) or `pip install flask pillow python-dotenv rich`.

## How it works

1. `find_dupe_images.py` scans the provided root directory (image extensions in `IMG_EXTS`, unless `--include-all` is supplied).
2. Files are bucketed by size first, then hashed with SHA-256 to find exact duplicates.
3. Each duplicate set is moved atomically into a decision folder (`_DECISION_DUPES` by default) and annotated with `_manifest.tsv`.
4. A Flask server from `modules.web_review` serves the grouped files and remembers what you selected via `_review_state.json`.
5. After you pick which files to keep from each group in the browser, the script copies the kept files back (with collision-safe names) and deletes the rest, cleaning up the decision folder afterward.

## Usage

```bash
python find_dupe_images.py /path/to/photos
```

### Common options

- `--decision-folder PATH` – where staged duplicate folders live (default `_DECISION_DUPES`).
- `--include-all` – include every regular file instead of just common image extensions.
- `--dry-run` – stage groups and log actions without moving files or starting the review UI.
- `--host` / `--port` – control where Flask listens (`127.0.0.1` and `5173` by default).
- `--no-open` – skip opening the browser automatically if you prefer to navigate to the UI yourself.

## How to use

1. Run the script pointing at a directory tree. If you want to test without touching files, add `--dry-run`.
2. When duplicates are found, the script will either resume an existing `_DECISION_DUPES` folder or stage new groups. Keep an eye on the terminal logging pane for progress messages.
3. A browser window opens (unless `--no-open`) with thumbnails grouped by exact hash. Use the UI to mark which files to keep (checkboxes) and then finish the group.
4. Back in the terminal, `find_dupe_images.py` copies the kept files back to safe names, deletes the rest (with retries for locked files), and removes the group folder.
5. Repeat the browser review for each group until none remain. The script will clean up `_DECISION_DUPES` when it becomes empty.

If you have a workflow where one folder should always win, the UI remembers your preferred folders and auto-selects their files in later groups (see `modules/web_review.py` for preference handling).
