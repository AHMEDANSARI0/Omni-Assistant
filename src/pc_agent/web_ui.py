"""Built-in web Command Center (Transformation Phase 5).

A single static page served by the local API at ``/``. The page itself
contains no data and needs no auth; every panel is populated live from
the token-authenticated ``/v1`` API. The bearer token the user pastes
is kept only in ``sessionStorage`` (gone when the tab closes) and is
sent as an ``Authorization`` header — never in a URL and never stored
server-side.

Nothing on the page is simulated: counts, statuses, graphs, approvals,
safety state, and the event tail are rendered verbatim from API
responses, and every button performs the matching real API action.
"""

from __future__ import annotations

__all__ = ["INDEX_HTML"]

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Omni-Assistant Command Center</title>
<style>
  :root{
    --bg:#050b16; --panel:#0a1526; --panel2:#0d1b33; --cyan:#38e1ff;
    --cyan-dim:#1b9fc0; --text:#d9ecff;
    --muted:#7fa3c8; --ok:#2fe6a7; --warn:#ffb84d; --bad:#ff5d73; --violet:#9d8bff;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:"Segoe UI",system-ui,-apple-system,Arial,sans-serif;
    background:radial-gradient(1200px 600px at 20% -10%,#0d2447 0%,var(--bg) 55%);
    color:var(--text);min-height:100vh}
  header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;
    padding:14px 20px;border-bottom:1px solid #16345e;background:rgba(6,15,30,.85);
    position:sticky;top:0;z-index:5;backdrop-filter:blur(6px)}
  header h1{font-size:19px;margin:0;letter-spacing:.14em;color:#bfeaff}
  header h1 span{color:var(--cyan)}
  .pill{border:1px solid #1f4a7d;border-radius:999px;padding:3px 10px;font-size:12px;color:var(--muted)}
  .pill b{color:var(--text)}
  .pill.ok b{color:var(--ok)} .pill.bad b{color:var(--bad)} .pill.warn b{color:var(--warn)}
  main{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;padding:16px 20px 28px}
  section{background:linear-gradient(180deg,var(--panel2),var(--panel));
    border:1px solid #16345e;border-radius:10px;padding:12px 14px;
    box-shadow:0 0 0 1px rgba(56,225,255,.04),0 8px 24px rgba(0,0,0,.35)}
  section.wide{grid-column:span 2}
  @media (max-width:760px){section.wide{grid-column:span 1}}
  h2{margin:2px 0 10px;font-size:13px;letter-spacing:.18em;color:var(--cyan);text-transform:uppercase}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}
  .card{border:1px solid #16345e;border-radius:8px;padding:8px;background:rgba(10,25,50,.6)}
  .card .num{font-size:22px;color:#eaf7ff}
  .card .lbl{font-size:11px;color:var(--muted);letter-spacing:.08em;text-transform:uppercase}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:5px 6px;border-bottom:1px solid #12294d;text-align:left;vertical-align:top}
  th{color:var(--muted);font-weight:600;font-size:11px;letter-spacing:.08em;text-transform:uppercase}
  .st{font-size:11px;border-radius:999px;padding:1px 8px;border:1px solid #2b5a8f;color:#bfeaff;white-space:nowrap}
  .st.succeeded{border-color:#14532d;color:var(--ok)} .st.failed,.st.cancelled{border-color:#5b2230;color:var(--bad)}
  .st.running,.st.planning,.st.verifying,.st.retrying{border-color:#7c5a16;color:var(--warn)}
  .st.needs_input,.st.waiting_for_approval{border-color:#4a3f8f;color:var(--violet)}
  button{background:#0d2b52;color:var(--text);border:1px solid #2566a8;border-radius:6px;
    padding:4px 10px;font-size:12px;cursor:pointer}
  button:hover{border-color:var(--cyan);color:#fff}
  button.danger{border-color:#7a2b3d;background:#2a1020}
  button.primary{background:#0a3d66;border-color:var(--cyan-dim)}
  button:disabled{opacity:.45;cursor:not-allowed}
  input,textarea,select{width:100%;background:#071326;border:1px solid #1f4a7d;color:var(--text);
    border-radius:6px;padding:7px 9px;font-size:13px;margin:4px 0 8px}
  label{font-size:12px;color:var(--muted)}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .mono{font-family:Consolas,"Cascadia Mono",monospace;font-size:12px}
  #events{max-height:320px;overflow:auto;font-size:12px}
  #events div{padding:3px 0;border-bottom:1px dashed #12294d}
  .muted{color:var(--muted)} .small{font-size:12px}
  #toast{position:fixed;right:16px;bottom:16px;background:#0d2b52;border:1px solid var(--cyan-dim);
    color:var(--text);border-radius:8px;padding:10px 14px;display:none;max-width:420px;z-index:9}
  .levels{display:flex;gap:10px;overflow-x:auto;padding:4px 0}
  .level{min-width:150px;border:1px dashed #2566a8;border-radius:8px;padding:6px}
  .level h3{margin:0 0 6px;font-size:11px;color:var(--muted);letter-spacing:.1em}
  .chip{border:1px solid #2b5a8f;border-radius:6px;padding:4px 6px;margin-bottom:5px;font-size:11px;background:rgba(7,19,38,.7)}
  .hidden{display:none}
  a{color:var(--cyan)}
  /* ---- Transformation Phase 17: accessibility ---- */
  :focus-visible{outline:3px solid var(--cyan);outline-offset:2px}
  .skip{position:absolute;left:-9999px;top:0;background:#0d2b52;color:#fff;
    padding:8px 14px;border-radius:0 0 8px 0;z-index:20}
  .skip:focus{left:0}
  body.high-contrast{--bg:#000;--panel:#0b0b0b;--panel2:#111;--text:#fff;
    --muted:#f0f0f0;--cyan:#00e5ff;--ok:#00ff9c;--warn:#ffd400;--bad:#ff5c5c}
  body.high-contrast section{border-color:#fff}
  [dir="rtl"] header,[dir="rtl"] main{direction:rtl;text-align:right}
  [dir="rtl"] th,[dir="rtl"] td{text-align:right}
  @media (prefers-reduced-motion: reduce){
    *{animation:none!important;transition:none!important;scroll-behavior:auto!important}
  }
  body.reduce-motion *{animation:none!important;transition:none!important}
  svg text{font-size:10px}
</style>
</head>
<body>
<a class="skip" href="#main">Skip to main content</a>
<header role="banner">
  <h1>OMNI<span>-</span>ASSISTANT <span>COMMAND CENTER</span></h1>
  <span class="pill" id="h-version">version <b>…</b></span>
  <span class="pill" id="h-schema">schema <b>…</b></span>
  <span class="pill" id="h-stop">emergency stop <b>…</b></span>
  <span class="pill" id="h-running">running <b>…</b></span>
  <span class="pill" id="h-conn">api <b>checking…</b></span>
  <span style="margin-left:auto" class="row">
    <input id="token" type="password" placeholder="API bearer token (pc-agent api serve --show-token)" style="width:320px;margin:0">
    <button class="primary" id="save-token">Connect</button>
    <button id="clear-token">Forget</button>
  </span>
</header>
<div id="toast" role="status" aria-live="polite"></div>
<div id="a11y-live" role="status" aria-live="polite" class="small muted" style="padding:0 20px"></div>
<main id="main" role="main" tabindex="-1">
  <section>
    <h2>Overview</h2>
    <div class="cards" id="overview"><div class="card"><div class="num">–</div><div class="lbl">loading</div></div></div>
    <p class="small muted" id="by-status"></p>
  </section>

  <section>
    <h2>Quick Run</h2>
    <label for="quick-instruction">Instruction (runs through the same Safety Kernel as the CLI)</label>
    <textarea id="quick-instruction" rows="2" placeholder="Merge PDFs from Folder A and Folder B then extract pages 1-2"></textarea>
    <div class="row">
      <label><input type="checkbox" id="quick-ai" style="width:auto"> use AI planner</label>
      <button class="primary" id="quick-run">Run task</button>
      <span class="small muted" id="quick-result"></span>
    </div>
  </section>

  <section class="wide">
    <h2>System (live)</h2>
    <div id="system" class="small">loading…</div>
    <div id="top-processes" class="small"></div>
  </section>

  <section>
    <h2>Safety</h2>
    <div id="safety" class="small">loading…</div>
    <div class="row" style="margin-top:8px">
      <button class="danger" id="safety-stop">Engage emergency stop</button>
      <button id="safety-resume">Clear stop</button>
    </div>
  </section>

  <section class="wide">
    <h2>Tasks</h2>
    <table><thead><tr><th>Status</th><th>Instruction</th><th>Created</th><th></th></tr></thead>
    <tbody id="tasks"><tr><td colspan="4" class="muted">loading…</td></tr></tbody></table>
  </section>

  <section class="wide">
    <h2>Mission Center</h2>
    <div class="row">
      <input id="mission-name" placeholder="New mission name" style="max-width:240px">
      <label><input type="checkbox" id="mission-chain" style="width:auto"> chain (each task depends on the previous)</label>
      <button class="primary" id="mission-create">Create mission</button>
    </div>
    <textarea id="mission-tasks" rows="3" placeholder="One task instruction per line"></textarea>
    <table><thead><tr><th>Status</th><th>Mission</th><th>Progress</th><th></th></tr></thead>
    <tbody id="missions"><tr><td colspan="4" class="muted">loading…</td></tr></tbody></table>
    <div id="mission-graph"></div>
  </section>

  <section>
    <h2>Approval Center — Pending</h2>
    <div id="approvals" class="small">loading…</div>
    <p class="small muted">Decisions stay exact-scope and are consumed once, exactly like the CLI.</p>
  </section>

  <section>
    <h2>Schedules</h2>
    <div id="schedules" class="small">loading…</div>
    <div class="row"><button id="schedules-run-due">Run due now</button></div>
  </section>

  <section class="wide">
    <h2>Live Events</h2>
    <div id="events"><div class="muted">waiting for events…</div></div>
  </section>

  <section class="wide">
    <h2>Providers (Platform v2)</h2>
    <div id="providers" class="small">loading…</div>
    <div id="provider-usage" class="small" style="margin-top:8px"></div>
    <div style="margin-top:10px">
      <input id="stream-prompt" type="text" placeholder="Ask the provider route (streamed)…"
             style="width:60%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <button id="stream-ask">Stream Ask</button>
      <button id="eval-run">Run Evaluations</button>
    </div>
    <div id="stream-out" class="small mono" style="white-space:pre-wrap;margin-top:8px;max-height:200px;overflow:auto"></div>
    <div id="eval-out" class="small" style="margin-top:6px"></div>
  </section>

  <section class="wide">
    <h2>Knowledge &amp; Memory v2</h2>
    <div id="knowledge-stats" class="small">loading…</div>
    <div id="knowledge-docs" class="small" style="margin-top:6px;max-height:160px;overflow:auto"></div>
    <div style="margin-top:10px">
      <input id="kb-path" type="text" placeholder="Workspace file to ingest (e.g. notes.md)"
             style="width:44%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <button id="kb-ingest">Ingest</button>
    </div>
    <div style="margin-top:8px">
      <input id="kb-query" type="text" placeholder="Search knowledge + memory, or ask a question…"
             style="width:44%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <button id="kb-search">Search</button>
      <button id="kb-ask">Ask (RAG)</button>
    </div>
    <div id="kb-out" class="small" style="white-space:pre-wrap;margin-top:8px;max-height:240px;overflow:auto"></div>
  </section>

  <section class="wide">
    <h2>Agent Studio</h2>
    <div id="agents-stats" class="small">loading…</div>
    <div id="agents-list" class="small" style="margin-top:6px;max-height:150px;overflow:auto"></div>
    <div style="margin-top:10px">
      <input id="agent-name" type="text" placeholder="Agent name (e.g. document-worker)"
             style="width:24%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <input id="agent-instruction" type="text" placeholder="Instruction for the agent, or a team goal…"
             style="width:44%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <button id="agent-run">Run agent</button>
      <button id="agent-team">Run team</button>
    </div>
    <div id="agent-out" class="small" style="white-space:pre-wrap;margin-top:8px;max-height:220px;overflow:auto"></div>
    <div id="agent-runs" class="small muted" style="margin-top:6px;max-height:140px;overflow:auto"></div>
  </section>

  <section class="wide">
    <h2>Voice</h2>
    <div id="voice-status" class="small">loading…</div>
    <div style="margin-top:8px">
      <label class="small"><input id="voice-enabled" type="checkbox"> Microphone enabled (explicit, persisted)</label>
      <label class="small" style="margin-left:14px"><input id="voice-wake" type="checkbox"> Optional wake word (second opt-in)</label>
    </div>
    <div style="margin-top:8px">
      <button id="voice-start">Push to talk (start)</button>
      <button id="voice-stop">Stop + transcribe</button>
      <button id="voice-cancel">Cancel</button>
      <button id="voice-purge">Purge retained audio</button>
    </div>
    <div style="margin-top:8px">
      <input id="voice-say" type="text" placeholder="Say something out loud…"
             style="width:44%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <button id="voice-speak">Speak</button>
    </div>
    <div id="voice-out" class="small" style="white-space:pre-wrap;margin-top:8px;max-height:200px;overflow:auto"></div>
    <div id="voice-sessions" class="small muted" style="margin-top:6px;max-height:130px;overflow:auto"></div>
  </section>

  <section class="wide">
    <h2>Proactive</h2>
    <div id="proactive-status" class="small">loading…</div>
    <div style="margin-top:10px">
      <input id="reminder-title" type="text" placeholder="Reminder title…"
             style="width:30%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <input id="reminder-due" type="text" placeholder="+15m / 09:30 / ISO"
             style="width:16%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <button id="reminder-add">Add reminder</button>
      <button id="proactive-tick">Tick now</button>
      <button id="proactive-briefing">Run briefing</button>
    </div>
    <div id="proactive-reminders" class="small muted" style="margin-top:8px;max-height:130px;overflow:auto"></div>
    <div id="proactive-out" class="small" style="white-space:pre-wrap;margin-top:8px;max-height:180px;overflow:auto"></div>
  </section>

  <section class="wide">
    <h2>Accessibility</h2>
    <div id="a11y-status" class="small">loading…</div>
    <div class="row" style="margin-top:8px">
      <label for="a11y-locale">Language</label>
      <select id="a11y-locale" style="width:auto;min-width:190px;margin:0" aria-label="Interface language"></select>
      <label for="a11y-scale">Text size %</label>
      <input id="a11y-scale" type="number" min="80" max="200" step="10" value="100"
             style="width:90px;margin:0" aria-label="Text size percent">
      <label><input id="a11y-contrast" type="checkbox" style="width:auto" aria-label="High contrast"> High contrast</label>
      <label><input id="a11y-motion" type="checkbox" style="width:auto" aria-label="Reduce motion"> Reduce motion</label>
      <button class="primary" id="a11y-save">Save</button>
      <button id="a11y-apply">Apply here now</button>
    </div>
    <div id="a11y-shortcuts" class="small muted" style="margin-top:8px"></div>
    <div id="a11y-limits" class="small muted" style="margin-top:8px;max-height:120px;overflow:auto"></div>
  </section>

  <section class="wide">
    <h2>Device sync</h2>
    <div id="sync-status" class="small">loading…</div>
    <div id="sync-peers" class="small muted" style="margin-top:6px;max-height:110px;overflow:auto"></div>
    <div style="margin-top:10px">
      <input id="sync-peer-name" type="text" placeholder="peer name"
             style="width:16%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <input id="sync-sign" type="text" placeholder="peer Ed25519 public key"
             style="width:28%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <input id="sync-encrypt" type="text" placeholder="peer X25519 public key"
             style="width:28%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <button id="sync-pair">Pair</button>
    </div>
    <div style="margin-top:8px">
      <input id="sync-path" type="text" placeholder="bundle path (.pcasync)"
             style="width:34%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <button id="sync-export">Export</button>
      <button id="sync-import">Import</button>
      <button id="sync-tick">Tick</button>
    </div>
    <div id="sync-out" class="small" style="white-space:pre-wrap;margin-top:8px;max-height:160px;overflow:auto"></div>
  </section>

  <section class="wide">
    <h2>Plugins</h2>
    <div id="plugins-status" class="small">loading…</div>
    <div id="plugins-list" class="small muted" style="margin-top:6px;max-height:130px;overflow:auto"></div>
    <div style="margin-top:10px">
      <input id="plugin-bundle" type="text" placeholder="Path to a signed .zip bundle…"
             style="width:34%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <button id="plugin-verify">Verify</button>
      <button id="plugin-install">Install</button>
    </div>
    <div style="margin-top:8px">
      <input id="plugin-id" type="text" placeholder="plugin id"
             style="width:20%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <input id="plugin-tool" type="text" placeholder="declared tool"
             style="width:16%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <input id="plugin-args" type="text" placeholder='{}' value="{}"
             style="width:20%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <button id="plugin-run">Run sandboxed</button>
      <button id="plugin-enable">Enable</button>
      <button id="plugin-disable">Disable</button>
    </div>
    <div id="plugin-out" class="small" style="white-space:pre-wrap;margin-top:8px;max-height:180px;overflow:auto"></div>
  </section>

  <section class="wide">
    <h2>Connectors</h2>
    <div id="connectors-status" class="small">loading…</div>
    <div id="connectors-events" class="small muted" style="margin-top:6px;max-height:130px;overflow:auto"></div>
    <div style="margin-top:10px">
      <button id="connector-calendar">Calendar (7 days)</button>
      <button id="connector-drive">Sync drive</button>
      <button id="connector-mail">Mail headers</button>
      <input id="connector-chat" type="text" placeholder="Chat message…"
             style="width:34%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <button id="connector-chat-send">Send</button>
    </div>
    <div id="connector-out" class="small" style="white-space:pre-wrap;margin-top:8px;max-height:180px;overflow:auto"></div>
  </section>

  <section class="wide">
    <h2>Workflow Builder</h2>
    <div id="flows-stats" class="small">loading…</div>
    <div id="flows-list" class="small" style="margin-top:6px;max-height:150px;overflow:auto"></div>
    <div style="margin-top:10px">
      <input id="flow-ref" type="text" placeholder="Flow id or exact name"
             style="width:28%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <button id="flow-run">Run flow</button>
      <button id="flow-show">Show graph</button>
      <button id="flow-tick">Tick triggers</button>
      <button id="flow-export">Export JSON</button>
    </div>
    <div style="margin-top:8px">
      <input id="flow-json" type="text" placeholder='Paste a flow JSON document (or an exported document)…'
             style="width:70%;padding:6px;background:#0a1830;border:1px solid #2566a8;border-radius:6px;color:#dff4ff">
      <button id="flow-create">Create flow</button>
      <button id="flow-import">Import document</button>
    </div>
    <div id="flow-out" class="small" style="white-space:pre-wrap;margin-top:8px;max-height:220px;overflow:auto"></div>
    <div id="flow-runs" class="small muted" style="margin-top:6px;max-height:140px;overflow:auto"></div>
  </section>

  <section>
    <h2>Tools</h2>
    <div id="tools" class="small" style="max-height:260px;overflow:auto">loading…</div>
  </section>

  <section>
    <h2>Settings (public)</h2>
    <div id="settings" class="small mono" style="max-height:260px;overflow:auto">loading…</div>
  </section>
</main>
<script>
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const state = { token: sessionStorage.getItem("omni_token") || "", cursor: 0,
  selectedMission: null, timer: null };
$("token").value = state.token;

function toast(msg){ const t=$("toast"); t.textContent=msg; t.style.display="block";
  setTimeout(()=>t.style.display="none", 5000); }
async function api(path, opts={}){
  const headers = Object.assign({}, opts.headers||{});
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, Object.assign({}, opts, {headers}));
  const payload = await res.json().catch(()=>({ok:false,error:{message:"Bad response"}}));
  if (!payload.ok) throw new Error(payload.error && payload.error.message ? payload.error.message : ("HTTP "+res.status));
  return payload.data;
}
async function apiStream(path, body, onEvent){
  const headers = {"Content-Type":"application/json"};
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  const res = await fetch(path, {method:"POST", headers, body: JSON.stringify(body)});
  if (!res.ok || !res.body){
    let message = "HTTP " + res.status;
    try{ const payload = await res.json(); message = payload.error && payload.error.message ? payload.error.message : message; }catch(e){}
    throw new Error(message);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for(;;){
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, {stream:true});
    let idx = buffer.indexOf("\n");
    while (idx >= 0){
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (line){ try{ onEvent(JSON.parse(line)); }catch(e){} }
      idx = buffer.indexOf("\n");
    }
  }
}
function fmtBytes(v){
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "n/a";
  let n = Number(v); const units = ["B","KB","MB","GB","TB"]; let i = 0;
  while (n >= 1024 && i < units.length - 1){ n /= 1024; i++; }
  return (i === 0 ? String(Math.round(n)) : n.toFixed(1)) + " " + units[i];
}
function needToken(){ if(!state.token){ toast("Paste the API token first (pc-agent api serve --show-token)."); return true;} return false; }
const pill = (s) => '<span class="st '+esc(s)+'">'+esc(s)+'</span>';

async function refreshHealth(){
  try{
    const h = await api("/v1/health");
    $("h-version").innerHTML = "version <b>"+esc(h.version)+"</b>";
    $("h-schema").innerHTML = "schema <b>v"+esc(h.schema_version)+"</b>";
    const stop = $("h-stop");
    stop.className = "pill " + (h.emergency_stop_engaged ? "bad" : "ok");
    stop.innerHTML = "emergency stop <b>"+(h.emergency_stop_engaged?"ENGAGED":"clear")+"</b>";
    const running = (h.running_tasks||[]).length + (h.running_missions||[]).length;
    $("h-running").innerHTML = "running <b>"+running+"</b>";
    $("h-conn").className = "pill ok"; $("h-conn").innerHTML = "api <b>online</b>";
  }catch(e){ $("h-conn").className="pill bad"; $("h-conn").innerHTML="api <b>offline</b>"; }
}

async function refreshData(){
  if (!state.token) return;
  try{
    const ov = await api("/v1/overview");
    const cards = [
      [ov.tasks.total,"tasks"],[ov.missions,"missions"],[ov.events_total,"events"],
      [ov.pending_approvals,"pending approvals"],[ov.tools,"tools"],[ov.schedules_enabled,"schedules on"]
    ];
    $("overview").innerHTML = cards.map(c=>'<div class="card"><div class="num">'+esc(c[0])+'</div><div class="lbl">'+esc(c[1])+'</div></div>').join("");
    const by = ov.tasks.by_status || {};
    $("by-status").textContent = Object.keys(by).sort().map(k=>k+"="+by[k]).join(" · ") || "no tasks yet";

    const sys = await api("/v1/system");
    const snap = sys.snapshot;
    const disk = (snap.disks && snap.disks.install) || {};
    const gpu = snap.gpu || {};
    const bar = (value, label) => {
      const pct = value === null || value === undefined ? 0 : value;
      const shown = value === null || value === undefined ? "n/a" : (value + "%");
      return label + " <div style='display:inline-block;vertical-align:middle;width:180px;height:10px;border:1px solid #2566a8;border-radius:5px'>" +
        "<div style='height:100%;width:" + pct + "%;background:linear-gradient(90deg,#1b9fc0,#38e1ff);border-radius:5px'></div></div> <b>" + esc(shown) + "</b>";
    };
    $("system").innerHTML =
      bar(snap.cpu.percent, "CPU (" + esc(snap.cpu.method) + ")") + "<br>" +
      bar(snap.memory.percent, "Memory") + " <span class='muted'>" + esc(fmtBytes(snap.memory.used_bytes)) + " / " + esc(fmtBytes(snap.memory.total_bytes)) + "</span><br>" +
      bar(disk.percent, "Disk") + " <span class='muted'>free " + esc(fmtBytes(disk.free_bytes)) + "</span><br>" +
      "Network ↓ <b>" + esc(fmtBytes(snap.network.receive_bytes_per_second)) + "/s</b> ↑ <b>" + esc(fmtBytes(snap.network.send_bytes_per_second)) + "/s</b> <span class='muted'>(" + esc(snap.network.method) + ")</span><br>" +
      "Agent process RSS <b>" + esc(fmtBytes(snap.process.rss_bytes)) + "</b> · threads <b>" + esc(snap.process.python_threads) + "</b> · uptime <b>" + esc(snap.process.uptime_seconds) + "s</b><br>" +
      "GPU: <b>" + (gpu.available ? esc(gpu.name) + " " + esc(gpu.utilization_percent) + "% · VRAM " + esc(gpu.memory_used_mib) + "/" + esc(gpu.memory_total_mib) + " MiB"
                                    : "unavailable (" + esc(gpu.method) + ")") + "</b>" +
      (snap.battery ? "<br>Battery <b>" + esc(snap.battery.percent) + "%</b> " + (snap.battery.plugged_in ? "(plugged in)" : "(on battery)") : "");
    $("top-processes").innerHTML = (snap.top_processes && snap.top_processes.length)
      ? "<div style='margin-top:8px'><b>Top processes (RSS)</b><br>" +
        snap.top_processes.map(p => esc(p.pid) + " " + esc(p.name) + " — " + esc(fmtBytes(p.rss_bytes))).join("<br>") + "</div>"
      : "";

    const safety = await api("/v1/safety");
    const es = safety.emergency_stop || {};
    $("safety").innerHTML =
      "hard timeouts <b>"+(safety.hard_timeouts?"on":"OFF")+"</b> · worker isolation <b>"+(safety.worker_isolation?"on":"off")+
      "</b> · max tool <b>"+esc(safety.max_tool_seconds)+"s</b><br>emergency stop: <b>"+
      (es.engaged?("ENGAGED ("+esc(es.source)+") "+esc(es.reason||"")):"clear")+
      "</b> · pending approvals <b>"+esc(safety.pending_approvals)+"</b>";

    const tasks = await api("/v1/tasks?limit=25");
    $("tasks").innerHTML = tasks.tasks.map(t=>{
      const act = t.status==="queued" ? '<button data-run="'+esc(t.id)+'">Run</button>' : "";
      const can = ["queued","running","planning","waiting_for_approval"].includes(t.status)
        ? '<button data-cancel="'+esc(t.id)+'">Cancel</button>' : "";
      const del = ["succeeded","failed","cancelled"].includes(t.status)
        ? '<button data-del="'+esc(t.id)+'">Delete</button>' : "";
      return "<tr><td>"+pill(t.status)+"</td><td>"+esc(t.instruction)+"</td><td class='mono'>"+
        esc((t.created_at||"").slice(0,19).replace("T"," "))+"</td><td class='row'>"+act+can+del+"</td></tr>";
    }).join("") || '<tr><td colspan="4" class="muted">no tasks yet</td></tr>';

    const missions = await api("/v1/missions?limit=25");
    $("missions").innerHTML = missions.missions.map(m=>{
      const p = m.progress||{};
      const prog = (p.succeeded||0)+"/"+(p.total||0)+" succeeded";
      const run = m.status!=="running" ? '<button data-mrun="'+esc(m.id)+'">Run</button>' : "";
      const can = m.status==="running" ? '<button data-mcancel="'+esc(m.id)+'">Cancel</button>' : "";
      return "<tr><td>"+pill(m.status)+"</td><td><a href='#' data-mshow='"+esc(m.id)+"'>"+esc(m.name)+"</a></td><td>"+
        esc(prog)+"</td><td class='row'>"+run+can+"</td></tr>";
    }).join("") || '<tr><td colspan="4" class="muted">no missions yet</td></tr>';
    if (state.selectedMission) await showMission(state.selectedMission, true);

    const approvals = await api("/v1/approvals");
    $("approvals").innerHTML = approvals.requests.length ? approvals.requests.map(r=>
      "<div style='margin-bottom:8px'><b>"+esc(r.request_key)+"</b> "+esc(r.tool_id)+" <span class='muted'>"+esc(r.action)+"</span><br>"+
      "<span class='muted'>"+esc(r.message||"")+"</span><br>"+
      "<button data-approve='"+esc(r.request_key)+"'>Approve</button> "+
      "<button class='danger' data-deny='"+esc(r.request_key)+"'>Deny</button></div>"
    ).join("") : "<span class='muted'>no pending requests</span>";

    const schedules = await api("/v1/schedules");
    $("schedules").innerHTML = schedules.schedules.length ? schedules.schedules.map(s=>
      "<div><b>"+esc(s.name)+"</b> ["+(s.enabled?"on":"off")+"] "+esc(s.kind)+
      " · next "+esc((s.next_run_at||"").slice(0,19).replace("T"," "))+" · runs "+esc(s.run_count)+
      "<br><span class='muted'>"+esc(s.instruction)+"</span></div>"
    ).join("<hr style='border-color:#12294d'>") : "<span class='muted'>no schedules</span>";

    const providers = await api("/v1/providers");
    const usageData = await api("/v1/providers/usage");
    const usage = usageData.usage || {};
    const evalData = await api("/v1/providers/evaluations");
    const badges = (p) => [p.local?"local":null, p.streaming?"streaming":null, p.vision?"vision":null,
                           p.priced?"priced":null, p.in_route?"in-route":null]
                           .filter(Boolean).map(b=>'<span class="st ok">'+esc(b)+'</span>').join(" ");
    $("providers").innerHTML =
      "strategy: <b>"+esc(providers.strategy||"default")+"</b> · route: <b>"+esc((providers.route||[]).join(" → "))+"</b><br>"+
      providers.providers.map(p=>
        esc(p.name)+" <span class='muted'>"+esc(p.model||"")+"</span> — "+
        (p.credential_configured?"credential set":"no credential")+" "+badges(p) +
        (p.context_tokens ? " <span class='muted'>ctx "+esc(p.context_tokens)+"</span>" : "")
      ).join("<br>");
    $("provider-usage").innerHTML = "<b>Usage analytics</b> (local, "+
      (usageData.analytics_enabled?"on":"off")+"): calls <b>"+esc(usage.calls||0)+"</b> · success <b>"+
      (usage.success_rate===null||usage.success_rate===undefined?"n/a":Math.round(usage.success_rate*100)+"%")+
      "</b> · tokens in <b>"+esc(usage.input_tokens||0)+"</b> out <b>"+esc(usage.output_tokens||0)+"</b>"+
      (usage.estimated_cost!==null&&usage.estimated_cost!==undefined ? " · est. cost <b>"+esc(usage.estimated_cost)+"</b>" : "") +
      ((usage.by_model||[]).length ? "<br>"+usage.by_model.map(r=>
        esc(r.provider)+"/"+esc(r.model)+": "+esc(r.calls)+" calls · median "+esc(r.median_latency_ms)+"ms"+
        (r.estimated_tokens_used?" <span class='muted'>(tokens estimated)</span>":"")
      ).join("<br>") : "");
    const ev = evalData.evaluation;
    $("eval-out").innerHTML = ev
      ? "<b>Latest evaluation:</b> "+esc(ev.passed)+"/"+esc(ev.total)+" passed · "+esc(ev.generated_at)+
        "<br>"+(ev.cases||[]).map(c=>(c.passed?"✅ ":"❌ ")+esc(c.id)+" ("+esc(c.latency_ms)+"ms)").join(" · ")
      : "<span class='muted'>No evaluation has run yet.</span>";

    const kbStats = (await api("/v1/knowledge/stats")).knowledge || {};
    $("knowledge-stats").innerHTML = "documents <b>"+esc(kbStats.documents||0)+"</b> · chunks <b>"+esc(kbStats.chunks||0)+
      "</b> · embeddings <b>"+esc(kbStats.active_embedding_model||"")+"</b> ("+(kbStats.embedding_local?"offline":"provider")+
      ") · notes <b>"+esc((kbStats.memories||{}).note||0)+"</b> (embedded "+esc(kbStats.embedded_notes||0)+")";
    const kbDocs = await api("/v1/knowledge/documents");
    $("knowledge-docs").innerHTML = (kbDocs.documents||[]).length
      ? kbDocs.documents.map(d=>"#"+esc(d.id)+" <b>"+esc(d.title)+"</b> ["+esc(d.kind)+"] · "+esc(d.chunk_count)+" chunks · "+esc(d.source_name)).join("<br>")
      : "<span class='muted'>no documents ingested yet</span>";

    const agentStats = (await api("/v1/agents/stats")).agents || {};
    $("agents-stats").innerHTML = "agents <b>"+esc(agentStats.agents||0)+"</b> · enabled <b>"+esc(agentStats.agents_enabled||0)+
      "</b> · runs <b>"+esc(agentStats.agent_runs||0)+"</b>";
    const agentList = await api("/v1/agents");
    $("agents-list").innerHTML = (agentList.agents||[]).map(a=>"<b>"+esc(a.name)+"</b> · "+esc(a.role)+" · "+esc(a.allowed_tools.length)+" tools"+(a.builtin?" · builtin":"")+(a.enabled?"":" · <span class='bad'>disabled</span>")).join("<br>") || "<span class='muted'>no agents</span>";
    const agentRuns = await api("/v1/agents/runs?limit=8");
    $("agent-runs").innerHTML = (agentRuns.runs||[]).length
      ? agentRuns.runs.map(r=>"#"+esc(r.id)+" <b>"+esc(r.agent)+"</b> ["+esc(r.status)+"] "+esc(r.instruction)).join("<br>")
      : "no agent runs yet";

    const voice = await api("/v1/voice/status");
    const mic = voice.microphone || {};
    $("voice-status").innerHTML =
      "microphone <b>"+esc(mic.indicator)+"</b> (state "+esc(mic.state)+", backend "+esc(mic.backend)+")"+
      " · retention <b>"+esc(mic.retention)+"</b>"+
      " · transcripts <b>"+(mic.store_transcripts?"stored":"not stored")+"</b>"+
      " · cloud STT <b>"+(mic.cloud_stt_allowed?"allowed with consent":"off")+"</b>"+
      "<br>STT <b>"+esc((voice.stt||{}).engine||"unavailable")+"</b>"+
      " · TTS <b>"+esc((voice.tts||{}).engine||"unavailable")+"</b>"+
      " · devices in/out <b>"+esc((voice.input_devices||{}).count)+"/"+esc((voice.output_devices||{}).count)+"</b>"+
      ((voice.emergency_stop)?" · <span class='bad'>emergency stop ENGAGED</span>":"");
    $("voice-enabled").checked = !!mic.enabled;
    $("voice-wake").checked = !!mic.wake_word_enabled;
    const voiceNotes = [];
    if (mic.backend_reason) voiceNotes.push(esc(mic.backend_reason));
    if (!(voice.stt||{}).available && (voice.stt||{}).reason) voiceNotes.push("STT: "+esc(voice.stt.reason));
    if (!(voice.tts||{}).available && (voice.tts||{}).reason) voiceNotes.push("TTS: "+esc(voice.tts.reason));
    if (voiceNotes.length) $("voice-out").innerHTML = voiceNotes.join("<br>");
    const voiceSessions = await api("/v1/voice/sessions?limit=8");
    $("voice-sessions").innerHTML = ((voiceSessions.sessions||[]).length)
      ? voiceSessions.sessions.map(s=>"#"+esc(s.id)+" "+esc(s.mode)+" · "+esc(s.audio_seconds)+"s · audio "+(s.audio_retained?"kept":"discarded")+" · chars "+esc(s.transcript_chars)).join("<br>")
      : "no voice sessions yet";

    const flows = await api("/v1/flows?limit=20");
    const summary = flows.summary || {};
    $("flows-stats").innerHTML =
      "<b>"+esc(summary.flows||0)+"</b> flow(s) · triggers enabled <b>"+esc(summary.triggers_enabled||0)+
      "</b> · runs <b>"+esc(summary.runs||0)+"</b> · last <b>"+esc(summary.last_run_at||"never")+"</b>";
    $("flows-list").innerHTML = (flows.flows||[]).length
      ? flows.flows.map(f=>"<span class='mono'>"+esc(f.id.slice(0,8))+"</span> v"+esc(f.version)+
          (f.enabled?"":" <span class='bad'>DISABLED</span>")+" · "+esc(f.node_count)+" node(s) · "+esc(f.name)).join("<br>")
      : "no flows stored yet";
    const flowRuns = await api("/v1/flow-runs?limit=8");
    $("flow-runs").innerHTML = (flowRuns.runs||[]).length
      ? flowRuns.runs.map(r=>"#"+esc(r.id.slice(0,8))+" <b>"+esc(r.flow_name)+"</b> ["+esc(r.status)+"] via "+esc(r.trigger_kind)).join("<br>")
      : "no flow runs yet";

    const proactive = await api("/v1/proactive/status");
    $("proactive-status").innerHTML =
      "proactive <b>"+(proactive.enabled?"on":"off")+"</b> · reminders <b>"+
      esc(proactive.reminders_open||0)+"</b> · next <b>"+esc(proactive.next_due_at||"none")+
      "</b> · notifications <b>"+esc(proactive.notifications||0)+"</b> (last hour "+
      esc(proactive.notifications_last_hour||0)+"/"+esc(proactive.notification_max_per_hour||0)+
      ") · quiet <b>"+esc(proactive.quiet_hours||"none")+"</b>"+
      (proactive.quiet_now?" <span class='bad'>active</span>":"")+
      " · briefing <b>"+(proactive.briefing_enabled?esc(proactive.briefing_time):"off")+"</b>";
    const reminders = await api("/v1/proactive/reminders?limit=10");
    $("proactive-reminders").innerHTML = ((reminders.reminders||[]).length)
      ? reminders.reminders.map(r=>"<span class='mono'>"+esc(r.id.slice(0,8))+"</span> "+esc(r.status)+
          " · "+esc(r.due_at.slice(0,19))+" · "+esc(r.title.slice(0,50))).join("<br>")
      : "no reminders stored yet";

    const a11y = await api("/v1/a11y/status");
    applyA11y(a11y);
    const i18n = await api("/v1/i18n/status");
    $("a11y-locale").innerHTML = i18n.locales.map(r =>
      "<option value='"+esc(r.locale)+"'"+(r.locale===i18n.locale?" selected":"")+">"+
      esc(r.locale)+" - "+esc(r.name)+" ("+esc(r.direction)+")</option>").join("");
    $("a11y-scale").value = Math.round(a11y.scale*100);
    $("a11y-contrast").checked = !!a11y.high_contrast;
    $("a11y-motion").checked = !!a11y.reduce_motion;
    $("a11y-status").innerHTML =
      "language <b>"+esc(a11y.locale)+"</b> ("+esc(a11y.language_name)+") · direction <b>"+
      esc(a11y.direction)+"</b> · coverage <b>"+esc(a11y.coverage.percent)+"%</b> · text <b>"+
      Math.round(a11y.scale*100)+"%</b> · contrast <b>"+esc(a11y.contrast.passing)+"/"+
      esc(a11y.contrast.total)+" "+esc(a11y.contrast.wcag_level)+"</b>";
    $("a11y-shortcuts").innerHTML = "Shortcuts: " + a11y.shortcuts.map(s =>
      "<b>"+esc(s.keys)+"</b> "+esc(s.action)).join(" · ");
    $("a11y-limits").innerHTML = "Known limits: " + a11y.limitations.map(esc).join(" · ");

    const sync = await api("/v1/sync/status");
    $("sync-status").innerHTML =
      "sync <b>"+(sync.enabled?"on":"off")+"</b> · device <b>"+
      esc((sync.device||{}).device_id||"").slice(0,12)+"</b> · peers <b>"+
      esc((sync.counts||{}).peers||0)+"</b> · bundles <b>"+
      esc((sync.counts||{}).bundles||0)+"</b> · pending <b>"+
      esc((sync.counts||{}).pending_requests||0)+"</b> · dir <b>"+
      esc(sync.sync_dir||"none")+"</b> ("+esc(sync.sync_dir_state||"-")+") · policy <b>"+
      esc(sync.conflict_policy||"-")+"</b>";
    const syncPeers = await api("/v1/sync/peers");
    $("sync-peers").innerHTML = ((syncPeers.peers||[]).length)
      ? syncPeers.peers.map(p=>esc(p.device_id.slice(0,10))+" <b>"+esc(p.name)+"</b> ["+
          esc(p.status)+"] fp "+esc(p.fingerprint.slice(0,19))).join("<br>")
      : "no paired devices";

    const plugins = await api("/v1/plugins/status");
    $("plugins-status").innerHTML =
      "plugins <b>"+(plugins.enabled?"on":"off")+"</b> · signatures <b>"+
      esc(plugins.signature_required?"required":"optional")+"</b> · keys <b>"+
      esc((plugins.trusted_keys||[]).length)+"</b> · installed <b>"+
      esc((plugins.counts||{}).plugins||0)+"</b> · enabled <b>"+
      esc((plugins.counts||{}).enabled||0)+"</b> · runs <b>"+
      esc((plugins.counts||{}).runs||0)+"</b> · sandbox <b>"+
      esc((plugins.sandbox||{}).mechanism||"none")+"</b>";
    $("plugins-list").innerHTML = ((plugins.plugins||[]).length)
      ? plugins.plugins.map(p=>"<span class='mono'>"+esc(p.id)+"</span> "+esc(p.version)+
          " ["+esc(p.status)+"] tools "+esc((p.tools||[]).length)+
          " · key "+esc(p.signature_key||"unsigned")).join("<br>")
      : "no plugins installed";

    const connectors = await api("/v1/connectors/status");
    $("connectors-status").innerHTML =
      "connectors <b>"+(connectors.allow_connectors?"on":"off")+"</b> · ready <b>"+
      esc((connectors.enabled||[]).join(", ")||"none")+"</b> · events <b>"+
      esc((connectors.ledger||{}).events||0)+"</b> · deliveries <b>"+
      esc((connectors.ledger||{}).deliveries||0)+"</b> ("+esc((connectors.ledger||{}).deliveries_failed||0)+" failed)";
    const connEvents = await api("/v1/connectors/events?limit=8");
    $("connectors-events").innerHTML = ((connEvents.events||[]).length)
      ? connEvents.events.map(e=>esc(e.received_at.slice(0,19))+" <b>"+esc(e.connector)+"</b> "+esc(e.kind)+" · "+esc(e.summary.slice(0,60))).join("<br>")
      : "no connector events yet";

    const tools = await api("/v1/tools");
    $("tools").innerHTML = "<b>"+esc(tools.total)+"</b> tools<br>" +
      tools.tools.map(t=>"<span class='mono'>"+esc(t.id)+"</span>").join(" · ");

    const settings = await api("/v1/settings");
    $("settings").innerHTML = Object.entries(settings.settings)
      .map(([k,v])=>esc(k)+" = "+esc(v)).join("<br>");

    const events = await api("/v1/events?limit=100&since_id="+state.cursor);
    if (events.events.length){
      const box = $("events");
      if (state.cursor===0) box.innerHTML = "";
      events.events.forEach(e=>{
        const div = document.createElement("div");
        div.innerHTML = "<span class='muted'>#"+esc(e.id)+" "+esc((e.created_at||"").slice(11,19))+"</span> <b>"+
          esc(e.event_type)+"</b> <span class='muted'>["+esc(e.source)+"]</span> "+esc(e.summary);
        box.appendChild(div);
      });
      state.cursor = events.cursor;
      box.scrollTop = box.scrollHeight;
    }
  }catch(e){ toast("Refresh failed: "+e.message); }
}

async function showMission(id, quiet){
  try{
    const m = await api("/v1/missions/"+encodeURIComponent(id));
    state.selectedMission = m.id;
    const statusById = {}; m.tasks.forEach(t=>statusById[t.id]=t);
    const levels = (m.graph && m.graph.levels) || [];
    $("mission-graph").innerHTML = "<h2 style='margin-top:12px'>Execution graph — "+esc(m.name)+"</h2>"+
      (levels.length ? "<div class='levels'>"+levels.map((level,i)=>
        "<div class='level'><h3>LEVEL "+i+"</h3>"+level.map(tid=>{
          const t = statusById[tid]||{status:"?",instruction:tid.slice(0,8)};
          return "<div class='chip'>"+pill(t.status)+"<br>"+esc(t.instruction)+"</div>";
        }).join("")+"</div>").join("")+"</div>"
      : "<p class='muted small'>no tasks in this mission yet</p>");
  }catch(e){ if(!quiet) toast(e.message); }
}

document.addEventListener("click", async (ev)=>{
  const el = ev.target.closest("[data-run],[data-cancel],[data-del],[data-mrun],[data-mcancel],[data-mshow],[data-approve],[data-deny]");
  if (el && el.dataset.mshow !== undefined && el.tagName === "A") ev.preventDefault();
  if (!el || needToken()) return;
  try{
    if (el.dataset.run) await api("/v1/tasks/"+el.dataset.run+"/run",{method:"POST",body:JSON.stringify({})});
    else if (el.dataset.cancel){ if(!confirm("Cancel this task?")) return;
      await api("/v1/tasks/"+el.dataset.cancel+"/cancel",{method:"POST",body:JSON.stringify({reason:"Cancelled from Command Center"})}); }
    else if (el.dataset.del){ if(!confirm("Delete this task record? Workspace files stay.")) return;
      await api("/v1/tasks/"+el.dataset.del,{method:"DELETE"}); }
    else if (el.dataset.mrun) await api("/v1/missions/"+el.dataset.mrun+"/run",{method:"POST",body:JSON.stringify({})});
    else if (el.dataset.mcancel){ if(!confirm("Cancel this mission?")) return;
      await api("/v1/missions/"+el.dataset.mcancel+"/cancel",{method:"POST",body:JSON.stringify({reason:"Cancelled from Command Center"})}); }
    else if (el.dataset.mshow){ await showMission(el.dataset.mshow); return; }
    else if (el.dataset.approve){ if(!confirm("Approve this exact-scope request?")) return;
      await api("/v1/approvals/"+el.dataset.approve+"/decide",{method:"POST",body:JSON.stringify({approve:true})}); }
    else if (el.dataset.deny){ if(!confirm("Deny this request?")) return;
      await api("/v1/approvals/"+el.dataset.deny+"/decide",{method:"POST",body:JSON.stringify({approve:false})}); }
    toast("Done."); refreshData();
  }catch(e){ toast(e.message); }
});

$("save-token").onclick = () => {
  state.token = $("token").value.trim();
  if (state.token) sessionStorage.setItem("omni_token", state.token);
  state.cursor = 0; $("events").innerHTML = "";
  toast(state.token ? "Token stored for this tab session." : "Token empty.");
  refreshData();
};
$("clear-token").onclick = () => { state.token=""; sessionStorage.removeItem("omni_token"); $("token").value=""; toast("Token forgotten."); };
$("quick-run").onclick = async () => {
  if (needToken()) return;
  const instruction = $("quick-instruction").value.trim(); if(!instruction) return toast("Write an instruction first.");
  try{
    const accepted = await api("/v1/run",{method:"POST",body:JSON.stringify({instruction, use_ai:$("quick-ai").checked})});
    $("quick-result").textContent = "task " + accepted.task_id.slice(0,8) + " accepted — watch Tasks/Events";
    refreshData();
  }catch(e){ toast(e.message); }
};
$("stream-ask").onclick = async () => {
  if (needToken()) return;
  const prompt = $("stream-prompt").value.trim(); if(!prompt) return toast("Write a prompt first.");
  const out = $("stream-out"); const btn = $("stream-ask");
  out.textContent = ""; btn.disabled = true;
  try{
    await apiStream("/v1/providers/stream", {prompt}, (ev) => {
      if (ev.type === "delta") out.textContent += ev.text;
      else if (ev.type === "error") out.textContent += "\n[error] " + ev.message;
      else if (ev.type === "done") out.textContent += "\n— done (" + ev.chars + " chars)";
      out.scrollTop = out.scrollHeight;
    });
  }catch(e){ toast(e.message); }
  btn.disabled = false;
  refreshData();
};
$("eval-run").onclick = async () => {
  if (needToken()) return;
  const btn = $("eval-run"); btn.disabled = true; btn.textContent = "Evaluating…";
  try{ await api("/v1/providers/evaluations/run",{method:"POST",body:JSON.stringify({})}); toast("Evaluations done."); }
  catch(e){ toast(e.message); }
  btn.disabled = false; btn.textContent = "Run Evaluations";
  refreshData();
};
$("kb-ingest").onclick = async () => {
  if (needToken()) return;
  const path = $("kb-path").value.trim(); if(!path) return toast("Give a workspace file path first.");
  try{
    const doc = await api("/v1/knowledge/ingest",{method:"POST",body:JSON.stringify({path})});
    $("kb-out").textContent = (doc.already_ingested ? "Already ingested" : "Ingested") + " #" + doc.id + ": " + doc.title + " (" + doc.chunk_count + " chunks)";
    refreshData();
  }catch(e){ toast(e.message); }
};
$("kb-search").onclick = async () => {
  if (needToken()) return;
  const q = $("kb-query").value.trim(); if(!q) return toast("Type a query first.");
  try{
    const data = await api("/v1/knowledge/search?q=" + encodeURIComponent(q) + "&limit=8");
    $("kb-out").innerHTML = data.results.length
      ? data.results.map((r,i)=>"[S"+(i+1)+"] <b>"+esc(r.source)+"</b> · "+esc(r.title)+" (score "+esc(r.score)+")<br>"+esc(r.snippet)).join("<br><br>")
      : "<span class='muted'>No matches.</span>";
  }catch(e){ toast(e.message); }
};
$("kb-ask").onclick = async () => {
  if (needToken()) return;
  const question = $("kb-query").value.trim(); if(!question) return toast("Type a question first.");
  const btn = $("kb-ask"); btn.disabled = true;
  try{
    const data = await api("/v1/knowledge/ask",{method:"POST",body:JSON.stringify({question})});
    $("kb-out").innerHTML = data.answer
      ? esc(data.answer) + "<br><br><span class='muted'>Sources: " + data.sources.map((s,i)=>"[S"+(i+1)+"] "+esc(s.title)).join(", ") + " · answered by " + esc(data.provider) + "/" + esc(data.model) + "</span>"
      : esc(data.note || "No sources matched.");
  }catch(e){ toast(e.message); }
  btn.disabled = false;
};
$("agent-run").onclick = async () => {
  if (needToken()) return;
  const name = $("agent-name").value.trim(); const instruction = $("agent-instruction").value.trim();
  if(!name || !instruction) return toast("Give an agent name and an instruction.");
  try{
    const accepted = await api("/v1/agents/"+encodeURIComponent(name)+"/run",{method:"POST",body:JSON.stringify({instruction})});
    $("agent-out").textContent = "Accepted: agent " + accepted.agent + " is running in the background — watch Recent runs below (auto-refresh).";
    refreshData();
  }catch(e){ toast(e.message); }
};
$("agent-team").onclick = async () => {
  if (needToken()) return;
  const goal = $("agent-instruction").value.trim(); if(!goal) return toast("Type a team goal in the instruction box.");
  try{
    const accepted = await api("/v1/agents/team",{method:"POST",body:JSON.stringify({goal})});
    $("agent-out").textContent = "Team " + accepted.team_run_id + " accepted (coordinator decomposes, then specialists run) — watch Recent runs below.";
    refreshData();
  }catch(e){ toast(e.message); }
};
$("voice-enabled").onchange = async () => {
  if (needToken()) { $("voice-enabled").checked = !$("voice-enabled").checked; return; }
  try{
    const res = await api("/v1/voice/enable",{method:"POST",body:JSON.stringify({enabled:$("voice-enabled").checked})});
    $("voice-out").textContent = "microphone " + res.indicator + " (state " + res.state + ")";
    refreshData();
  }catch(e){ toast(e.message); refreshData(); }
};
$("voice-wake").onchange = async () => {
  if (needToken()) { $("voice-wake").checked = !$("voice-wake").checked; return; }
  try{
    const res = await api("/v1/voice/wake-word",{method:"POST",body:JSON.stringify({enabled:$("voice-wake").checked})});
    $("voice-out").textContent = "wake word " + (res.wake_word_enabled?"enabled":"disabled");
    refreshData();
  }catch(e){ toast(e.message); refreshData(); }
};
$("reminder-add").onclick = async () => {
  const title = $("reminder-title").value.trim();
  const due = $("reminder-due").value.trim() || "+15m";
  if (!title) { toast("Type a reminder title first."); return; }
  if (needToken()) return;
  try {
    const out = await api("/v1/proactive/reminders",{method:"POST",body:JSON.stringify({title:title,due:due})});
    $("proactive-out").textContent = "reminder "+out.id.slice(0,8)+" due "+out.due_at.slice(0,19);
    toast("Reminder saved."); refreshData();
  } catch(e){ $("proactive-out").textContent = e.message; }
};
$("proactive-tick").onclick = async () => {
  if (needToken()) return;
  try {
    const out = await api("/v1/proactive/tick",{method:"POST",body:JSON.stringify({})});
    $("proactive-out").textContent = "delivered "+out.total+", deferred "+out.deferred.length+
      ((out.briefing&&out.briefing.briefing)?", briefing "+out.briefing.briefing.item_count+" item(s)":"");
    refreshData();
  } catch(e){ $("proactive-out").textContent = e.message; }
};
$("proactive-briefing").onclick = async () => {
  if (needToken()) return;
  try {
    const out = await api("/v1/proactive/briefing",{method:"POST",body:JSON.stringify({force:true})});
    $("proactive-out").textContent = out.briefing.summary + "\n" +
      Object.entries(out.sections).map(([k,v])=>k+": "+v.summary).join("\n");
    refreshData();
  } catch(e){ $("proactive-out").textContent = e.message; }
};
function applyA11y(a11y){
  const root = document.documentElement;
  root.lang = a11y.locale;
  root.dir = a11y.rtl ? "rtl" : "ltr";
  document.body.classList.toggle("high-contrast", !!a11y.high_contrast);
  document.body.classList.toggle("reduce-motion", !!a11y.reduce_motion);
  root.style.fontSize = Math.round(100*a11y.scale)+"%";
  $("a11y-live").textContent = a11y.fallback_notice || "";
}
$("a11y-apply").onclick = () => {
  const a11y = {
    locale: $("a11y-locale").value.split(" - ")[0].trim(),
    name: $("a11y-locale").value,
    rtl: $("a11y-locale").value.indexOf("rtl") > -1,
    scale: (parseInt($("a11y-scale").value||"100",10)||100)/100,
    high_contrast: $("a11y-contrast").checked,
    reduce_motion: $("a11y-motion").checked,
    coverage: {percent: "–"},
    contrast: {passing: "–", total: "–", wcag_level: ""},
    fallback_notice: ""
  };
  applyA11y(a11y);
  toast("Applied in this tab. Save to keep it for next time.");
};
$("a11y-save").onclick = async () => {
  if (needToken()) return;
  try {
    const out = await api("/v1/i18n/locale",{method:"POST",body:JSON.stringify({
      locale: $("a11y-locale").value.split(" - ")[0].trim(),
      scale: (parseInt($("a11y-scale").value||"100",10)||100)/100,
      high_contrast: $("a11y-contrast").checked,
      reduce_motion: $("a11y-motion").checked
    })});
    toast("Saved: "+Object.keys(out.saved).join(", ")+" — restart the desktop app to apply.");
    refreshData();
  } catch(e){ toast(e.message); }
};
$("sync-pair").onclick = async () => {
  const name = $("sync-peer-name").value.trim();
  const sign = $("sync-sign").value.trim();
  const enc = $("sync-encrypt").value.trim();
  if (!name || !sign || !enc) { toast("Peer name and both public keys are required."); return; }
  if (needToken()) return;
  try {
    const out = await api("/v1/sync/pair",{method:"POST",
      body:JSON.stringify({name:name,sign_public:sign,encrypt_public:enc,confirm:"PAIR"})});
    $("sync-out").textContent = "paired "+out.name+" ("+out.device_id.slice(0,12)+")";
    toast("Device paired."); refreshData();
  } catch(e){ $("sync-out").textContent = e.message; }
};
$("sync-export").onclick = async () => {
  if (needToken()) return;
  try {
    const body = {}; const p = $("sync-path").value.trim();
    if (p) body.path = p;
    const out = await api("/v1/sync/export",{method:"POST",body:JSON.stringify(body)});
    $("sync-out").textContent = "exported "+out.records+" record(s), "+out.size_bytes+
      " bytes for "+out.recipients.length+" device(s)";
    refreshData();
  } catch(e){ $("sync-out").textContent = e.message; }
};
$("sync-import").onclick = async () => {
  const path = $("sync-path").value.trim();
  if (!path) { toast("Type a bundle path first."); return; }
  if (needToken()) return;
  try {
    const out = await api("/v1/sync/import",{method:"POST",body:JSON.stringify({path:path})});
    $("sync-out").textContent = "imported seq "+out.seq+": applied "+out.applied+
      ", skipped "+out.skipped+", conflicts "+out.conflicts.length;
    refreshData();
  } catch(e){ $("sync-out").textContent = e.message; }
};
$("sync-tick").onclick = async () => {
  if (needToken()) return;
  try {
    const out = await api("/v1/sync/tick",{method:"POST",body:JSON.stringify({})});
    $("sync-out").textContent = "exported 1 bundle; imported "+out.imported.length+
      ", refused "+out.refused.length;
    refreshData();
  } catch(e){ $("sync-out").textContent = e.message; }
};
$("plugin-verify").onclick = async () => {
  const bundle = $("plugin-bundle").value.trim();
  if (!bundle) { toast("Type a bundle path first."); return; }
  if (needToken()) return;
  try {
    const out = await api("/v1/plugins/verify",{method:"POST",body:JSON.stringify({bundle:bundle})});
    $("plugin-out").textContent = out.manifest.name+" "+out.manifest.version+
      " files="+out.file_count+" signature="+out.signature_status;
  } catch(e){ $("plugin-out").textContent = e.message; }
};
$("plugin-install").onclick = async () => {
  const bundle = $("plugin-bundle").value.trim();
  if (!bundle) { toast("Type a bundle path first."); return; }
  if (needToken()) return;
  try {
    const out = await api("/v1/plugins/install",{method:"POST",body:JSON.stringify({bundle:bundle})});
    $("plugin-out").textContent = "installed "+out.name+" "+out.version+
      " ("+out.signature_status+"); enable it to run tools";
    toast("Plugin installed."); refreshData();
  } catch(e){ $("plugin-out").textContent = e.message; }
};
$("plugin-run").onclick = async () => {
  const id = $("plugin-id").value.trim();
  const tool = $("plugin-tool").value.trim();
  if (!id || !tool) { toast("Type the plugin id and tool name first."); return; }
  if (needToken()) return;
  let values = {};
  try { values = JSON.parse($("plugin-args").value.trim() || "{}"); }
  catch(e) { toast("Tool arguments must be JSON."); return; }
  try {
    const out = await api("/v1/plugins/"+encodeURIComponent(id)+"/run",
      {method:"POST",body:JSON.stringify({tool:tool,values:values,confirm:id})});
    $("plugin-out").textContent = out.status+" in "+out.duration_ms+"ms ("+
      out.sandbox.mechanism+")\n"+JSON.stringify(out.result);
    refreshData();
  } catch(e){ $("plugin-out").textContent = e.message; }
};
$("plugin-enable").onclick = async () => {
  const id = $("plugin-id").value.trim();
  if (!id) { toast("Type the plugin id first."); return; }
  if (needToken()) return;
  try {
    const out = await api("/v1/plugins/"+encodeURIComponent(id)+"/enabled",
      {method:"POST",body:JSON.stringify({enabled:true})});
    $("plugin-out").textContent = out.id+" is now "+out.status; refreshData();
  } catch(e){ $("plugin-out").textContent = e.message; }
};
$("plugin-disable").onclick = async () => {
  const id = $("plugin-id").value.trim();
  if (!id) { toast("Type the plugin id first."); return; }
  if (needToken()) return;
  try {
    const out = await api("/v1/plugins/"+encodeURIComponent(id)+"/enabled",
      {method:"POST",body:JSON.stringify({enabled:false})});
    $("plugin-out").textContent = out.id+" is now "+out.status; refreshData();
  } catch(e){ $("plugin-out").textContent = e.message; }
};
$("connector-calendar").onclick = async () => {
  if (needToken()) return;
  try {
    const out = await api("/v1/connectors/calendar?days=7");
    $("connector-out").textContent = (out.events||[]).length
      ? out.events.map(e=>e.start+" → "+e.end+"  "+e.summary).join("\n")
      : "no events in the next "+out.days_ahead+" day(s)";
  } catch(e){ $("connector-out").textContent = e.message; }
};
$("connector-drive").onclick = async () => {
  if (needToken()) return;
  try {
    const out = await api("/v1/connectors/drive/sync",{method:"POST",body:JSON.stringify({})});
    $("connector-out").textContent =
      out.files+" file(s) indexed, "+out.total_bytes+" bytes; added "+out.added+
      " modified "+out.modified+" removed "+out.removed+" skipped "+out.skipped;
    refreshData();
  } catch(e){ $("connector-out").textContent = e.message; }
};
$("connector-mail").onclick = async () => {
  if (needToken()) return;
  try {
    const out = await api("/v1/connectors/mail/inbox?limit=5");
    $("connector-out").textContent = (out.messages||[]).length
      ? out.messages.map(m=>m.date.slice(0,28)+"  "+m.from+"  "+m.subject).join("\n")
      : "the mailbox returned no message headers";
  } catch(e){ $("connector-out").textContent = e.message; }
};
$("connector-chat-send").onclick = async () => {
  const text = $("connector-chat").value.trim();
  if (!text) { toast("Type the chat message first."); return; }
  if (needToken()) return;
  try {
    const out = await api("/v1/connectors/chat/send",{method:"POST",body:JSON.stringify({text:text})});
    $("connector-out").textContent = (out.result.ok?"delivered":"failed")+
      " HTTP "+out.result.status_code+" → "+out.result.target;
    refreshData();
  } catch(e){ $("connector-out").textContent = e.message; }
};
function flowRef(){
  const ref = $("flow-ref").value.trim();
  if (!ref) { toast("Type a flow id or exact name first."); return null; }
  return ref;
}
$("flow-run").onclick = async () => {
  const ref = flowRef(); if (!ref || needToken()) return;
  try {
    const out = await api("/v1/flows/"+encodeURIComponent(ref)+"/run",{method:"POST",body:JSON.stringify({})});
    $("flow-out").textContent = "started mission "+(out.mission_id||"")+" levels "+JSON.stringify(out.levels||[]);
    toast("Flow started."); refreshData();
  } catch(e){ toast(e.message); }
};
$("flow-show").onclick = async () => {
  const ref = flowRef(); if (!ref || needToken()) return;
  try {
    const flow = await api("/v1/flows/"+encodeURIComponent(ref));
    const nodes = (flow.flow||{}).nodes||[];
    const edges = (flow.flow||{}).edges||[];
    $("flow-out").textContent =
      flow.name+" v"+flow.version+" ("+nodes.length+" nodes, "+((flow.levels||[]).length)+" levels)\n"+
      nodes.map(n=>"• "+n.id+(n.title?" ("+n.title+")":"")+" → "+n.instruction).join("\n")+
      "\nedges: "+edges.map(e=>e.from+"→"+e.to).join(", ")+
      "\ntriggers: "+((flow.triggers||[]).map(t=>t.kind+":"+(t.next_run_at||"-")).join(", ")||"none");
  } catch(e){ toast(e.message); }
};
$("flow-tick").onclick = async () => {
  if (needToken()) return;
  try {
    const out = await api("/v1/flows/tick",{method:"POST",body:JSON.stringify({limit:10})});
    $("flow-out").textContent = "fired "+out.total+" trigger(s)";
    toast("Triggers ticked."); refreshData();
  } catch(e){ toast(e.message); }
};
$("flow-export").onclick = async () => {
  const ref = flowRef(); if (!ref || needToken()) return;
  try {
    const doc = await api("/v1/flows/"+encodeURIComponent(ref)+"/export");
    $("flow-json").value = JSON.stringify(doc);
    $("flow-out").textContent = "exported with checksum "+doc.checksum;
  } catch(e){ toast(e.message); }
};
$("flow-create").onclick = async () => {
  if (needToken()) return;
  let doc; try { doc = JSON.parse($("flow-json").value); } catch(e){ return toast("That is not valid JSON."); }
  try {
    const flow = await api("/v1/flows",{method:"POST",body:JSON.stringify(doc)});
    $("flow-out").textContent = "created "+flow.name+" v"+flow.version;
    toast("Flow created."); refreshData();
  } catch(e){ toast(e.message); }
};
$("flow-import").onclick = async () => {
  if (needToken()) return;
  let doc; try { doc = JSON.parse($("flow-json").value); } catch(e){ return toast("That is not valid JSON."); }
  try {
    const out = await api("/v1/flows/import",{method:"POST",body:JSON.stringify({document:doc,overwrite:true})});
    $("flow-out").textContent = "imported "+out.flow.name+" v"+out.flow.version+
      " with "+((out.triggers||[]).length)+" trigger(s)";
    toast("Flow imported."); refreshData();
  } catch(e){ toast(e.message); }
};

$("voice-start").onclick = async () => {
  if (needToken()) return;
  try{
    const res = await api("/v1/voice/capture/start",{method:"POST",body:JSON.stringify({mode:"push_to_talk"})});
    $("voice-out").textContent = "recording via " + res.backend + " (max " + res.max_seconds + "s) — press Stop to transcribe.";
    refreshData();
  }catch(e){ toast(e.message); }
};
$("voice-stop").onclick = async () => {
  if (needToken()) return;
  try{
    const res = await api("/v1/voice/capture/stop",{method:"POST",body:JSON.stringify({cloud_consent:false})});
    $("voice-out").textContent = res.text ? ("transcript ("+res.engine+"): " + res.text) : ("no transcript: " + (res.error||"silence"));
    refreshData();
  }catch(e){ toast(e.message); }
};
$("voice-cancel").onclick = async () => {
  if (needToken()) return;
  try{ await api("/v1/voice/capture/cancel",{method:"POST",body:JSON.stringify({})}); $("voice-out").textContent="capture cancelled"; refreshData(); }
  catch(e){ toast(e.message); }
};
$("voice-purge").onclick = async () => {
  if (needToken()) return;
  if(!confirm("Delete every retained recording?")) return;
  try{ const res = await api("/v1/voice/purge",{method:"POST",body:JSON.stringify({})}); $("voice-out").textContent="purged " + res.removed + " recording(s)"; refreshData(); }
  catch(e){ toast(e.message); }
};
$("voice-speak").onclick = async () => {
  if (needToken()) return;
  const text = $("voice-say").value.trim(); if(!text) return toast("Type something to speak.");
  try{ const res = await api("/v1/voice/speak",{method:"POST",body:JSON.stringify({text})});
    $("voice-out").textContent = res.ok ? ("spoke via " + res.engine) : ("speech failed: " + res.error);
    refreshData();
  }catch(e){ toast(e.message); }
};
$("mission-create").onclick = async () => {
  if (needToken()) return;
  const name = $("mission-name").value.trim(); if(!name) return toast("Give the mission a name.");
  const lines = $("mission-tasks").value.split("\n").map(s=>s.trim()).filter(Boolean);
  const chain = $("mission-chain").checked;
  const tasks = lines.map((instruction,i)=>({key:"t"+i, instruction, depends_on: chain && i>0 ? ["t"+(i-1)] : []}));
  try{
    const created = await api("/v1/missions",{method:"POST",body:JSON.stringify({name, tasks})});
    state.selectedMission = created.id; toast("Mission created."); refreshData();
  }catch(e){ toast(e.message); }
};
$("safety-stop").onclick = async () => {
  if (needToken()) return;
  const reason = prompt("Reason for the emergency stop (no secrets):", "Command Center"); if(reason===null) return;
  try{ await api("/v1/safety/stop",{method:"POST",body:JSON.stringify({reason})}); toast("Emergency stop engaged."); refreshHealth(); refreshData(); }
  catch(e){ toast(e.message); }
};
$("safety-resume").onclick = async () => {
  if (needToken()) return;
  if(!confirm("Clear the emergency stop so tool execution can run again?")) return;
  try{ await api("/v1/safety/resume",{method:"POST",body:JSON.stringify({})}); toast("Emergency stop cleared."); refreshHealth(); refreshData(); }
  catch(e){ toast(e.message); }
};
$("schedules-run-due").onclick = async () => {
  if (needToken()) return;
  try{ const r = await api("/v1/schedules/run-due",{method:"POST",body:JSON.stringify({})});
    toast("Triggered "+r.total+" schedule(s)."); refreshData(); }
  catch(e){ toast(e.message); }
};

refreshHealth(); refreshData();
setInterval(()=>{ refreshHealth(); refreshData(); }, 2500);
</script>
</body>
</html>
"""
