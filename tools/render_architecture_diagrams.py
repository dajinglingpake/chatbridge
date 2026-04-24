#!/usr/bin/env python3
"""Render ChatBridge architecture diagrams as static SVG assets.

The diagram is intentionally hand-laid out. Auto-layout tools made the README
diagram hard to read after Mermaid renderer changes, so this script keeps the
source editable while preserving stable spacing.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = ROOT / "docs" / "images"
FONT_CACHE_DIR = ROOT / ".runtime" / "cache" / "diagram_fonts"
WIDTH = 2700
HEIGHT = 2050


@dataclass(frozen=True)
class Region:
    key: str
    x: int
    y: int
    w: int
    h: int
    fill: str


@dataclass(frozen=True)
class Node:
    key: str
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    label: str
    dashed: bool = False
    via: tuple[tuple[int, int], ...] = ()


REGIONS = [
    Region("entry", 56, 162, 394, 1664, "#dbeafe"),
    Region("app", 515, 162, 550, 1664, "#dcfce7"),
    Region("backend", 1130, 162, 435, 760, "#fef3c7"),
    Region("process", 1635, 162, 435, 760, "#fee2e2"),
    Region("media", 1130, 1032, 435, 780, "#ffedd5"),
    Region("storage", 1635, 1032, 1025, 780, "#ede9fe"),
]

NODES = [
    Node("wechat", 107, 313, 300, 145),
    Node("browser", 107, 678, 300, 145),
    Node("mcp_client", 107, 1040, 300, 145),
    Node("bridge", 580, 283, 410, 150),
    Node("ui", 580, 575, 410, 145),
    Node("mcp_service", 580, 868, 410, 165),
    Node("app_service", 580, 1210, 410, 145),
    Node("hub", 580, 1500, 410, 145),
    Node("registry", 1206, 313, 300, 145),
    Node("codex_backend", 1206, 510, 300, 105),
    Node("claude_backend", 1206, 660, 300, 105),
    Node("opencode_backend", 1206, 806, 300, 105),
    Node("codex", 1710, 345, 300, 105),
    Node("claude", 1710, 552, 300, 105),
    Node("opencode", 1710, 759, 300, 105),
    Node("sender_worker", 1206, 1110, 300, 105),
    Node("media_sender", 1206, 1248, 300, 145),
    Node("cdn", 1206, 1448, 300, 145),
    Node("sendmessage", 1206, 1648, 300, 130),
    Node("config", 1675, 1150, 210, 105),
    Node("accounts", 1915, 1150, 210, 105),
    Node("runtime", 2155, 1150, 210, 105),
    Node("text_outbox", 2378, 1150, 235, 120),
    Node("exports", 1675, 1410, 210, 105),
    Node("sessions", 1915, 1410, 210, 105),
    Node("workspace", 2155, 1410, 210, 105),
    Node("codex_state", 2378, 1410, 235, 126),
]


TEXT = {
    "zh": {
        "title": "ChatBridge 技术架构图",
        "regions": {
            "entry": "外部入口",
            "app": "Python 应用层",
            "backend": "Agent 后端适配层",
            "process": "外部 CLI 子进程",
            "media": "媒体发送链路",
            "storage": "本地状态与配置",
        },
        "nodes": {
            "wechat": ("微信客户端", ["iLink Bot API"]),
            "browser": ("浏览器 / 桌面壳", ["NiceGUI"]),
            "mcp_client": ("MCP 客户端", ["JSON-RPC", "tools/call"]),
            "bridge": ("微信桥接服务", ["WeixinBridge", "消息轮询 / Slash 命令", "结果回传"]),
            "ui": ("Web 控制台", ["配置 / 状态", "服务操作"]),
            "mcp_service": ("MCP 服务", ["状态 / 会话 / 重启", "send_weixin_media"]),
            "app_service": ("应用服务", ["Hub / Bridge", "生命周期"]),
            "hub": ("Agent 中心", ["任务队列 / 会话", "Agent 调度"]),
            "registry": ("后端注册表", ["agent_backends/"]),
            "codex_backend": ("CodexBackend", []),
            "claude_backend": ("ClaudeBackend", []),
            "opencode_backend": ("OpenCodeBackend", []),
            "codex": ("codex", []),
            "claude": ("claude", []),
            "opencode": ("opencode", []),
            "media_sender": ("媒体发送器", ["路径校验", "getuploadurl", "加密上传"]),
            "sender_worker": ("发送线程", ["SenderWorker"]),
            "cdn": ("WeChat CDN", ["AES-128-ECB", "媒体上传"]),
            "sendmessage": ("发送消息", ["image_item", "file_item"]),
            "config": ("config/*.json", []),
            "accounts": ("accounts/*.json", []),
            "runtime": (".runtime/state", [".runtime/logs"]),
            "text_outbox": ("文本队列", ["outbox.jsonl"]),
            "exports": (".runtime/exports", []),
            "sessions": ("sessions/*.txt", []),
            "workspace": ("workspace/", []),
            "codex_state": ("Codex 状态", ["state.sqlite", "rollout log"]),
        },
        "edges": {
            "poll": "轮询/发送",
            "web": "网页请求",
            "tool": "工具调用",
            "ops": "运维",
            "task": "任务",
            "load": "加载",
            "proc": "子进程",
            "reuse": "复用发送",
            "media": "媒体发送",
            "enqueue": "文本入队",
            "consume": "消费发送",
            "textsend": "文本发送",
            "upload": "上传",
            "download": "下载参数",
            "image": "图片/附件",
            "state": "状态",
            "config": "配置",
            "account": "账号",
            "session": "会话",
            "workdir": "工作区",
            "context": "ctx%",
        },
    },
    "en": {
        "title": "ChatBridge Technical Architecture",
        "regions": {
            "entry": "External Entry",
            "app": "Python Application Layer",
            "backend": "Agent Backend Adapters",
            "process": "External CLI Processes",
            "media": "Media Sending Pipeline",
            "storage": "Local State and Config",
        },
        "nodes": {
            "wechat": ("WeChat Client", ["iLink Bot API"]),
            "browser": ("Browser / Desktop", ["NiceGUI"]),
            "mcp_client": ("MCP Client", ["JSON-RPC", "tools/call"]),
            "bridge": ("WeixinBridge", ["polling / slash commands", "replies"]),
            "ui": ("Web UI", ["config / status", "service operations"]),
            "mcp_service": ("MCP Service", ["status / sessions / restart", "send_weixin_media"]),
            "app_service": ("App Service", ["Hub / Bridge", "lifecycle"]),
            "hub": ("AgentHub", ["task queue / sessions", "agent dispatch"]),
            "registry": ("Backend Registry", ["agent_backends/"]),
            "codex_backend": ("CodexBackend", []),
            "claude_backend": ("ClaudeBackend", []),
            "opencode_backend": ("OpenCodeBackend", []),
            "codex": ("codex", []),
            "claude": ("claude", []),
            "opencode": ("opencode", []),
            "media_sender": ("Media Sender", ["path guard", "getuploadurl", "encrypted upload"]),
            "sender_worker": ("Sender Worker", ["text retry"]),
            "cdn": ("WeChat CDN", ["AES-128-ECB", "media upload"]),
            "sendmessage": ("sendmessage", ["image_item", "file_item"]),
            "config": ("config/*.json", []),
            "accounts": ("accounts/*.json", []),
            "runtime": (".runtime/state", [".runtime/logs"]),
            "text_outbox": ("Text Queue", ["outbox.jsonl"]),
            "exports": (".runtime/exports", []),
            "sessions": ("sessions/*.txt", []),
            "workspace": ("workspace/", []),
            "codex_state": ("Codex State", ["state.sqlite", "rollout log"]),
        },
        "edges": {
            "poll": "poll/send",
            "web": "web request",
            "tool": "tool call",
            "ops": "ops",
            "task": "task",
            "load": "load",
            "proc": "subprocess",
            "reuse": "reuse",
            "media": "media send",
            "enqueue": "enqueue",
            "consume": "consume",
            "textsend": "text send",
            "upload": "upload",
            "download": "download param",
            "image": "image/file",
            "state": "state",
            "config": "config",
            "account": "account",
            "session": "session",
            "workdir": "workdir",
            "context": "ctx%",
        },
    },
}


EDGES = [
    Edge("wechat", "bridge", "poll"),
    Edge("browser", "ui", "web"),
    Edge("mcp_client", "mcp_service", "tool"),
    Edge("ui", "app_service", "ops"),
    Edge("mcp_service", "app_service", "ops"),
    Edge("bridge", "hub", "task"),
    Edge("bridge", "text_outbox", "enqueue", dashed=True),
    Edge("text_outbox", "sender_worker", "consume"),
    Edge("sender_worker", "sendmessage", "textsend"),
    Edge("hub", "registry", "load"),
    Edge("registry", "codex_backend", "load"),
    Edge("registry", "claude_backend", "load"),
    Edge("registry", "opencode_backend", "load"),
    Edge("codex_backend", "codex", "proc"),
    Edge("claude_backend", "claude", "proc"),
    Edge("opencode_backend", "opencode", "proc"),
    Edge("bridge", "media_sender", "reuse", dashed=True),
    Edge("mcp_service", "media_sender", "media"),
    Edge("media_sender", "cdn", "upload"),
    Edge("cdn", "sendmessage", "download"),
    Edge("sendmessage", "wechat", "image", dashed=True),
    Edge("bridge", "runtime", "state", dashed=True),
    Edge("bridge", "config", "config", dashed=True),
    Edge("bridge", "accounts", "account", dashed=True),
    Edge("hub", "sessions", "session", dashed=True),
    Edge("hub", "runtime", "state", dashed=True),
    Edge("hub", "codex_state", "context", dashed=True),
    Edge("codex", "workspace", "workdir", dashed=True),
    Edge("claude", "workspace", "workdir", dashed=True),
    Edge("opencode", "workspace", "workdir", dashed=True),
]

EDGE_LABEL_OFFSETS = {
    ("bridge", "media_sender"): (-10, 44),
    ("mcp_service", "media_sender"): (-28, 22),
    ("bridge", "config"): (58, 68),
    ("bridge", "accounts"): (126, 88),
    ("bridge", "runtime"): (212, 108),
    ("hub", "runtime"): (150, -6),
    ("hub", "sessions"): (128, 22),
    ("codex", "workspace"): (130, -54),
    ("claude", "workspace"): (160, -8),
    ("opencode", "workspace"): (188, 40),
}

EDGE_LABEL_POSITIONS = {
    ("bridge", "media_sender"): (1090, 824),
    ("mcp_service", "media_sender"): (1128, 1152),
    ("bridge", "config"): (1350, 1060),
    ("bridge", "accounts"): (1640, 1132),
    ("bridge", "runtime"): (2070, 1150),
    ("hub", "runtime"): (1850, 1620),
    ("hub", "sessions"): (1835, 1728),
    ("codex", "workspace"): (2342, 920),
    ("claude", "workspace"): (2372, 1108),
    ("opencode", "workspace"): (2402, 1298),
}

EDGE_LABEL_TEXT_OVERRIDES = {
    ("ui", "app_service"): {"zh": "", "en": ""},
    ("mcp_service", "app_service"): {"zh": "", "en": ""},
    ("bridge", "text_outbox"): {"zh": "", "en": ""},
    ("text_outbox", "sender_worker"): {"zh": "", "en": ""},
    ("sender_worker", "sendmessage"): {"zh": "", "en": ""},
    ("hub", "codex_state"): {"zh": "", "en": ""},
    ("bridge", "config"): {"zh": "", "en": ""},
    ("bridge", "accounts"): {"zh": "", "en": ""},
    ("bridge", "runtime"): {"zh": "", "en": ""},
    ("hub", "runtime"): {"zh": "", "en": ""},
    ("hub", "sessions"): {"zh": "", "en": ""},
    ("codex", "workspace"): {"zh": "", "en": ""},
    ("claude", "workspace"): {"zh": "", "en": ""},
    ("opencode", "workspace"): {"zh": "", "en": ""},
    ("sendmessage", "wechat"): {"zh": "", "en": ""},
}


def esc(value: str) -> str:
    return html.escape(value, quote=True)


def node_center(node: Node) -> tuple[int, int]:
    return node.x + node.w // 2, node.y + node.h // 2


def edge_points(src: Node, dst: Node) -> list[tuple[int, int]]:
    sx, sy = node_center(src)
    dx, dy = node_center(dst)
    if abs(dx - sx) > abs(dy - sy):
        start = (src.x + src.w if dx > sx else src.x, sy)
        end = (dst.x if dx > sx else dst.x + dst.w, dy)
    else:
        start = (sx, src.y + src.h if dy > sy else src.y)
        end = (dx, dst.y if dy > sy else dst.y + dst.h)
    return [start, end]


def label_position(edge: Edge, points: list[tuple[int, int]]) -> tuple[int, int]:
    fixed = EDGE_LABEL_POSITIONS.get((edge.src, edge.dst))
    if fixed is not None:
        return fixed
    start = points[len(points) // 2 - 1]
    end = points[len(points) // 2]
    lx = (start[0] + end[0]) // 2
    ly = (start[1] + end[1]) // 2
    if abs(end[0] - start[0]) >= abs(end[1] - start[1]):
        lx, ly = lx, ly - 24
    else:
        lx, ly = lx + 20, ly
    dx, dy = EDGE_LABEL_OFFSETS.get((edge.src, edge.dst), (0, 0))
    return lx + dx, ly + dy


def _all_visible_text() -> str:
    parts: list[str] = []
    for lang in TEXT.values():
        parts.append(lang["title"])
        parts.extend(lang["regions"].values())
        parts.extend(lang["edges"].values())
        for override in EDGE_LABEL_TEXT_OVERRIDES.values():
            if override["zh"]:
                parts.append(override["zh"])
            if override["en"]:
                parts.append(override["en"])
        for title, lines in lang["nodes"].values():
            parts.append(title)
            parts.extend(lines)
    return "\n".join(parts)


def edge_label_text(edge: Edge, labels: dict[str, str]) -> str:
    lang = "zh" if labels is TEXT["zh"]["edges"] else "en"
    override = EDGE_LABEL_TEXT_OVERRIDES.get((edge.src, edge.dst))
    if override is not None:
        return override[lang]
    return labels[edge.label]


FONT_TEXT = _all_visible_text()


def _download_font_bytes(weight: int) -> bytes:
    query = urllib.parse.quote(FONT_TEXT)
    css_url = f"https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@{weight}&text={query}"
    request = urllib.request.Request(css_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        css = response.read().decode("utf-8")
    match = re.search(r"url\((https://[^)]+)\)", css)
    if not match:
        raise RuntimeError(f"Failed to resolve font download URL for weight {weight}")
    with urllib.request.urlopen(match.group(1), timeout=30) as response:
        return response.read()


def _font_cache_path(weight: int) -> Path:
    digest = hashlib.sha1(f"{weight}:{FONT_TEXT}".encode("utf-8")).hexdigest()[:12]
    return FONT_CACHE_DIR / f"noto-sans-sc-{weight}-{digest}.ttf"


@lru_cache(maxsize=4)
def _font_bytes(weight: int) -> bytes:
    cache_path = _font_cache_path(weight)
    if cache_path.exists():
        return cache_path.read_bytes()
    FONT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = _download_font_bytes(weight)
    cache_path.write_bytes(data)
    return data


@lru_cache(maxsize=8)
def _svg_font_face_css() -> str:
    css_parts = []
    for weight in (400, 500):
        font_data = base64.b64encode(_font_bytes(weight)).decode("ascii")
        css_parts.append(
            "@font-face { "
            "font-family: 'ChatBridge Diagram Sans'; "
            f"src: url(data:font/ttf;base64,{font_data}) format('truetype'); "
            f"font-weight: {weight}; "
            "font-style: normal; }"
        )
    return " ".join(css_parts)


def svg_text(x: int, y: int, value: str, size: int, *, anchor: str = "start", cls: str = "") -> str:
    class_attr = f' class="{cls}"' if cls else ""
    return f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}"{class_attr}>{esc(value)}</text>'


def draw_region(region: Region, labels: dict[str, str]) -> str:
    return "\n".join(
        [
            f'<rect class="region" x="{region.x}" y="{region.y}" width="{region.w}" height="{region.h}" '
            f'rx="30" fill="{region.fill}"/>',
            svg_text(region.x + 30, region.y + 66, labels[region.key], 36, cls="region-title"),
        ]
    )


def draw_node(node: Node, labels: dict[str, tuple[str, list[str]]]) -> str:
    title, lines = labels[node.key]
    parts = [
        f'<rect class="node" x="{node.x}" y="{node.y}" width="{node.w}" height="{node.h}" rx="22"/>',
        svg_text(node.x + 18, node.y + 47, title, 30, cls="node-title"),
        f'<line class="node-rule" x1="{node.x + 18}" y1="{node.y + 58}" x2="{node.x + node.w - 18}" y2="{node.y + 58}"/>',
    ]
    if lines:
        line_height = 26
        total = len(lines) * line_height
        start_y = node.y + 88 + max(0, (node.h - 118 - total) // 2)
        for idx, line in enumerate(lines):
            parts.append(svg_text(node.x + node.w // 2, start_y + idx * line_height, line, 22, anchor="middle", cls="node-line"))
    return "\n".join(parts)


def draw_edge(edge: Edge, nodes: dict[str, Node], labels: dict[str, str]) -> str:
    points = edge_points(nodes[edge.src], nodes[edge.dst])
    points_attr = " ".join(f"{x},{y}" for x, y in points)
    dash = ' stroke-dasharray="14 12"' if edge.dashed else ""
    label = edge_label_text(edge, labels)
    if not label:
        return f'<polyline class="edge" points="{points_attr}"{dash} marker-end="url(#arrow)"/>'
    lx, ly = label_position(edge, points)
    label_w = max(60, len(label) * 14 + 18)
    return "\n".join(
        [
            f'<polyline class="edge" points="{points_attr}"{dash} marker-end="url(#arrow)"/>',
            f'<rect class="edge-label-bg" x="{lx - label_w // 2}" y="{ly - 18}" width="{label_w}" height="32" rx="8"/>',
            svg_text(lx, ly + 5, label, 17, anchor="middle", cls="edge-label"),
        ]
    )


def render(lang: str) -> str:
    text = TEXT[lang]
    node_map = {node.key: node for node in NODES}
    font_face_css = _svg_font_face_css()
    body: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        "<defs>",
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">',
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#64748b"/>',
        "</marker>",
        "</defs>",
        "<style>",
        font_face_css,
        "svg { background: #f8fafc; font-family: 'ChatBridge Diagram Sans', 'Noto Sans', 'Segoe UI', Arial, sans-serif; }",
        ".title { fill: #0f172a; font-weight: 500; letter-spacing: 1px; }",
        ".region { stroke: #cbd5e1; stroke-width: 1.3; opacity: 0.82; }",
        ".region-title { fill: #0f172a; font-weight: 500; }",
        ".node { fill: #ffffff; stroke: #334155; stroke-width: 2.2; }",
        ".node-title { fill: #0f172a; font-weight: 500; }",
        ".node-line { fill: #475569; font-weight: 500; }",
        ".node-rule { stroke: #94a3b8; stroke-width: 1.4; }",
        ".edge { fill: none; stroke: #64748b; stroke-width: 3; opacity: 0.88; stroke-linecap: round; stroke-linejoin: round; }",
        ".edge-label-bg { fill: #f8fafc; stroke: #e2e8f0; stroke-width: 1; opacity: 0.96; }",
        ".edge-label { fill: #64748b; font-weight: 500; }",
        "</style>",
        svg_text(78, 118, text["title"], 64, cls="title"),
    ]
    body.extend(draw_region(region, text["regions"]) for region in REGIONS)
    body.extend(draw_edge(edge, node_map, text["edges"]) for edge in EDGES)
    body.extend(draw_node(node, text["nodes"]) for node in NODES)
    body.append("</svg>")
    return "\n".join(body)


def _has_pillow() -> bool:
    try:
        from PIL import Image, ImageDraw, ImageFont

        return bool(Image and ImageDraw and ImageFont)
    except ModuleNotFoundError:
        return False


def _ensure_png_runtime(output_format: str) -> None:
    if output_format not in {"png", "all"}:
        return
    if _has_pillow():
        return
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from ui_main import ensure_ui_dependencies

    ensure_ui_dependencies(Path(__file__).resolve())
    if not _has_pillow():
        raise RuntimeError("Pillow is still unavailable after the shared dependency bootstrap")


@lru_cache(maxsize=32)
def _pil_font(size: int, weight: int):
    from PIL import ImageFont

    return ImageFont.truetype(BytesIO(_font_bytes(weight)), size)


def _text_bbox(draw, text: str, font) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _fit_font(draw, texts: list[str], *, preferred_size: int, min_size: int, weight: int, max_width: int) -> tuple[object, int]:
    for size in range(preferred_size, min_size - 1, -1):
        font = _pil_font(size, weight)
        if all(_text_bbox(draw, text, font)[0] <= max_width for text in texts):
            return font, size
    return _pil_font(min_size, weight), min_size


def _fit_multiline_font(
    draw,
    texts: list[str],
    *,
    preferred_size: int,
    min_size: int,
    weight: int,
    max_width: int,
    max_height: int,
) -> tuple[object, list[int], int]:
    for size in range(preferred_size, min_size - 1, -1):
        font = _pil_font(size, weight)
        widths = [_text_bbox(draw, text, font)[0] for text in texts]
        if widths and max(widths) > max_width:
            continue
        line_heights = [_text_bbox(draw, text, font)[1] for text in texts]
        line_gap = 6 if size >= 20 else 4 if size >= 16 else 2
        total_height = sum(line_heights) + max(0, len(texts) - 1) * line_gap
        if total_height <= max_height:
            return font, line_heights, line_gap
    font = _pil_font(min_size, weight)
    line_heights = [_text_bbox(draw, text, font)[1] for text in texts]
    return font, line_heights, 2


def _draw_text(draw, x: float, y: float, text: str, *, font, fill: str, anchor: str = "start") -> None:
    width, height = _text_bbox(draw, text, font)
    x_pos = x
    if anchor == "middle":
        x_pos = x - width / 2
    elif anchor == "end":
        x_pos = x - width
    draw.text((x_pos, y), text, font=font, fill=fill)


def _rounded_rectangle(draw, box: tuple[int, int, int, int], *, radius: int, fill: str, outline: str, width: int) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _draw_arrow(draw, start: tuple[int, int], end: tuple[int, int], *, color: str, width: int, dashed: bool) -> None:
    from math import atan2, cos, hypot, sin

    sx, sy = start
    ex, ey = end
    arrow_len = 22
    arrow_half = 9
    total = hypot(ex - sx, ey - sy)
    if total <= arrow_len:
        return
    ux = (ex - sx) / total
    uy = (ey - sy) / total
    line_end = (ex - ux * arrow_len, ey - uy * arrow_len)
    if dashed:
        _draw_dashed_line(draw, start, line_end, color=color, width=width, dash=(14, 12))
    else:
        draw.line([start, line_end], fill=color, width=width)
    angle = atan2(ey - sy, ex - sx)
    left = (ex - arrow_len * cos(angle) + arrow_half * sin(angle), ey - arrow_len * sin(angle) - arrow_half * cos(angle))
    right = (ex - arrow_len * cos(angle) - arrow_half * sin(angle), ey - arrow_len * sin(angle) + arrow_half * cos(angle))
    draw.polygon([end, left, right], fill=color)


def _draw_dashed_line(draw, start: tuple[float, float], end: tuple[float, float], *, color: str, width: int, dash: tuple[int, int]) -> None:
    from math import hypot

    sx, sy = start
    ex, ey = end
    total = hypot(ex - sx, ey - sy)
    if total == 0:
        return
    ux = (ex - sx) / total
    uy = (ey - sy) / total
    drawn = 0.0
    dash_on, dash_off = dash
    while drawn < total:
        seg_start = drawn
        seg_end = min(total, drawn + dash_on)
        p1 = (sx + ux * seg_start, sy + uy * seg_start)
        p2 = (sx + ux * seg_end, sy + uy * seg_end)
        draw.line([p1, p2], fill=color, width=width)
        drawn += dash_on + dash_off


def _draw_region_png(draw, region: Region, labels: dict[str, str]) -> None:
    _rounded_rectangle(
        draw,
        (region.x, region.y, region.x + region.w, region.y + region.h),
        radius=30,
        fill=region.fill,
        outline="#cbd5e1",
        width=3,
    )
    _draw_text(draw, region.x + 30, region.y + 24, labels[region.key], font=_pil_font(36, 500), fill="#0f172a")


def _draw_node_png(draw, node: Node, labels: dict[str, tuple[str, list[str]]]) -> None:
    title, lines = labels[node.key]
    _rounded_rectangle(
        draw,
        (node.x, node.y, node.x + node.w, node.y + node.h),
        radius=22,
        fill="#ffffff",
        outline="#334155",
        width=3,
    )
    title_font, _ = _fit_font(
        draw,
        [title],
        preferred_size=30,
        min_size=24,
        weight=500,
        max_width=node.w - 36,
    )
    _draw_text(draw, node.x + 18, node.y + 18, title, font=title_font, fill="#0f172a")
    draw.line([(node.x + 18, node.y + 58), (node.x + node.w - 18, node.y + 58)], fill="#94a3b8", width=2)
    if not lines:
        return
    line_font, line_heights, line_gap = _fit_multiline_font(
        draw,
        lines,
        preferred_size=18 if len(lines) >= 3 else 22,
        min_size=14,
        weight=400,
        max_width=node.w - 32,
        max_height=node.h - 72,
    )
    total_height = sum(line_heights) + max(0, len(lines) - 1) * line_gap
    top = node.y + 66 + max(0, (node.h - 72 - total_height) // 2)
    current_y = top
    for index, line in enumerate(lines):
        _draw_text(
            draw,
            node.x + node.w / 2,
            current_y,
            line,
            font=line_font,
            fill="#475569",
            anchor="middle",
        )
        current_y += line_heights[index] + line_gap


def _draw_edge_png(draw, edge: Edge, nodes: dict[str, Node], labels: dict[str, str]) -> None:
    points = edge_points(nodes[edge.src], nodes[edge.dst])
    start, end = points[0], points[1]
    _draw_arrow(draw, start, end, color="#64748b", width=4, dashed=edge.dashed)
    label = edge_label_text(edge, labels)
    if not label:
        return
    lx, ly = label_position(edge, points)
    font = _pil_font(17, 500)
    text_w, text_h = _text_bbox(draw, label, font)
    pad_x = 10
    pad_y = 6
    box = (
        int(lx - text_w / 2 - pad_x),
        int(ly - text_h / 2 - pad_y),
        int(lx + text_w / 2 + pad_x),
        int(ly + text_h / 2 + pad_y),
    )
    _rounded_rectangle(
        draw,
        box,
        radius=8,
        fill="#f8fafc",
        outline="#e2e8f0",
        width=1,
    )
    _draw_text(draw, lx, ly - text_h / 2 - 1, label, font=font, fill="#64748b", anchor="middle")


def _render_png(lang: str, png_path: Path) -> None:
    from PIL import Image, ImageDraw

    text = TEXT[lang]
    node_map = {node.key: node for node in NODES}
    image = Image.new("RGBA", (WIDTH, HEIGHT), "#f8fafc")
    draw = ImageDraw.Draw(image)
    _draw_text(draw, 78, 38, text["title"], font=_pil_font(64, 500), fill="#0f172a")
    for region in REGIONS:
        _draw_region_png(draw, region, text["regions"])
    for edge in EDGES:
        _draw_edge_png(draw, edge, node_map, text["edges"])
    for node in NODES:
        _draw_node_png(draw, node, text["nodes"])
    image.save(png_path)


def write_diagrams(out_dir: Path, languages: Iterable[str], output_format: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for lang in languages:
        svg_target = out_dir / f"chatbridge-architecture-{lang}.svg"
        svg_target.write_text(render(lang), encoding="utf-8")
        if output_format in {"svg", "all"}:
            print(svg_target.relative_to(ROOT))
        if output_format in {"png", "all"}:
            png_target = out_dir / f"chatbridge-architecture-{lang}.png"
            _render_png(lang, png_target)
            print(png_target.relative_to(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render ChatBridge architecture SVG diagrams.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Output directory.")
    parser.add_argument("--lang", choices=["zh", "en", "all"], default="all", help="Language to render.")
    parser.add_argument("--format", choices=["svg", "png", "all"], default="svg", help="Output format.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _ensure_png_runtime(args.format)
    languages = ("zh", "en") if args.lang == "all" else (args.lang,)
    write_diagrams(args.out_dir, languages, args.format)


if __name__ == "__main__":
    main()
