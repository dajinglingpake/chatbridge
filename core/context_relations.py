from __future__ import annotations

from typing import Callable


Translator = Callable[..., str]


def build_context_relation_lines(
    translate: Translator,
    *,
    agent_id: str,
    agent_backend: str,
    agent_model: str,
    agent_workdir: str,
    session_name: str,
    session_backend: str,
    session_model: str,
    session_workdir: str,
) -> list[str]:
    return [
        translate("bridge.context.title"),
        translate(
            "bridge.context.agent",
            agent=agent_id,
            backend=agent_backend,
            model=agent_model,
            workdir=agent_workdir,
        ),
        translate(
            "bridge.context.session",
            session=session_name,
            backend=session_backend,
            model=session_model,
            workdir=session_workdir,
        ),
        translate("bridge.context.rule.agent"),
        translate("bridge.context.rule.session"),
        translate("bridge.context.rule.backend"),
        translate("bridge.context.rule.model"),
        translate("bridge.context.rule.project"),
    ]
