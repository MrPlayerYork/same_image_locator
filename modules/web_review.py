from __future__ import annotations

import json
import threading
import time
import mimetypes
import logging
import flask.cli
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, request, Response, render_template

STATE_FILENAME = "_review_state.json"
MANIFEST_NAME = "_manifest.tsv"
GROUP_META_NAME = "_group_meta.json"


@dataclass
class ReviewResult:
    keep_names: set[str]
    confirmed: bool


class ReviewServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 5173):
        self.host = host
        self.port = port

        self._app = Flask(__name__, template_folder="templates", static_folder="static")
        self._thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._group_dir: Optional[Path] = None
        self._mode: str = "exact"  # "exact", "ahash", or "phash"
        self._result_ready = threading.Event()
        self._result: Optional[ReviewResult] = None

        self._browser_opened = False
        self._register_routes()

        # Global preferences across groups (most recent first)
        self._preferred_folder_order: list[str] = []
        self._auto_finish_global = False

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        flask.cli.show_server_banner = lambda *args, **kwargs: None
        logging.getLogger("werkzeug").setLevel(logging.INFO)

        def run():
            self._app.run(
                host=self.host,
                port=self.port,
                debug=False,
                use_reloader=False,
                threaded=True,
            )

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        time.sleep(0.35)

    def _apply_global_folder_preference_locked(self) -> None:
        """
        If any globally-preferred folder appears in this group, auto-select those files.
        Most-recent preferred folder wins (but we select *all* files in that folder within the group).
        """
        assert self._group_dir is not None
        items = self._list_items_locked()

        if not items or not self._preferred_folder_order:
            return

        # pick the most recently preferred folder that exists in this group
        folder_in_group = None
        folders_here = {it.get("folder_path") for it in items}
        for fp in self._preferred_folder_order:
            if fp in folders_here:
                folder_in_group = fp
                break

        if not folder_in_group:
            return

        keep = [it["name"] for it in items if it.get("folder_path") == folder_in_group]

        state = self._load_state_locked() or {}
        state["keep"] = sorted(set(keep))
        state["preferred_folder"] = folder_in_group
        # do NOT change finished_clicks here; no auto-finish.
        self._save_state_locked(state)

    def set_group(self, group_dir: Path, mode: str = "exact") -> None:
        group_dir = group_dir.resolve()
        with self._lock:
            self._group_dir = group_dir
            safe_mode = (mode or "exact").lower()
            if safe_mode not in {"exact", "ahash", "phash"}:
                safe_mode = "exact"
            self._mode = safe_mode
            self._result = None
            self._result_ready.clear()

            # init state if missing
            if self._load_state_locked() is None:
                self._save_state_locked(
                    {
                        "keep": [],
                        "finished_clicks": 0,
                        "auto_finish": False,
                        "preferred_folder": None,
                    }
                )
            # apply remembered preferences across groups
            self._apply_global_folder_preference_locked()
            state = self._load_state_locked() or {}
            state["auto_finish"] = (
                bool(self._auto_finish_global) if self._mode == "exact" else False
            )
            self._save_state_locked(state)

    def wait_result(self) -> ReviewResult:
        self._result_ready.wait()
        assert self._result is not None
        return self._result

    # ---------------------------
    # Helpers
    # ---------------------------
    def _state_path_locked(self) -> Path:
        assert self._group_dir is not None
        return self._group_dir / STATE_FILENAME

    def _load_state_locked(self) -> Optional[dict]:
        if self._group_dir is None:
            return None
        p = self._state_path_locked()
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _save_state_locked(self, state: dict) -> None:
        p = self._state_path_locked()
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _read_manifest_locked(self) -> dict[str, str]:
        """
        Returns moved_filename -> original_path (posix string)
        """
        assert self._group_dir is not None
        mpath = self._group_dir / MANIFEST_NAME
        mapping: dict[str, str] = {}
        if not mpath.exists():
            return mapping
        for line in mpath.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            moved_s, original_s = line.split("\t", 1)
            moved_name = Path(moved_s).name
            mapping[moved_name] = original_s
        return mapping

    def _list_items_locked(self) -> list[dict]:
        """
        Returns list of items:
          { name, folder_name, folder_path }
        folder_* based on original path in manifest.
        """
        assert self._group_dir is not None
        manifest = self._read_manifest_locked()

        items: list[dict] = []
        for p in sorted(self._group_dir.iterdir()):
            if not p.is_file():
                continue
            if p.name.lower() in {
                MANIFEST_NAME.lower(),
                STATE_FILENAME.lower(),
                GROUP_META_NAME.lower(),
                "_preview.html",
            }:
                continue

            original = manifest.get(p.name)
            if original:
                op = Path(original)
                folder_path = str(op.parent)
                folder_name = op.parent.name
            else:
                folder_path = ""
                folder_name = "unknown"

            items.append(
                {
                    "name": p.name,
                    "folder_name": folder_name,
                    "folder_path": folder_path,
                }
            )
        return items

    # ---------------------------
    # Routes
    # ---------------------------
    def _register_routes(self) -> None:
        app = self._app

        @app.get("/")
        def index():
            return render_template("index.html")

        @app.get("/api/group")
        def api_group():
            with self._lock:
                if self._group_dir is None:
                    return jsonify({"active": False})

                items = self._list_items_locked()
                state = self._load_state_locked() or {}
                auto_finish = bool(state.get("auto_finish", False))
                if self._mode != "exact":
                    auto_finish = False
                return jsonify(
                    {
                        "active": True,
                        "group_dir": str(self._group_dir),
                        "mode": self._mode,
                        "items": items,
                        "keep": state.get("keep", []),
                        "finished_clicks": state.get("finished_clicks", 0),
                        "auto_finish": auto_finish,
                        "preferred_folder": state.get("preferred_folder", None),
                    }
                )

        @app.get("/files/<path:filename>")
        def files(filename: str):
            with self._lock:
                if self._group_dir is None:
                    return ("No active group", 404)
                path = (self._group_dir / filename).resolve()

                # safety: ensure path is inside group dir
                if self._group_dir not in path.parents and path != self._group_dir:
                    return ("Invalid path", 400)

            try:
                data = path.read_bytes()  # file handle closes immediately
            except FileNotFoundError:
                return ("Not found", 404)

            mime, _ = mimetypes.guess_type(str(path))
            return Response(
                data,
                mimetype=mime or "application/octet-stream",
                headers={"Cache-Control": "no-store"},
            )

        @app.post("/api/toggle_keep")
        def api_toggle_keep():
            data = request.get_json(force=True, silent=True) or {}
            name = str(data.get("name", ""))

            with self._lock:
                if self._group_dir is None:
                    return jsonify({"ok": False, "error": "No active group"}), 400

                present = {it["name"] for it in self._list_items_locked()}
                if name not in present:
                    return jsonify({"ok": False, "error": "File not in group"}), 404

                state = self._load_state_locked() or {
                    "keep": [],
                    "finished_clicks": 0,
                    "auto_finish": False,
                }
                keep = set(state.get("keep", []))

                if name in keep:
                    keep.remove(name)
                else:
                    keep.add(name)

                state["keep"] = sorted(keep)
                self._save_state_locked(state)
                return jsonify({"ok": True, "keep": state["keep"]})

        @app.post("/api/prefer_folder")
        def api_prefer_folder():
            """
            Prefer a folder: auto-select all items from that folder_path and unselect others.
            Does NOT auto-finish.
            """
            data = request.get_json(force=True, silent=True) or {}
            folder_path = str(data.get("folder_path", ""))

            state = self._load_state_locked() or {}
            current = state.get("preferred_folder")

            # toggle off if clicked again
            if current == folder_path:
                state["preferred_folder"] = None
                # do not change keep; just untoggle preference
                self._save_state_locked(state)
                return jsonify(
                    {
                        "ok": True,
                        "preferred_folder": None,
                        "keep": state.get("keep", []),
                    }
                )

            with self._lock:
                if self._group_dir is None:
                    return jsonify({"ok": False, "error": "No active group"}), 400

                items = self._list_items_locked()
                keep = [
                    it["name"] for it in items if it.get("folder_path") == folder_path
                ]

                state = self._load_state_locked() or {}
                state["keep"] = sorted(set(keep))
                state["preferred_folder"] = folder_path
                # record global preference (most recent first)
                if folder_path:
                    self._preferred_folder_order = [folder_path] + [
                        fp for fp in self._preferred_folder_order if fp != folder_path
                    ]

                self._save_state_locked(state)
                return jsonify(
                    {"ok": True, "keep": state["keep"], "preferred_folder": folder_path}
                )

        @app.post("/api/toggle_auto_finish")
        def api_toggle_auto_finish():
            with self._lock:
                if self._group_dir is None:
                    return jsonify({"ok": False, "error": "No active group"}), 400

                if self._mode != "exact":
                    state = self._load_state_locked() or {}
                    state["auto_finish"] = False
                    self._save_state_locked(state)
                    return (
                        jsonify(
                            {
                                "ok": False,
                                "error": "Auto-finish is only available in exact mode.",
                            }
                        ),
                        400,
                    )

                self._auto_finish_global = not bool(self._auto_finish_global)

                state = self._load_state_locked() or {}
                state["auto_finish"] = bool(self._auto_finish_global)
                self._save_state_locked(state)
                return jsonify(
                    {"ok": True, "auto_finish": bool(self._auto_finish_global)}
                )

        @app.post("/api/finished")
        def api_finished():
            with self._lock:
                if self._group_dir is None:
                    return jsonify({"ok": False, "error": "No active group"}), 400

                state = self._load_state_locked() or {"keep": [], "finished_clicks": 0}
                clicks = int(state.get("finished_clicks", 0)) + 1
                state["finished_clicks"] = clicks
                self._save_state_locked(state)

                if clicks < 2:
                    return jsonify(
                        {"ok": True, "confirmed": False, "finished_clicks": clicks}
                    )

                keep_names = set(state.get("keep", []))
                self._result = ReviewResult(keep_names=keep_names, confirmed=True)
                self._result_ready.set()
                return jsonify(
                    {"ok": True, "confirmed": True, "finished_clicks": clicks}
                )

        @app.post("/api/reset_finished")
        def api_reset_finished():
            with self._lock:
                if self._group_dir is None:
                    return jsonify({"ok": False, "error": "No active group"}), 400
                state = self._load_state_locked() or {}
                state["finished_clicks"] = 0
                self._save_state_locked(state)
                return jsonify({"ok": True})


def attach_flask_logger(app: Flask, ui) -> None:
    # Route werkzeug logs to ui.log_flask
    logger = logging.getLogger("werkzeug")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    class UILogHandler(logging.Handler):
        def emit(self, record):
            try:
                ui.log_flask(record.getMessage())
            except Exception:
                pass

    logger.addHandler(UILogHandler())

    # ALSO: Werkzeug prints some startup warnings via its internal _log (not logging)
    # Patch it so it doesn't blast the terminal and mess with Rich's Live screen.
    try:
        import werkzeug.serving as ws

        orig_log = getattr(ws, "_log", None)

        def patched_log(log_type, message, *args):
            # format like werkzeug does
            try:
                msg = message % args if args else str(message)
            except Exception:
                msg = f"{message} {' '.join(map(str, args))}"

            # silence only the annoying dev-server warning
            if "This is a development server" in msg:
                return

            try:
                ui.log_flask(msg)
            except Exception:
                # fallback to original behavior if UI logging fails
                if orig_log:
                    orig_log(log_type, message, *args)

        if orig_log:
            ws._log = patched_log  # type: ignore
    except Exception:
        # If Werkzeug internals change, don't crash the program.
        pass


def serve_review_ui(
    server: ReviewServer,
    group_dir: Path,
    open_browser: bool = True,
    mode: str = "exact",
) -> ReviewResult:
    server.start()
    server.set_group(group_dir, mode=mode)

    if open_browser and (not server._browser_opened):
        import webbrowser

        webbrowser.open(server.base_url)
        server._browser_opened = True

    return server.wait_result()
