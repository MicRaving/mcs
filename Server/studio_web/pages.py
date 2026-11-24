"""The gateway's own small pages (login, project list, "preparing")."""

from __future__ import annotations

import html

_STYLE = """
:root{color-scheme:light dark;--fg:#1d2330;--muted:#677085;--bg:#f5f6f8;--card:#fff;--accent:#3559d6}
@media (prefers-color-scheme:dark){:root{--fg:#e7eaf0;--muted:#98a1b3;--bg:#15181f;--card:#1e222b;--accent:#7f9bff}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;
background:var(--bg);color:var(--fg);display:grid;place-items:center;min-height:100vh}
main{background:var(--card);padding:28px 32px;border-radius:12px;width:min(380px,92vw);
box-shadow:0 2px 16px rgba(0,0,0,.08)}h1{font-size:1.25rem;margin:0 0 4px}p{color:var(--muted);margin:0 0 18px}
label{display:block;font-size:.85rem;color:var(--muted);margin:12px 0 4px}
input{width:100%;padding:9px 10px;border:1px solid #c5cad6;border-radius:8px;font:inherit;background:transparent;color:inherit}
button{margin-top:18px;width:100%;padding:10px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;cursor:pointer}
.err{color:#c0392b;margin-top:12px}a{color:var(--accent)}ul{list-style:none;padding:0;margin:0}
li a{display:block;padding:10px 12px;border-radius:8px;text-decoration:none}li a:hover{background:rgba(127,155,255,.12)}
.spin{width:28px;height:28px;border:3px solid rgba(127,127,127,.25);border-top-color:var(--accent);
border-radius:50%;animation:s 1s linear infinite;margin:6px 0 14px}@keyframes s{to{transform:rotate(1turn)}}
"""


def _page(title: str, body: str, head: str = "") -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{_STYLE}</style>{head}</head>"
            f"<body><main>{body}</main></body></html>")


def login_page(error: str = "", username: str = "") -> str:
    err = f"<div class=err>{html.escape(error)}</div>" if error else ""
    return _page("Coding Studio — sign in", f"""
<h1>Coding Studio</h1><p>Sign in with your project account.</p>
<form method=post action="/app/login">
<label for=u>Username</label><input id=u name=username autocomplete=username required value="{html.escape(username)}" autofocus>
<label for=p>Password</label><input id=p name=password type=password autocomplete=current-password required>
<button type=submit>Sign in</button>{err}</form>""")


def projects_page(user: dict, projects: list[dict]) -> str:
    name = html.escape(str(user.get("display_name") or user.get("username") or ""))
    if projects:
        items = "".join(
            f"<li><a href='/app/p/{html.escape(str(p.get('id')))}/'>{html.escape(str(p.get('name') or p.get('id')))}"
            f" <small style='color:var(--muted)'>({html.escape(str(p.get('role') or ''))})</small></a></li>"
            for p in projects)
        body = f"<ul>{items}</ul>"
    else:
        body = "<p>You are not a member of any project yet. Ask the project owner to add you.</p>"
    return _page("Coding Studio — projects", f"""
<h1>Projects</h1><p>Signed in as {name}.</p>{body}
<form method=post action="/app/logout"><button type=submit>Sign out</button></form>""")


def preparing_page(project_name: str = "") -> str:
    label = html.escape(project_name or "your project")
    return _page("Coding Studio — preparing", f"""
<h1>Preparing your workspace</h1><div class=spin></div>
<p>Opening {label}. The first start after a while takes a few seconds; this page reloads by itself.</p>""",
                 head="<meta http-equiv=refresh content=2>")
