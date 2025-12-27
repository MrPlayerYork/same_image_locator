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
let PREFERRED_FOLDER = null;

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
        try { await navigator.clipboard.writeText(item.name); } catch { }
    };

    row.appendChild(label);
    row.appendChild(copyBtn);

    const sub = document.createElement("div");
    sub.className = "subRow";

    const folder = document.createElement("div");
    folder.className = "folder";
    folder.textContent = item.folder_name ? ("📁 " + item.folder_name) : "folder unknown";

    const preferBtn = document.createElement("button");
    preferBtn.className = "preferBtn";
    preferBtn.textContent = "Prefer this folder";

    function updatePreferredUI() {
        const isPref =
            PREFERRED_FOLDER && item.folder_path === PREFERRED_FOLDER;

        card.classList.toggle("preferred", isPref);
        preferBtn.classList.toggle("on", isPref);
        preferBtn.textContent = isPref
            ? "Preferred (current)"
            : "Prefer this folder";
    }

    preferBtn.onclick = async (e) => {
        e.preventDefault();
        e.stopPropagation();

        const res = await postJSON("/api/prefer_folder", {
            folder_path: item.folder_path,
        });
        if (res.ok) {
            KEEP = new Set(res.keep || []);
            PREFERRED_FOLDER = res.preferred_folder || null;

            updateKeepClasses();
            updatePreferredUI();
            await maybeAutoFinish();
            // Don't auto finish
        }
    };

    sub.appendChild(folder);
    sub.appendChild(preferBtn);

    const img = document.createElement("img");
    img.src = "/files/" + encodeURIComponent(item.name);
    img.loading = "lazy";
    img.onclick = async () => {
        const res = await postJSON("/api/toggle_keep", { name: item.name });
        if (res.ok) {
            KEEP = new Set(res.keep || []);
            updateKeepClasses();
        }
    };

    card.appendChild(row);
    card.appendChild(sub);
    card.appendChild(img);

    updatePreferredUI();

    return { card, img };
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
        PREFERRED_FOLDER = data.preferred_folder || null;
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
    AUTO_FINISH = MODE === "exact" ? !!data.auto_finish : false;

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
