from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from config import Settings, load_settings
from health import acknowledge_issue, get_snapshot, run_health_checks
from notifier import get_recent_log_entries


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_server_thread: threading.Thread | None = None
_server_instance: ThreadingHTTPServer | None = None
_server_lock = threading.Lock()


HTML = """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
  <title>Grocery AI Admin</title>
  <style>
    :root { --bg:#f3efe5; --panel:#fffaf1; --ink:#1f2933; --muted:#6b7280; --ok:#1d7f4e; --warn:#b7791f; --bad:#b42318; --line:#d8d1c4; }
    body { margin:0; font-family: Georgia, 'Iowan Old Style', serif; background:linear-gradient(180deg,#efe6d5 0%,#f8f4ec 55%,#ece7dc 100%); color:var(--ink); }
    header { padding:24px 28px; border-bottom:1px solid var(--line); background:rgba(255,250,241,.82); position:sticky; top:0; backdrop-filter: blur(8px); }
    h1,h2,h3 { margin:0 0 10px; }
    main { padding:24px; display:grid; gap:18px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); }
    section { background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow:0 14px 34px rgba(77,57,25,.08); }
    .wide { grid-column:1 / -1; }
    .grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); }
    .card { border:1px solid var(--line); border-radius:14px; padding:12px; background:#fff; }
    .state { font-weight:700; text-transform:capitalize; }
    .healthy { color:var(--ok); } .degraded { color:var(--warn); } .disconnected, .critical { color:var(--bad); } .unknown { color:var(--muted); }
    .toolbar, .filters { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
    button,input,select { font:inherit; padding:10px 12px; border-radius:10px; border:1px solid var(--line); background:#fff; }
    button { cursor:pointer; }
    .pill { display:inline-block; padding:3px 8px; border-radius:999px; background:#f0e6d4; font-size:12px; }
    .list { display:grid; gap:10px; }
    .row { border:1px solid var(--line); border-radius:12px; padding:12px; background:#fff; }
    .meta { color:var(--muted); font-size:13px; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    @media (max-width: 720px) { header, main { padding:16px; } }
  </style>
</head>
<body>
  <header>
    <div class=\"toolbar\">
      <div>
        <h1>Administrator Interface</h1>
        <div class=\"meta\">Local status dashboard for Grocery AI</div>
      </div>
      <button id=\"healthCheckButton\">Run health check now</button>
      <span class=\"pill\" id=\"refreshStatus\">Refreshing every 5 seconds</span>
    </div>
    <div id=\"overallStatus\" style=\"margin-top:14px;padding:12px 14px;border-radius:14px;border:1px solid var(--line);background:#fff\"></div>
  </header>
  <main>
    <section>
      <h2>Connections</h2>
      <div id=\"connections\" class=\"grid\"></div>
    </section>
    <section>
      <h2>Modes</h2>
      <div id=\"modes\" class=\"grid\"></div>
    </section>
    <section class=\"wide\">
      <h2>Active Issues</h2>
      <div id=\"issues\" class=\"list\"></div>
    </section>
    <section class=\"wide\">
      <h2>Important Events</h2>
      <div id=\"events\" class=\"list\"></div>
    </section>
    <section class=\"wide\">
      <h2>Reolink E1 Pro Tips</h2>
      <div class=\"list\">
        <div class=\"row\">
          <div><strong>Recommended stream</strong></div>
          <div class=\"meta mono\">rtsp://user:pass@camera-ip:554/Preview_01_sub</div>
          <div class=\"meta\">Use the sub stream first for continuous passive monitoring and door-open detection.</div>
        </div>
        <div class=\"row\">
          <div><strong>When to switch streams</strong></div>
          <div class=\"meta\">If item recognition detail is too low, test <span class=\"mono\">Preview_01_main</span>. If FPS drops or scans lag, switch back to the sub stream.</div>
        </div>
        <div class=\"row\">
          <div><strong>If camera health degrades</strong></div>
          <div class=\"meta\">Check that RTSP is enabled, confirm the camera IP and credentials, and test the RTSP URL independently.</div>
        </div>
        <div class=\"row\">
          <div><strong>If door-open detection is noisy</strong></div>
          <div class=\"meta\">Increase the door-open motion threshold, settle time, or cooldown values in the environment settings.</div>
        </div>
      </div>
    </section>
    <section class=\"wide\">
      <h2>Log Viewer</h2>
      <div class=\"filters\">
        <select id=\"levelFilter\"><option value=\"\">All levels</option><option>INFO</option><option>WARNING</option><option>ERROR</option></select>
        <input id=\"moduleFilter\" placeholder=\"Filter by module\">
        <input id=\"searchFilter\" placeholder=\"Search logs\">
      </div>
      <div id=\"logs\" class=\"list\" style=\"margin-top:12px\"></div>
    </section>
  </main>
  <script>
    const stateClass = (value) => String(value || 'unknown').toLowerCase();
    const fmt = (value) => value || '—';
    async function refresh() {
      const params = new URLSearchParams({
        level: document.getElementById('levelFilter').value,
        module: document.getElementById('moduleFilter').value,
        search: document.getElementById('searchFilter').value,
      });
      const response = await fetch('/api/snapshot?' + params.toString());
      const data = await response.json();
      renderOverallStatus(data.overall_status);
      renderConnections(data.connections);
      renderModes(data.modes);
      renderIssues(data.issues);
      renderEvents(data.recent_events);
      renderLogs(data.logs);
    }
    function renderOverallStatus(status) {
      const el = document.getElementById('overallStatus');
      const state = status?.state || 'unknown';
      const message = status?.message || 'Status unavailable';
      const colors = {
        healthy: ['#eaf7ee', '#1d7f4e'],
        warning: ['#fff6e5', '#b7791f'],
        unsafe: ['#fff0ef', '#b42318'],
        unknown: ['#f4f4f5', '#6b7280'],
      };
      const [bg, fg] = colors[state] || colors.unknown;
      el.style.background = bg;
      el.style.color = fg;
      el.style.borderColor = fg + '33';
      el.innerHTML = `<strong style=\"text-transform:capitalize\">${state}</strong> · ${message}`;
    }
    function renderConnections(connections) {
      const el = document.getElementById('connections');
      el.innerHTML = Object.values(connections).map(item => `
        <div class=\"card\">
          <h3>${item.name}</h3>
          <div class=\"state ${stateClass(item.state)}\">${item.state}</div>
          <div class=\"meta\">Last success: ${fmt(item.last_success_timestamp)}</div>
          <div class=\"meta\">Last failure: ${fmt(item.last_failure_timestamp)}</div>
          <div class=\"meta\">Failures: ${item.consecutive_failures}</div>
          <div class=\"meta\">Error: ${fmt(item.error_message)}</div>
        </div>`).join('');
    }
    function renderModes(modes) {
      const el = document.getElementById('modes');
      el.innerHTML = Object.values(modes).map(item => `
        <div class=\"card\">
          <h3>${item.name}</h3>
          <div class=\"state ${item.running ? 'healthy' : 'unknown'}\">${item.running ? 'running' : 'stopped'}</div>
          <div class=\"meta\">Last passive scan: ${fmt(item.last_passive_scan_timestamp)}</div>
          <div class=\"meta\">Last gesture detect: ${fmt(item.last_successful_gesture_detection_timestamp)}</div>
          <div class=\"meta\">Last item add: ${fmt(item.last_successful_item_add_timestamp)}</div>
        </div>`).join('');
    }
    function renderIssues(issues) {
      const el = document.getElementById('issues');
      const active = Object.values(issues).filter(issue => issue.active);
      if (!active.length) {
        el.innerHTML = '<div class="row">No active issues.</div>';
        return;
      }
      el.innerHTML = active.map(issue => `
        <div class=\"row\">
          <div class=\"toolbar\">
            <strong class=\"${stateClass(issue.severity)}\">${issue.severity}</strong>
            <span class=\"pill mono\">${issue.code}</span>
            <button onclick=\"ackIssue('${issue.code}')\">Acknowledge alert</button>
          </div>
          <div>${issue.message}</div>
          <div class=\"meta\">Recommended action: ${issue.recommended_action}</div>
          <div class=\"meta\">First detected: ${issue.first_detected_timestamp}</div>
          <div class=\"meta\">Last seen: ${issue.last_seen_timestamp}</div>
        </div>`).join('');
    }
    function renderEvents(events) {
      const el = document.getElementById('events');
      el.innerHTML = events.slice(0,50).map(event => `
        <div class=\"row\">
          <div><strong>${event.category}</strong></div>
          <div>${event.message}</div>
          <div class=\"meta\">${event.timestamp}</div>
        </div>`).join('') || '<div class="row">No events yet.</div>';
    }
    function renderLogs(logs) {
      const el = document.getElementById('logs');
      el.innerHTML = logs.map(log => `
        <div class=\"row\">
          <div class=\"toolbar\"><strong>${log.level}</strong><span class=\"pill mono\">${log.module}</span><span class=\"pill mono\">${log.action}</span></div>
          <div>${log.message}</div>
          <div class=\"meta\">${log.timestamp}</div>
        </div>`).join('') || '<div class="row">No logs available.</div>';
    }
    async function ackIssue(code) {
      await fetch('/api/issues/' + encodeURIComponent(code) + '/ack', { method: 'POST' });
      await refresh();
    }
    document.getElementById('healthCheckButton').addEventListener('click', async () => {
      await fetch('/api/health-check', { method: 'POST' });
      await refresh();
    });
    document.getElementById('levelFilter').addEventListener('change', refresh);
    document.getElementById('moduleFilter').addEventListener('input', refresh);
    document.getElementById('searchFilter').addEventListener('input', refresh);
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>"""


class AdminUIHandler(BaseHTTPRequestHandler):
    server: "AdminUIServer"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(HTML)
            return
        if parsed.path == "/api/snapshot":
            query = parse_qs(parsed.query)
            level = _first(query.get("level"))
            module = _first(query.get("module"))
            search = _first(query.get("search"))
            payload = get_snapshot().as_dict()
            payload["logs"] = get_recent_log_entries(
                limit=300,
                level=level or None,
                module=module or None,
                search_text=search or None,
            )
            self._send_json(payload)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health-check":
            results = run_health_checks(self.server.settings)
            self._send_json({"ok": True, "results": results})
            return
        if parsed.path.startswith("/api/issues/") and parsed.path.endswith("/ack"):
            code = parsed.path[len("/api/issues/") : -len("/ack")].strip("/")
            self._send_json({"ok": acknowledge_issue(code)})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        return

    def _send_html(self, html: str) -> None:
        encoded = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class AdminUIServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], settings: Settings) -> None:
        super().__init__(server_address, AdminUIHandler)
        self.settings = settings


def start_admin_ui(settings: Settings | None = None) -> str:
    resolved_settings = settings or load_settings()
    host = getattr(resolved_settings, "admin_ui_host", DEFAULT_HOST)
    port = getattr(resolved_settings, "admin_ui_port", DEFAULT_PORT)

    global _server_thread, _server_instance
    with _server_lock:
        if _server_instance is not None:
            return f"http://{host}:{port}"

        server = AdminUIServer((host, port), resolved_settings)
        thread = threading.Thread(target=server.serve_forever, daemon=True, name="admin-ui-thread")
        thread.start()
        _server_instance = server
        _server_thread = thread
        return f"http://{host}:{port}"


def _first(values: list[str] | None) -> str:
    if not values:
        return ""
    return values[0]