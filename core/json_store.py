from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TypeVar


JsonValueT = TypeVar("JsonValueT")


def load_json(
    path: Path,
    default: JsonValueT,
    *,
    expect_type: type[object] | tuple[type[object], ...] | None = None,
) -> JsonValueT:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default
    if expect_type is not None and not isinstance(data, expect_type):
        return default
    return data


def save_json(path: Path, payload: object, *, ensure_ascii: bool = False, indent: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=ensure_ascii, indent=indent),
        encoding="utf-8",
    )
    temp_path.replace(path)
