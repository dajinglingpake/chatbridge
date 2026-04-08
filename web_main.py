from __future__ import annotations

import argparse

from ui.app import run_ui


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ChatBridge Web 模式")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_ui(host=args.host, port=args.port, native=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
