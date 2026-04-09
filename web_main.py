from __future__ import annotations

import argparse
import sys

from ui_main import run_ui_entry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ChatBridge 旧 Web 兼容入口，推荐改用 ui_main.py 或 start-chatbridge-web.sh")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print("Legacy web wrapper detected. Redirecting to ui_main.py", file=sys.stderr)
    run_ui_entry(host=args.host, port=args.port, native=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
