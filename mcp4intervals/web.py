"""Self-service onboarding web app (stdlib only, no new dependencies).

Two pages:
* GET /            self-service onboarding form (open). Each user creates or
                   edits their own entry; modifications are protected by the
                   entry's manage secret. The user list is NOT shown here.
* GET /admin       admin page (requires ONBOARD_ADMIN_TOKEN): lists every user
                   and can toggle/delete any entry without a manage secret.

Secrets are never returned; editing without re-entering a secret keeps the
stored value. Bind to 127.0.0.1 by default; expose only behind Tailscale /
your own reverse proxy.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .core import AppError
from .ilink import ILinkClient, ILinkError
from .users import UserStore, canonical_athlete, make_user, user_to_dict


PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<base href="__BASE__">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCP4Intervals 开通</title>
<style>
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; max-width: 760px; margin: 0 auto; padding: 20px; color: #1f2937; }
  h1 { font-size: 21px; }
  .card { border: 1px solid #e5e7eb; border-radius: 10px; padding: 18px; margin: 14px 0; background: #fff; }
  label { display: block; margin: 10px 0 4px; font-weight: 600; font-size: 13px; }
  input, select { width: 100%; padding: 8px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 14px; box-sizing: border-box; }
  .row { display: flex; gap: 12px; }
  .row > div { flex: 1; }
  button { background: #2563eb; color: #fff; border: 0; border-radius: 6px; padding: 9px 14px; font-size: 14px; cursor: pointer; }
  button.secondary { background: #e5e7eb; color: #111; }
  .step { background: #f0f9ff; border-left: 4px solid #0ea5e9; padding: 10px 14px; margin: 10px 0; font-size: 13px; }
  a.cta { display: inline-block; margin: 8px 0 0; background: #07c160; color: #fff; padding: 10px 16px; border-radius: 6px; text-decoration: none; font-weight: 600; }
  #msg { padding: 10px; border-radius: 6px; margin: 10px 0; display: none; }
  #msg.ok { display: block; background: #dcfce7; color: #166534; }
  #msg.err { display: block; background: #fee2e2; color: #991b1b; }
  h3 { font-size: 15px; margin: 16px 0 4px; border-top: 1px dashed #e5e7eb; padding-top: 14px; }
  .hint { color: #6b7280; font-size: 12px; margin-top: 4px; }
</style>
</head>
<body>
<h1>MCP4Intervals · 每日骑行日报开通</h1>

<div class="step">
  <b>第 1 步 · 绑定微信（必须）</b>：点「生成二维码」，用微信扫码并确认登录。
  绑定成功后，<b>用微信给新添加的机器人发任意一条消息</b>（如「开通」），这样每天才能收到日报。
  <br><button onclick="startBind()">生成绑定二维码</button>
  <div id="bindBox" style="display:none; margin-top:10px">
    <img id="qrImg" alt="绑定二维码" style="width:220px;height:220px;display:none">
    <div><a id="qrLink" href="#" target="_blank" rel="noopener" style="display:none">在微信中打开绑定链接 →</a></div>
    <div id="qrStatus" style="margin-top:6px">等待扫码…</div>
  </div>
  <div id="bindState" style="margin-top:6px">微信状态：未绑定</div>
</div>
<div class="step">
  <b>第 2 步 · 填自己的凭据并保存</b>：Intervals.icu 的 API Key 在 Intervals 网站
  Settings → Developer settings 里创建；模型选填（report_mode 选 summary 则不用模型）。
  保存后每天按你设置的时区和时间自动推送训练建议。
</div>

<div class="card">
  <h2 id="formTitle">开通 / 修改我的配置</h2>
  <label>Intervals 运动员 ID（athlete_id，如 i123 或 123）</label>
  <input id="athlete_id" placeholder="i123">
  <div class="row">
    <div>
      <label>管理密钥（首次开通时自设 ≥6 位；修改时输入）</label>
      <input id="manage_secret" type="password" autocomplete="off">
    </div>
  </div>
  <div style="margin:10px 0 0">
    <button class="secondary" onclick="loadMine()">载入我的配置（修改时先点这个）</button>
  </div>
  <h3>账号</h3>
  <label>Intervals.icu API Key</label>
  <input id="intervals_api_key" type="password" autocomplete="off">
  <div class="row">
    <div><label>时区</label><input id="timezone" value="Europe/Amsterdam"></div>
    <div><label>每日发送时间</label><input id="send_at" value="07:00" placeholder="HH:MM"></div>
    <div>
      <label>报告类型</label>
      <select id="report_mode">
        <option value="ai" selected>AI 分析（调用模型）</option>
        <option value="summary">规则摘要（不调用模型）</option>
      </select>
    </div>
  </div>

  <h3>模型 API（report_mode 选 AI 时必填）</h3>
  <label>Base URL（如 https://…/v1）</label>
  <input id="base_url" placeholder="https://…/v1">
  <label>模型名</label>
  <input id="model" placeholder="模型名">
  <label>模型 API Key（本地模型可留空）</label>
  <input id="model_api_key" type="password" autocomplete="off">
  <div class="row">
    <div><label>超时（秒）</label><input id="timeout" value="120"></div>
    <div><label>最大 tokens</label><input id="max_tokens" value="2500"></div>
    <div>
      <label>允许明文 HTTP</label>
      <select id="allow_http"><option value="false" selected>否</option><option value="true">是</option></select>
    </div>
  </div>

  <div style="margin-top:16px">
    <button onclick="save()">保存</button>
    <button class="secondary" onclick="resetForm()">清空</button>
  </div>
  <div class="hint">留空的密钥字段在修改时会保留原值；管理密钥请务必牢记。</div>
  <div id="msg"></div>
</div>

<script>
function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function el(id) { return document.getElementById(id); }
function showMsg(text, ok) { var m = el('msg'); m.textContent = text; m.className = ok ? 'ok' : 'err'; }

var editingId = '';
var bindQrcode = '';
var bindDone = false;

function resetForm() {
  el('formTitle').textContent = '开通 / 修改我的配置';
  el('athlete_id').value = '';
  el('athlete_id').disabled = false;
  el('manage_secret').value = '';
  el('intervals_api_key').value = '';
  el('model_api_key').value = '';
  el('timezone').value = 'Europe/Amsterdam';
  el('send_at').value = '07:00';
  el('report_mode').value = 'ai';
  el('base_url').value = '';
  el('model').value = '';
  el('timeout').value = '120';
  el('max_tokens').value = '2500';
  el('allow_http').value = 'false';
  editingId = '';
  bindQrcode = '';
  bindDone = false;
  el('bindBox').style.display = 'none';
  el('bindState').textContent = '微信状态：未绑定';
  showMsg('', false);
}

function fillFrom(u) {
  el('timezone').value = u.timezone;
  el('send_at').value = u.send_at;
  el('report_mode').value = u.report_mode;
  el('base_url').value = u.model_base_url || '';
  el('model').value = u.model_name || '';
  el('timeout').value = u.model_timeout || 120;
  el('max_tokens').value = u.model_max_tokens || 2500;
  el('allow_http').value = u.model_allow_http ? 'true' : 'false';
}

function loadMine() {
  var id = el('athlete_id').value.trim();
  var secret = el('manage_secret').value.trim();
  if (!id) { showMsg('请先填写运动员 ID', false); return; }
  if (!secret) { showMsg('请输入管理密钥以载入配置', false); return; }
  fetch('api/user/' + encodeURIComponent(id) + '/load', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ manage_secret: secret }) })
    .then(function(r){ return r.json().then(function(d){ return { ok: r.ok, d: d }; }); })
    .then(function(res){
      if (!res.ok) { showMsg(res.d.message || '载入失败', false); return; }
      fillFrom(res.d);
      el('athlete_id').disabled = true;
      editingId = res.d.athlete_id;
      el('bindState').textContent = '微信状态：' + (res.d.wechat_bound ? '已绑定' : '未绑定');
      showMsg('已载入 ' + res.d.athlete_id + '，修改后点保存', true);
    })
    .catch(function(){ showMsg('请求失败', false); });
}

function startBind() {
  fetch('api/ilink/qr', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: '{}' })
    .then(function(r){ return r.json().then(function(d){ return { ok: r.ok, d: d }; }); })
    .then(function(res){
      if (!res.ok) { showMsg(res.d.message || '生成二维码失败', false); return; }
      bindQrcode = res.d.qrcode;
      bindDone = false;
      el('bindBox').style.display = 'block';
      if (res.d.qr_png) {
        el('qrImg').src = 'data:image/png;base64,' + res.d.qr_png;
        el('qrImg').style.display = 'block';
        el('qrLink').style.display = 'none';
      } else {
        el('qrImg').style.display = 'none';
        el('qrLink').href = res.d.qrcode_img_content;
        el('qrLink').style.display = 'inline';
      }
      el('qrStatus').textContent = '等待扫码…';
      el('bindState').textContent = '微信状态：等待扫码';
      pollBind();
    })
    .catch(function(){ showMsg('请求失败', false); });
}

function pollBind() {
  if (!bindQrcode || bindDone) return;
  fetch('api/ilink/qr/status', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ qrcode: bindQrcode }) })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d.status === 'confirmed') {
        bindDone = true;
        el('qrStatus').textContent = '已确认登录';
        el('bindState').textContent = '微信状态：已绑定';
        showMsg('微信绑定成功，请继续填写并保存；保存后记得用微信给机器人发一条消息。', true);
      } else if (d.status === 'expired') {
        bindQrcode = '';
        el('qrStatus').textContent = '二维码已过期，请重新生成';
        el('bindState').textContent = '微信状态：未绑定';
      } else if (d.status === 'scaned') {
        el('qrStatus').textContent = '已扫码，请在手机确认…';
        setTimeout(pollBind, 3000);
      } else {
        el('qrStatus').textContent = '等待扫码…';
        setTimeout(pollBind, 3000);
      }
    })
    .catch(function(){ setTimeout(pollBind, 3000); });
}

function save() {
  var id = el('athlete_id').value.trim();
  if (!id) { showMsg('请填写运动员 ID', false); return; }
  if (!editingId && !bindDone) { showMsg('请先完成第 1 步微信绑定', false); return; }
  var body = {
    athlete_id: id,
    manage_secret: el('manage_secret').value.trim(),
    intervals_api_key: el('intervals_api_key').value.trim(),
    timezone: el('timezone').value.trim(),
    send_at: el('send_at').value.trim(),
    report_mode: el('report_mode').value
  };
  if (bindQrcode) { body.qrcode = bindQrcode; }
  var base = el('base_url').value.trim();
  var model = el('model').value.trim();
  if (base || model) {
    body.model = {
      base_url: base,
      model: model,
      api_key: el('model_api_key').value.trim(),
      timeout: parseInt(el('timeout').value, 10) || 120,
      max_tokens: parseInt(el('max_tokens').value, 10) || 2500,
      allow_http: el('allow_http').value === 'true'
    };
  }
  fetch('api/user', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(function(r){ return r.json().then(function(d){ return { ok: r.ok, d: d }; }); })
    .then(function(res){
      if (res.ok) { showMsg('已保存 ' + res.d.athlete_id, true); resetForm(); }
      else { showMsg(res.d.message || '保存失败', false); }
    })
    .catch(function(){ showMsg('请求失败', false); });
}

resetForm();
</script>
</body>
</html>
"""

ADMIN_LOGIN = """<!doctype html>
<html lang="zh">
<head><meta charset="utf-8"><base href="__BASE__"><title>管理员登录</title></head>
<body>
<h1>MCP4Intervals 管理员</h1>
<p>请输入管理员令牌：</p>
<input id="token" type="password" style="width:260px">
<button onclick="login()">进入</button>
<script>
function login() {
  var t = document.getElementById('token').value;
  fetch('login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: t }) })
    .then(function(r){ if (r.ok) { location.reload(); } else { alert('令牌错误'); } });
}
</script>
</body>
</html>
"""

ADMIN_PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<base href="__BASE__">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCP4Intervals 管理员</title>
<style>
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; max-width: 820px; margin: 0 auto; padding: 20px; color: #1f2937; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px; border-bottom: 1px solid #eee; vertical-align: top; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; background: #eef2ff; color: #4338ca; }
  .badge.off { background: #fee2e2; color: #991b1b; }
  a { color: #2563eb; }
</style>
</head>
<body>
<h1>MCP4Intervals · 已开通用户（管理员）</h1>
<table>
  <thead><tr><th>运动员</th><th>报告</th><th>时间</th><th>时区</th><th>凭据</th><th>状态</th><th>操作</th></tr></thead>
  <tbody id="userRows"><tr><td colspan="7">加载中…</td></tr></tbody>
</table>
<script>
function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function td(html) { var t = document.createElement('td'); t.innerHTML = html; return t; }
function link(text, fn) { var a = document.createElement('a'); a.href = '#'; a.textContent = text; a.onclick = function(e){ e.preventDefault(); fn(); }; return a; }
function loadUsers() {
  fetch('api/users', { credentials: 'same-origin' }).then(function(r){ return r.json(); }).then(function(data){
    var rows = data.users || [];
    var tb = document.getElementById('userRows');
    tb.innerHTML = '';
    if (rows.length === 0) { tb.innerHTML = '<tr><td colspan="7">还没有用户。</td></tr>'; return; }
    rows.forEach(function(u){
      var tr = document.createElement('tr');
      tr.appendChild(td(esc(u.athlete_id)));
      tr.appendChild(td(esc(u.report_mode) + (u.model_name ? ' (' + esc(u.model_name) + ')' : '')));
      tr.appendChild(td(esc(u.send_at)));
      tr.appendChild(td(esc(u.timezone)));
      tr.appendChild(td((u.intervals_api_key_set ? 'I✓' : 'I✗') + ' ' + (u.wechat_bound ? 'W✓' : 'W✗') + ' ' + (u.model_api_key_set ? 'M✓' : 'M—')));
      tr.appendChild(td(u.enabled ? '<span class="badge">启用</span>' : '<span class="badge off">停用</span>'));
      var ops = document.createElement('td');
      ops.appendChild(link(u.enabled ? '停用' : '启用', function(){ toggleUser(u.athlete_id); }));
      ops.appendChild(document.createTextNode(' · '));
      var del = link('删除', function(){ delUser(u.athlete_id); });
      del.style.color = '#dc2626';
      ops.appendChild(del);
      tr.appendChild(ops);
      tb.appendChild(tr);
    });
  }).catch(function(){ document.getElementById('userRows').innerHTML = '<tr><td colspan="7">加载失败</td></tr>'; });
}
function toggleUser(id) {
  fetch('api/user/' + encodeURIComponent(id) + '/toggle', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(function(){ loadUsers(); });
}
function delUser(id) {
  if (!confirm('确认删除 ' + id + ' 的配置？')) return;
  fetch('api/user/' + encodeURIComponent(id) + '/delete', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(function(){ loadUsers(); });
}
loadUsers();
</script>
</body>
</html>
"""


def configured_token() -> str:
    return os.getenv("ONBOARD_ADMIN_TOKEN", "").strip()


def web_base() -> str:
    prefix = os.getenv("WEB_PREFIX", "").strip().strip("/")
    return "/" + prefix + "/" if prefix else "/"


def app_path(path: str) -> str:
    prefix = os.getenv("WEB_PREFIX", "").strip().strip("/")
    if prefix:
        prefixed = "/" + prefix
        if path == prefixed or path.startswith(prefixed + "/"):
            return path[len(prefixed):] or "/"
    return path


def _parse_cookies(header: str) -> dict:
    out = {}
    for part in header.split(";"):
        if "=" in part:
            key, _, value = part.partition("=")
            out[key.strip()] = value.strip()
    return out


def _token_ok(value) -> bool:
    token = configured_token()
    return bool(token) and hmac.compare_digest(value or "", token)


def _is_admin(handler) -> bool:
    if not configured_token():
        return False
    query = parse_qs(urlparse(handler.path).query)
    if _token_ok((query.get("token") or [""])[0]):
        return True
    if _token_ok(handler.headers.get("X-Onboard-Token", "")):
        return True
    cookies = _parse_cookies(handler.headers.get("Cookie", ""))
    return _token_ok(cookies.get("onboard_token", ""))


_PENDING_BINDS: dict = {}
_PENDING_LOCK = threading.Lock()


def _qr_png_base64(url: str) -> str | None:
    """Render a URL as a base64 PNG QR code via segno, when installed."""
    try:
        import segno
    except ImportError:
        return None
    try:
        import io
        buf = io.BytesIO()
        segno.make_qr(url, error="m").save(buf, kind="png", scale=8, border=2)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def make_handler(bound_store: UserStore):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        store = bound_store

        def log_message(self, *args):
            pass

        def _respond(self, status, body, content_type, set_cookie=False):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if set_cookie:
                self.send_header("Set-Cookie", "onboard_token=" + configured_token() + "; HttpOnly; Path=/")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status, payload):
            self._respond(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

        def _body_json(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise AppError("请求体不是有效的 JSON。") from None
            return data if isinstance(data, dict) else {}

        def _merge(self, existing, data):
            merged = user_to_dict(existing)
            for key in ("intervals_api_key", "bot_token", "ilink_bot_id", "ilink_user_id", "base_url"):
                if data.get(key) not in (None, ""):
                    merged[key] = data[key]
            for key in ("timezone", "send_at", "report_mode"):
                if data.get(key) not in (None, ""):
                    merged[key] = data[key]
            model = data.get("model")
            if model is not None:
                if not isinstance(model, dict):
                    raise AppError("model 应为对象。")
                base = merged.get("model") or {}
                if not isinstance(base, dict):
                    base = {}
                new_model = {
                    "base_url": model.get("base_url") if model.get("base_url") not in (None, "") else base.get("base_url"),
                    "model": model.get("model") if model.get("model") not in (None, "") else base.get("model"),
                    "api_key": model.get("api_key") if model.get("api_key") not in (None, "") else base.get("api_key"),
                    "timeout": model.get("timeout", base.get("timeout", 120)),
                    "max_tokens": model.get("max_tokens", base.get("max_tokens", 2500)),
                    "allow_http": model.get("allow_http", base.get("allow_http", False)),
                }
                merged["model"] = new_model if (new_model["base_url"] or new_model["model"]) else None
            return merged

        def _require_manage_secret(self, athlete_id, data):
            if _is_admin(self):
                return
            secret = (data.get("manage_secret") or "").strip() if isinstance(data, dict) else ""
            if not self.store.check_manage_secret(athlete_id, secret):
                raise AppError("管理密钥错误或缺失；只有创建者可以修改该条目。")

        def do_GET(self):
            parsed = urlparse(self.path)
            path = app_path(parsed.path)
            if path == "/healthz":
                self._json(200, {"ok": True})
            elif path == "/api/users":
                if _is_admin(self):
                    self._json(200, {"users": [u.public_detail() for u in self.store.all()]})
                else:
                    self._json(401, {"status": "error", "message": "仅管理员可查看用户列表。"})
            elif path == "/admin":
                page = ADMIN_PAGE if _is_admin(self) else ADMIN_LOGIN
                self._respond(200, page.replace("__BASE__", web_base()), "text/html; charset=utf-8")
            elif path in ("/", ""):
                self._respond(200, PAGE.replace("__BASE__", web_base()), "text/html; charset=utf-8")
            else:
                self._json(404, {"status": "error", "message": "未找到"})

        def do_POST(self):
            parsed = urlparse(self.path)
            path = app_path(parsed.path)
            if path == "/login":
                try:
                    data = self._body_json()
                    if _token_ok(data.get("token", "")):
                        self._respond(200, json.dumps({"status": "ok"}), "application/json; charset=utf-8", set_cookie=True)
                    else:
                        self._json(401, {"status": "error", "message": "令牌错误"})
                except AppError as exc:
                    self._json(400, {"status": "error", "message": str(exc)})
                return
            try:
                if path == "/api/ilink/qr":
                    try:
                        qr = ILinkClient().get_qr_code()
                    except ILinkError as exc:
                        self._json(502, {"status": "error", "message": f"无法获取绑定二维码：{exc}"})
                        return
                    qrcode = qr["qrcode"]
                    with _PENDING_LOCK:
                        _PENDING_BINDS[qrcode] = {"account": None}
                    self._json(200, {
                        "qrcode": qrcode,
                        "qrcode_img_content": qr["qrcode_img_content"],
                        "qr_png": _qr_png_base64(qr["qrcode_img_content"]),
                    })
                    return
                if path == "/api/ilink/qr/status":
                    data = self._body_json()
                    qrcode = (data.get("qrcode") or "").strip()
                    if not qrcode:
                        self._json(400, {"status": "error", "message": "缺少 qrcode"})
                        return
                    try:
                        status = ILinkClient().get_qr_status(qrcode, timeout=8)
                    except ILinkError:
                        self._json(200, {"status": "wait"})
                        return
                    st = status.get("status")
                    if st == "confirmed":
                        try:
                            account = ILinkClient.account_from_status(status)
                        except ILinkError:
                            self._json(200, {"status": "wait"})
                            return
                        with _PENDING_LOCK:
                            _PENDING_BINDS[qrcode] = {"account": account}
                        self._json(200, {"status": "confirmed", "owner_id": account["owner_id"]})
                    else:
                        self._json(200, {"status": st})
                    return
                if path == "/api/user":
                    data = self._body_json()
                    qrcode = (data.get("qrcode") or "").strip()
                    if qrcode:
                        with _PENDING_LOCK:
                            entry = _PENDING_BINDS.get(qrcode)
                        account = entry.get("account") if entry else None
                        if not account:
                            raise AppError("微信绑定未完成，请先扫码确认。")
                        data = dict(data)
                        data["bot_token"] = account["token"]
                        data["ilink_bot_id"] = account["bot_id"]
                        data["ilink_user_id"] = account["owner_id"]
                        data["base_url"] = account["base_url"]
                    athlete = canonical_athlete(data.get("athlete_id", ""))
                    existing = self.store.get(athlete)
                    if existing:
                        self._require_manage_secret(athlete, data)
                        user = make_user(self._merge(existing, data))
                        self.store.upsert(user)
                    else:
                        secret = (data.get("manage_secret") or "").strip()
                        if len(secret) < 6:
                            raise AppError("首次开通必须设置至少 6 位管理密钥。")
                        user = make_user(data)
                        self.store.upsert(user)
                        self.store.set_manage_secret(athlete, secret)
                    self._json(200, {"status": "saved", "athlete_id": user.athlete_id})
                    return
                if path.startswith("/api/user/") and path.endswith("/toggle"):
                    athlete = canonical_athlete(unquote(path[len("/api/user/"):-len("/toggle")]))
                    existing = self.store.get(athlete)
                    if existing is None:
                        self._json(404, {"status": "error", "message": "未找到用户"})
                        return
                    self._require_manage_secret(athlete, self._body_json())
                    user = self.store.set_enabled(athlete, not existing.enabled)
                    self._json(200, {"status": "ok", "athlete_id": user.athlete_id, "enabled": user.enabled})
                    return
                if path.startswith("/api/user/") and path.endswith("/delete"):
                    athlete = canonical_athlete(unquote(path[len("/api/user/"):-len("/delete")]))
                    existing = self.store.get(athlete)
                    if existing is None:
                        self._json(200, {"status": "not_found"})
                        return
                    self._require_manage_secret(athlete, self._body_json())
                    self.store.remove(athlete)
                    self._json(200, {"status": "deleted"})
                    return
                if path.startswith("/api/user/") and path.endswith("/load"):
                    athlete = canonical_athlete(unquote(path[len("/api/user/"):-len("/load")]))
                    existing = self.store.get(athlete)
                    if existing is None:
                        self._json(404, {"status": "error", "message": "未找到该运动员，尚未开通。"})
                        return
                    self._require_manage_secret(athlete, self._body_json())
                    self._json(200, existing.public_detail())
                    return
                self._json(404, {"status": "error", "message": "未找到"})
            except AppError as exc:
                self._json(400, {"status": "error", "message": str(exc)})

    return Handler


def run(store: UserStore, host=None, port=None) -> None:
    host = host or os.getenv("WEB_HOST", "127.0.0.1")
    port = int(port or os.getenv("WEB_PORT", "8010"))
    server = ThreadingHTTPServer((host, port), make_handler(store))
    token = configured_token()
    print(f"自助开通网页：http://{host}:{port}" + ("；管理员页 /admin 需令牌" if token else "；未设置管理员令牌"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
