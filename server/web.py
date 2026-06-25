"""Static HTML for the /ui dashboard.

A single self-contained page (no build step, no external assets). It talks to
the JSON API with an optional bearer token kept in localStorage, so it works for
both open-auth and token-auth servers.
"""

UI_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>luduclone</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; margin: 0; background: Canvas; color: CanvasText; }
  header { padding: 16px 24px; border-bottom: 1px solid #8884; display: flex;
           align-items: center; gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 20px; margin: 0; }
  .muted { opacity: 0.7; font-size: 13px; }
  main { padding: 24px; max-width: 900px; margin: 0 auto; }
  input { padding: 6px 8px; border: 1px solid #8886; border-radius: 6px;
          background: Field; color: FieldText; }
  button { padding: 6px 12px; border: 1px solid #8886; border-radius: 6px;
           background: ButtonFace; color: ButtonText; cursor: pointer; }
  button:hover { border-color: #888; }
  table { width: 100%; border-collapse: collapse; margin-top: 12px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #8883; font-size: 14px; }
  th { font-weight: 600; opacity: 0.8; }
  tr.game { cursor: pointer; }
  tr.game:hover { background: #8881; }
  .versions { font-size: 13px; opacity: 0.85; }
  .pill { display: inline-block; padding: 1px 8px; border-radius: 999px;
          background: #8882; font-size: 12px; }
  #status { margin-left: auto; font-size: 13px; }
  .empty { opacity: 0.6; padding: 24px 0; }
</style>
</head>
<body>
<header>
  <h1>luduclone</h1>
  <span class="muted">self-hosted save sync</span>
  <input id="token" type="password" placeholder="token (blank if open auth)" size="22">
  <button id="reload">Load</button>
  <span id="status"></span>
</header>
<main>
  <div id="content"><p class="empty">Click <b>Load</b> to list backed-up games.</p></div>
</main>
<script>
const $ = (s) => document.querySelector(s);
const tokenBox = $("#token");
tokenBox.value = localStorage.getItem("luduclone_token") || "";

function headers() {
  const t = tokenBox.value.trim();
  return t ? { "Authorization": "Bearer " + t } : {};
}
function fmtBytes(n) {
  const u = ["B","KB","MB","GB","TB"]; let i = 0; n = Number(n)||0;
  while (n >= 1024 && i < u.length-1) { n /= 1024; i++; }
  return n.toFixed(1) + u[i];
}
function fmtTime(s) {
  if (!s) return "";
  return new Date(s * 1000).toLocaleString();
}

async function load() {
  localStorage.setItem("luduclone_token", tokenBox.value.trim());
  $("#status").textContent = "loading…";
  try {
    const r = await fetch("games", { headers: headers() });
    if (r.status === 401) { $("#status").textContent = "401 — check token"; return; }
    if (!r.ok) { $("#status").textContent = "error " + r.status; return; }
    const games = (await r.json()).games || [];
    render(games);
    $("#status").textContent = games.length + " game(s)";
  } catch (e) {
    $("#status").textContent = "network error";
  }
}

function render(games) {
  const c = $("#content");
  if (!games.length) { c.innerHTML = '<p class="empty">No backups uploaded yet.</p>'; return; }
  let html = '<table><thead><tr><th>Game</th><th>Versions</th><th>Latest</th>' +
             '<th>Last updated</th></tr></thead><tbody>';
  for (const g of games) {
    html += `<tr class="game" data-game="${encodeURIComponent(g.game)}">` +
            `<td>${escapeHtml(g.game)}</td>` +
            `<td><span class="pill">${g.versions}</span></td>` +
            `<td>v${g.latest}</td>` +
            `<td>${fmtTime(g.updated)}</td></tr>` +
            `<tr class="detail" hidden><td colspan="4" class="versions">…</td></tr>`;
  }
  html += "</tbody></table>";
  c.innerHTML = html;
  c.querySelectorAll("tr.game").forEach((row) => {
    row.addEventListener("click", () => toggle(row));
  });
}

async function toggle(row) {
  const detail = row.nextElementSibling;
  detail.hidden = !detail.hidden;
  if (detail.hidden) return;
  const game = row.getAttribute("data-game");
  const cell = detail.querySelector("td");
  cell.textContent = "loading…";
  const r = await fetch(`games/${game}/saves`, { headers: headers() });
  if (!r.ok) { cell.textContent = "error " + r.status; return; }
  const versions = (await r.json()).versions || [];
  cell.innerHTML = versions.map(v =>
    `v${v.version} · ${v.source_os} · ${fmtBytes(v.size)} · ${fmtTime(v.created_at)}`
  ).join("<br>");
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",
    '"':"&quot;","'":"&#39;"}[c]));
}

$("#reload").addEventListener("click", load);
tokenBox.addEventListener("keydown", (e) => { if (e.key === "Enter") load(); });
load();
</script>
</body>
</html>
"""
