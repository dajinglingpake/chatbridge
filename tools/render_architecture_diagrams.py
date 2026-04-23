#!/usr/bin/env python3
"""Render ChatBridge architecture diagrams as static SVG assets.

The diagram is intentionally hand-laid out. Auto-layout tools made the README
diagram hard to read after Mermaid renderer changes, so this script keeps the
source editable while preserving stable spacing.
"""

from __future__ import annotations

import argparse
import html
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = ROOT / "docs" / "images"
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
    Node("media_sender", 1206, 1155, 300, 155),
    Node("cdn", 1206, 1402, 300, 145),
    Node("sendmessage", 1206, 1620, 300, 155),
    Node("config", 1710, 1150, 285, 105),
    Node("accounts", 2058, 1150, 285, 105),
    Node("runtime", 2406, 1150, 220, 105),
    Node("exports", 1710, 1410, 285, 105),
    Node("sessions", 2058, 1410, 285, 105),
    Node("workspace", 2406, 1410, 220, 105),
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
            "cdn": ("WeChat CDN", ["AES-128-ECB", "媒体上传"]),
            "sendmessage": ("发送消息", ["image_item", "file_item"]),
            "config": ("config/*.json", []),
            "accounts": ("accounts/*.json", []),
            "runtime": (".runtime/state", [".runtime/logs"]),
            "exports": (".runtime/exports", []),
            "sessions": ("sessions/*.txt", []),
            "workspace": ("workspace/", []),
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
            "upload": "上传",
            "download": "下载参数",
            "image": "图片/附件",
            "state": "状态",
            "config": "配置",
            "account": "账号",
            "session": "会话",
            "workdir": "工作区",
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
            "cdn": ("WeChat CDN", ["AES-128-ECB", "media upload"]),
            "sendmessage": ("sendmessage", ["image_item", "file_item"]),
            "config": ("config/*.json", []),
            "accounts": ("accounts/*.json", []),
            "runtime": (".runtime/state", [".runtime/logs"]),
            "exports": (".runtime/exports", []),
            "sessions": ("sessions/*.txt", []),
            "workspace": ("workspace/", []),
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
            "upload": "upload",
            "download": "download param",
            "image": "image/file",
            "state": "state",
            "config": "config",
            "account": "account",
            "session": "session",
            "workdir": "workdir",
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
    Edge("codex", "workspace", "workdir", dashed=True),
    Edge("claude", "workspace", "workdir", dashed=True),
    Edge("opencode", "workspace", "workdir", dashed=True),
]


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


def label_position(points: list[tuple[int, int]]) -> tuple[int, int]:
    start = points[len(points) // 2 - 1]
    end = points[len(points) // 2]
    return (start[0] + end[0]) // 2, (start[1] + end[1]) // 2


def svg_text(x: int, y: int, value: str, size: int, *, anchor: str = "start", cls: str = "") -> str:
    class_attr = f' class="{cls}"' if cls else ""
    return f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}"{class_attr}>{esc(value)}</text>'


def draw_region(region: Region, labels: dict[str, str]) -> str:
    return "\n".join(
        [
            f'<rect class="region" x="{region.x}" y="{region.y}" width="{region.w}" height="{region.h}" '
            f'rx="30" fill="{region.fill}"/>',
            svg_text(region.x + 30, region.y + 64, labels[region.key], 34, cls="region-title"),
        ]
    )


def draw_node(node: Node, labels: dict[str, tuple[str, list[str]]]) -> str:
    title, lines = labels[node.key]
    parts = [
        f'<rect class="node" x="{node.x}" y="{node.y}" width="{node.w}" height="{node.h}" rx="22"/>',
        svg_text(node.x + 18, node.y + 45, title, 28, cls="node-title"),
        f'<line class="node-rule" x1="{node.x + 18}" y1="{node.y + 58}" x2="{node.x + node.w - 18}" y2="{node.y + 58}"/>',
    ]
    if lines:
        line_height = 28
        total = len(lines) * line_height
        start_y = node.y + 88 + max(0, (node.h - 118 - total) // 2)
        for idx, line in enumerate(lines):
            parts.append(svg_text(node.x + node.w // 2, start_y + idx * line_height, line, 20, anchor="middle", cls="node-line"))
    return "\n".join(parts)


def draw_edge(edge: Edge, nodes: dict[str, Node], labels: dict[str, str]) -> str:
    points = edge_points(nodes[edge.src], nodes[edge.dst])
    points_attr = " ".join(f"{x},{y}" for x, y in points)
    dash = ' stroke-dasharray="14 12"' if edge.dashed else ""
    label = labels[edge.label]
    lx, ly = label_position(points)
    label_w = max(54, len(label) * 13 + 18)
    return "\n".join(
        [
            f'<polyline class="edge" points="{points_attr}"{dash} marker-end="url(#arrow)"/>',
            f'<rect class="edge-label-bg" x="{lx - label_w // 2}" y="{ly - 18}" width="{label_w}" height="30" rx="8"/>',
            svg_text(lx, ly + 4, label, 16, anchor="middle", cls="edge-label"),
        ]
    )


def render(lang: str) -> str:
    text = TEXT[lang]
    node_map = {node.key: node for node in NODES}
    body: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        "<defs>",
        '<marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">',
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#64748b"/>',
        "</marker>",
        "</defs>",
        "<style>",
        "svg { background: #f8fafc; font-family: 'Noto Sans CJK SC', 'Noto Sans', 'Segoe UI', Arial, sans-serif; }",
        ".title { fill: #0f172a; font-weight: 500; letter-spacing: 1px; }",
        ".region { stroke: #cbd5e1; stroke-width: 1.3; opacity: 0.82; }",
        ".region-title { fill: #0f172a; font-weight: 500; }",
        ".node { fill: #ffffff; stroke: #334155; stroke-width: 2.2; }",
        ".node-title { fill: #0f172a; font-weight: 500; }",
        ".node-line { fill: #475569; }",
        ".node-rule { stroke: #94a3b8; stroke-width: 1.4; }",
        ".edge { fill: none; stroke: #64748b; stroke-width: 3; opacity: 0.88; }",
        ".edge-label-bg { fill: #f8fafc; stroke: #e2e8f0; stroke-width: 1; opacity: 0.96; }",
        ".edge-label { fill: #64748b; }",
        "</style>",
        svg_text(78, 118, text["title"], 64, cls="title"),
    ]
    body.extend(draw_region(region, text["regions"]) for region in REGIONS)
    body.extend(draw_edge(edge, node_map, text["edges"]) for edge in EDGES)
    body.extend(draw_node(node, text["nodes"]) for node in NODES)
    body.append("</svg>")
    return "\n".join(body)


def write_diagrams(out_dir: Path, languages: Iterable[str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for lang in languages:
        target = out_dir / f"chatbridge-architecture-{lang}.svg"
        target.write_text(render(lang), encoding="utf-8")
        print(target.relative_to(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render ChatBridge architecture SVG diagrams.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Output directory.")
    parser.add_argument("--lang", choices=["zh", "en", "all"], default="all", help="Language to render.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    languages = ("zh", "en") if args.lang == "all" else (args.lang,)
    write_diagrams(args.out_dir, languages)


if __name__ == "__main__":
    main()
