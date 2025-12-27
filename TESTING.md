# Testing Guide

Use this checklist to exercise the duplicate finder across exact and perceptual modes. Commands assume the repo root.

## Setup

- Install deps: `pip install -r requirements.txt` (or `pip install flask pillow python-dotenv rich numpy`).
- Prepare a small fixture tree with:
  - Identical files (same bytes).
  - Same image saved at different sizes / JPEG qualities.
  - Visually similar but different scenes (to probe thresholds).
  - At least one non-image file to confirm it is skipped/handled.

## Exact mode (`--mode exact`)

- Run: `python find_dupe_images.py tests/fixtures --decision-folder _DECISION_DUPES_EXACT --mode exact --no-open`.
- Confirm staging log shows only exact groups, and group folder names contain `exact/sha`.
- Open the UI manually; toggle auto-finish on and verify selecting exactly one image auto-completes the group.
- Ensure `_group_meta.json` exists inside staged groups with `"mode": "exact"`.
- Finish a group and verify kept files are restored to their original path (or a collision-safe variant) and other copies are deleted; decision folder cleans up.

## Perceptual modes (`--mode ahash` / `--mode phash`)

- Run with a strict threshold (e.g., `--mode ahash --threshold 3`). Confirm log line shows aHash near-duplicate group count.
- Open the UI and verify the status pill shows `mode: ahash` (or `phash`), and the Auto-finish button is disabled/marked N/A. Confirm toggling `/api/toggle_auto_finish` is rejected (button should not call it).
- Select items and finish; ensure state saves across refreshes and `_review_state.json` records keep selections.
- Inspect `_group_meta.json` for each group: mode matches the run, and threshold is recorded.
- Re-run with a looser threshold (e.g., 8) and verify additional similar-but-not-identical images cluster together. Confirm obviously different images remain in separate groups.
- Compare ahash vs phash on resized/compressed images; pHash should remain stable across light resizing, while aHash may drift with lighting.

## Resume behavior

- Stop the process mid-run with staged groups present. Re-run the command with the same `--decision-folder`; confirm it logs the pending group count and skips rescanning.
- Verify previously staged perceptual groups still report the correct mode/threshold in the UI and API.

## UI and API checks

- Folder preference: mark a preferred folder in one group, then load another group containing that folder path and confirm auto-selection of that folder's items.
- Reset finished: hit "Reset Finished" and confirm finished click count resets without altering keep selections.
- File serving safety: attempt to request `/files/../somefile` and confirm it is rejected (400/404).

## Edge cases

- Locked/readonly files: open an image externally to hold a lock, finish the group, and ensure the script logs the retry/quarantine message instead of crashing.
- Include-all: run with `--include-all` and confirm non-image files are skipped with a warning, not a crash.
- Large batches: spot-check performance on a directory with hundreds of images to ensure hashing completes without obvious slowdowns or memory issues.
