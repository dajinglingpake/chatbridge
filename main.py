from __future__ import annotations

import argparse
import sys

from ui_main import run_ui_entry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ChatBridge 旧桌面兼容入口，推荐改用 ui_main.py 或 start-chatbridge-desktop.cmd")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--native", action="store_true", default=True, help="以本地壳模式启动统一 UI")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print("Legacy desktop wrapper detected. Redirecting to ui_main.py --native", file=sys.stderr)
    run_ui_entry(host=args.host, port=args.port, native=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
