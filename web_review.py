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

from flask import Flask, jsonify, request, Response

STATE_FILENAME = "_review_state.json"
MANIFEST_NAME = "_manifest.tsv"


@dataclass
class ReviewResult:
    keep_names: set[str]
    confirmed: bool


class ReviewServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 5173):
        self.host = host
        self.port = port

        self._app = Flask(__name__)
        self._thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._group_dir: Optional[Path] = None
        self._mode: str = "exact"  # "exact" or later "phash"
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
            self._mode = mode
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
            # ✅ apply remembered preferences across groups
            self._apply_global_folder_preference_locked()
            state = self._load_state_locked() or {}
            state["auto_finish"] = bool(self._auto_finish_global)
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
            return Response(self._page_html(), mimetype="text/html")

        @app.get("/api/group")
        def api_group():
            with self._lock:
                if self._group_dir is None:
                    return jsonify({"active": False})

                items = self._list_items_locked()
                state = self._load_state_locked() or {}
                return jsonify(
                    {
                        "active": True,
                        "group_dir": str(self._group_dir),
                        "mode": self._mode,
                        "items": items,
                        "keep": state.get("keep", []),
                        "finished_clicks": state.get("finished_clicks", 0),
                        "auto_finish": bool(self._auto_finish_global),
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
                # ✅ record global preference (most recent first)
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

    # ---------------------------
    # UI
    # ---------------------------
    def _page_html(self) -> str:
        return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Dupe Reviewer</title>
  <style>
    body { font-family: system-ui, Segoe UI, Arial; margin: 16px; }
    h2 { margin: 0 0 10px; }
    .topbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
    .pill { padding: 6px 10px; border-radius: 999px; background: #f2f2f2; font-size: 12px; }
    button { padding: 10px 14px; border-radius: 10px; border: 1px solid #ccc; background: #fff; cursor: pointer; }
    button:hover { background: #f7f7f7; }
    .danger { border-color: #d55; }
    .toggleOn { border-color: #2ecc71; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 14px; }
    .card { border: 1px solid #ddd; border-radius: 12px; padding: 10px; }
    .nameRow { display: flex; gap: 8px; align-items: center; justify-content: space-between; margin-bottom: 6px; }
    .name { font-size: 12px; color: #333; word-break: break-all; user-select: text; cursor: text; }
    .subRow { display: flex; gap: 8px; align-items: center; justify-content: space-between; margin-bottom: 8px; }
    .folder { font-size: 12px; color: #666; user-select: text; }
    .copyBtn, .preferBtn { padding: 6px 10px; border-radius: 10px; font-size: 12px; }
    img { width: 100%; height: auto; border-radius: 10px; }
    .keep { outline: 4px solid #2ecc71; }
    .hint { color: #666; font-size: 13px; margin: 8px 0 14px; }
  </style>
</head>
<body>
  <h2>Dupe Reviewer</h2>
  <div class="hint">
    Click images to mark <b>KEEP</b>. Click <b>Finished</b> twice to confirm.
    <span id="autoHint"></span>
  </div>

  <div class="topbar">
    <div class="pill" id="status">Loading...</div>
    <div class="pill" id="finishedClicks">Finished clicks: 0/2</div>
    <button id="btnAutoFinish">Auto-finish: OFF</button>
    <button id="btnFinished" class="danger">Finished (double confirm)</button>
    <button id="btnReset">Reset Finished</button>
    <button id="btnRefresh">Refresh</button>
  </div>

  <div class="grid" id="grid"></div>

<script>
async function getJSON(url) {
  const r = await fetch(url);
  return await r.json();
}
async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {})
  });
  return await r.json();
}

const GRID = document.getElementById("grid");
const STATUS = document.getElementById("status");
const FINISHED = document.getElementById("finishedClicks");
const BTN_AUTOFIN = document.getElementById("btnAutoFinish");
const AUTOHINT = document.getElementById("autoHint");

// state
let KEEP = new Set();
let ITEMS = []; // {name, folder_name, folder_path}
let MODE = "exact";
let AUTO_FINISH = false;
let finishedClicks = 0;

let lastKeyItems = "";
let lastKeyKeep = "";

// DOM cache
const cards = new Map();

function itemsKey(items) {
  // stable compare for diffing
  return items.map(it => it.name + "|" + (it.folder_path || "")).sort().join("\\n");
}
function keepKey(setKeep) {
  return [...setKeep].sort().join("\\n");
}

function updateKeepClasses() {
  for (const [name, obj] of cards.entries()) {
    if (KEEP.has(name)) obj.img.classList.add("keep");
    else obj.img.classList.remove("keep");
  }
}

function mkCard(item) {
  const card = document.createElement("div");
  card.className = "card";

  const row = document.createElement("div");
  row.className = "nameRow";

  const label = document.createElement("div");
  label.className = "name";
  label.textContent = item.name;

  const copyBtn = document.createElement("button");
  copyBtn.className = "copyBtn";
  copyBtn.textContent = "Copy";
  copyBtn.onclick = async (e) => {
    e.preventDefault(); e.stopPropagation();
    try { await navigator.clipboard.writeText(item.name); } catch {}
  };

  row.appendChild(label);
  row.appendChild(copyBtn);

  const sub = document.createElement("div");
  sub.className = "subRow";

  const folder = document.createElement("div");
  folder.className = "folder";
  folder.textContent = item.folder_name ? ("📁 " + item.folder_name) : "📁 unknown";

  const preferBtn = document.createElement("button");
  preferBtn.className = "preferBtn";
  preferBtn.textContent = "Prefer this folder";
  preferBtn.onclick = async (e) => {
    e.preventDefault(); e.stopPropagation();
    if (!item.folder_path) return;
    const res = await postJSON("/api/prefer_folder", { folder_path: item.folder_path });
    if (res.ok) {
      KEEP = new Set(res.keep || []);
      updateKeepClasses();
      await maybeAutoFinish();
      // Don't auto finish here.
    }
  };

  sub.appendChild(folder);
  sub.appendChild(preferBtn);

  const img = document.createElement("img");
  img.src = "/files/" + encodeURIComponent(item.name);
  img.loading = "lazy";
  img.onclick = async () => {
    const res = await postJSON("/api/toggle_keep", {name: item.name});
    if (res.ok) {
      KEEP = new Set(res.keep || []);
      updateKeepClasses();
    }
  };

  card.appendChild(row);
  card.appendChild(sub);
  card.appendChild(img);

  return {card, img};
}

function reconcileGrid() {
  const wantNames = new Set(ITEMS.map(it => it.name));

  // remove missing
  for (const [name, obj] of cards.entries()) {
    if (!wantNames.has(name)) {
      obj.card.remove();
      cards.delete(name);
    }
  }

  // add missing
  for (const it of ITEMS) {
    if (!cards.has(it.name)) {
      const obj = mkCard(it);
      cards.set(it.name, obj);
    }
  }

  // ensure order stable
  for (const it of ITEMS) {
    GRID.appendChild(cards.get(it.name).card);
  }

  updateKeepClasses();
}

let autoFinishArmed = false;
let lastGroupDir = null;

async function maybeAutoFinish() {
  // Only for exact mode, only when toggle enabled
  if (MODE !== "exact") return;
  if (!AUTO_FINISH) return;

  // "wait for 1 image to be selected then auto-finish"
  if (KEEP.size !== 1) {
    autoFinishArmed = true; // arm once user hits 1 later
    return;
  }
  if (!autoFinishArmed) return; // prevents immediate firing on page load if state already has 1
  if (finishedClicks >= 2) return;

  // fire two "finished" clicks
  await postJSON("/api/finished", {});
  await postJSON("/api/finished", {});
  autoFinishArmed = false;
}

async function refresh() {
  const data = await getJSON("/api/group");
  if (!data.active) {
    STATUS.textContent = "No active group yet...";
    FINISHED.textContent = "Finished clicks: 0/2";
    ITEMS = [];
    KEEP = new Set();
    MODE = "exact";
    AUTO_FINISH = false;
    finishedClicks = 0;
    lastKeyItems = "";
    lastKeyKeep = "";
    if (cards.size > 0) {
      for (const [, obj] of cards.entries()) obj.card.remove();
      cards.clear();
    }
    return;
  }

  MODE = data.mode || "exact";
  ITEMS = data.items || [];
  KEEP = new Set(data.keep || []);
  finishedClicks = data.finished_clicks || 0;
  AUTO_FINISH = !!data.auto_finish;

  STATUS.textContent = "Active group: " + data.group_dir + "  (mode: " + MODE + ")";
  FINISHED.textContent = "Finished clicks: " + finishedClicks + "/2";

  const groupChanged = (lastGroupDir !== data.group_dir);
  if (groupChanged) {
    // New group: allow auto-finish to trigger after the first meaningful selection change.
    autoFinishArmed = true;
  }
  lastGroupDir = data.group_dir;


  // Auto-finish UI
  if (MODE !== "exact") {
    BTN_AUTOFIN.disabled = true;
    BTN_AUTOFIN.textContent = "Auto-finish: N/A";
    BTN_AUTOFIN.classList.remove("toggleOn");
    AUTOHINT.textContent = "";
  } else {
    BTN_AUTOFIN.disabled = false;
    BTN_AUTOFIN.textContent = "Auto-finish: " + (AUTO_FINISH ? "ON" : "OFF");
    BTN_AUTOFIN.classList.toggle("toggleOn", AUTO_FINISH);
    AUTOHINT.textContent = AUTO_FINISH ? " (Auto-finish will trigger when exactly 1 is selected.)" : "";
  }

  const kItems = itemsKey(ITEMS);
  const kKeep = keepKey(KEEP);

  if (kItems !== lastKeyItems) {
    reconcileGrid();
  } else if (kKeep !== lastKeyKeep) {
    updateKeepClasses();
  }

  lastKeyItems = kItems;
  lastKeyKeep = kKeep;

  await maybeAutoFinish();
}

document.getElementById("btnRefresh").onclick = refresh;
document.getElementById("btnReset").onclick = async () => {
  await postJSON("/api/reset_finished", {});
  autoFinishArmed = true;
  await refresh();
};
document.getElementById("btnFinished").onclick = async () => {
  await postJSON("/api/finished", {});
  await refresh();
};
BTN_AUTOFIN.onclick = async () => {
  if (MODE !== "exact") return;
  const res = await postJSON("/api/toggle_auto_finish", {});
  if (res.ok) {
    AUTO_FINISH = !!res.auto_finish;
    autoFinishArmed = true; // arm when user toggles
    await refresh();
  }
};

// poll
setInterval(refresh, 900);
refresh();
</script>
</body>
</html>
"""


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
