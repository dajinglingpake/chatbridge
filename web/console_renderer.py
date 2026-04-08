from __future__ import annotations

import html
from pathlib import Path

from core.action_defs import TOPBAR_ACTIONS
from core.navigation import PRIMARY_PAGES
from core.shell_schema import APP_SHELL
from core.view_models import build_web_console_view_model
from localization import Localizer
from web.section_renderer import (
    render_diagnostics_section,
    render_home_sections,
    render_issue_section,
    render_session_section,
)


def _escape(value: object) -> str:
    return html.escape(str(value or ""))


def render_console_html(app_dir: Path, localizer: Localizer, last_message: str = "") -> str:
    model = build_web_console_view_model(app_dir, localizer.translate)
    flash = f"<div class='flash'>{_escape(last_message)}</div>" if last_message else ""
    nav_links = "".join(
        f"<a href='#{_escape(page.anchor)}'>{_escape(page.title)}</a>"
        for page in PRIMARY_PAGES
    )
    action_links = "".join(
        f"<a href='#{_escape(page.anchor)}'>{_escape(action.label)}</a>"
        for action, page in zip(TOPBAR_ACTIONS[2:], PRIMARY_PAGES[2:], strict=False)
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(APP_SHELL.app_name)} Web Console</title>
  <style>
    :root {{
      --bg: #f4efe7;
      --panel: #fffdf8;
      --ink: #1f2937;
      --muted: #6b7280;
      --line: #d6cbbd;
      --accent: #14532d;
      --accent-2: #9a3412;
      --danger: #991b1b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Noto Sans CJK SC", "PingFang SC", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, #efe2cf 0, transparent 28%),
        linear-gradient(180deg, #f7f2ea 0, var(--bg) 100%);
    }}
    main {{ max-width: 1280px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 8px; font-size: 32px; }}
    h2 {{ margin: 0; font-size: 26px; }}
    h3 {{ margin: 0 0 12px; font-size: 20px; }}
    p {{ color: var(--muted); }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 10;
      margin: 18px 0 20px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: rgba(255, 253, 248, 0.92);
      backdrop-filter: blur(10px);
    }}
    .topnav {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .topactions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 10px;
    }}
    .topnav a {{
      display: inline-flex;
      align-items: center;
      padding: 10px 14px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--ink);
      text-decoration: none;
      background: #fff8ef;
    }}
    .topnav a:hover {{
      background: #f2e6d5;
    }}
    .topactions a {{
      display: inline-flex;
      align-items: center;
      padding: 8px 12px;
      border-radius: 999px;
      text-decoration: none;
      background: #ede0cb;
      color: var(--ink);
    }}
    .topactions a:hover {{
      background: #e4d2b6;
    }}
    .flash {{
      margin: 16px 0;
      padding: 12px 14px;
      border: 1px solid var(--line);
      background: #fff7ed;
    }}
    .page-section {{
      margin-top: 22px;
      scroll-margin-top: 92px;
    }}
    .section-heading {{
      margin-bottom: 14px;
    }}
    .section-heading p {{
      margin: 6px 0 0;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 16px;
      margin-top: 18px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 18px;
      box-shadow: 0 10px 30px rgba(31, 41, 55, 0.05);
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 12px;
    }}
    button {{
      border: 0;
      border-radius: 10px;
      padding: 10px 14px;
      background: var(--accent);
      color: white;
      cursor: pointer;
    }}
    button.secondary {{ background: var(--accent-2); }}
    button.danger {{ background: var(--danger); }}
    form {{ margin: 0; }}
    label {{ display: block; margin: 12px 0 6px; font-weight: 600; }}
    input, select, textarea {{
      width: 100%;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: white;
      color: var(--ink);
    }}
    textarea {{ min-height: 140px; resize: vertical; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 12px;
      background: var(--panel);
    }}
    th, td {{
      padding: 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    .ok {{ color: var(--accent); font-weight: 700; }}
    .bad {{ color: var(--danger); font-weight: 700; }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: "JetBrains Mono", monospace;
    }}
    .muted {{ color: var(--muted); }}
  </style>
</head>
<body>
  <main>
    <h1>{_escape(APP_SHELL.app_name)} Web Console</h1>
    <p>{_escape(APP_SHELL.app_subtitle)}</p>
    {flash}
    <div class="topbar">
      <nav class="topnav" aria-label="页面导航">
        {nav_links}
      </nav>
      <div class="topactions" aria-label="快捷入口">
        <a href="#home">{_escape(TOPBAR_ACTIONS[0].label)}</a>
        <a href="#home">{_escape(TOPBAR_ACTIONS[1].label)}</a>
        {action_links}
      </div>
    </div>
    {render_home_sections(model)}
    {render_issue_section(model)}
    {render_session_section(model)}
    {render_diagnostics_section(model)}
  </main>
</body>
</html>"""
