from __future__ import annotations

import py_compile
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_DIRS = {
    ".git",
    ".runtime",
    ".venv",
    "__pycache__",
}


def iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def main() -> int:
    python_files = iter_python_files(ROOT)
    if not python_files:
        print("No Python files found.")
        return 0

    failures: list[tuple[Path, str]] = []
    for path in python_files:
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            failures.append((path, str(exc)))

    if failures:
        for path, error in failures:
            print(f"[FAIL] {path.relative_to(ROOT)}")
            print(error)
        return 1

    print(f"Verified {len(python_files)} Python files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
