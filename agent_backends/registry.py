from __future__ import annotations

import importlib
import inspect
import pkgutil

from agent_backends.base import AgentBackend

DEFAULT_BACKEND_KEY = "codex"


def build_backend_registry() -> dict[str, AgentBackend]:
    discovered: dict[str, AgentBackend] = {}
    package_name = __name__.rsplit(".", 1)[0]
    package = importlib.import_module(package_name)
    module_infos = sorted(pkgutil.iter_modules(package.__path__), key=lambda item: item.name)
    for module_info in module_infos:
        if not module_info.name.endswith("_backend"):
            continue
        module = importlib.import_module(f"{package_name}.{module_info.name}")
        for _, obj in sorted(inspect.getmembers(module, inspect.isclass), key=lambda item: item[0]):
            if obj.__module__ != module.__name__:
                continue
            key = getattr(obj, "key", "")
            invoke = getattr(obj, "invoke", None)
            if not isinstance(key, str) or not key.strip() or not callable(invoke):
                continue
            backend = obj()
            discovered[backend.key] = backend
    return discovered


def supported_backend_keys() -> tuple[str, ...]:
    return tuple(build_backend_registry().keys())


def supported_backend_options(include_default: bool = False) -> dict[str, str]:
    options = {key: key for key in supported_backend_keys()}
    if include_default:
        return {"": "跟随 Agent 默认配置", **options}
    return options
