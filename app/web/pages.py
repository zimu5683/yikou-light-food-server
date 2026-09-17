"""网页版鉴权相关的内联页面：登录 / 注册申请 / 管理员审批。

为什么用 Python 内联 HTML 而不是 React 组件
--------------------------------------------
登录页必须在**未登录**状态下就能看到，也就是要先于 SPA 加载；放进前端产物会带来
「为了改一句提示语就要重新构建前端」的循环依赖。这些页面的职责极其单一（几个表单），
用内联 HTML 反而最稳：不依赖构建、不依赖 CDN、断网也能渲染。

安全注意：所有插值一律经 :func:`html.escape`，特别是用户名与邀请码 —— 它们来自
公网输入，直接拼进 HTML 就是存储型 XSS。
"""
from __future__ import annotations

import html
from typing import Any

#: 移动端优先的极简样式。访客多半用手机打开，所以按窄屏设计。
_BASE_CSS = """
*,*::before,*::after{box-sizing:border-box}
body{margin:0;padding:24px 16px 48px;background:#0b1020;color:#e8ecf7;
 font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC",sans-serif}
.wrap{max-width:420px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:15px;margin:28px 0 8px;color:#9fb0d0;font-weight:600}
p.sub{margin:0 0 20px;color:#8b9bbd;font-size:13px}
.card{background:#141a2e;border:1px solid #24304d;border-radius:12px;padding:18px;margin:0 0 16px}
label{display:block;font-size:13px;color:#9fb0d0;margin:12px 0 5px}
input{width:100%;padding:11px 12px;font-size:16px;background:#0b1020;color:#e8ecf7;
 border:1px solid #2b3853;border-radius:8px}
input:focus{outline:none;border-color:#4a7dff}
button{width:100%;margin-top:18px;padding:12px;font-size:16px;font-weight:600;color:#fff;
 background:#3b6bf5;border:0;border-radius:8px;cursor:pointer}
button:hover{background:#2f5be0}
button.ghost{background:transparent;border:1px solid #2b3853;color:#9fb0d0;font-weight:500}
button.ghost:hover{background:#1b2380;color:#e8ecf7}
.msg{padding:11px 13px;border-radius:8px;font-size:13px;margin:0 0 16px;word-break:break-word}
.msg.err{background:#3a1620;border:1px solid #7d2135;color:#ffc2cf}
.msg.ok{background:#123024;border:1px solid #1f6b45;color:#a9f0c8}
.msg.info{background:#152546;border:1px solid #2b4a86;color:#c2d6ff}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #24304d;vertical-align:middle}
th{color:#8b9bbd;font-weight:600;font-size:12px}
td.act{white-space:nowrap;text-align:right}
form.inline{display:inline}
form.inline button{width:auto;margin:0 0 0 5px;padding:6px 11px;font-size:12px;border-radius:6px}
code{background:#0b1020;border:1px solid #2b3853;border-radius:5px;padding:2px 6px;
 font-size:13px;word-break:break-all}
.tag{display:inline-block;padding:2px 7px;border-radius:99px;font-size:11px;border:1px solid}
.tag.pending{color:#ffd479;border-color:#7a5a12;background:#2c2208}
.tag.approved{color:#a9f0c8;border-color:#1f6b45;background:#123024}
.tag.rejected{color:#ffc2cf;border-color:#7d2135;background:#3a1620}
.empty{color:#6d7d9c;font-size:13px;padding:6px 0}
.nav{margin-top:26px;font-size:12px;color:#6d7d9c;text-align:center}
a{color:#7ea6ff}
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _page(title: str, body: str, *, wide: bool = False) -> str:
    """统一的 HTML 外壳。``wide`` 用于审批页（表格需要更宽的版心）。"""
    max_width = "820px" if wide else "420px"
    # 先算好样式再插值：f-string 表达式里不能出现反斜杠（3.12 以前）。
    style = _BASE_CSS.replace("max-width:420px", "max-width:" + max_width)
    head = (
        '<!doctype html><html lang="zh-CN"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        "<title>" + _e(title) + "</title><style>" + style + "</style></head>"
    )
    return head + '<body><div class="wrap">' + body + "</div></body></html>"


def _alert(message: str, kind: str = "err") -> str:
    return f"<div class=\"msg {_e(kind)}\">{_e(message)}</div>" if message else ""


def _status_tag(status: str, label: str) -> str:
    return f"<span class=\"tag {_e(status)}\">{_e(label)}</span>"


# ----------------------------------------------------------------------
# 登录 / 注册
# ----------------------------------------------------------------------
def login_page(*, error: str = "", notice: str = "", pending_count: int = 0,
               allow_register: bool = True, next_url: str = "/") -> str:
    """登录页。注册与登录放同一页，用 ``action`` 区分两个表单。"""
    register_block = ""
    if allow_register:
        register_block = f"""
<h2>没有账号？申请访问</h2>
<div class="card">
  <p class="sub" style="margin-bottom:4px">提交后需管理员批准才能登录。</p>
  <form method="post" action="/login">
    <input type="hidden" name="action" value="register">
    <input type="hidden" name="next" value="{_e(next_url)}">
    <label for="ru">想用的账号（邮箱或用户名）</label>
    <input id="ru" name="username" autocomplete="username" required>
    <label for="rp">设置密码（至少 6 位）</label>
    <input id="rp" name="password" type="password" autocomplete="new-password" required>
    <label for="rc">邀请码（有就填，可免审批）</label>
    <input id="rc" name="invite_code" autocomplete="off" placeholder="选填">
    <button type="submit">提交访问申请</button>
  </form>
</div>"""

    admin_hint = ""
    if pending_count:
        admin_hint = (f"<div class=\"msg info\">当前有 {int(pending_count)} 个申请等待管理员审批。</div>")

    body = f"""
<h1>一口轻食 · 需要登录</h1>
<p class="sub">本站仅对已批准的账号开放。</p>
{_alert(error, "err")}
{_alert(notice, "ok")}
{admin_hint}
<div class="card">
  <form method="post" action="/login">
    <input type="hidden" name="action" value="login">
    <input type="hidden" name="next" value="{_e(next_url)}">
    <label for="lu">账号</label>
    <input id="lu" name="username" autocomplete="username" autofocus required>
    <label for="lp">密码</label>
    <input id="lp" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">登录</button>
  </form>
</div>
{register_block}
<div class="nav">受保护的服务 · 未授权访问一律拒绝</div>"""
    return _page("登录 · 一口轻食", body)


def denied_page(*, title: str, message: str, username: str = "") -> str:
    """403/待审批等「已识别身份但无权访问」的提示页。"""
    body = f"""
<h1>{_e(title)}</h1>
{_alert(message, "err")}
<p class="sub">当前账号：<code>{_e(username)}</code></p>
<form method="post" action="/logout"><button class="ghost" type="submit">退出登录</button></form>
<div class="nav"><a href="/login">返回登录页</a></div>"""
    return _page(title, body)


# ----------------------------------------------------------------------
# 管理员审批页
# ----------------------------------------------------------------------
def _user_rows(users: list[dict[str, Any]], *, actor: str, actionable: bool) -> str:
    rows = []
    for user in users:
        username = str(user.get("username") or "")
        status = str(user.get("status") or "")
        label = str(user.get("status_label") or status)
        buttons = ""
        if actionable and username != actor:
            approve = (f"<form class=\"inline\" method=\"post\" action=\"/admin\">"
                       f"<input type=\"hidden\" name=\"action\" value=\"approve\">"
                       f"<input type=\"hidden\" name=\"username\" value=\"{_e(username)}\">"
                       f"<button type=\"submit\">同意</button></form>")
            reject = (f"<form class=\"inline\" method=\"post\" action=\"/admin\">"
                      f"<input type=\"hidden\" name=\"action\" value=\"reject\">"
                      f"<input type=\"hidden\" name=\"username\" value=\"{_e(username)}\">"
                      f"<button class=\"ghost\" type=\"submit\">拒绝</button></form>")
            buttons = approve + reject
        elif username == actor:
            buttons = "<span class=\"empty\">（你自己）</span>"
        rows.append(
            "<tr>"
            f"<td><code>{_e(username)}</code></td>"
            f"<td>{_status_tag(status, label)}</td>"
            f"<td>{_e(user.get('role') or '')}</td>"
            f"<td>{_e(user.get('note') or '')}</td>"
            f"<td class=\"act\">{buttons}</td>"
            "</tr>"
        )
    if not rows:
        return "<tr><td colspan=\"5\" class=\"empty\">暂无记录</td></tr>"
    return "".join(rows)


def _invite_rows(invites: list[dict[str, Any]]) -> str:
    rows = []
    for invite in invites:
        code = str(invite.get("code") or "")
        used = f"{int(invite.get('uses') or 0)}/{int(invite.get('max_uses') or 1)}"
        state = "已停用" if invite.get("revoked") else "有效"
        button = ""
        if not invite.get("revoked"):
            button = ("<form class=\"inline\" method=\"post\" action=\"/admin\">"
                      "<input type=\"hidden\" name=\"action\" value=\"revoke_invite\">"
                      f"<input type=\"hidden\" name=\"code\" value=\"{_e(code)}\">"
                      "<button class=\"ghost\" type=\"submit\">停用</button></form>")
        rows.append(
            "<tr>"
            f"<td><code>{_e(code)}</code></td>"
            f"<td>{_e(used)}</td><td>{_e(state)}</td>"
            f"<td>{_e(invite.get('note') or '')}</td>"
            f"<td class=\"act\">{button}</td>"
            "</tr>"
        )
    if not rows:
        return "<tr><td colspan=\"5\" class=\"empty\">还没有邀请码</td></tr>"
    return "".join(rows)


def admin_page(*, actor: str, pending: list[dict[str, Any]], users: list[dict[str, Any]],
               invites: list[dict[str, Any]], error: str = "", notice: str = "",
               access_email: str = "", access_enabled: bool = False) -> str:
    """管理员审批页：处理待审批申请、管理账号与邀请码。"""
    access_note = ""
    if access_enabled:
        access_note = (f"<div class=\"msg ok\">Cloudflare Access 已验证通过"
                       f"（{_e(access_email or '未知邮箱')}）</div>")
    else:
        access_note = ("<div class=\"msg info\">Cloudflare Access 尚未启用："
                       "当前仅由本站账号密码保护此页。</div>")

    body = f"""
<h1>访问审批</h1>
<p class="sub">当前管理员：<code>{_e(actor)}</code></p>
{_alert(error, "err")}
{_alert(notice, "ok")}
{access_note}

<h2>待审批申请（{len(pending)}）</h2>
<div class="card">
<table>
  <tr><th>账号</th><th>状态</th><th>角色</th><th>备注</th><th></th></tr>
  {_user_rows(pending, actor=actor, actionable=True)}
</table>
</div>

<h2>邀请码</h2>
<div class="card">
<form method="post" action="/admin">
  <input type="hidden" name="action" value="new_invite">
  <label for="mu">可用次数</label>
  <input id="mu" name="max_uses" type="number" min="1" value="1">
  <label for="mn">备注（给谁用）</label>
  <input id="mn" name="note" placeholder="选填">
  <button type="submit">生成邀请码</button>
</form>
<table style="margin-top:14px">
  <tr><th>邀请码</th><th>已用</th><th>状态</th><th>备注</th><th></th></tr>
  {_invite_rows(invites)}
</table>
</div>

<h2>全部账号（{len(users)}）</h2>
<div class="card">
<table>
  <tr><th>账号</th><th>状态</th><th>角色</th><th>备注</th><th></th></tr>
  {_user_rows(users, actor=actor, actionable=True)}
</table>
</div>

<h2>修改我的密码</h2>
<div class="card">
<form method="post" action="/admin">
  <input type="hidden" name="action" value="change_password">
  <label for="np">新密码（至少 6 位）</label>
  <input id="np" name="password" type="password" autocomplete="new-password" required>
  <button type="submit">更新密码</button>
</form>
</div>

<form method="post" action="/logout"><button class="ghost" type="submit">退出登录</button></form>
<div class="nav"><a href="/">进入应用</a></div>"""
    return _page("访问审批 · 一口轻食", body, wide=True)
