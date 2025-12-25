from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, request, send_from_directory, Response


STATE_FILENAME = "_review_state.json"


@dataclass
class ReviewResult:
    keep_names: set[str]
    confirmed: bool


class ReviewServer:
    """
    A tiny local Flask server used to review ONE group folder at a time.

    Workflow:
      - set_group(group_dir)
      - user selects images to keep
      - user clicks Finished twice
      - wait_result() returns keep set
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 5173):
        self.host = host
        self.port = port

        self._app = Flask(__name__)
        self._thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._group_dir: Optional[Path] = None
        self._result_ready = threading.Event()
        self._result: Optional[ReviewResult] = None

        # open browser only once per run
        self._browser_opened = False

        self._register_routes()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def run():
            self._app.run(
                host=self.host,
                port=self.port,
                debug=True,
                use_reloader=False,
                threaded=True,
            )

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        time.sleep(0.35)

    def set_group(self, group_dir: Path) -> None:
        group_dir = group_dir.resolve()
        with self._lock:
            self._group_dir = group_dir
            self._result = None
            self._result_ready.clear()

            state = self._load_state_locked()
            if state is None:
                self._save_state_locked({"keep": [], "finished_clicks": 0})

    def wait_result(self) -> ReviewResult:
        self._result_ready.wait()
        assert self._result is not None
        return self._result

    # ---------------------------
    # Internal helpers
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

    def _list_images_locked(self) -> list[str]:
        assert self._group_dir is not None
        names: list[str] = []
        for p in sorted(self._group_dir.iterdir()):
            if p.is_file() and p.name.lower() not in {
                "_manifest.tsv",
                "_preview.html",
                STATE_FILENAME.lower(),
            }:
                names.append(p.name)
        return names

    # ---------------------------
    # Flask routes
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
                imgs = self._list_images_locked()
                state = self._load_state_locked() or {"keep": [], "finished_clicks": 0}
                return jsonify(
                    {
                        "active": True,
                        "group_dir": str(self._group_dir),
                        "images": imgs,
                        "keep": state.get("keep", []),
                        "finished_clicks": state.get("finished_clicks", 0),
                    }
                )

        @app.get("/files/<path:filename>")
        def files(filename: str):
            with self._lock:
                if self._group_dir is None:
                    return ("No active group", 404)
                return send_from_directory(
                    self._group_dir, filename, as_attachment=False
                )

        @app.post("/api/toggle_keep")
        def api_toggle_keep():
            data = request.get_json(force=True, silent=True) or {}
            name = str(data.get("name", ""))
            if not name:
                return jsonify({"ok": False, "error": "Missing name"}), 400

            with self._lock:
                if self._group_dir is None:
                    return jsonify({"ok": False, "error": "No active group"}), 400

                imgs = set(self._list_images_locked())
                if name not in imgs:
                    return jsonify({"ok": False, "error": "File not in group"}), 404

                state = self._load_state_locked() or {"keep": [], "finished_clicks": 0}
                keep = set(state.get("keep", []))

                if name in keep:
                    keep.remove(name)
                else:
                    keep.add(name)

                state["keep"] = sorted(keep)
                self._save_state_locked(state)
                return jsonify({"ok": True, "keep": state["keep"]})

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
                state = self._load_state_locked() or {"keep": [], "finished_clicks": 0}
                state["finished_clicks"] = 0
                self._save_state_locked(state)
                return jsonify({"ok": True})

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
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 14px; }
    .card { border: 1px solid #ddd; border-radius: 12px; padding: 10px; }
    .nameRow { display: flex; gap: 8px; align-items: center; justify-content: space-between; margin-bottom: 8px; }
    .name {
      font-size: 12px; color: #333; word-break: break-all;
      user-select: text; cursor: text;
    }
    .copyBtn { padding: 6px 10px; border-radius: 10px; font-size: 12px; }
    img { width: 100%; height: auto; border-radius: 10px; }
    .keep { outline: 4px solid #2ecc71; }
    .hint { color: #666; font-size: 13px; margin: 8px 0 14px; }
    .toast {
      position: fixed; right: 16px; bottom: 16px;
      background: #222; color: #fff; padding: 10px 12px; border-radius: 10px;
      opacity: 0; transform: translateY(8px); transition: opacity 120ms, transform 120ms;
      font-size: 13px; pointer-events: none;
    }
    .toast.show { opacity: 0.92; transform: translateY(0); }
  </style>
</head>
<body>
  <h2>Dupe Reviewer</h2>
  <div class="hint">
    Click images to mark <b>KEEP</b>. Then click <b>Finished</b> twice to confirm.
  </div>

  <div class="topbar">
    <div class="pill" id="status">Loading...</div>
    <div class="pill" id="finishedClicks">Finished clicks: 0/2</div>
    <button id="btnFinished" class="danger">Finished (double confirm)</button>
    <button id="btnReset">Reset Finished</button>
    <button id="btnRefresh">Refresh</button>
  </div>

  <div class="grid" id="grid"></div>
  <div class="toast" id="toast"></div>

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
const TOAST = document.getElementById("toast");

// state
let KEEP = new Set();
let IMAGES = [];
let lastGroupDir = null;
let lastImagesKey = "";
let lastKeepKey = "";
let lastFinished = -1;

// DOM cache: name -> {card, img}
const cards = new Map();

function toast(msg) {
  // not an alert; small non-blocking blip
  TOAST.textContent = msg;
  TOAST.classList.add("show");
  setTimeout(() => TOAST.classList.remove("show"), 900);
}

function mkCard(name) {
  const card = document.createElement("div");
  card.className = "card";

  const row = document.createElement("div");
  row.className = "nameRow";

  const label = document.createElement("div");
  label.className = "name";
  label.textContent = name;

  const btn = document.createElement("button");
  btn.className = "copyBtn";
  btn.textContent = "Copy";
  btn.onclick = async (e) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(name);
      toast("Copied filename");
    } catch {
      // fallback if clipboard blocked
      const ta = document.createElement("textarea");
      ta.value = name;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      toast("Copied filename");
    }
  };

  row.appendChild(label);
  row.appendChild(btn);

  const img = document.createElement("img");
  img.src = "/files/" + encodeURIComponent(name);
  img.loading = "lazy";
  img.className = KEEP.has(name) ? "keep" : "";
  img.onclick = async () => {
    const res = await postJSON("/api/toggle_keep", {name});
    if (res.ok) {
      KEEP = new Set(res.keep || []);
      // only update classes, don't nuke the DOM
      updateKeepClasses();
    } else {
      toast(res.error || "Toggle failed");
    }
  };

  card.appendChild(row);
  card.appendChild(img);

  return {card, img};
}

function imagesKey(images) {
  // stable compare (order-insensitive)
  return [...images].sort().join("\\n");
}

function keepKey(keep) {
  return [...keep].sort().join("\\n");
}

function reconcileGrid() {
  const want = new Set(IMAGES);

  // remove cards not present anymore
  for (const [name, obj] of cards.entries()) {
    if (!want.has(name)) {
      obj.card.remove();
      cards.delete(name);
    }
  }

  // add missing cards
  // to keep ordering stable, rebuild ordering by appending in sorted order,
  // but only moving nodes if needed.
  const ordered = [...IMAGES];
  for (const name of ordered) {
    if (!cards.has(name)) {
      const obj = mkCard(name);
      cards.set(name, obj);
    }
  }

  // ensure DOM order matches IMAGES
  for (const name of ordered) {
    GRID.appendChild(cards.get(name).card);
    // appending an existing node just moves it; cheap enough
  }

  updateKeepClasses();
}

function updateKeepClasses() {
  for (const [name, obj] of cards.entries()) {
    if (KEEP.has(name)) obj.img.classList.add("keep");
    else obj.img.classList.remove("keep");
  }
}

async function refresh() {
  const data = await getJSON("/api/group");

  if (!data.active) {
    STATUS.textContent = "No active group yet (script is preparing one)...";
    FINISHED.textContent = "Finished clicks: 0/2";
    IMAGES = [];
    KEEP = new Set();
    lastGroupDir = null;
    lastImagesKey = "";
    lastKeepKey = "";
    lastFinished = -1;

    // clear grid once (not every tick)
    if (cards.size > 0) {
      for (const [, obj] of cards.entries()) obj.card.remove();
      cards.clear();
    }
    return;
  }

  const groupDir = data.group_dir;
  const images = data.images || [];
  const keep = new Set(data.keep || []);
  const finishedClicks = data.finished_clicks || 0;

  STATUS.textContent = "Active group: " + groupDir;
  FINISHED.textContent = "Finished clicks: " + finishedClicks + "/2";

  const newImagesKey = imagesKey(images);
  const newKeepKey = keepKey(keep);

  // If group changed, we rebuild only what we need (diff-based anyway)
  const groupChanged = (lastGroupDir !== groupDir);

  // Update state
  IMAGES = images;
  KEEP = keep;

  // Only touch the DOM if necessary
  if (groupChanged || newImagesKey !== lastImagesKey) {
    reconcileGrid();
  } else if (newKeepKey !== lastKeepKey) {
    updateKeepClasses();
  }

  lastGroupDir = groupDir;
  lastImagesKey = newImagesKey;
  lastKeepKey = newKeepKey;
  lastFinished = finishedClicks;
}

document.getElementById("btnRefresh").onclick = refresh;

document.getElementById("btnReset").onclick = async () => {
  await postJSON("/api/reset_finished", {});
  await refresh();
};

document.getElementById("btnFinished").onclick = async () => {
  // No alerts. The pill is the UI.
  await postJSON("/api/finished", {});
  await refresh();
};

// auto refresh occasionally
setInterval(refresh, 1000);
refresh();
</script>
</body>
</html>
"""


def serve_review_ui(
    server: ReviewServer, group_dir: Path, open_browser: bool = True
) -> ReviewResult:
    server.start()
    server.set_group(group_dir)

    if open_browser and (not server._browser_opened):
        import webbrowser

        webbrowser.open(server.base_url)
        server._browser_opened = True

    return server.wait_result()
