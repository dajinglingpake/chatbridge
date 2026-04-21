from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AcceptanceCheck:
    name: str
    command: list[str]


CHECKS = [
    AcceptanceCheck(
        name="语法校验",
        command=[sys.executable, "tools/verify_python_syntax.py"],
    ),
    AcceptanceCheck(
        name="核心单测",
        command=[
            sys.executable,
            "-m",
            "unittest",
            "-v",
            "tests.test_weixin_bridge_commands",
            "tests.test_management_agent",
            "tests.test_mcp_service",
        ],
    ),
    AcceptanceCheck(
        name="桥接命令 smoke",
        command=[sys.executable, "tools/smoke_weixin_bridge.py"],
    ),
    AcceptanceCheck(
        name="管理助手会话总览 smoke",
        command=[
            sys.executable,
            "tools/smoke_management_agent.py",
            "--seed-history",
            "--prompt",
            "列出所有会话",
            "--timeout",
            "180",
        ],
    ),
    AcceptanceCheck(
        name="管理助手状态总览 smoke",
        command=[
            sys.executable,
            "tools/smoke_management_agent.py",
            "--seed-history",
            "--prompt",
            "/status",
            "--timeout",
            "120",
        ],
    ),
    AcceptanceCheck(
        name="异步事件回执 smoke",
        command=[
            sys.executable,
            "tools/smoke_management_agent.py",
            "--seed-history",
            "--prompt",
            "/events 3",
            "--timeout",
            "180",
        ],
    ),
]


def _run_check(check: AcceptanceCheck) -> int:
    command = " ".join(check.command)
    print(f"[RUN] {check.name}")
    print(f"      {command}")
    completed = subprocess.run(check.command, cwd=ROOT_DIR)
    if completed.returncode == 0:
        print(f"[PASS] {check.name}")
    else:
        print(f"[FAIL] {check.name} (exit={completed.returncode})")
    print()
    return int(completed.returncode)


def main() -> int:
    failures: list[str] = []
    for check in CHECKS:
        if _run_check(check) != 0:
            failures.append(check.name)
            break

    print("Acceptance summary:")
    for check in CHECKS:
        marker = "FAILED" if check.name in failures else "PASSED"
        print(f"- {marker}: {check.name}")
        if failures and check.name == failures[-1]:
            break

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
