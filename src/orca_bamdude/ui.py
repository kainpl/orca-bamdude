"""The BamDude tab's page (spec r2 §8): one HTML document, every text a text node.

The sentences come in the page as one JSON object in OrcaSlicer's language (``say``), escaped so no text can close the
script. The page talks to the plugin only through ``window.orca.postMessage`` / ``window.orca.onMessage``
(rc ``PLUGIN_PAGE_BRIDGE_JS``); it renders what the plugin posts with ``textContent`` and DOM nodes, never
``innerHTML`` -- a printer's name, a file name or a server's sentence never becomes markup. It sends a pulse every
15 s (which renews nothing), the person's clicks and keys at most every 5 s (which renew the live-monitoring lease),
and the window's visibility.
"""

from __future__ import annotations

import json

STEPS = ("permission", "reach", "protocol", "key", "printers", "features")
PHASES = ("prepared", "uploading", "uploaded", "submitting", "accepted", "unknown", "finished", "upload_only")
ACTIONS = ("reconcile", "resume", "cancel", "send_as_new")
MILESTONES = ("queued", "assigned", "started", "first_layer", "completed", "failed", "cancelled", "unavailable")
RIGHTS = {  # spec r2 §9.1: the right each feature needs, shown by name
    "monitor": "printers:read",
    "library_upload": "library:upload + library:read_all",
    "submissions": "queue:create + library:read_all",
    "camera_stream": "camera:view",
}
KEYS = (
    "tab.title",
    "tab.settings",
    "tab.server_url",
    "tab.api_key",
    "tab.api_key_set",
    "tab.api_key_unset",
    "tab.keep",
    "tab.replace",
    "tab.clear",
    "tab.ca_pem",
    "tab.headers",
    "tab.header_name",
    "tab.header_value",
    "tab.header_kept",
    "tab.add_header",
    "tab.remove",
    "tab.export_dir",
    "tab.save",
    "tab.saved",
    "tab.not_recorded",
    "tab.source_base",
    "tab.source_preset",
    "tab.override_note",
    "tab.ignored_note",
    "tab.previous_load",
    "tab.state_repaired",
    "tab.held",
    "tab.ask_again",
    "tab.check",
    "tab.step_ok",
    "tab.missing_rights",
    "tab.no_missing",
    "tab.printers_seen",
    "tab.fleet",
    "tab.live",
    "tab.live_expired",
    "tab.refresh",
    "tab.read_at",
    "tab.read_old",
    "tab.tracking_paused",
    "tab.track_again",
    *(f"tab.milestone.{m}" for m in MILESTONES),
    "tab.writer_blocked",
    "tab.unknown",
    "tab.minutes",
    "tab.col_name",
    "tab.col_model",
    "tab.col_state",
    "tab.col_progress",
    "tab.col_remaining",
    "tab.snapshot",
    "tab.exports",
    "tab.list_files",
    "tab.no_files",
    "tab.send_to",
    "tab.autoqueue",
    "tab.autoqueue_model",
    "tab.send",
    "tab.history",
    "tab.reload_history",
    "tab.no_history",
    "tab.foreign",
    "tab.confirm_new",
    "tab.open_bamdude",
    "tab.error",
    *(f"tab.step.{step}" for step in STEPS),
    *(f"tab.phase.{phase}" for phase in PHASES),
    *(f"tab.action.{action}" for action in ACTIONS),
    "tab.notice.dashboard_needs_restart",
    "tab.notice.dashboard_refused",
    "tab.blocked.problem",
    "tab.blocked.missing",
    "tab.blocked.state_unreadable",
    "tab.blocked.held",
    "tab.blocked.needs_restart",
)


def sentences(say) -> dict:
    """Every text the page shows, in the plugin's language, plus the codes' own sentences it reuses."""
    texts = {key: say(key) for key in KEYS}
    return texts


def _script_json(value) -> str:
    """JSON that cannot close or open markup inside a ``<script>``."""
    text = json.dumps(value, ensure_ascii=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render(say) -> str:
    return _PAGE.replace("/*T*/", _script_json(sentences(say))).replace("/*RIGHTS*/", _script_json(RIGHTS))


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>BamDude</title>
<style>
:root{--fg:#1f2328;--muted:#59636e;--bg:#fff;--card:#f6f8fa;--line:#d1d9e0;--accent:#0969da;--warn:#9a6700;--bad:#cf222e;--ok:#1a7f37}
@media (prefers-color-scheme:dark){:root{--fg:#e6edf3;--muted:#9198a1;--bg:#212121;--card:#2b2b2b;--line:#3d444d;--accent:#4493f8;--warn:#d29922;--bad:#f85149;--ok:#3fb950}}
body{font:13px system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--fg);background:var(--bg);margin:0 auto;padding:16px;max-width:960px}
h1{font-size:18px;margin:0 0 4px} h2{font-size:14px;margin:0 0 8px}
section{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:12px;margin:0 0 12px}
label{display:block;margin:6px 0 2px;color:var(--muted)} input,textarea,select{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:4px 6px}
input[type=text],input[type=password],textarea{width:100%;box-sizing:border-box} textarea{min-height:70px;font-family:ui-monospace,monospace}
button{font:inherit;padding:4px 12px;margin:4px 6px 0 0;border:1px solid var(--line);border-radius:4px;background:var(--bg);color:var(--fg);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
table{border-collapse:collapse;width:100%} th,td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line)} th{color:var(--muted);font-weight:600}
.muted{color:var(--muted)} .warn{color:var(--warn)} .bad{color:var(--bad)} .ok{color:var(--ok)} .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap} .row input[type=text],.row input[type=password]{flex:1;width:auto;min-width:120px}
img.shot{max-width:100%;border-radius:4px;margin-top:8px}
</style></head>
<body>
<h1 id="title"></h1><p id="source" class="muted"></p><div id="notices"></div>
<section id="held-box" hidden><p id="held" class="warn"></p><button id="ask-again"></button></section>
<section><h2 id="h-settings"></h2>
<label id="l-url" for="url"></label><input id="url" type="text" autocomplete="off">
<label id="l-key"></label><p id="key-state" class="muted"></p>
<div class="row"><select id="key-action"></select><input id="key" type="password" autocomplete="off"></div>
<label id="l-ca" for="ca"></label><textarea id="ca" spellcheck="false"></textarea>
<label id="l-headers"></label><div id="headers"></div><button id="add-header"></button>
<label id="l-export" for="export"></label><input id="export" type="text" autocomplete="off">
<p id="override" class="muted" hidden></p><p id="ignored" class="warn" hidden></p>
<button id="save" class="primary"></button> <span id="saved" class="muted"></span>
</section>
<section><h2 id="h-check"></h2><button id="check"></button><ul id="steps"></ul></section>
<section><h2 id="h-fleet"></h2>
<div class="row"><label class="row"><input id="live" type="checkbox"> <span id="l-live"></span></label><button id="refresh"></button><span id="read-at" class="muted"></span></div>
<p id="live-expired" class="warn" hidden></p>
<table><thead><tr id="fleet-head"></tr></thead><tbody id="fleet"></tbody></table><img id="shot" class="shot" hidden alt="">
</section>
<section><h2 id="h-exports"></h2><button id="list-files"></button>
<div class="row"><span id="l-send-to"></span><select id="target"></select><input id="aq-model" type="text" hidden></div>
<table><tbody id="files"></tbody></table></section>
<section><h2 id="h-history"></h2><button id="reload-history"></button><table><tbody id="history"></tbody></table></section>
<button id="open-bamdude"></button>
<script>
"use strict";
var T = /*T*/, RIGHTS = /*RIGHTS*/;
var state = {settings: null, headers: [], printers: []};
function t(key) { return Object.prototype.hasOwnProperty.call(T, key) ? T[key] : key; }
function send(message) { if (window.orca && window.orca.postMessage) { window.orca.postMessage(message); } }
function el(id) { return document.getElementById(id); }
function text(id, value) { el(id).textContent = value; }
function node(tag, value, cls) { var n = document.createElement(tag); if (value !== undefined) { n.textContent = value; } if (cls) { n.className = cls; } return n; }
function clear(n) { while (n.firstChild) { n.removeChild(n.firstChild); } }
function button(label, onclick) { var b = node("button", label); b.onclick = onclick; return b; }

function labels() {
  text("title", t("tab.title")); text("h-settings", t("tab.settings")); text("l-url", t("tab.server_url"));
  text("l-key", t("tab.api_key")); text("l-ca", t("tab.ca_pem")); text("l-headers", t("tab.headers"));
  text("add-header", t("tab.add_header")); text("l-export", t("tab.export_dir")); text("save", t("tab.save"));
  text("h-check", t("tab.check")); text("check", t("tab.check")); text("h-fleet", t("tab.fleet"));
  text("l-live", t("tab.live")); text("refresh", t("tab.refresh")); text("h-exports", t("tab.exports"));
  text("list-files", t("tab.list_files")); text("l-send-to", t("tab.send_to")); text("h-history", t("tab.history"));
  text("reload-history", t("tab.reload_history")); text("open-bamdude", t("tab.open_bamdude")); text("ask-again", t("tab.ask_again"));
  var actions = el("key-action");
  ["keep", "replace", "clear"].forEach(function (a) { var o = node("option", t("tab." + a)); o.value = a; actions.appendChild(o); });
  var head = el("fleet-head");
  ["name", "model", "state", "progress", "remaining"].forEach(function (c) { head.appendChild(node("th", t("tab.col_" + c))); });
  head.appendChild(node("th", ""));
  el("aq-model").placeholder = t("tab.autoqueue_model");
}

function showState(s) {
  var v = s.settings || {};
  state.settings = v;
  text("source", v.source === "preset" ? t("tab.source_preset").replace("{name}", v.preset_name || "") : t("tab.source_base"));
  el("url").value = v.server_url || ""; el("ca").value = v.ca_pem || ""; el("export").value = v.export_dir || "";
  var readOnly = v.read_only || [];
  el("url").disabled = readOnly.indexOf("server_url") >= 0; el("ca").disabled = readOnly.indexOf("ca_pem") >= 0;
  el("override").hidden = readOnly.length === 0; text("override", t("tab.override_note").replace("{name}", v.preset_name || ""));
  el("ignored").hidden = !(v.ignored && v.ignored.length); text("ignored", t("tab.ignored_note").replace("{fields}", (v.ignored || []).join(", ")));
  text("key-state", v.api_key_set ? t("tab.api_key_set") : t("tab.api_key_unset"));
  el("key-action").value = v.api_key_set ? "keep" : "replace"; keyField();
  state.headers = (v.headers || []).map(function (h) { return {name: h.name, action: "keep"}; });
  drawHeaders();
  var notices = el("notices"); clear(notices);
  if (s.blocked) { notices.appendChild(node("p", t("tab.blocked." + s.blocked), "warn")); }
  if (s.previous_load) { notices.appendChild(node("p", t("tab.previous_load"), "muted")); }
  if (s.state_repaired) { notices.appendChild(node("p", t("tab.state_repaired"), "warn")); }
  el("held-box").hidden = !(s.held && s.held.length);
  text("held", t("tab.held").replace("{targets}", (s.held || []).map(function (h) { return h.replace(/^[a-z_]+:/, ""); }).join(", ")));
  if (s.saved) { text("saved", s.recorded === false ? t("tab.not_recorded") : t("tab.saved")); }
}

function keyField() { el("key").hidden = el("key-action").value !== "replace"; }

function drawHeaders() {
  var box = el("headers"); clear(box);
  state.headers.forEach(function (h, i) {
    var row = node("div", undefined, "row");
    var name = node("input"); name.type = "text"; name.value = h.name; name.placeholder = t("tab.header_name");
    name.oninput = function () { h.name = name.value; };
    var value = node("input"); value.type = "password"; value.placeholder = h.action === "keep" ? t("tab.header_kept") : t("tab.header_value");
    value.oninput = function () { h.action = value.value ? "replace" : "keep"; h.value = value.value; };
    row.appendChild(name); row.appendChild(value);
    row.appendChild(button(t("tab.remove"), function () { state.headers.splice(i, 1); drawHeaders(); }));
    box.appendChild(row);
  });
}

function save() {
  var edit = {server_url: el("url").value, ca_pem: el("ca").value, export_dir: el("export").value,
    headers: state.headers.filter(function (h) { return h.name; }).map(function (h) {
      return h.action === "keep" ? {name: h.name, action: "keep"} : {name: h.name, value: h.value || ""}; })};
  var action = el("key-action").value;
  edit.api_key = action === "replace" ? {action: "replace", value: el("key").value} : {action: action};
  var readOnly = (state.settings && state.settings.read_only) || [];
  readOnly.forEach(function (f) { delete edit[f]; });
  el("key").value = ""; text("saved", "");
  send({op: "save_settings", edit: edit});
}

function showSteps(steps) {
  var list = el("steps"); clear(list);
  steps.forEach(function (s) {
    var line = t("tab.step." + s.step) + ": " + (s.ok ? t("tab.step_ok") : t("tab.error").replace("{code}", s.code || ""));
    if (s.step === "printers" && s.ok) { line += " " + t("tab.printers_seen").replace("{count}", s.count).replace("{known}", s.known); }
    if (s.step === "features") {
      line = t("tab.step.features") + ": " + (s.missing.length ? t("tab.missing_rights").replace("{rights}",
        s.missing.map(function (f) { return RIGHTS[f] || f; }).join("; ")) : t("tab.no_missing"));
    }
    list.appendChild(node("li", line, s.ok ? "ok" : "bad"));
  });
}

var STALE_SECONDS = 60, lastView = null;  // a view no read refreshed for a minute is not current (§8.2)
function stale(view) { return Date.now() / 1000 - view.read_at > STALE_SECONDS; }

function showFleet(view) {
  var body = el("fleet"); clear(body);
  lastView = view; state.printers = view.rows;
  var old = stale(view);
  view.rows.forEach(function (r) {
    var known = r.connected === true && !old;  // a printer BamDude cannot reach now, or an old read: unknown
    var tr = node("tr");
    tr.appendChild(node("td", r.name)); tr.appendChild(node("td", r.model || t("tab.unknown")));
    tr.appendChild(node("td", known && r.state ? r.state : t("tab.unknown")));
    tr.appendChild(node("td", !known || r.progress === null ? "" : Math.round(r.progress) + "%"));
    // remaining_time is the printer's own mc_remaining_time: minutes already (H4 review R2)
    tr.appendChild(node("td", !known || r.remaining === null ? "" : t("tab.minutes").replace("{n}", Math.round(r.remaining))));
    var cell = node("td"); cell.appendChild(button(t("tab.snapshot"), function () { send({op: "snapshot", printer_id: r.id}); }));
    tr.appendChild(cell); body.appendChild(tr);
  });
  text("read-at", t(old ? "tab.read_old" : "tab.read_at").replace("{time}", new Date(view.read_at * 1000).toLocaleTimeString()));
  var select = el("target"), chosen = select.value; clear(select);
  view.rows.forEach(function (r) { var o = node("option", r.name); o.value = "printer:" + r.id; select.appendChild(o); });
  var aq = node("option", t("tab.autoqueue")); aq.value = "auto"; select.appendChild(aq);
  if (chosen) { select.value = chosen; }
  el("aq-model").hidden = select.value !== "auto";
}

function showFiles(rows) {
  var body = el("files"); clear(body);
  if (!rows.length) { var tr = node("tr"); tr.appendChild(node("td", t("tab.no_files"), "muted")); body.appendChild(tr); return; }
  rows.forEach(function (r) {
    var tr = node("tr"); tr.appendChild(node("td", r.relative)); tr.appendChild(node("td", Math.round(r.size / 1024) + " KB", "muted"));
    var cell = node("td");
    cell.appendChild(button(t("tab.send"), function () {
      var v = el("target").value, target;
      if (v === "auto") { target = {kind: "auto", model: el("aq-model").value}; }
      else { target = {kind: "printer", printer_id: parseInt(v.split(":")[1], 10)}; }
      send({op: "send", token: r.token, target: target});
    }));
    tr.appendChild(cell); body.appendChild(tr);
  });
}

function showHistory(rows, writable, paused) {
  var body = el("history"); clear(body);
  if (paused) {
    var pause = node("tr"), cell = node("td", t("tab.tracking_paused").replace("{reason}", paused), "warn");
    cell.appendChild(button(t("tab.track_again"), function () { send({op: "track_again"}); }));
    pause.appendChild(cell); body.appendChild(pause);
  }
  if (writable === false) { var note = node("tr"); note.appendChild(node("td", t("tab.writer_blocked"), "warn")); body.appendChild(note); }
  if (!rows.length) { var empty = node("tr"); empty.appendChild(node("td", t("tab.no_history"), "muted")); body.appendChild(empty); return; }
  rows.forEach(function (r) {
    var tr = node("tr");
    tr.appendChild(node("td", new Date(r.created * 1000).toLocaleString(), "muted")); tr.appendChild(node("td", r.file));
    var phase = r.foreign ? t("tab.foreign") : t("tab.phase." + r.phase);
    if (!r.foreign && r.milestone) { phase += " · " + t("tab.milestone." + r.milestone); }
    tr.appendChild(node("td", phase, r.phase === "unknown" || r.foreign ? "warn" : ""));
    var cell = node("td");
    r.actions.forEach(function (a) {
      cell.appendChild(button(t("tab.action." + a), function () {
        var message = {op: "history_action", operation_id: r.operation_id, action: a};
        if (a === "send_as_new") { if (!window.confirm(t("tab.confirm_new"))) { return; } message.confirmed = true; }
        send(message);
      }));
    });
    tr.appendChild(cell); body.appendChild(tr);
  });
}

function receive(m) {
  if (!m || typeof m !== "object") { return; }
  if (m.kind === "state") { showState(m); }
  else if (m.kind === "lease") { el("live").checked = !!m.on; el("live-expired").hidden = !m.expired; text("live-expired", t("tab.live_expired")); }
  else if (m.kind === "fleet") { showFleet(m); }
  else if (m.kind === "test") { showSteps(m.steps); }
  else if (m.kind === "exports") { showFiles(m.rows); }
  else if (m.kind === "history") { showHistory(m.rows, m.writable, m.paused); }
  else if (m.kind === "snapshot") { var img = el("shot"); img.src = m.data_url; img.hidden = false; }
  else if (m.kind === "notice") { el("notices").appendChild(node("p", t("tab.notice." + m.code), "warn")); }
  else if (m.kind === "error") { el("notices").appendChild(node("p", t("tab.error").replace("{code}", (m.op || "") + ": " + (m.code || "")), "bad")); }
}

labels();
el("save").onclick = save;
el("key-action").onchange = keyField;
el("add-header").onclick = function () { state.headers.push({name: "", action: "replace", value: ""}); drawHeaders(); };
el("check").onclick = function () { clear(el("steps")); send({op: "test"}); };
el("live").onchange = function () { send({op: "live", on: el("live").checked}); };
el("refresh").onclick = function () { send({op: "refresh"}); };
el("list-files").onclick = function () { send({op: "exports"}); };
el("target").onchange = function () { el("aq-model").hidden = el("target").value !== "auto"; };
el("reload-history").onclick = function () { send({op: "history"}); };
el("open-bamdude").onclick = function () { send({op: "open_bamdude"}); };
el("ask-again").onclick = function () { send({op: "ask_again"}); };
var lastActivity = 0;
function activity() { var now = Date.now(); if (now - lastActivity > 5000) { lastActivity = now; send({op: "activity"}); } }
document.addEventListener("click", activity, true); document.addEventListener("keydown", activity, true);
document.addEventListener("visibilitychange", function () { send({op: "visibility", hidden: document.hidden}); });
setInterval(function () { send({op: "pulse"}); }, 15000);
setInterval(function () { if (lastView && stale(lastView)) { showFleet(lastView); } }, 5000);
if (window.orca && window.orca.onMessage) { window.orca.onMessage(receive); }
send({op: "ready"}); send({op: "history"});
</script>
</body></html>
"""


# -- the agent's config UI (§8.1 ⟨R23-A1⟩) --------------------------------------------------------------------------

CONFIG_KEYS = (
    "config.global_note",
    "config.preset_heading",
    "config.preset_note",
    "config.server_url",
    "config.ca_pem",
    "config.from_tab",
    "config.save",
    "config.restore",
    "config.saved",
    "config.read_only",
    "config.invalid_url",
    "config.invalid_ca",
    "config.held_note",
)


def config_ui(say, *, held: bool) -> str:
    """One page for both of OrcaSlicer's dialogs (rc ``plugin-config-ui.js``: ``getContext().scope``). In the Plugins
    dialog (``global``) it only explains; in a preset's Capabilities (``preset``) it edits the override's two fields --
    the address and the CA -- with its own Save and Restore. ``getConfig()`` there is the base, key included, when no
    override exists: the page reads only the two fields from it, as a hint, and saves only those two."""
    texts = {key: say(key) for key in CONFIG_KEYS}
    return _CONFIG_PAGE.replace("/*T*/", _script_json(texts)).replace("/*STATE*/", _script_json({"held": bool(held)}))


_CONFIG_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<style>
body{font:13px system-ui,-apple-system,"Segoe UI",sans-serif;margin:12px;color:var(--orca-fg,#1f2328);background:var(--orca-bg,transparent)}
label{display:block;margin:8px 0 2px;opacity:.8} input,textarea{font:inherit;width:100%;box-sizing:border-box;padding:4px 6px}
textarea{min-height:90px;font-family:ui-monospace,monospace} button{font:inherit;padding:4px 12px;margin:8px 6px 0 0}
.muted{opacity:.75} .warn{color:#9a6700}
</style></head>
<body>
<p id="note"></p>
<div id="editor" hidden>
<h3 id="heading"></h3><p id="preset-note" class="muted"></p>
<label id="l-url" for="url"></label><input id="url" type="text" autocomplete="off">
<label id="l-ca" for="ca"></label><textarea id="ca" spellcheck="false"></textarea>
<p id="from-tab" class="muted"></p><p id="problem" class="warn"></p><p id="held" class="warn" hidden></p>
<button id="save"></button><button id="restore"></button> <span id="saved" class="muted"></span>
</div>
<script>
"use strict";
var T = /*T*/, STATE = /*STATE*/;
function t(key) { return Object.prototype.hasOwnProperty.call(T, key) ? T[key] : key; }
function el(id) { return document.getElementById(id); }
/*payload*/
function overridePayload(url, ca) {
  var out = {};
  url = String(url || "").trim(); ca = String(ca || "").trim();
  if (url) { out.server_url = url; }
  if (ca) { out.ca_pem = ca; }
  return out;
}
/*payload*/
var orca = window.orca || {};
var context = orca.getContext ? orca.getContext() : {scope: "global"};
if (context.scope !== "preset") {
  el("note").textContent = t("config.global_note");
} else {
  el("editor").hidden = false;
  el("heading").textContent = t("config.preset_heading"); el("preset-note").textContent = t("config.preset_note");
  el("l-url").textContent = t("config.server_url"); el("l-ca").textContent = t("config.ca_pem");
  el("save").textContent = t("config.save"); el("restore").textContent = t("config.restore");
  el("held").hidden = !STATE.held; el("held").textContent = t("config.held_note");
  var shown = function (config) {
    // The bridge replaces its context after a Save or a Restore (rc PluginsConfigDialog ApplyCapabilityConfig): read
    // the one it holds now, never the one this page started with (H4 review R1).
    var now = orca.getContext ? orca.getContext() : context;
    var c = config && typeof config === "object" ? config : {};
    var url = typeof c.server_url === "string" ? c.server_url : "", ca = typeof c.ca_pem === "string" ? c.ca_pem : "";
    if (now.hasPresetOverride) { el("url").value = url; el("ca").value = ca; el("url").placeholder = ""; el("from-tab").textContent = ""; }
    else { el("url").value = ""; el("ca").value = ""; el("url").placeholder = url; el("from-tab").textContent = t("config.from_tab"); }
    var locked = now.readOnly === true;
    el("url").disabled = el("ca").disabled = el("save").disabled = el("restore").disabled = locked;
    el("note").textContent = locked ? t("config.read_only") : "";
  };
  if (orca.onConfig) { orca.onConfig(shown); } else { shown(orca.getConfig ? orca.getConfig() : {}); }
  el("save").onclick = function () {
    var payload = overridePayload(el("url").value, el("ca").value);
    el("problem").textContent = ""; el("saved").textContent = "";
    if (payload.server_url && !/^https?:[/][/][^ /]+/i.test(payload.server_url)) { el("problem").textContent = t("config.invalid_url"); return; }
    if (payload.ca_pem && payload.ca_pem.indexOf("-----BEGIN CERTIFICATE-----") < 0) { el("problem").textContent = t("config.invalid_ca"); return; }
    if (!payload.server_url && !payload.ca_pem) { orca.restoreDefaults(); return; }  // nothing of its own: no override
    orca.saveConfig(payload); el("saved").textContent = t("config.saved");
  };
  el("restore").onclick = function () { orca.restoreDefaults(); el("saved").textContent = ""; };
}
</script>
</body></html>
"""
