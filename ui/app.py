from __future__ import annotations

import copy
from collections.abc import Callable, MutableMapping
import json
import os
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
import uuid

from starlette.requests import Request
from starlette.responses import JSONResponse

from core.app_service import cancel_hub_task, delete_agent, reset_weixin_conversation, run_named_action, run_repair_command, save_agent, schedule_named_action, set_weixin_notice_enabled, submit_hub_task, switch_active_account, switch_bridge_agent, switch_weixin_session_backend, terminate_external_agent
from core.navigation import PRIMARY_PAGES
from core.shell_schema import APP_SHELL
from core.dashboard import refresh_dashboard_cache
from core.view_models import build_web_console_view_model
from localization import Localizer, normalize_language
from ui.mobile import MOBILE_UPLOAD_ROOT, build_mobile_access_url, build_mobile_qr_data_url, build_stream_sidebar_state_snapshot, build_stream_signature_snapshot, build_stream_state_snapshot, codex_thread_id_from_session_name, install_mobile_routes, is_mobile_access_authorized, load_codex_threads_page, stream_hub_state_file_signature, stream_qq_current_session_name
from ui.qr_login import install_qr_login_dialog
from ui.qq_login import install_qq_login_dialog
from ui.sections import STREAM_MANUAL_HISTORY_LIMIT, render_diagnostics_section, render_home_section, render_mobile_section, render_mobile_stream_composer_section, render_mobile_stream_messages_section, render_mobile_stream_shell, render_sessions_section


APP_DIR = Path(__file__).resolve().parent.parent
ASYNC_SERVICE_ACTIONS = {
    "restart",
    "restart-bridge",
    "restart-hub",
    "restart-onebot-runtime",
    "restart-qq-bridge",
    "restart-qq-stack",
}
STREAM_HISTORY_PAGE_SIZE = 20
STREAM_SIDEBAR_PAGE_SIZE = 40
WEB_THEME_OPTIONS = ("dark", "light", "forest")
STREAM_UI_LOG_PATH = APP_DIR / ".runtime" / "logs" / "ui_stream_refresh.jsonl"
STREAM_UI_DEBUG_LOG_ENABLED = os.environ.get("CHATBRIDGE_UI_DEBUG_LOG", "").strip().lower() in {"1", "true", "yes", "on"}
STREAM_UI_ALWAYS_LOG_EVENTS = {"refresh_timer_cancel", "client_disconnect"}
NICEGUI_RECONNECT_TIMEOUT_SECONDS = 30.0
NICEGUI_MESSAGE_HISTORY_LENGTH = 200
_CLIENT_STATE_STORAGE_KEY = "chatbridge_ui_state"


def _refresh_runtime_status_on_ui_entry() -> None:
    refresh_dashboard_cache(APP_DIR, "runtime")


class _ClientState(MutableMapping[str, object]):
    """Expose one UI state mapping for whichever NiceGUI client is active."""

    def __init__(
        self,
        defaults: dict[str, object],
        storage_provider: Callable[[], MutableMapping[str, object]],
    ) -> None:
        self._defaults = defaults
        self._storage_provider = storage_provider

    def _data(self) -> MutableMapping[str, object]:
        storage = self._storage_provider()
        state = storage.get(_CLIENT_STATE_STORAGE_KEY)
        if not isinstance(state, dict):
            state = copy.deepcopy(self._defaults)
            storage[_CLIENT_STATE_STORAGE_KEY] = state
        return state

    def __getitem__(self, key: str) -> object:
        return self._data()[key]

    def __setitem__(self, key: str, value: object) -> None:
        self._data()[key] = value

    def __delitem__(self, key: str) -> None:
        del self._data()[key]

    def __iter__(self):
        return iter(self._data())

    def __len__(self) -> int:
        return len(self._data())


def _stream_initial_history_limit(session_name: str) -> int:
    return STREAM_MANUAL_HISTORY_LIMIT


def _append_stream_ui_log(event: str, **fields: object) -> None:
    if not STREAM_UI_DEBUG_LOG_ENABLED and not (
        event in STREAM_UI_ALWAYS_LOG_EVENTS
        or event.startswith("browser_")
        or event.endswith("_error")
    ):
        return
    try:
        STREAM_UI_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
            **fields,
        }
        with STREAM_UI_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass

def normalize_web_theme(value: str) -> str:
    cleaned = str(value or "").strip().lower()
    return cleaned if cleaned in WEB_THEME_OPTIONS else "dark"

def _stream_time_sort_key(value: object) -> tuple[int, float, str]:
    text = str(value or "").strip()
    if not text:
        return (1, 0.0, "")
    normalized = text.replace("Z", "+00:00")
    try:
        return (0, datetime.fromisoformat(normalized).timestamp(), text)
    except ValueError:
        pass
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return (0, datetime.strptime(text, fmt).timestamp(), text)
        except ValueError:
            continue
    return (1, 0.0, text)

def _codex_thread_workspace_key(thread: dict[str, object]) -> tuple[str, str, str]:
    cwd = str(thread.get("cwd") or "").strip()
    project = str(thread.get("project") or "").strip()
    if not project and cwd:
        project = Path(cwd).name
    key = cwd or project or "__unknown__"
    return key, project, cwd

def _codex_thread_updated_key(thread: dict[str, object]) -> tuple[tuple[int, float, str], str]:
    return (
        _stream_time_sort_key(thread.get("updated_at")),
        str(thread.get("id") or "").strip(),
    )

def group_codex_threads_by_workspace(threads: list[object]) -> list[dict[str, object]]:
    groups: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for thread in threads:
        if not isinstance(thread, dict):
            continue
        thread_id = str(thread.get("id") or "").strip()
        session_name = str(thread.get("session_name") or "").strip()
        if not thread_id or not session_name:
            continue
        key, project, cwd = _codex_thread_workspace_key(thread)
        if key not in groups:
            groups[key] = {"key": key, "project": project, "cwd": cwd, "threads": []}
            order.append(key)
        group_threads = groups[key]["threads"]
        if isinstance(group_threads, list):
            group_threads.append(thread)
    result = [groups[key] for key in order]
    for group in result:
        group_threads = group.get("threads")
        if isinstance(group_threads, list):
            group_threads.sort(key=_codex_thread_updated_key, reverse=True)
    return sorted(
        result,
        key=lambda group: max(
            (_codex_thread_updated_key(thread) for thread in group.get("threads", []) if isinstance(thread, dict)),
            default=((1, 0.0, ""), ""),
        ),
        reverse=True,
    )


def _load_nicegui():
    try:
        from nicegui import ui
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise SystemExit(
            "Missing dependency: nicegui. "
            "Linux 请优先运行 `./start-chatbridge-web.sh` 自动安装依赖，"
            "或手动执行 `python3 -m pip install -r requirements.txt`。"
        ) from exc
    return ui


def create_ui(host: str = "0.0.0.0", port: int = 8765) -> None:
    ui = _load_nicegui()
    from nicegui import context
    default_language = Localizer().language

    def translate(key: str, **kwargs: object) -> str:
        language = str(state.get("language") or default_language)
        localizer = state.get("_localizer")
        if not isinstance(localizer, Localizer) or state.get("_localizer_language") != language:
            localizer = Localizer(language)
            state["_localizer"] = localizer
            state["_localizer_language"] = language
        return localizer.translate(key, **kwargs)

    def t(key: str, fallback: str = "", **kwargs: object) -> str:
        value = translate(key, **kwargs)
        return value if value != key else fallback.format(**kwargs)

    def page_label(page_key: str, fallback: str) -> str:
        return {
            "home": t("ui.tab.home", fallback),
            "sessions": t("ui.tab.sessions", fallback),
            "mobile": t("ui.tab.mobile", fallback),
            "stream": t("ui.tab.stream", fallback),
            "diagnostics": t("ui.tab.logs", fallback),
        }.get(page_key, fallback)

    ui.add_head_html(
        """
        <script>
        (() => {
            const allowedThemes = new Set(['dark', 'light', 'forest']);
            const readThemeCookie = () => {
                const item = document.cookie.split('; ').find((part) => part.startsWith('cb_theme='));
                return item ? decodeURIComponent(item.split('=').slice(1).join('=')) : '';
            };
            const storedTheme = localStorage.getItem('cb_theme') || readThemeCookie() || 'dark';
            const initialTheme = allowedThemes.has(storedTheme) ? storedTheme : 'dark';
            document.documentElement.dataset.cbTheme = initialTheme;
            document.cookie = `cb_theme=${encodeURIComponent(initialTheme)}; path=/; max-age=31536000; SameSite=Lax`;
            window.__cbApplyTheme = (theme) => {
                const selected = allowedThemes.has(theme) ? theme : 'dark';
                document.documentElement.dataset.cbTheme = selected;
                localStorage.setItem('cb_theme', selected);
                document.cookie = `cb_theme=${encodeURIComponent(selected)}; path=/; max-age=31536000; SameSite=Lax`;
            };
        })();
        (() => {
            if (window.__cbShellBootstrapInstalled === '1') return;
            window.__cbShellBootstrapInstalled = '1';
            const sendUiLog = (event, payload = {}) => {
                try {
                    const body = JSON.stringify({
                        event,
                        url: window.location.href,
                        ...payload,
                    });
                    if (navigator.sendBeacon) {
                        navigator.sendBeacon('/api/ui/stream-log', new Blob([body], { type: 'application/json' }));
                        return;
                    }
                    fetch('/api/ui/stream-log', {
                        method: 'POST',
                        headers: { 'content-type': 'application/json' },
                        body,
                        keepalive: true,
                    }).catch(() => {});
                } catch {}
            };
            window.__cbUiLog = sendUiLog;
            if (window.__cbUiErrorLogInstalled !== '1') {
                window.__cbUiErrorLogInstalled = '1';
                window.addEventListener('error', (event) => {
                    sendUiLog('browser_error', {
                        message: String(event.message || '').slice(0, 1200),
                        source: String(event.filename || '').slice(0, 400),
                        line: event.lineno || 0,
                        column: event.colno || 0,
                        stack: String(event.error?.stack || '').slice(0, 1200),
                    });
                });
                window.addEventListener('unhandledrejection', (event) => {
                    const reason = event.reason;
                    sendUiLog('browser_unhandledrejection', {
                        message: String(reason?.message || reason || '').slice(0, 1200),
                        stack: String(reason?.stack || '').slice(0, 1200),
                    });
                });
            }
            const decodeStreamKey = (value) => {
                try {
                    return decodeURIComponent(value || '');
                } catch {
                    return value || '';
                }
            };
            const restorePageFromHash = () => {
                const hashPage = (window.location.hash || '').replace(/^#/, '').trim();
                const pages = new Set(['home', 'sessions', 'mobile', 'stream', 'diagnostics']);
                if (!hashPage || !pages.has(hashPage) || window.location.search.includes('page=')) return;
                const url = new URL(window.location.href);
                url.searchParams.set('page', hashPage);
                window.location.replace(url.toString());
            };
            restorePageFromHash();
            const openStreamSession = (sessionName) => {
                const cleaned = (sessionName || '').trim();
                if (!cleaned) return;
                const url = new URL(window.location.href);
                url.pathname = url.pathname || '/';
                url.searchParams.delete('page');
                url.searchParams.set('session', cleaned);
                url.hash = 'stream';
                window.location.href = url.toString();
            };
            if (window.__cbSidebarStreamDelegateInstalled !== '1') {
                window.__cbSidebarStreamDelegateInstalled = '1';
                document.addEventListener('click', (event) => {
                    const link = event.target?.closest?.('[data-stream-session-link]');
                    if (link) {
                        if (link.matches('button, .q-btn') || link.closest('button, .q-btn') === link) {
                            return;
                        }
                        event.preventDefault();
                        event.stopPropagation();
                        openStreamSession(decodeStreamKey(link.getAttribute('data-stream-session-link') || ''));
                        return;
                    }
                }, true);
                document.addEventListener('click', (event) => {
                    const closeButton = event.target?.closest?.('[data-sidebar-close-action="1"]');
                    if (!closeButton) return;
                    event.preventDefault();
                    event.stopPropagation();
                    document.body.classList.remove('cb-sidebar-open');
                }, true);
                document.addEventListener('click', (event) => {
                    const summary = event.target?.closest?.('.cb-codex-workspace > summary');
                    if (!summary) return;
                    window.setTimeout(() => {
                        const details = summary.parentElement;
                        if (!details?.open) return;
                        const sidebar = details.closest('.cb-sidebar-shell');
                        if (!sidebar) return;
                        const sidebarRect = sidebar.getBoundingClientRect();
                        const detailsRect = details.getBoundingClientRect();
                        sidebar.scrollBy({
                            top: detailsRect.top - sidebarRect.top - 12,
                            behavior: 'smooth',
                        });
                    }, 0);
                }, true);
            }
        })();
        </script>
        <style>
        :root {
            --cb-bg: #111111;
            --cb-surface: #191919;
            --cb-surface-muted: #242424;
            --cb-surface-raised: #1f1f1f;
            --cb-border: #30302d;
            --cb-border-strong: #4a4a45;
            --cb-ink: #f5f5f0;
            --cb-muted: #a3a39b;
            --cb-accent: #2f6f5e;
            --cb-accent-bright: #3f8f72;
            --cb-accent-deep: #17382f;
            --cb-accent-soft: rgba(47, 111, 94, 0.16);
            --q-primary: #2f6f5e;
            --cb-info: #5f8fa3;
            --cb-info-soft: rgba(95, 143, 163, 0.13);
            --cb-ok: #3f8f72;
            --cb-ok-soft: rgba(63, 143, 114, 0.12);
            --cb-warn: #b7791f;
            --cb-warn-soft: rgba(183, 121, 31, 0.13);
            --cb-danger: #c85d68;
            --cb-danger-soft: rgba(200, 93, 104, 0.13);
            --cb-code-bg: #0c0c0b;
            --cb-code-ink: #f1f1ec;
            --cb-shadow: 0 1px 2px rgba(0, 0, 0, 0.30);
            --cb-radius: 8px;
        }
        :root[data-cb-theme="light"] {
            --cb-bg: #f7f7f4;
            --cb-surface: #ffffff;
            --cb-surface-muted: #efefeb;
            --cb-surface-raised: #fbfbf8;
            --cb-border: #ddddd6;
            --cb-border-strong: #b9b9ae;
            --cb-ink: #20201d;
            --cb-muted: #686861;
            --cb-accent: #2f6f5e;
            --cb-accent-bright: #25584b;
            --cb-accent-deep: #dce8e3;
            --cb-accent-soft: rgba(47, 111, 94, 0.10);
            --q-primary: #2f6f5e;
            --cb-info: #3d7891;
            --cb-info-soft: rgba(61, 120, 145, 0.10);
            --cb-ok: #2f7d63;
            --cb-ok-soft: rgba(47, 125, 99, 0.10);
            --cb-warn: #9a6418;
            --cb-warn-soft: rgba(154, 100, 24, 0.10);
            --cb-danger: #b94e5b;
            --cb-danger-soft: rgba(185, 78, 91, 0.10);
            --cb-code-bg: #eeeeea;
            --cb-code-ink: #242420;
            --cb-shadow: 0 1px 2px rgba(32, 32, 29, 0.10);
        }
        :root[data-cb-theme="forest"] {
            --cb-bg: #101311;
            --cb-surface: #181c19;
            --cb-surface-muted: #222823;
            --cb-surface-raised: #1d231f;
            --cb-border: #303832;
            --cb-border-strong: #4a554d;
            --cb-ink: #f2f5f1;
            --cb-muted: #a0aaa1;
            --cb-accent: #3f8f72;
            --cb-accent-bright: #58a082;
            --cb-accent-deep: #1a3c32;
            --cb-accent-soft: rgba(63, 143, 114, 0.13);
            --q-primary: #3f8f72;
            --cb-info: #78a0b3;
            --cb-info-soft: rgba(120, 160, 179, 0.12);
            --cb-ok: #59a783;
            --cb-ok-soft: rgba(89, 167, 131, 0.12);
            --cb-warn: #c3994a;
            --cb-warn-soft: rgba(195, 153, 74, 0.12);
            --cb-danger: #d27a82;
            --cb-danger-soft: rgba(210, 122, 130, 0.12);
            --cb-code-bg: #0b0f0c;
            --cb-code-ink: #eef3ef;
            --cb-shadow: 0 1px 2px rgba(0, 0, 0, 0.26);
        }
        body {
            --q-primary: var(--cb-accent) !important;
            background: var(--cb-bg);
            color: var(--cb-ink);
            font-size: 14px;
        }
        .nicegui-content {
            background: transparent !important;
            padding: 0 !important;
        }
        .cb-shell-main {
            min-height: 100vh;
        }
        .cb-shell-content {
            padding: 1rem;
        }
        .cb-shell-content-stream {
            height: 100vh;
            padding: 0;
            transition: width 160ms ease, max-width 160ms ease;
        }
        .cb-sidebar {
            background: var(--cb-surface);
            color: var(--cb-ink);
        }
        .cb-sidebar-shell {
            position: fixed;
            top: 0;
            right: 0;
            z-index: 2200;
            width: 360px;
            max-width: calc(100vw - 24px);
            height: 100vh;
            height: 100dvh;
            overflow: auto;
            border-left: 1px solid var(--cb-border);
            box-shadow: -16px 0 36px rgba(0, 0, 0, 0.36);
            transform: translateX(100%);
            transition: transform 160ms ease;
        }
        body.cb-sidebar-open {
            overflow: hidden;
        }
        .cb-sidebar-content {
            min-height: 100vh;
            height: auto;
            box-sizing: border-box;
            min-width: 0;
            overflow: visible;
        }
        body.cb-sidebar-open .cb-sidebar-shell {
            transform: translateX(0);
        }
        .cb-sidebar-backdrop {
            position: fixed;
            inset: 0;
            z-index: 2199;
            pointer-events: none;
            background: transparent;
            opacity: 0;
            transition: opacity 160ms ease;
        }
        body.cb-sidebar-open .cb-sidebar-backdrop {
            pointer-events: auto;
            background: rgba(0, 0, 0, 0.32);
            opacity: 1;
        }
        .cb-sidebar-toggle {
            position: relative;
            z-index: 2102;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.36);
        }
        html body .q-btn.cb-sidebar-toggle.bg-primary,
        html body .q-btn.cb-composer-send-button.bg-primary,
        html body .q-btn.cb-composer-send-button.cb-composer-send-ready {
            background: var(--cb-accent) !important;
            background-color: var(--cb-accent) !important;
            color: #ffffff !important;
        }
        html body .q-btn.cb-composer-send-button.cb-composer-send-ready .q-icon,
        html body .q-btn.cb-composer-send-button.cb-composer-send-ready .q-btn__content {
            color: #ffffff !important;
        }
        html body .q-btn.cb-sidebar-toggle.bg-primary:hover,
        html body .q-btn.cb-composer-send-button.bg-primary:hover,
        html body .q-btn.cb-composer-send-button.cb-composer-send-ready:hover {
            background: var(--cb-accent-bright) !important;
            background-color: var(--cb-accent-bright) !important;
        }
        .q-page-sticky:has(.cb-sidebar-toggle) {
            z-index: 2102 !important;
        }
        @media (min-width: 768px) {
            body.cb-sidebar-open .cb-shell-content-stream {
                width: calc(100vw - 360px) !important;
                max-width: calc(100vw - 360px) !important;
                margin-left: 0 !important;
                margin-right: auto !important;
            }
            body.cb-sidebar-open .cb-sidebar-backdrop {
                pointer-events: none;
                background: transparent;
                opacity: 0;
            }
            body.cb-sidebar-open .cb-sidebar-toggle {
                transform: translateX(-360px);
                pointer-events: none;
            }
            body.cb-sidebar-open .cb-scroll-bottom-button {
                left: calc((100vw - 360px) / 2);
            }
        }
        .cb-shell-nav {
            background: transparent;
            border-bottom: 0;
        }
        .cb-nav-button {
            justify-content: flex-start;
            width: 100%;
            min-width: 0;
            height: 2.5rem;
        }
        .cb-nav-button.q-btn--outline {
            border-color: var(--cb-border);
            color: var(--cb-muted);
            background: var(--cb-surface);
        }
        .cb-nav-button.cb-nav-active {
            background: var(--cb-accent) !important;
            color: #ffffff !important;
            border-color: var(--cb-accent) !important;
        }
        .cb-card,
        .cb-panel,
        .cb-code {
            border-radius: var(--cb-radius);
            border: 1px solid var(--cb-border);
            box-shadow: var(--cb-shadow);
        }
        .cb-card {
            background: var(--cb-surface);
        }
        .cb-panel {
            background: var(--cb-surface-raised);
        }
        .cb-hero {
            background: #111d1a;
            border-color: #253b33;
            color: #ffffff;
        }
        .cb-code {
            background: var(--cb-code-bg);
            color: var(--cb-code-ink);
            padding: 0.85rem 1rem;
            max-width: 100%;
            overflow: auto;
            overflow-wrap: anywhere;
            word-break: break-word;
            white-space: pre-wrap;
            font-size: 0.82rem;
            line-height: 1.5;
        }
        .cb-hero .cb-code {
            background: rgba(255, 255, 255, 0.08);
            border-color: rgba(255, 255, 255, 0.14);
            color: #d9f3e7;
            box-shadow: none;
        }
        .cb-hero .cb-panel {
            background: rgba(255, 255, 255, 0.07);
            border-color: rgba(255, 255, 255, 0.14);
            color: #eefcf4;
            box-shadow: none;
        }
        .cb-section-title {
            font-size: 1rem;
            font-weight: 700;
            color: var(--cb-ink);
        }
        .cb-kicker {
            text-transform: uppercase;
            letter-spacing: 0;
            font-size: 0.75rem;
            font-weight: 700;
            color: var(--cb-accent);
        }
        .cb-muted {
            color: var(--cb-muted);
        }
        .cb-ink {
            color: var(--cb-ink);
        }
        .cb-chip {
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            flex-shrink: 0;
            min-width: 0;
            max-width: 100%;
            padding: 0.25rem 0.55rem;
            border-radius: 999px;
            background: var(--cb-accent-soft);
            color: var(--cb-accent-bright);
            font-size: 0.78rem;
            font-weight: 600;
            overflow-wrap: anywhere;
            white-space: nowrap;
        }
        .cb-chip-ok {
            background: var(--cb-ok-soft);
            color: var(--cb-ok);
        }
        .cb-chip-warn {
            background: var(--cb-warn-soft);
            color: var(--cb-warn);
        }
        .cb-chip-danger {
            background: var(--cb-danger-soft);
            color: var(--cb-danger);
        }
        .cb-table {
            overflow: auto;
        }
        .cb-status-panel {
            border-radius: var(--cb-radius);
            padding: 0.9rem 1rem;
            border: 1px solid var(--cb-border);
        }
        .cb-status-running {
            background: var(--cb-ok-soft);
            border-color: var(--cb-ok);
        }
        .cb-status-partial {
            background: var(--cb-warn-soft);
            border-color: var(--cb-warn);
        }
        .cb-status-stopped {
            background: var(--cb-danger-soft);
            border-color: var(--cb-danger);
        }
        .cb-stat-value {
            font-size: 1.45rem;
            font-weight: 800;
            line-height: 1;
            color: var(--cb-ink);
        }
        .cb-stat-label {
            font-size: 0.82rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--cb-muted);
        }
        .q-card {
            border-radius: var(--cb-radius);
        }
        .q-btn {
            border-radius: 6px;
            text-transform: none;
            font-weight: 700;
            letter-spacing: 0;
            min-height: 2.25rem;
        }
        .q-btn.bg-primary {
            background: var(--cb-accent) !important;
        }
        html body .q-btn.bg-primary,
        html body .q-btn.bg-primary.text-white,
        html body .q-btn.bg-primary .q-icon,
        html body .q-btn.bg-primary .q-btn__content {
            color: #ffffff !important;
        }
        html body .q-btn.bg-primary .cb-muted {
            color: rgba(255, 255, 255, 0.78) !important;
        }
        html body .q-btn.bg-primary .cb-chip {
            background: rgba(255, 255, 255, 0.16);
            color: #ffffff;
        }
        html body .q-btn.q-btn--outline {
            border-color: var(--cb-border-strong) !important;
            color: var(--cb-ink) !important;
            background: var(--cb-surface) !important;
            background-color: var(--cb-surface) !important;
        }
        html body .q-btn.q-btn--outline .q-icon,
        html body .q-btn.q-btn--outline .q-btn__content,
        html body .q-btn.q-btn--flat,
        html body .q-btn.q-btn--flat .q-icon,
        html body .q-btn.q-btn--flat .q-btn__content {
            color: inherit !important;
        }
        html body .q-btn.q-btn--flat {
            color: var(--cb-ink) !important;
        }
        html body .q-btn.q-btn--outline.text-primary,
        html body .q-btn.q-btn--outline.text-primary .q-icon,
        html body .q-btn.q-btn--outline.text-primary .q-btn__content,
        html body .cb-nav-button.q-btn--outline,
        html body .cb-nav-button.q-btn--outline .q-icon,
        html body .cb-nav-button.q-btn--outline .q-btn__content {
            color: var(--cb-ink) !important;
        }
        html body .q-btn.q-btn--outline:hover,
        html body .q-btn.q-btn--flat:hover {
            background: var(--cb-surface-muted) !important;
        }
        .q-field__control,
        .q-field--outlined .q-field__control {
            border-radius: 6px !important;
            background: var(--cb-surface);
        }
        html body .q-field__native,
        html body .q-field__input,
        html body .q-field__label,
        html body .q-field__append,
        html body .q-field__prepend {
            color: var(--cb-ink) !important;
        }
        html body .q-field__label,
        html body .q-field__marginal,
        html body .q-placeholder,
        html body .q-field__native::placeholder,
        html body .q-field__input::placeholder {
            color: var(--cb-muted) !important;
            opacity: 1 !important;
        }
        html body .q-menu,
        html body .q-menu .q-list {
            background: var(--cb-surface-raised) !important;
            color: var(--cb-ink) !important;
        }
        html body .q-menu .q-item {
            color: var(--cb-ink) !important;
        }
        html body .q-menu .q-item--active,
        html body .q-menu .q-item.q-manual-focusable--focused {
            background: var(--cb-accent) !important;
            color: #ffffff !important;
        }
        .nicegui-markdown,
        .nicegui-markdown .codehilite,
        .nicegui-markdown pre {
            max-width: 100%;
            box-sizing: border-box;
        }
        .nicegui-markdown .codehilite,
        .nicegui-markdown pre {
            overflow-x: auto;
        }
        .nicegui-markdown code {
            overflow-wrap: anywhere;
        }
        .q-table {
            border-radius: 6px;
            overflow: hidden;
        }
        .q-table,
        .q-table__container,
        .q-table__middle,
        .q-table th,
        .q-table td {
            background: var(--cb-surface);
            color: var(--cb-ink);
        }
        .q-table.cb-table,
        .cb-table .q-table__middle {
            max-width: 100%;
            overflow: auto;
        }
        .q-table thead tr {
            background: var(--cb-surface-muted);
        }
        .q-table tbody tr:nth-child(even) {
            background: rgba(255, 255, 255, 0.03);
        }
        .q-tab-panels {
            border: 0 !important;
        }
        .cb-page-title {
            font-size: 1.35rem;
            line-height: 1.2;
            font-weight: 800;
        }
        .cb-toolbar-button {
            height: 2.25rem;
        }
        .cb-sidebar-section-title {
            font-size: 0.78rem;
            font-weight: 800;
            text-transform: uppercase;
            color: var(--cb-muted);
        }
        .cb-codex-workspace {
            border: 1px solid var(--cb-border);
            border-radius: var(--cb-radius);
            background: var(--cb-surface-raised);
            overflow: visible;
            min-width: 0;
        }
        .cb-codex-workspace * {
            min-width: 0;
        }
        .cb-codex-workspace > summary {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 0.7rem;
            cursor: pointer;
            list-style: none;
            padding: 0.75rem;
        }
        .cb-codex-workspace > summary::-webkit-details-marker {
            display: none;
        }
        .cb-codex-workspace > summary::after {
            content: "expand_more";
            flex: 0 0 auto;
            font-family: "Material Icons";
            font-size: 1.25rem;
            line-height: 1.25rem;
            color: var(--cb-muted);
        }
        .cb-codex-workspace[open] > summary::after {
            content: "expand_less";
        }
        .cb-codex-workspace:not([open]) .cb-codex-workspace-body {
            display: none;
        }
        .cb-codex-workspace-body {
            box-sizing: border-box;
            width: 100%;
            max-width: 100%;
            padding: 0 0.75rem 0.75rem;
            overflow-x: hidden;
        }
        .cb-stream-task-button {
            justify-content: flex-start;
            text-transform: none;
            min-height: 4.75rem;
            padding: 0.75rem;
        }
        .cb-stream-task-button .q-btn__content {
            width: 100%;
            align-items: stretch;
            gap: 0.5rem;
        }
        .cb-stream-task-button .q-btn__content,
        .cb-stream-task-button .q-btn__content * {
            pointer-events: none;
        }
        .cb-sidebar .cb-codex-workspace .cb-chip {
            flex-shrink: 1;
            white-space: normal;
            text-align: left;
        }
        .cb-agent-panel {
            height: 100vh;
            height: 100dvh;
            min-height: 0;
            display: flex;
            flex-direction: column;
            background: var(--cb-bg);
            position: relative;
        }
        .cb-agent-panel[data-stream-pending="1"] .cb-agent-stream-content,
        .cb-agent-stream[data-stream-pending="1"] .cb-agent-stream-content {
            visibility: hidden;
        }
        .cb-agent-stream[data-stream-pending="1"]::before {
            content: "";
            position: absolute;
            z-index: 3;
            left: 50%;
            top: calc((100dvh - var(--cb-composer-height, 5.5rem)) / 2);
            width: 1.75rem;
            height: 1.75rem;
            margin: -0.875rem 0 0 -0.875rem;
            border: 2px solid var(--cb-border);
            border-top-color: var(--cb-accent-bright);
            border-radius: 999px;
            animation: cb-stream-pending-spin 800ms linear infinite;
            pointer-events: none;
        }
        .cb-agent-stream {
            flex: 1;
            min-height: 0;
            overflow: auto;
            padding: 1rem;
            position: relative;
            overscroll-behavior-y: contain;
        }
        @media (any-hover: hover) and (any-pointer: fine) {
            .cb-agent-stream {
                overflow-y: auto !important;
                scrollbar-gutter: auto;
                scrollbar-width: auto;
                scrollbar-color: #c7d0d9 #1c2228;
            }
            .cb-agent-stream::-webkit-scrollbar {
                display: block;
                width: 1rem;
                background: #1c2228;
            }
            .cb-agent-stream::-webkit-scrollbar-track {
                background: #1c2228;
                box-shadow: inset 1px 0 0 #39424c;
            }
            .cb-agent-stream::-webkit-scrollbar-thumb {
                min-height: 3.5rem;
                border: 0.25rem solid #1c2228;
                border-radius: 0.5rem;
                background: #c7d0d9;
                box-shadow: inset 0 0 0 1px #eef2f6;
            }
            .cb-agent-stream::-webkit-scrollbar-thumb:hover {
                background: #f1f5f9;
            }
        }
        .cb-agent-stream-content {
            display: flex;
            flex-direction: column;
            width: 100%;
            max-width: 51.25rem;
            min-height: 100%;
            justify-content: flex-end;
            margin: 0 auto;
            padding: 0 0.5rem;
        }
        @keyframes cb-stream-pending-spin {
            to {
                transform: rotate(360deg);
            }
        }
        .cb-agent-titlebar {
            min-height: 3rem;
            background: var(--cb-bg);
        }
        .cb-stream-turn {
            width: 100%;
            margin-bottom: 1rem;
        }
        .cb-stream-turn-with-footer {
            margin-bottom: 0;
        }
        .cb-stream-load-older-wrap {
            display: flex;
            justify-content: center;
            width: 100%;
            padding: 0.25rem 0 1.25rem;
        }
        .cb-stream-load-older-button {
            color: var(--cb-muted);
            font-size: 0.82rem;
        }
        .cb-stream-empty-state {
            width: 100%;
            max-width: 51.25rem;
            min-height: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 3rem 0.5rem;
        }
        .cb-stream-empty-text {
            color: var(--cb-muted);
            font-size: 0.875rem;
            line-height: 20px;
            text-align: center;
        }
        .cb-stream-message {
            width: 100%;
            overflow-wrap: anywhere;
        }
        .cb-stream-message + .cb-stream-message {
            margin-top: 1rem;
        }
        .cb-stream-body {
            min-width: 0;
            white-space: pre-wrap;
            font-size: 1rem;
            line-height: 22px;
            color: var(--cb-ink);
        }
        .cb-stream-markdown {
            white-space: normal;
            font-size: 1rem;
            line-height: 22px;
        }
        .cb-stream-markdown > *:first-child {
            margin-top: 0;
        }
        .cb-stream-markdown > *:last-child {
            margin-bottom: 0;
        }
        .cb-stream-markdown p {
            margin: 0 0 0.75rem;
        }
        .cb-stream-markdown strong {
            font-weight: 500;
        }
        .cb-stream-markdown s,
        .cb-stream-markdown del {
            color: var(--cb-muted);
            text-decoration-line: line-through;
        }
        .cb-stream-markdown h1,
        .cb-stream-markdown h2,
        .cb-stream-markdown h3,
        .cb-stream-markdown h4,
        .cb-stream-markdown h5 {
            color: var(--cb-ink);
        }
        .cb-stream-markdown h1 {
            margin: 1.5rem 0 0.75rem;
            padding-bottom: 0.5rem;
            border-bottom: 1px solid var(--cb-border);
            font-size: 26px;
            font-weight: bold;
            line-height: 32px;
        }
        .cb-stream-markdown h2 {
            margin: 1.5rem 0 0.75rem;
            padding-bottom: 0.5rem;
            border-bottom: 1px solid var(--cb-border);
            font-size: 22px;
            font-weight: bold;
            line-height: 28px;
        }
        .cb-stream-markdown h3 {
            margin: 1rem 0 0.5rem;
            font-size: 20px;
            font-weight: 600;
            line-height: 26px;
        }
        .cb-stream-markdown h4 {
            margin: 1rem 0 0.5rem;
            font-size: 18px;
            font-weight: 600;
            line-height: 24px;
        }
        .cb-stream-markdown h5 {
            margin: 0.75rem 0 0.25rem;
            font-size: 16px;
            font-weight: 600;
            line-height: 22px;
        }
        .cb-stream-markdown h6 {
            margin: 0.75rem 0 0.25rem;
            color: var(--cb-muted);
            font-size: 16px;
            font-weight: 600;
            line-height: 20px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .cb-stream-markdown hr {
            height: 1px;
            margin: 1.5rem 0;
            border: 0;
            background: var(--cb-border);
        }
        .cb-stream-markdown ul,
        .cb-stream-markdown ol {
            margin: 0.25rem 0 1rem;
            padding-left: 1.25rem;
            list-style-position: outside;
        }
        .cb-stream-markdown ul ul,
        .cb-stream-markdown ol ol,
        .cb-stream-markdown ul ol,
        .cb-stream-markdown ol ul {
            margin: 0.25rem 0;
        }
        .cb-stream-markdown li {
            margin: 0 0 0.25rem;
            padding-left: 0.1rem;
        }
        .cb-stream-markdown pre {
            position: relative;
            margin: 0.75rem 0;
            border: 1px solid var(--cb-border);
            border-radius: 6px;
            background: var(--cb-code-bg);
            color: var(--cb-code-ink);
            padding: 0.75rem 2.75rem 0.75rem 0.75rem;
            overflow: auto;
            line-height: 17px;
            font-size: 12px;
            white-space: pre;
        }
        .cb-stream-markdown code {
            border-radius: 6px;
            background: var(--cb-surface-muted);
            color: var(--cb-ink);
            padding: 0.125rem 0.25rem;
            font-size: 12px;
            line-height: 17px;
        }
        .cb-stream-markdown pre code {
            background: transparent;
            padding: 0;
            color: inherit;
            font-size: inherit;
        }
        .cb-stream-code-copy-button {
            position: absolute;
            top: 0.5rem;
            right: 0.5rem;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 1.75rem;
            height: 1.75rem;
            border: 0;
            border-radius: 6px;
            background: transparent;
            color: var(--cb-muted);
            cursor: pointer;
            font-family: "Material Icons";
            font-size: 1rem;
            line-height: 1;
            opacity: 0;
            transition: opacity 120ms ease, background 120ms ease, color 120ms ease;
        }
        .cb-stream-markdown pre:hover .cb-stream-code-copy-button,
        .cb-stream-code-copy-button:focus-visible,
        .cb-stream-code-copy-button-copied {
            opacity: 1;
        }
        .cb-stream-code-copy-button:hover,
        .cb-stream-code-copy-button:focus-visible {
            background: var(--cb-surface-muted);
            color: var(--cb-ink);
        }
        .cb-stream-code-copy-button-copied {
            color: var(--cb-accent-bright);
        }
        .cb-stream-markdown blockquote {
            margin: 0.75rem 0;
            border-left: 4px solid var(--cb-accent);
            border-radius: 6px;
            background: var(--cb-surface-raised);
            padding: 0.75rem 1rem;
            color: var(--cb-ink);
        }
        .cb-stream-markdown a {
            color: var(--cb-accent-bright);
            text-decoration: none;
            overflow-wrap: anywhere;
        }
        .cb-stream-markdown a[href^="#chatbridge-file="],
        .cb-stream-markdown a.cb-stream-file-link {
            cursor: pointer;
            text-decoration-style: dotted;
        }
        .cb-stream-markdown a[href^="#chatbridge-file="] code,
        .cb-stream-markdown a.cb-stream-file-link code {
            color: var(--cb-accent-bright);
            background: var(--cb-accent-soft);
        }
        .cb-stream-markdown a.cb-stream-file-link-copied,
        .cb-stream-markdown a.cb-stream-file-link-copied code {
            background: var(--cb-ok-soft);
            color: var(--cb-ok);
        }
        .cb-stream-markdown img {
            display: block;
            max-width: 100%;
            height: auto;
            max-height: min(60vh, 28rem);
            border-radius: 6px;
            border: 1px solid var(--cb-border);
        }
        .cb-stream-markdown table {
            display: block;
            width: max-content;
            min-width: 100%;
            max-width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            margin: 0.75rem 0;
            overflow-x: auto;
            overflow-y: hidden;
            border: 1px solid var(--cb-border);
            border-radius: 6px;
            font-size: 0.875rem;
        }
        .cb-stream-markdown th,
        .cb-stream-markdown td {
            border: 0;
            border-right: 1px solid var(--cb-border);
            border-bottom: 1px solid var(--cb-border);
            padding: 0.5rem;
            min-width: 7.5rem;
            text-align: left;
            vertical-align: top;
            color: var(--cb-ink);
            font-size: 0.875rem;
            overflow-wrap: anywhere;
            word-break: normal;
        }
        .cb-stream-markdown th:last-child,
        .cb-stream-markdown td:last-child {
            border-right: 0;
        }
        .cb-stream-markdown tbody tr:last-child td {
            border-bottom: 0;
        }
        .cb-stream-markdown thead:last-child tr:last-child th {
            border-bottom: 0;
        }
        .cb-stream-markdown th {
            background: var(--cb-surface-muted);
            font-weight: 600;
        }
        .cb-stream-markdown table th,
        .cb-stream-markdown table td {
            border-right-color: var(--cb-border);
            border-bottom-color: var(--cb-border);
        }
        .cb-stream-user {
            display: flex;
            justify-content: flex-end;
        }
        .cb-stream-user-content {
            max-width: 100%;
            display: flex;
            flex-direction: column;
            align-items: flex-end;
        }
        .cb-stream-user-bubble {
            border-radius: 16px;
            border-top-right-radius: 2px;
            background: var(--cb-surface-muted);
            padding: 1rem;
            min-width: 0;
            flex-shrink: 1;
        }
        .cb-stream-user .cb-stream-body {
            padding: 0;
            line-height: 22px;
        }
        .cb-stream-attachments {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            margin-bottom: 0.6rem;
            justify-content: flex-end;
        }
        .cb-stream-image-attachment {
            width: min(72vw, 20rem);
            max-width: 100%;
            height: auto;
            min-height: 8rem;
            max-height: 20rem;
            border-radius: 6px;
            overflow: hidden;
            border: 1px solid var(--cb-border);
            background: var(--cb-surface-raised);
            object-fit: contain;
        }
        .cb-stream-image-attachment .q-img__image {
            object-fit: contain !important;
        }
        .cb-stream-tool-image-details {
            width: min(72vw, 20rem);
            max-width: 100%;
        }
        .cb-stream-output-tool-image {
            margin: 0.4rem 0 0.6rem;
        }
        .cb-stream-tool-image-summary {
            display: flex;
            align-items: center;
            padding: 0.3rem 0;
            cursor: pointer;
            color: var(--cb-accent-bright);
            font-weight: 600;
            list-style: none;
        }
        .cb-stream-tool-image-summary::-webkit-details-marker {
            display: none;
        }
        .cb-stream-tool-image-summary::before {
            content: ">";
            display: inline-block;
            margin-right: 0.4rem;
            transition: transform 120ms ease;
        }
        .cb-stream-tool-image-details[open] .cb-stream-tool-image-summary::before {
            transform: rotate(90deg);
        }
        .cb-stream-tool-image-details[open] .cb-stream-tool-image-summary {
            margin-bottom: 0.5rem;
        }
        .cb-stream-image-lightbox-trigger {
            cursor: zoom-in;
            transition: transform 120ms ease, border-color 120ms ease;
        }
        .cb-stream-image-lightbox-trigger:hover {
            transform: translateY(-1px);
            border-color: var(--cb-accent);
        }
        .cb-image-lightbox {
            position: fixed;
            inset: 0;
            z-index: 2600;
            display: none;
            align-items: center;
            justify-content: center;
            padding: 0;
            background: rgba(0, 0, 0, 0.92);
            overscroll-behavior: contain;
            touch-action: none;
            user-select: none;
        }
        .cb-image-lightbox.cb-image-lightbox-open {
            display: flex;
        }
        .cb-image-lightbox-panel {
            position: relative;
            display: flex;
            flex-direction: column;
            width: 100vw;
            height: 100vh;
            height: 100dvh;
            overflow: hidden;
            touch-action: none;
        }
        .cb-image-lightbox-stage {
            position: relative;
            flex: 1 1 auto;
            min-height: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            touch-action: none;
            cursor: grab;
        }
        .cb-image-lightbox-image {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
            border-radius: 0;
            background: #0c0d0f;
            user-select: none;
            transform: translate3d(var(--cb-lightbox-x, 0px), var(--cb-lightbox-y, 0px), 0) scale(var(--cb-lightbox-scale, 1));
            transform-origin: center center;
            will-change: transform;
        }
        .cb-image-lightbox-caption {
            position: absolute;
            left: 0.75rem;
            right: 8.75rem;
            bottom: max(0.75rem, env(safe-area-inset-bottom));
            color: #f8fafc;
            font-size: 0.86rem;
            overflow-wrap: anywhere;
            text-shadow: 0 1px 4px rgba(0, 0, 0, 0.7);
        }
        .cb-image-lightbox-close {
            position: absolute;
            top: max(0.75rem, env(safe-area-inset-top));
            right: 0.75rem;
            z-index: 1;
            width: 2.5rem;
            height: 2.5rem;
            border: 0;
            border-radius: 999px;
            background: rgba(247, 247, 244, 0.92);
            color: #111111;
            font-weight: 800;
            cursor: pointer;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.28);
        }
        .cb-image-lightbox-nav {
            position: absolute;
            top: 50%;
            z-index: 1;
            width: 2.75rem;
            height: 2.75rem;
            border: 0;
            border-radius: 999px;
            background: rgba(247, 247, 244, 0.76);
            color: #111111;
            font-size: 1.8rem;
            font-weight: 800;
            line-height: 1;
            cursor: pointer;
            transform: translateY(-50%);
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.28);
        }
        .cb-image-lightbox-nav-prev {
            left: 0.75rem;
        }
        .cb-image-lightbox-nav-next {
            right: 0.75rem;
        }
        .cb-image-lightbox-controls {
            position: absolute;
            right: 0.75rem;
            bottom: max(0.75rem, env(safe-area-inset-bottom));
            z-index: 1;
            display: flex;
            gap: 0.4rem;
        }
        .cb-image-lightbox-control {
            width: 2.5rem;
            height: 2.5rem;
            border: 0;
            border-radius: 999px;
            background: rgba(247, 247, 244, 0.92);
            color: #111111;
            font-size: 1rem;
            font-weight: 800;
            cursor: pointer;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.28);
        }
        .cb-stream-file-attachment {
            display: flex;
            flex-direction: column;
            gap: 0.1rem;
            max-width: 14rem;
            border-radius: 8px;
            border: 1px solid var(--cb-border);
            background: rgba(21, 22, 25, 0.82);
            padding: 0.45rem 0.6rem;
            text-align: left;
        }
        .cb-stream-file-kind {
            font-size: 0.72rem;
            font-weight: 800;
            color: var(--cb-muted);
        }
        .cb-stream-file-name {
            font-size: 0.82rem;
            color: var(--cb-ink);
            overflow-wrap: anywhere;
        }
        .cb-stream-assistant {
            display: block;
            padding: 0.75rem 0;
        }
        .cb-stream-assistant-content {
            width: 100%;
            min-width: 0;
        }
        .cb-stream-reasoning {
            display: block;
            margin-bottom: 0.75rem;
            overflow: hidden;
            border: 1px solid var(--cb-border);
            border-radius: 8px;
            background: color-mix(in srgb, var(--cb-surface-raised) 72%, transparent);
            color: var(--cb-muted);
            transition: border-color 160ms ease, background-color 160ms ease;
        }
        .cb-stream-reasoning[open] {
            border-color: var(--cb-border-strong);
            background: var(--cb-surface-raised);
        }
        .cb-stream-reasoning-summary {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
            align-items: start;
            gap: 0.45rem 0.75rem;
            box-sizing: border-box;
            width: 100%;
            padding: 0.65rem 0.75rem 0.7rem;
            cursor: pointer;
            list-style: none;
            user-select: none;
        }
        .cb-stream-reasoning-summary:hover {
            background: var(--cb-surface-muted);
        }
        .cb-stream-reasoning-summary:focus-visible {
            outline: 2px solid var(--cb-accent-bright);
            outline-offset: -2px;
        }
        .cb-stream-reasoning-summary::-webkit-details-marker {
            display: none;
        }
        .cb-stream-reasoning-heading,
        .cb-stream-reasoning-toggle {
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            min-width: 0;
        }
        .cb-stream-reasoning-toggle {
            justify-content: flex-end;
            color: var(--cb-muted);
            white-space: nowrap;
        }
        .cb-stream-reasoning-icon,
        .cb-stream-reasoning-chevron {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-family: "Material Icons";
            font-size: 0.95rem;
            line-height: 1;
        }
        .cb-stream-reasoning-icon::before {
            content: "psychology";
        }
        .cb-stream-reasoning-chevron::before {
            content: "chevron_right";
        }
        .cb-stream-reasoning-chevron {
            transition: transform 160ms ease;
        }
        .cb-stream-reasoning[open] .cb-stream-reasoning-chevron {
            transform: rotate(90deg);
        }
        .cb-stream-reasoning-label {
            font-size: 0.82rem;
            font-weight: 700;
            line-height: 1.35;
        }
        .cb-stream-reasoning-toggle-label {
            font-size: 0.76rem;
            font-weight: 600;
            line-height: 1.35;
        }
        .cb-stream-reasoning-toggle-label-close {
            display: none;
        }
        .cb-stream-reasoning[open] .cb-stream-reasoning-toggle-label-open {
            display: none;
        }
        .cb-stream-reasoning[open] .cb-stream-reasoning-toggle-label-close {
            display: inline;
        }
        .cb-stream-reasoning-preview {
            display: flex;
            flex-direction: column;
            justify-content: flex-end;
            grid-column: 1 / -1;
            min-height: 4.1rem;
            max-height: 4.1rem;
            color: var(--cb-muted);
            font-size: 0.88rem;
            font-weight: 400;
            line-height: 1.55;
            overflow: hidden;
            overflow-wrap: anywhere;
            user-select: text;
        }
        .cb-stream-reasoning[open] .cb-stream-reasoning-preview {
            display: none;
        }
        .cb-stream-reasoning-static {
            display: grid;
            grid-template-columns: minmax(0, 1fr);
            align-items: start;
            gap: 0.4rem;
            padding: 0.65rem 0.75rem 0.7rem;
        }
        .cb-stream-reasoning-static .cb-stream-reasoning-preview {
            display: block;
            grid-column: 1;
            min-height: 0;
            max-height: none;
        }
        .cb-stream-reasoning-body {
            max-height: min(52vh, 28rem);
            overflow: auto;
            border-top: 1px solid var(--cb-border);
            padding: 0.75rem 0.9rem 0.85rem;
            color: var(--cb-muted);
            font-size: 0.9rem;
            line-height: 1.6;
            animation: cb-reasoning-reveal 160ms ease-out;
        }
        @keyframes cb-reasoning-reveal {
            from { opacity: 0; transform: translateY(-0.25rem); }
            to { opacity: 1; transform: translateY(0); }
        }
        .cb-stream-timeline {
            display: grid;
            gap: 0.5rem;
            margin-bottom: 0.75rem;
        }
        .cb-stream-timeline .cb-stream-reasoning {
            margin-bottom: 0;
        }
        .cb-stream-command {
            overflow: hidden;
            border: 1px solid color-mix(in srgb, var(--cb-border-strong) 72%, transparent);
            border-radius: 8px;
            background: color-mix(in srgb, var(--cb-surface-raised) 78%, transparent);
            transition: border-color 160ms ease, background-color 160ms ease, box-shadow 160ms ease;
        }
        .cb-stream-command:hover,
        .cb-stream-command[open] {
            border-color: color-mix(in srgb, var(--cb-accent-bright) 50%, var(--cb-border-strong));
            background: var(--cb-surface-raised);
        }
        .cb-stream-command[open] {
            box-shadow: inset 3px 0 0 color-mix(in srgb, var(--cb-accent-bright) 72%, transparent);
        }
        .cb-stream-command-summary {
            display: grid;
            grid-template-columns: auto minmax(0, 1fr) auto;
            align-items: end;
            gap: 0.65rem;
            padding: 0.62rem 0.72rem;
            cursor: pointer;
            list-style: none;
            user-select: none;
        }
        .cb-stream-command-summary::-webkit-details-marker {
            display: none;
        }
        .cb-stream-command-summary:focus-visible {
            outline: 2px solid var(--cb-accent-bright);
            outline-offset: -2px;
        }
        .cb-stream-command-status-icon,
        .cb-stream-command-chevron {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-family: "Material Icons";
            line-height: 1;
        }
        .cb-stream-command-status-icon {
            width: 1.75rem;
            height: 1.75rem;
            border-radius: 6px;
            background: color-mix(in srgb, var(--cb-accent-bright) 14%, transparent);
            color: var(--cb-accent-bright);
            font-size: 1rem;
        }
        .cb-stream-command-status-icon::before {
            content: "terminal";
        }
        .cb-stream-command-tool .cb-stream-command-status-icon::before {
            content: "build";
        }
        .cb-stream-command-running .cb-stream-command-status-icon {
            animation: cb-command-running 1.25s ease-in-out infinite;
        }
        .cb-stream-command-completed .cb-stream-command-status-icon {
            color: var(--cb-ok);
            background: color-mix(in srgb, var(--cb-ok) 14%, transparent);
        }
        .cb-stream-command-failed .cb-stream-command-status-icon {
            color: var(--cb-danger);
            background: color-mix(in srgb, var(--cb-danger) 14%, transparent);
        }
        .cb-stream-command-interrupted .cb-stream-command-status-icon {
            color: var(--cb-muted);
            background: var(--cb-surface-muted);
        }
        @keyframes cb-command-running {
            0%, 100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--cb-accent-bright) 0%, transparent); }
            50% { box-shadow: 0 0 0 4px color-mix(in srgb, var(--cb-accent-bright) 12%, transparent); }
        }
        .cb-stream-command-heading {
            display: flex;
            flex-direction: column;
            justify-content: flex-end;
            min-width: 0;
            min-height: 4.8rem;
            max-height: 4.8rem;
            overflow: hidden;
        }
        .cb-stream-command-label {
            color: var(--cb-ink);
            font-size: 0.8rem;
            font-weight: 750;
            line-height: 1.35;
        }
        .cb-stream-command-command-preview,
        .cb-stream-command-preview {
            overflow: hidden;
            margin-top: 0.1rem;
            color: var(--cb-muted);
            font-family: "JetBrains Mono", "Cascadia Code", monospace;
            font-size: 0.78rem;
            line-height: 1.45;
        }
        .cb-stream-command-command-preview {
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .cb-stream-command-preview {
            display: -webkit-box;
            color: color-mix(in srgb, var(--cb-ink) 78%, var(--cb-muted));
            -webkit-box-orient: vertical;
            -webkit-line-clamp: 2;
            overflow-wrap: anywhere;
        }
        .cb-stream-command-toggle {
            display: inline-flex;
            align-items: center;
            gap: 0.25rem;
            color: var(--cb-muted);
            white-space: nowrap;
        }
        .cb-stream-command-toggle-label {
            font-size: 0.74rem;
            font-weight: 600;
        }
        .cb-stream-command-toggle-label-close {
            display: none;
        }
        .cb-stream-command-chevron {
            font-size: 0.95rem;
            transition: transform 160ms ease;
        }
        .cb-stream-command-chevron::before {
            content: "chevron_right";
        }
        .cb-stream-command[open] .cb-stream-command-toggle-label-open {
            display: none;
        }
        .cb-stream-command[open] .cb-stream-command-toggle-label-close {
            display: inline;
        }
        .cb-stream-command[open] .cb-stream-command-chevron {
            transform: rotate(90deg);
        }
        .cb-stream-command-body {
            display: grid;
            max-height: min(48vh, 26rem);
            gap: 0.4rem;
            overflow: auto;
            border-top: 1px solid var(--cb-border);
            padding: 0.72rem 0.82rem 0.8rem;
            animation: cb-reasoning-reveal 160ms ease-out;
        }
        .cb-stream-command-section-label {
            margin-top: 0.15rem;
            color: var(--cb-muted);
            font-size: 0.7rem;
            font-weight: 800;
            letter-spacing: 0.08em;
            text-transform: uppercase;
        }
        .cb-stream-command-code,
        .cb-stream-command-output {
            margin: 0;
            border-radius: 6px;
            background: var(--cb-code-bg);
            color: var(--cb-code-ink);
            font-family: "JetBrains Mono", "Cascadia Code", monospace;
            font-size: 0.78rem;
            line-height: 1.55;
            overflow-wrap: anywhere;
            padding: 0.6rem 0.68rem;
            white-space: pre-wrap;
            user-select: text;
        }
        .cb-stream-command-output {
            max-height: 16rem;
            overflow: auto;
        }
        .cb-stream-command-meta {
            color: var(--cb-muted);
            font-size: 0.72rem;
            line-height: 1.45;
            overflow-wrap: anywhere;
        }
        @media (max-width: 640px) {
            .cb-stream-command-summary {
                grid-template-columns: auto minmax(0, 1fr);
            }
            .cb-stream-command-toggle {
                display: none;
            }
        }
        .cb-stream-progress {
            border-left: 3px solid #d97706;
            padding-left: 0.85rem;
            color: #f2c14e;
        }
        .cb-stream-error {
            border-left: 3px solid var(--cb-danger);
            padding-left: 0.85rem;
            color: var(--cb-danger);
        }
        .cb-stream-tool-block {
            border-radius: 8px;
            background: rgba(242, 193, 78, 0.12);
            border: 1px solid rgba(242, 193, 78, 0.34);
            padding: 0.75rem 0.85rem;
            margin-bottom: 0.85rem;
        }
        .cb-stream-tool-label {
            font-size: 0.78rem;
            font-weight: 800;
            color: #f2c14e;
            margin-bottom: 0.35rem;
        }
        .cb-stream-tool-body {
            font-size: 0.9rem;
            line-height: 1.55;
            color: #f7d98a;
        }
        .cb-stream-activity-log {
            display: flex;
            flex-direction: column;
            gap: 0.25rem;
            margin-bottom: 0.25rem;
        }
        .cb-stream-activity-item {
            display: block;
            border-radius: 8px;
            border: 1px solid transparent;
            background: transparent;
            overflow: hidden;
        }
        .cb-stream-activity-summary {
            display: flex;
            align-items: flex-start;
            gap: 0.5rem;
            padding: 0.625rem 0.75rem;
            cursor: pointer;
            list-style: none;
        }
        .cb-stream-activity-summary::-webkit-details-marker {
            display: none;
        }
        .cb-stream-activity-system {
            background: rgba(39, 39, 42, 0.5);
            border-color: transparent;
        }
        .cb-stream-activity-info {
            background: var(--cb-info-soft);
            border-color: transparent;
        }
        .cb-stream-activity-success {
            background: var(--cb-ok-soft);
            border-color: transparent;
        }
        .cb-stream-activity-error {
            background: var(--cb-danger-soft);
            border-color: transparent;
        }
        .cb-stream-activity-icon {
            flex: 0 0 auto;
            width: 1.25rem;
            height: 1.25rem;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            color: var(--cb-muted);
            font-family: "Material Icons";
            font-size: 1rem;
            line-height: 1;
        }
        .cb-stream-activity-icon::before {
            content: "radio_button_unchecked";
        }
        .cb-stream-activity-info .cb-stream-activity-icon {
            color: var(--cb-info);
        }
        .cb-stream-activity-info .cb-stream-activity-icon::before {
            content: "info";
        }
        .cb-stream-activity-success .cb-stream-activity-icon {
            color: var(--cb-ok);
        }
        .cb-stream-activity-success .cb-stream-activity-icon::before {
            content: "check_circle";
        }
        .cb-stream-activity-error .cb-stream-activity-icon {
            color: var(--cb-danger);
        }
        .cb-stream-activity-error .cb-stream-activity-icon::before {
            content: "error";
        }
        .cb-stream-activity-system .cb-stream-activity-icon,
        .cb-stream-activity-system .cb-stream-activity-message {
            color: var(--cb-muted);
        }
        .cb-stream-activity-info .cb-stream-activity-message {
            color: var(--cb-info);
        }
        .cb-stream-activity-success .cb-stream-activity-message {
            color: var(--cb-ok);
        }
        .cb-stream-activity-error .cb-stream-activity-message {
            color: var(--cb-danger);
        }
        .cb-stream-activity-copy {
            flex: 1 1 auto;
            min-width: 0;
        }
        .cb-stream-activity-message {
            font-size: 0.875rem;
            font-weight: 400;
            line-height: 20px;
        }
        .cb-stream-activity-details-row {
            display: flex;
            align-items: center;
            gap: 0.25rem;
            margin-top: 0.25rem;
        }
        .cb-stream-activity-details-label {
            font-size: 0.75rem;
            color: var(--cb-muted);
            line-height: 1.35;
            margin-right: 0.25rem;
        }
        .cb-stream-activity-detail {
            font-size: 0.75rem;
            color: var(--cb-muted);
            line-height: 1.35;
            overflow-wrap: anywhere;
        }
        .cb-stream-activity-time {
            font-size: 0.75rem;
            color: var(--cb-muted);
            line-height: 1.35;
            white-space: nowrap;
        }
        .cb-stream-activity-chevron {
            flex: 0 0 auto;
            width: 0.75rem;
            height: 0.75rem;
            color: var(--cb-muted);
            font-family: "Material Icons";
            font-size: 0.75rem;
            line-height: 1;
        }
        .cb-stream-activity-chevron::before {
            content: "chevron_right";
        }
        .cb-stream-activity-item[open] .cb-stream-activity-chevron::before {
            content: "expand_more";
        }
        .cb-stream-activity-metadata {
            margin: 0 0.75rem 0.75rem 2.5rem;
            border: 1px solid var(--cb-border);
            border-radius: 4px;
            background: var(--cb-surface-raised);
            padding: 0.5rem;
        }
        .cb-stream-activity-metadata-text {
            color: var(--cb-ink);
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-size: 0.75rem;
            line-height: 16px;
            overflow-wrap: anywhere;
            white-space: pre-wrap;
        }
        .cb-stream-activity-metadata .cb-stream-activity-metadata-text {
            color: var(--cb-ink);
        }
        .cb-stream-turn-footer,
        .cb-stream-user-footer {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            min-height: 1.5rem;
            color: var(--cb-muted);
            font-size: 13px;
        }
        .cb-stream-turn-footer {
            margin-top: 1rem;
            padding-bottom: 1.5rem;
        }
        .cb-stream-user-footer {
            margin-top: 0.5rem;
            opacity: 0.88;
        }
        .cb-stream-user-footer,
        .cb-stream-copy-button {
            opacity: 0;
            pointer-events: none;
            transition: opacity 120ms ease;
        }
        .cb-stream-turn:hover .cb-stream-user-footer,
        .cb-stream-turn.cb-stream-turn-hover .cb-stream-user-footer,
        .cb-stream-turn:hover .cb-stream-copy-button,
        .cb-stream-turn.cb-stream-turn-hover .cb-stream-copy-button,
        .cb-stream-copy-button:focus-visible {
            opacity: 1;
            pointer-events: auto;
        }
        html body .q-btn.cb-stream-copy-button {
            min-width: auto;
            min-height: auto;
            width: auto;
            height: auto;
            padding: 0.25rem !important;
            color: var(--cb-muted) !important;
        }
        html body .q-btn.cb-stream-copy-button:hover,
        html body .q-btn.cb-stream-copy-button:focus-visible {
            color: var(--cb-ink) !important;
        }
        .cb-stream-copy-button .q-btn__content {
            width: 1rem;
            height: 1rem;
            min-height: 1rem;
        }
        .cb-stream-copy-button .q-icon {
            font-size: 1rem;
        }
        .cb-stream-user-footer .cb-stream-copy-button {
            margin-right: -0.25rem;
        }
        .cb-stream-turn-footer .cb-stream-copy-button {
            margin-left: -0.25rem;
        }
        .cb-stream-stop-button {
            min-height: 1.5rem;
            width: 1.5rem;
            padding: 0;
            color: var(--cb-danger);
        }
        .cb-stream-copy-button-copied {
            color: var(--cb-ok) !important;
        }
        .cb-stream-footer-label-wrap {
            position: relative;
            display: inline-block;
            min-height: 1.25rem;
            line-height: 1.25rem;
        }
        .cb-stream-footer-label-wrap[role="button"] {
            cursor: pointer;
        }
        .cb-stream-footer-label {
            color: var(--cb-muted);
            white-space: nowrap;
        }
        .cb-stream-footer-label-sizer {
            opacity: 0;
            white-space: nowrap;
        }
        .cb-stream-footer-label-main,
        .cb-stream-footer-label-alt {
            position: absolute;
            inset: 0 auto auto 0;
            transition: opacity 120ms ease;
        }
        .cb-stream-footer-label-main {
            opacity: 1;
        }
        .cb-stream-footer-label-alt {
            opacity: 0;
        }
        .cb-stream-turn:hover .cb-stream-footer-label-main {
            opacity: 0;
        }
        .cb-stream-turn.cb-stream-turn-hover .cb-stream-footer-label-main {
            opacity: 0;
        }
        .cb-stream-footer-label-wrap.cb-stream-footer-label-revealed .cb-stream-footer-label-main {
            opacity: 0;
        }
        .cb-stream-turn:hover .cb-stream-footer-label-alt {
            opacity: 1;
        }
        .cb-stream-turn.cb-stream-turn-hover .cb-stream-footer-label-alt {
            opacity: 1;
        }
        .cb-stream-footer-label-wrap.cb-stream-footer-label-revealed .cb-stream-footer-label-alt {
            opacity: 1;
        }
        .cb-stream-working-loader {
            flex: 0 0 auto;
            width: 14px;
            height: 14px;
            position: relative;
            display: grid;
            grid-template-columns: repeat(2, 3px);
            grid-template-rows: repeat(3, 3px);
            gap: 2px;
            place-content: center;
            margin-left: -2px;
        }
        .cb-stream-working-loader-dot {
            width: 3px;
            height: 3px;
            border-radius: 999px;
            background: #b45309;
            opacity: 0;
            animation: cb-synced-loader 0.95s linear infinite;
        }
        .cb-stream-working-loader-dot-0 {
            animation-delay: 0ms;
        }
        .cb-stream-working-loader-dot-1 {
            animation-delay: 158ms;
        }
        .cb-stream-working-loader-dot-3 {
            animation-delay: 316ms;
        }
        .cb-stream-working-loader-dot-5 {
            animation-delay: 474ms;
        }
        .cb-stream-working-loader-dot-4 {
            animation-delay: 632ms;
        }
        .cb-stream-working-loader-dot-2 {
            animation-delay: 790ms;
        }
        .cb-stream-live-elapsed {
            color: var(--cb-muted);
            font-size: 13px;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }
        @keyframes cb-synced-loader {
            0% {
                opacity: 1;
            }
            35% {
                opacity: 0.78;
            }
            55% {
                opacity: 0.56;
            }
            75% {
                opacity: 0.34;
            }
            100% {
                opacity: 0;
            }
        }
        .cb-scroll-bottom-button {
            position: fixed;
            left: 50%;
            bottom: calc(var(--cb-composer-height, 6rem) + 0.75rem);
            z-index: 1200;
            display: none;
            width: 3rem;
            height: 3rem;
            border-radius: 24px !important;
            transform: translateX(-50%);
            background: var(--cb-surface-muted) !important;
            color: var(--cb-ink) !important;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.02);
        }
        html body .q-btn.cb-scroll-bottom-button.bg-primary {
            background: var(--cb-surface-muted) !important;
            background-color: var(--cb-surface-muted) !important;
            color: var(--cb-ink) !important;
        }
        .cb-scroll-bottom-button-visible {
            display: inline-flex;
        }
        .cb-chat-scroll {
            overflow: auto;
        }
        .cb-composer-zone {
            flex: 0 0 auto;
            background: var(--cb-bg);
            padding: 0.75rem 1rem max(1rem, env(safe-area-inset-bottom));
        }
        .cb-composer-inner {
            width: 100%;
            max-width: 51.25rem;
            margin: 0 auto;
        }
        .cb-composer-box {
            border: 1px solid var(--cb-border);
            border-radius: 16px;
            background: var(--cb-surface);
            box-shadow: none;
            padding: 1rem;
        }
        .cb-composer-box:focus-within {
            border-color: var(--cb-border-strong);
            box-shadow: none;
        }
        .cb-composer-queue-track {
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            padding: 0 0 0.5rem;
        }
        .cb-composer-queue-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.5rem;
            border: 1px solid var(--cb-border);
            border-radius: 8px;
            background: var(--cb-surface-raised);
            padding: 0.5rem 0.75rem;
        }
        .cb-composer-queue-text {
            min-width: 0;
            flex: 1 1 auto;
            color: var(--cb-ink);
            font-size: 1rem;
            line-height: 1.4;
            overflow: hidden;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            white-space: normal;
        }
        .cb-composer-queue-actions {
            flex: 0 0 auto;
            align-items: center;
            gap: 0.5rem;
            flex-wrap: nowrap;
        }
        html body .q-btn.cb-composer-queue-cancel {
            min-height: 2rem;
            height: 2rem;
            width: 2rem;
            padding: 0;
            border-radius: 999px;
            background: var(--cb-surface-muted);
            color: var(--cb-muted) !important;
        }
        .cb-composer-attachment-tray {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            padding: 0 0 0.5rem;
        }
        .cb-composer-upload-panel {
            border-radius: 8px;
            border: 1px dashed var(--cb-border-strong);
            background: var(--cb-surface-muted);
            padding: 0.55rem;
            margin-bottom: 0.55rem;
        }
        .cb-composer-upload-panel-hidden {
            display: none;
        }
        .cb-composer-upload-title {
            color: var(--cb-muted);
            font-size: 0.78rem;
            font-weight: 800;
            margin-bottom: 0.4rem;
        }
        .cb-composer-upload {
            max-width: 100%;
            margin-bottom: 0.55rem;
        }
        .cb-composer-upload .q-uploader {
            max-width: 100%;
            width: 100%;
        }
        .cb-composer-attachment-pill {
            display: inline-flex;
            align-items: center;
            position: relative;
            max-width: 3rem;
            min-width: 3rem;
            width: 3rem;
            height: 3rem;
            padding: 0;
            background: transparent;
        }
        .cb-composer-attachment-thumb {
            width: 3rem;
            height: 3rem;
            flex: 0 0 auto;
            border-radius: 6px;
            border: 1px solid var(--cb-border);
            overflow: hidden;
            object-fit: cover;
            background: var(--cb-surface-raised);
        }
        .cb-composer-attachment-name {
            display: none;
        }
        html body .q-btn.cb-composer-attachment-remove {
            position: absolute;
            top: -0.5rem;
            left: -0.5rem;
            min-height: 1.5rem;
            height: 1.5rem;
            width: 1.5rem;
            padding: 0;
            border-radius: 999px;
            border: 1px solid var(--cb-border);
            background: var(--cb-surface-muted);
            color: var(--cb-muted) !important;
            opacity: 0;
            pointer-events: none;
            z-index: 1;
        }
        .cb-composer-attachment-pill:hover .cb-composer-attachment-remove,
        .cb-composer-attachment-remove:focus-visible {
            opacity: 1;
            pointer-events: auto;
        }
        .cb-composer-input .q-field__control {
            min-height: 46px;
            background: transparent !important;
        }
        .cb-composer-input textarea {
            font-size: 16px;
            line-height: 22.4px;
            padding-top: 0.25rem !important;
        }
        .cb-composer-actions {
            min-height: 2rem;
            align-items: flex-end;
            margin: 0 -0.375rem;
            gap: 0;
        }
        .cb-composer-right-actions {
            flex-shrink: 0;
            min-width: 0;
            gap: 0.25rem;
        }
        .cb-context-meter {
            display: inline-flex;
            align-items: center;
            gap: 0.45rem;
            min-width: 0;
            color: var(--cb-muted);
            font-size: 0.78rem;
            white-space: nowrap;
        }
        .cb-context-meter-track {
            position: relative;
            display: inline-flex;
            width: 4.5rem;
            height: 0.35rem;
            overflow: hidden;
            border-radius: 999px;
            background: var(--cb-border);
        }
        .cb-context-meter-fill {
            display: block;
            height: 100%;
            border-radius: inherit;
            background: var(--cb-accent);
        }
        .cb-context-meter-label {
            color: var(--cb-muted);
            font-size: 0.78rem;
        }
        .cb-composer-tool-button,
        .cb-composer-stop-button,
        .cb-composer-send-button {
            width: 1.75rem;
            min-width: 1.75rem;
            height: 1.75rem;
            min-height: 1.75rem;
            padding: 0 !important;
        }
        .cb-composer-send-button {
            margin-left: 0.25rem;
        }
        .cb-composer-tool-button .q-btn__content,
        .cb-composer-stop-button .q-btn__content,
        .cb-composer-send-button .q-btn__content {
            width: 1.75rem;
            height: 1.75rem;
            min-height: 1.75rem;
        }
        .cb-composer-tool-button .q-icon,
        .cb-composer-stop-button .q-icon,
        .cb-composer-send-button .q-icon {
            font-size: 1rem;
        }
        .cb-composer-tool-button {
            color: var(--cb-muted);
        }
        .cb-composer-tool-button:hover {
            background: var(--cb-surface-muted) !important;
        }
        .cb-composer-cancel-button {
            background: var(--cb-danger) !important;
        }
        .cb-composer-send-button:disabled,
        .cb-composer-send-button.cb-composer-send-disabled {
            background: var(--cb-surface-muted) !important;
            background-color: var(--cb-surface-muted) !important;
            color: var(--cb-muted) !important;
            opacity: 1;
            cursor: default;
            pointer-events: none;
        }
        html body .q-btn.cb-composer-send-button:disabled,
        html body .q-btn.cb-composer-send-button.cb-composer-send-disabled,
        html body .q-btn.cb-composer-send-button.bg-primary:disabled,
        html body .q-btn.cb-composer-send-button.bg-primary.cb-composer-send-disabled {
            background: var(--cb-surface-muted) !important;
            background-color: var(--cb-surface-muted) !important;
            color: var(--cb-muted) !important;
            opacity: 1;
        }
        .cb-composer-send-button:disabled .q-icon,
        .cb-composer-send-button.cb-composer-send-disabled .q-icon,
        .cb-composer-send-button:disabled .q-btn__content,
        .cb-composer-send-button.cb-composer-send-disabled .q-btn__content {
            color: var(--cb-muted) !important;
        }
        .cb-language-toggle {
            border: 1px solid var(--cb-border);
            border-radius: 6px;
            overflow: hidden;
        }
        .cb-language-toggle .q-btn {
            min-height: 2.25rem;
        }
        .cb-language-toggle .q-btn[aria-pressed="true"],
        .cb-language-toggle .q-btn.q-btn--active,
        .cb-language-toggle .q-btn.bg-primary {
            background: var(--cb-accent) !important;
            color: #ffffff !important;
        }
        html body .cb-language-toggle .q-btn.bg-white.text-primary,
        html body .cb-language-toggle .q-btn.bg-white.text-primary .q-btn__content,
        html body .q-btn-group.cb-language-toggle.q-btn-toggle .q-btn.q-btn.bg-white.text-primary.q-btn--rectangle {
            background: var(--cb-surface-muted) !important;
            background-color: var(--cb-surface-muted) !important;
            color: var(--cb-ink) !important;
        }
        .cb-disclosure summary {
            cursor: pointer;
            list-style: none;
        }
        .cb-disclosure summary::-webkit-details-marker {
            display: none;
        }
        .cb-disclosure summary::after {
            content: "expand_more";
            font-family: "Material Icons";
            font-size: 1.25rem;
            color: var(--cb-muted);
        }
        .cb-disclosure[open] summary::after {
            content: "expand_less";
        }
        @media (max-width: 767px) {
            body {
                font-size: 13px;
            }
            .cb-sidebar-shell {
                width: 100vw;
                max-width: 100vw;
            }
            body:not(.cb-sidebar-open) .cb-sidebar-shell {
                display: none;
            }
            .cb-sidebar-content {
                min-height: 100dvh;
            }
            .cb-shell-content {
                padding: 0.75rem;
            }
            .cb-shell-content-stream {
                padding: 0;
            }
            .cb-agent-stream {
                padding: 1rem 0.75rem;
            }
            .cb-agent-stream-content {
                padding: 0 0.5rem;
            }
            .cb-stream-user-content {
                max-width: 100%;
            }
            .cb-stream-user-footer,
            .cb-stream-copy-button {
                opacity: 1;
                pointer-events: auto;
            }
            .cb-stream-markdown pre {
                padding-top: 2.5rem;
            }
            .cb-stream-code-copy-button {
                opacity: 1;
            }
            .cb-scroll-bottom-button {
                bottom: calc(var(--cb-composer-height, 5.5rem) + 0.5rem);
            }
            .cb-composer-zone {
                padding: 0.75rem 1rem max(1rem, env(safe-area-inset-bottom));
            }
            .cb-composer-box {
                padding: 0.5rem 0.75rem;
            }
            .cb-context-meter-track {
                display: none;
            }
            .cb-context-meter-label {
                max-width: 7.5rem;
                overflow: hidden;
                text-overflow: ellipsis;
            }
            .cb-composer-attachment-name {
                max-width: 8rem;
            }
            html body .q-btn.cb-composer-attachment-remove {
                opacity: 1;
                pointer-events: auto;
            }
            .cb-composer-queue-item {
                align-items: center;
            }
            .cb-composer-queue-text {
                white-space: normal;
                display: -webkit-box;
                -webkit-line-clamp: 2;
                -webkit-box-orient: vertical;
            }
            .cb-card {
                border-radius: var(--cb-radius);
            }
            .q-table {
                font-size: 0.78rem;
            }
            .q-table th,
            .q-table td {
                white-space: normal;
                word-break: break-word;
                padding-left: 0.45rem;
                padding-right: 0.45rem;
            }
            .q-card {
                max-width: calc(100vw - 1rem);
            }
            .q-dialog__inner > div {
                max-width: calc(100vw - 1rem) !important;
                min-width: 0 !important;
            }
        }
        </style>
        """,
        shared=True,
    )
    state_defaults = {
        "selected_session_name": "",
        "selected_task_id": "",
        "selected_task_status": "",
        "selected_task_agent": "",
        "selected_task_backend": "",
        "session_page": 1,
        "task_page": 1,
        "agent_page": 1,
        "checks_page": 1,
        "load_session_detail": False,
        "load_session_rows": False,
        "load_session_files": False,
        "load_task_list": False,
        "load_task_detail": False,
        "load_weixin_bindings": False,
        "load_logs": False,
        "checks_in_progress": False,
        "active_page": "stream",
        "bridge_mode": "weixin",
        "bridge_mode_selected": False,
        "language": default_language,
        "theme": "dark",
        "qr_login_open": False,
        "stream_session_task_limits": {},
        "stream_pending_images": {},
        "stream_refresh_signature": None,
        "stream_hub_state_file_signature": None,
        "stream_composer_signature": None,
        "stream_panel_render_snapshot": None,
        "stream_panel_render_signature": None,
        "stream_panel_hub_state_file_signature": None,
        "stream_scroll_runtime_clients": set(),
        "stream_force_bottom_session": "",
        "stream_force_bottom_next": False,
        "stream_preserve_top_session": "",
        "stream_switch_refresh_pending": False,
        "stream_switch_sequence": 0,
        "stream_sidebar_task_limit": STREAM_SIDEBAR_PAGE_SIZE,
        "stream_sidebar_codex_loaded": False,
        "stream_sidebar_codex_threads": [],
        "stream_sidebar_codex_cursor": "",
        "stream_sidebar_codex_archived": False,
        "stream_sidebar_codex_done": False,
        "stream_sidebar_codex_error": "",
        "sidebar_content_loaded": False,
    }
    state = _ClientState(state_defaults, lambda: context.client.storage)

    def refresh_model():
        model = build_web_console_view_model(
            APP_DIR,
            translate,
            page_key=state["active_page"],
            session_page=state["session_page"],
            task_page=state["task_page"],
            agent_page=state["agent_page"],
            checks_page=state["checks_page"],
            load_session_detail=state["load_session_detail"],
            load_session_rows=state["load_session_rows"],
            load_session_files=state["load_session_files"],
            load_task_list=state["load_task_list"],
            load_task_detail=state["load_task_detail"],
            load_weixin_bindings=state["load_weixin_bindings"],
            load_logs=state["load_logs"],
            selected_session_name=state["selected_session_name"],
            selected_task_id=state["selected_task_id"],
            selected_task_status=state["selected_task_status"],
            selected_task_agent=state["selected_task_agent"],
            selected_task_backend=state["selected_task_backend"],
        )
        if state["active_page"] != "stream" or not state["selected_session_name"]:
            state["selected_session_name"] = model.selected_session_name
        state["selected_task_id"] = model.selected_task_id
        state["selected_task_status"] = model.selected_task_status
        state["selected_task_agent"] = model.selected_task_agent
        state["selected_task_backend"] = model.selected_task_backend
        state["session_page"] = model.session_page
        state["task_page"] = model.task_page
        state["agent_page"] = model.agent_page
        state["checks_page"] = model.checks_page
        state["checks_in_progress"] = model.checks_in_progress
        if not state["bridge_mode_selected"] and model.home.runtime_bridge_mode in {"weixin", "qq"}:
            state["bridge_mode"] = model.home.runtime_bridge_mode
        return model

    def jump_to(anchor: str) -> None:
        target = next((page for page in PRIMARY_PAGES if page.anchor == anchor or page.key == anchor), None)
        previous_page = state["active_page"]
        if target is not None:
            state["active_page"] = target.key
        target_key = target.key if target is not None else str(anchor or "").strip()
        if target_key == "sessions" and previous_page != "sessions":
            state["load_session_rows"] = False
            state["load_session_files"] = False
            state["load_task_list"] = False
            state["load_task_detail"] = False
            state["load_weixin_bindings"] = False
        if target_key == "diagnostics" and previous_page != "diagnostics":
            state["load_logs"] = False
        selected_session_name = str(state.get("selected_session_name") or "").strip()
        if target_key == "stream":
            state["stream_force_bottom_next"] = True
        ui.run_javascript(
            f"""
            (() => {{
                const url = new URL(window.location.href);
                url.hash = {str(anchor or '').strip()!r};
                if ({target_key!r} === 'stream') {{
                    url.searchParams.delete('page');
                    if ({selected_session_name!r}) {{
                        url.searchParams.set('session', {selected_session_name!r});
                    }}
                }} else {{
                    url.searchParams.set('page', {target_key!r});
                    url.searchParams.delete('session');
                }}
                window.history.replaceState(null, '', url.toString());
                document.body.classList.remove('cb-sidebar-open');
            }})();
            """
        )
        sidebar_navigation_view.refresh()
        content_view.refresh()

    def refresh_after_qr_login() -> None:
        content_view.refresh()

    def notify_only(result_message: str) -> None:
        ui.notify(result_message, position="top")

    def switch_language(language: str) -> None:
        selected = normalize_language(str(language or "").strip())
        if selected not in {"zh-CN", "en-US"}:
            return
        state["language"] = selected
        ui.run_javascript(f"window.location.href = '/?lang={selected}'")

    def switch_theme(theme: str) -> None:
        selected = normalize_web_theme(theme)
        state["theme"] = selected
        ui.run_javascript(f"window.__cbApplyTheme && window.__cbApplyTheme({selected!r})")

    def set_bridge_mode(mode: str) -> None:
        cleaned = str(mode or "").strip()
        if cleaned not in {"weixin", "qq"}:
            return
        state["bridge_mode"] = cleaned
        state["bridge_mode_selected"] = True
        content_view.refresh()

    def apply_request_language(request) -> None:
        selected = normalize_language(str(request.query_params.get("lang") or ""))
        if selected in {"zh-CN", "en-US"} and selected != state["language"]:
            state["language"] = selected

    def apply_request_theme(request) -> None:
        selected = normalize_web_theme(str(request.query_params.get("theme") or request.cookies.get("cb_theme") or ""))
        state["theme"] = selected

    def apply_request_page(request) -> None:
        requested_page = str(request.query_params.get("page") or "").strip()
        page = next((item for item in PRIMARY_PAGES if item.key == requested_page or item.anchor == requested_page), None)
        if page is not None:
            state["active_page"] = page.key

    def apply_request_session(request) -> None:
        has_session_query = "session" in request.query_params
        requested_session = str(request.query_params.get("session") or "").strip()
        if requested_session:
            state["active_page"] = "stream"
            state["selected_session_name"] = requested_session
            state["stream_force_bottom_session"] = requested_session
            state["stream_force_bottom_next"] = True
        elif has_session_query and state["active_page"] == "stream":
            state["selected_session_name"] = ""
            state["stream_force_bottom_next"] = True
        elif state["active_page"] == "stream":
            preserved_session = str(state.get("selected_session_name") or "").strip()
            if preserved_session:
                state["stream_force_bottom_session"] = preserved_session
            state["stream_force_bottom_next"] = True

    def mark_qr_login_open() -> None:
        state["qr_login_open"] = True

    def mark_qr_login_closed() -> None:
        state["qr_login_open"] = False
        content_view.refresh()

    open_qr_login = install_qr_login_dialog(
        ui,
        notify_only,
        refresh_after_qr_login,
        translate,
        on_open=mark_qr_login_open,
        on_close=mark_qr_login_closed,
    )
    open_qq_login = install_qq_login_dialog(ui, notify_only, translate, on_success=lambda: content_view.refresh())

    def _stream_session_task_limit(session_name: str) -> int:
        cleaned_session_name = str(session_name or "").strip() or "default"
        initial_limit = _stream_initial_history_limit(cleaned_session_name)
        limits = state["stream_session_task_limits"]
        if not isinstance(limits, dict):
            return initial_limit
        try:
            return max(initial_limit, int(limits.get(cleaned_session_name) or initial_limit))
        except (TypeError, ValueError):
            return initial_limit

    def _stream_global_task_limit(session_name: str) -> int:
        return 0 if str(session_name or "").strip() else 1

    def _stream_state_snapshot() -> dict[str, object]:
        session_name = str(state["selected_session_name"] or "").strip()
        stream_state = build_stream_state_snapshot(
            selected_session_name=session_name,
            task_limit=_stream_global_task_limit(session_name),
            session_task_limit=_stream_session_task_limit(session_name),
        )
        if session_name:
            return stream_state
        inferred_session = _resolve_stream_active_session(stream_state)
        if inferred_session == "default":
            return stream_state
        state["selected_session_name"] = inferred_session
        state["stream_force_bottom_session"] = inferred_session
        return build_stream_state_snapshot(
            selected_session_name=inferred_session,
            task_limit=_stream_global_task_limit(inferred_session),
            session_task_limit=_stream_session_task_limit(inferred_session),
        )

    def _stream_render_snapshot() -> tuple[dict[str, object], str]:
        cached_snapshot = state.get("stream_panel_render_snapshot")
        if (
            isinstance(cached_snapshot, tuple)
            and len(cached_snapshot) == 2
            and isinstance(cached_snapshot[0], dict)
            and isinstance(cached_snapshot[1], str)
        ):
            return cached_snapshot
        stream_state = _stream_state_snapshot()
        active_stream_session = _resolve_stream_active_session(stream_state)
        return stream_state, active_stream_session

    def _stream_signature_snapshot() -> tuple:
        session_name = str(state["selected_session_name"] or "").strip()
        return build_stream_signature_snapshot(
            selected_session_name=session_name,
            task_limit=_stream_global_task_limit(session_name),
            session_task_limit=_stream_session_task_limit(session_name),
        )

    def _stream_task_order_key(task: dict[str, object]) -> tuple[tuple[int, float, str], str, str]:
        raw_order = str(task.get("stream_order") or "")
        order_text = f"{int(raw_order):08d}" if raw_order.isdigit() else ""
        return (_stream_time_sort_key(task.get("created_at")), order_text, str(task.get("id") or ""))

    def _stream_session_order(sessions: dict[str, list[dict[str, object]]], session_names: list[str]) -> list[str]:
        return sorted(
            session_names,
            key=lambda session_name: max((_stream_task_order_key(task) for task in sessions.get(session_name, [])), default=((1, 0.0, ""), "", "")),
            reverse=True,
        )

    def _resolve_stream_active_session(mobile_state: dict[str, object]) -> str:
        tasks = mobile_state.get("tasks") if isinstance(mobile_state.get("tasks"), list) else []
        sessions: dict[str, list[dict[str, object]]] = {}
        session_order: list[str] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            session_name = str(task.get("session_name") or "default")
            if session_name not in session_order:
                session_order.append(session_name)
                sessions[session_name] = []
            sessions[session_name].append(task)
        session_order = _stream_session_order(sessions, session_order)
        selected_session_name = str(state["selected_session_name"] or "").strip()
        if selected_session_name:
            return selected_session_name
        return session_order[0] if session_order else "default"

    def _stream_composer_signature(mobile_state: dict[str, object], active_session: str) -> tuple:
        tasks = mobile_state.get("tasks") if isinstance(mobile_state.get("tasks"), list) else []
        active_tasks = [
            task
            for task in tasks
            if isinstance(task, dict)
            and str(task.get("session_name") or "default") == active_session
            and str(task.get("status") or "").strip() in {"queued", "running"}
        ]
        latest_task = next(
            (
                task
                for task in reversed([task for task in tasks if isinstance(task, dict) and str(task.get("session_name") or "default") == active_session])
                if str(task.get("id") or "").strip()
            ),
            {},
        )
        pending_images = tuple(_stream_pending_image_paths(active_session))
        return (
            active_session,
            tuple(
                (
                    str(task.get("id") or ""),
                    str(task.get("status") or ""),
                    str(task.get("prompt") or ""),
                )
                for task in active_tasks
            ),
            str(latest_task.get("agent_id") or ""),
            str(latest_task.get("backend") or ""),
            str(latest_task.get("context_left_percent") or ""),
            pending_images,
        )

    def _stream_pending_image_paths(session_name: str) -> list[str]:
        cleaned_session_name = str(session_name or "").strip() or "default"
        pending = state["stream_pending_images"]
        if not isinstance(pending, dict):
            return []
        values = pending.get(cleaned_session_name)
        if not isinstance(values, list):
            return []
        return [str(item) for item in values if str(item).strip()]

    def _stream_pending_image_items(session_name: str) -> list[dict[str, str]]:
        root = MOBILE_UPLOAD_ROOT.resolve()
        items: list[dict[str, str]] = []
        for image_path in _stream_pending_image_paths(session_name):
            try:
                path = Path(image_path).expanduser().resolve()
            except OSError:
                continue
            label = path.name or image_path
            try:
                rel = path.relative_to(root)
            except ValueError:
                source = ""
            else:
                source = f"/mobile-upload/{quote(rel.as_posix())}"
            items.append({"path": str(path), "label": label, "source": source})
        return items

    def _refresh_stream_signatures(stream_state: dict[str, object], active_stream_session: str) -> None:
        render_signature = state.get("stream_panel_render_signature")
        state["stream_refresh_signature"] = render_signature if isinstance(render_signature, tuple) else _stream_signature_snapshot()
        render_hub_file_signature = state.get("stream_panel_hub_state_file_signature")
        state["stream_hub_state_file_signature"] = (
            render_hub_file_signature
            if isinstance(render_hub_file_signature, tuple)
            else stream_hub_state_file_signature()
        )
        state["stream_composer_signature"] = _stream_composer_signature(stream_state, active_stream_session)

    @ui.refreshable
    def stream_messages_view() -> None:
        current_client = context.client
        if getattr(current_client, "_deleted", False):
            return
        stream_state, active_stream_session = _stream_render_snapshot()
        _refresh_stream_signatures(stream_state, active_stream_session)
        force_bottom_session = str(state.get("stream_force_bottom_session") or "").strip()
        force_bottom_next = bool(state.get("stream_force_bottom_next"))
        force_bottom = force_bottom_next or force_bottom_session == active_stream_session
        preserve_top_session = str(state.get("stream_preserve_top_session") or "").strip()
        preserve_top = preserve_top_session == active_stream_session
        render_mobile_stream_messages_section(
            ui,
            translate,
            stream_state,
            active_stream_session,
            _copy_stream_text,
            _cancel_stream_task,
            _load_older_stream_messages,
        )
        if force_bottom:
            state["stream_force_bottom_session"] = ""
            state["stream_force_bottom_next"] = False
        if preserve_top:
            state["stream_preserve_top_session"] = ""
        scroll_stream_to_bottom(active_stream_session, force_bottom=force_bottom, preserve_top=preserve_top)

    @ui.refreshable
    def stream_composer_view() -> None:
        current_client = context.client
        if getattr(current_client, "_deleted", False):
            return
        stream_state, active_stream_session = _stream_render_snapshot()
        state["stream_composer_signature"] = _stream_composer_signature(stream_state, active_stream_session)
        render_mobile_stream_composer_section(
            ui,
            translate,
            stream_state,
            active_stream_session,
            _stream_pending_image_items(active_stream_session),
            _submit_stream_message,
            _cancel_stream_task,
            _upload_stream_image,
            _remove_stream_image,
        )

    @ui.refreshable
    def stream_panel_view() -> None:
        current_client = context.client
        if getattr(current_client, "_deleted", False):
            return
        stream_state = _stream_state_snapshot()
        active_stream_session = _resolve_stream_active_session(stream_state)
        _refresh_stream_signatures(stream_state, active_stream_session)
        state["stream_panel_render_snapshot"] = (stream_state, active_stream_session)
        try:
            render_mobile_stream_shell(
                ui,
                active_stream_session,
                stream_messages_view,
                stream_composer_view,
            )
        finally:
            state["stream_panel_render_snapshot"] = None

    def _refresh_stream_parts(
        stream_state: dict[str, object] | None = None,
        active_stream_session: str = "",
        *,
        refresh_composer: bool = True,
        refresh_messages: bool = True,
        refresh_signature: tuple | None = None,
        hub_file_signature: tuple | None = None,
    ) -> None:
        start_time = time.perf_counter()
        if stream_state is None or not active_stream_session:
            stream_state = _stream_state_snapshot()
            active_stream_session = _resolve_stream_active_session(stream_state)
        current_client = context.client
        client_id = str(getattr(current_client, "id", id(current_client)))
        _append_stream_ui_log(
            "refresh_parts_start",
            client_id=client_id,
            active_session=active_stream_session,
            refresh_composer=refresh_composer,
            refresh_messages=refresh_messages,
            active_page=state.get("active_page"),
            has_socket=bool(getattr(current_client, "has_socket_connection", False)),
            deleted=bool(getattr(current_client, "_deleted", False)),
        )
        state["stream_panel_render_snapshot"] = (stream_state, active_stream_session)
        state["stream_panel_render_signature"] = refresh_signature
        state["stream_panel_hub_state_file_signature"] = hub_file_signature
        try:
            if refresh_composer:
                stream_composer_view.refresh()
            if refresh_messages:
                stream_messages_view.refresh()
            _append_stream_ui_log(
                "refresh_parts_ok",
                client_id=client_id,
                active_session=active_stream_session,
                refresh_composer=refresh_composer,
                refresh_messages=refresh_messages,
                elapsed_ms=round((time.perf_counter() - start_time) * 1000, 3),
            )
        except Exception as exc:
            _append_stream_ui_log(
                "refresh_parts_error",
                client_id=client_id,
                active_session=active_stream_session,
                refresh_composer=refresh_composer,
                refresh_messages=refresh_messages,
                error=repr(exc),
                traceback=traceback.format_exc(),
                elapsed_ms=round((time.perf_counter() - start_time) * 1000, 3),
            )
            raise
        finally:
            state["stream_panel_render_snapshot"] = None
            state["stream_panel_render_signature"] = None
            state["stream_panel_hub_state_file_signature"] = None

    @ui.refreshable
    def content_view() -> None:
        current_client = context.client
        if getattr(current_client, "_deleted", False):
            return
        model = None if state["active_page"] in {"mobile", "stream"} else refresh_model()
        content_width = "max-w-none" if state["active_page"] == "stream" else "max-w-7xl"
        stream_content_class = "cb-shell-content-stream" if state["active_page"] == "stream" else ""
        with ui.column().classes(f"cb-shell-content {stream_content_class} w-full {content_width} mx-auto gap-6"):
            if state["active_page"] == "home":
                render_home_section(
                    ui,
                    model,
                    translate,
                    _run_action,
                    _refresh_home_status,
                    _submit_task,
                    _switch_account,
                    _set_weixin_notice_enabled,
                    open_qr_login,
                    open_qq_login,
                    state["bridge_mode"],
                    set_bridge_mode,
                )
            elif state["active_page"] == "sessions":
                render_sessions_section(
                    ui,
                    model,
                    translate,
                    _select_session,
                    _load_session_rows,
                    _set_session_page,
                    _load_session_files,
                    _load_selected_session_detail,
                    _select_task,
                    _load_task_list,
                    _set_task_page,
                    _load_selected_task_detail,
                    _set_task_filters,
                    _find_task_by_id,
                    _open_weixin_binding,
                    _open_weixin_binding_task,
                    _load_weixin_bindings,
                    _switch_weixin_binding_backend,
                    _reset_weixin_binding,
                )
            elif state["active_page"] == "mobile":
                mobile_url = build_mobile_access_url(host=host, port=port)
                render_mobile_section(
                    ui,
                    translate,
                    mobile_url,
                    build_mobile_qr_data_url(mobile_url),
                    lambda url=mobile_url: _copy_mobile_url(url),
                    lambda url=mobile_url: _open_mobile_url(url),
                )
            elif state["active_page"] == "stream":
                stream_panel_view()
            else:
                render_diagnostics_section(
                    ui,
                    model,
                    translate,
                    _refresh_checks,
                    _refresh_logs,
                    _refresh_external_agents,
                    _set_checks_page,
                    _switch_bridge_agent,
                    _set_agent_page,
                    _save_agent,
                    _delete_agent,
                    _terminate_external_agent,
                    _copy_external_session_hint,
                    _run_repair_command,
                )

    def _notify(result_message: str) -> None:
        ui.notify(result_message, position="top")
        if state["active_page"] == "stream":
            _refresh_stream_parts()
        else:
            content_view.refresh()

    def _run_action(action: str) -> None:
        result = schedule_named_action(action, delay_seconds=1.0) if action in ASYNC_SERVICE_ACTIONS else run_named_action(action)
        _notify(result.message)

    def _switch_account(account_id: str) -> None:
        result = switch_active_account(account_id, restart_if_running=False)
        _notify(result.message)

    def _switch_bridge_agent(agent_id: str) -> None:
        result = switch_bridge_agent(agent_id)
        _notify(result.message)

    def _set_weixin_notice_enabled(service_enabled: bool, config_enabled: bool, task_enabled: bool) -> None:
        result = set_weixin_notice_enabled(service_enabled, config_enabled, task_enabled)
        _notify(result.message)

    def _submit_task(agent_id: str, prompt: str, session_name: str, backend: str) -> None:
        source = "qq-web" if state["bridge_mode"] == "qq" else "web"
        result = submit_hub_task(agent_id=agent_id, prompt=prompt, session_name=session_name, backend=backend, source=source)
        _notify(result.message)

    def _run_repair_command(command: str, label: str) -> None:
        result = run_repair_command(command, label)
        _notify(result.message)

    def _refresh_checks() -> None:
        key = "checks_full" if state["active_page"] == "diagnostics" else "checks_light"
        refresh_dashboard_cache(APP_DIR, "runtime")
        refresh_dashboard_cache(APP_DIR, key)
        _notify(t("ui.web.notify.checks_refreshed", "环境检查已刷新"))

    def _refresh_home_status() -> None:
        refresh_dashboard_cache(APP_DIR, "runtime")
        refresh_dashboard_cache(APP_DIR, "checks_light")
        _notify(t("ui.web.notify.status_refreshed", "状态已刷新"))

    def _refresh_logs() -> None:
        refresh_dashboard_cache(APP_DIR, "logs")
        state["load_logs"] = True
        _notify(t("ui.web.notify.logs_refreshed", "运行日志已刷新"))

    def _refresh_external_agents() -> None:
        refresh_dashboard_cache(APP_DIR, "external_agent_processes")
        _notify(t("ui.web.notify.external_agents_refreshed", "外部进程列表已刷新"))

    def _save_agent(
        agent_id: str,
        name: str,
        workdir: str,
        session_file: str,
        backend: str,
        model_name: str,
        prompt_prefix: str,
        enabled: bool,
    ) -> None:
        result = save_agent(agent_id, name, workdir, session_file, backend, model_name, prompt_prefix, enabled)
        _notify(result.message)

    def _delete_agent(agent_id: str) -> None:
        result = delete_agent(agent_id)
        if state["selected_task_agent"] == agent_id:
            state["selected_task_agent"] = ""
        _notify(result.message)

    def _terminate_external_agent(pid: int) -> None:
        result = terminate_external_agent(pid)
        _notify(result.message)

    def _copy_external_session_hint(session_hint: str) -> None:
        cleaned_hint = session_hint.strip()
        if not cleaned_hint:
            _notify(t("ui.web.notify.no_session_hint", "当前外部进程没有可复制的会话标识"))
            return
        ui.run_javascript(f"navigator.clipboard.writeText({cleaned_hint!r})")
        ui.notify(t("ui.web.notify.session_hint_copied", "已复制会话标识：{hint}", hint=cleaned_hint), position="top")

    def _copy_mobile_url(url: str) -> None:
        cleaned_url = url.strip()
        if not cleaned_url:
            return
        ui.run_javascript(f"navigator.clipboard.writeText({cleaned_url!r})")
        ui.notify(t("ui.web.notify.mobile_url_copied", "已复制手机入口地址"), position="top")

    def _copy_stream_text(value: str) -> None:
        cleaned_value = str(value or "").strip()
        if not cleaned_value:
            return
        ui.run_javascript(f"navigator.clipboard.writeText({cleaned_value!r})")
        ui.notify(t("ui.web.notify.stream_text_copied", "已复制实时对话内容"), position="top")

    def _safe_stream_upload_session(session_name: str) -> str:
        cleaned = str(session_name or "").strip() or "default"
        safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in cleaned).strip("-_")
        return safe or "default"

    def _upload_stream_image(session_name: str, event) -> None:
        cleaned_session_name = str(session_name or "").strip() or "default"
        uploaded_file = getattr(event, "file", None)
        original_name = str(getattr(uploaded_file, "name", "") or "image").replace("\\", "/").rsplit("/", 1)[-1]
        suffix = Path(original_name).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
            ui.notify(t("ui.web.notify.stream_image_unsupported", "只支持上传图片附件"), position="top")
            return
        try:
            content = uploaded_file.read()
        except (AttributeError, OSError) as exc:
            ui.notify(t("ui.web.notify.stream_image_failed", "图片上传失败：{message}", message=str(exc)), position="top")
            return
        if isinstance(content, str):
            content = content.encode("utf-8")
        if not isinstance(content, (bytes, bytearray)):
            ui.notify(t("ui.web.notify.stream_image_failed", "图片上传失败：{message}", message="invalid upload content"), position="top")
            return
        target_dir = MOBILE_UPLOAD_ROOT / "web" / _safe_stream_upload_session(cleaned_session_name)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{uuid.uuid4().hex[:12]}{suffix}"
        try:
            target_path.write_bytes(content)
        except OSError as exc:
            ui.notify(t("ui.web.notify.stream_image_failed", "图片上传失败：{message}", message=str(exc)), position="top")
            return
        pending = state["stream_pending_images"]
        if not isinstance(pending, dict):
            pending = {}
            state["stream_pending_images"] = pending
        images = pending.setdefault(cleaned_session_name, [])
        if isinstance(images, list):
            images.append(str(target_path.resolve()))
        ui.notify(t("ui.web.notify.stream_image_added", "已添加图片附件：{name}", name=original_name), position="top")
        stream_composer_view.refresh()

    def _remove_stream_image(session_name: str, image_path: str) -> None:
        cleaned_session_name = str(session_name or "").strip() or "default"
        pending = state["stream_pending_images"]
        if not isinstance(pending, dict):
            return
        images = pending.get(cleaned_session_name)
        if not isinstance(images, list):
            return
        cleaned_path = str(image_path or "").strip()
        pending[cleaned_session_name] = [item for item in images if str(item) != cleaned_path]
        stream_composer_view.refresh()

    def _cancel_stream_task(task_id: str) -> None:
        cleaned_task_id = str(task_id or "").strip()
        if not cleaned_task_id:
            ui.notify(t("ui.web.notify.no_task_to_cancel", "当前没有可停止的任务"), position="top")
            return
        result = cancel_hub_task(cleaned_task_id)
        if result.ok:
            ui.notify(t("ui.web.notify.task_cancel_requested", "已请求停止任务：{task_id}", task_id=cleaned_task_id), position="top")
        else:
            ui.notify(t("ui.web.notify.task_cancel_failed", "停止任务失败：{message}", message=result.message), position="top")
        _refresh_stream_parts()

    def _load_older_stream_messages(session_name: str) -> None:
        cleaned_session_name = str(session_name or "").strip() or "default"
        limits = state["stream_session_task_limits"]
        if not isinstance(limits, dict):
            limits = {}
            state["stream_session_task_limits"] = limits
        state["stream_force_bottom_session"] = ""
        state["stream_preserve_top_session"] = cleaned_session_name
        state["selected_session_name"] = cleaned_session_name
        ui.run_javascript(
            f"""
            (() => {{
                const scroller = document.querySelector('.cb-agent-stream');
                if (!scroller) return;
                window.__cbStreamLoadOlderAnchor = {{
                    key: {cleaned_session_name!r},
                    scrollHeight: scroller.scrollHeight,
                    scrollTop: scroller.scrollTop,
                    stickToBottom: Math.max(0, scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight) <= 120
                        || Date.now() < Number(window.__cbStreamForceBottomUntil || 0),
                }};
            }})();
            """
        )
        limits[cleaned_session_name] = _stream_session_task_limit(cleaned_session_name) + STREAM_HISTORY_PAGE_SIZE
        _refresh_stream_parts(refresh_composer=False, refresh_messages=True)
        scroll_stream_to_bottom(cleaned_session_name, preserve_top=True)

    def _submit_stream_message(prompt: str, session_name: str, agent_id: str, backend: str) -> bool:
        cleaned_prompt = str(prompt or "").strip()
        if not cleaned_prompt:
            ui.notify(t("ui.web.notify.enter_message", "请输入消息内容"), position="top")
            return False
        cleaned_session_name = session_name.strip() or "default"
        state["selected_session_name"] = cleaned_session_name
        codex_thread_id = codex_thread_id_from_session_name(cleaned_session_name)
        codex_thread = {}
        if codex_thread_id:
            stream_state = _stream_state_snapshot()
            selected_codex_thread = stream_state.get("selected_codex_thread")
            codex_thread = selected_codex_thread if isinstance(selected_codex_thread, dict) else {}
        result = submit_hub_task(
            agent_id=agent_id.strip() or "main",
            prompt=cleaned_prompt,
            session_name=state["selected_session_name"],
            backend=backend.strip(),
            source="stream-web",
            workdir=str(codex_thread.get("cwd") or ""),
            session_id=codex_thread_id,
            images=_stream_pending_image_paths(state["selected_session_name"]),
        )
        if result.ok:
            pending = state["stream_pending_images"]
            if isinstance(pending, dict):
                pending[state["selected_session_name"]] = []
        _notify(result.message)
        return result.ok

    def _open_mobile_url(url: str) -> None:
        cleaned_url = url.strip()
        if not cleaned_url:
            return
        ui.run_javascript(f"window.open({cleaned_url!r}, '_blank')")

    def _select_session(session_name: str) -> None:
        state["selected_session_name"] = session_name
        state["session_page"] = 1
        state["task_page"] = 1
        state["load_session_rows"] = True
        state["load_session_detail"] = False
        state["load_task_list"] = False
        state["load_task_detail"] = False
        content_view.refresh()

    def _select_task(task_id: str, session_name: str = "") -> None:
        state["selected_task_id"] = task_id
        if session_name:
            state["selected_session_name"] = session_name
        state["load_task_list"] = True
        state["load_task_detail"] = False
        content_view.refresh()

    def _set_task_filters(status: str = "", agent: str = "", backend: str = "") -> None:
        state["load_task_list"] = True
        state["selected_task_status"] = status
        state["selected_task_agent"] = agent
        state["selected_task_backend"] = backend
        state["selected_task_id"] = ""
        state["task_page"] = 1
        state["load_task_detail"] = False
        content_view.refresh()

    def _set_session_page(page: int) -> None:
        state["load_session_rows"] = True
        state["session_page"] = max(1, int(page))
        content_view.refresh()

    def _set_task_page(page: int) -> None:
        state["load_task_list"] = True
        state["task_page"] = max(1, int(page))
        content_view.refresh()

    def _set_agent_page(page: int) -> None:
        state["agent_page"] = max(1, int(page))
        content_view.refresh()

    def _set_checks_page(page: int) -> None:
        state["checks_page"] = max(1, int(page))
        content_view.refresh()

    def _find_task_by_id(task_id: str) -> None:
        cleaned_id = task_id.strip()
        if not cleaned_id:
            _notify(t("ui.web.notify.enter_task_id", "请输入 task_id"))
            return
        state["load_task_list"] = True
        model = refresh_model()
        matched = next((task for task in model.tasks if task.task_id == cleaned_id), None)
        if matched is None:
            _notify(t("ui.web.notify.task_not_found", "最近任务中未找到：{task_id}", task_id=cleaned_id))
            return
        state["selected_task_id"] = matched.task_id
        state["selected_session_name"] = matched.session_name
        state["load_session_detail"] = False
        state["load_task_list"] = True
        state["load_task_detail"] = False
        content_view.refresh()

    def _load_selected_session_detail() -> None:
        state["load_session_rows"] = True
        state["load_session_detail"] = True
        content_view.refresh()

    def _load_session_rows() -> None:
        state["load_session_rows"] = True
        content_view.refresh()

    def _load_session_files() -> None:
        state["load_session_rows"] = True
        state["load_session_files"] = True
        content_view.refresh()

    def _load_task_list() -> None:
        state["load_task_list"] = True
        content_view.refresh()

    def _load_selected_task_detail() -> None:
        state["load_task_list"] = True
        state["load_task_detail"] = True
        content_view.refresh()

    def _load_weixin_bindings() -> None:
        state["load_weixin_bindings"] = True
        content_view.refresh()

    def _open_weixin_binding(session_name: str) -> None:
        cleaned_name = session_name.strip()
        if not cleaned_name:
            _notify(t("ui.web.notify.no_binding_session", "当前微信会话没有可定位的会话名"))
            return
        state["active_page"] = "sessions"
        state["selected_session_name"] = cleaned_name
        state["selected_task_id"] = ""
        state["load_session_rows"] = False
        state["load_session_files"] = False
        state["load_session_detail"] = False
        state["load_task_list"] = False
        state["load_task_detail"] = False
        state["load_weixin_bindings"] = True
        jump_to("sessions")

    def _open_weixin_binding_task(task_id: str, session_name: str) -> None:
        cleaned_task_id = task_id.strip()
        if not cleaned_task_id:
            _notify(t("ui.web.notify.no_latest_task", "该发送方还没有最近任务"))
            return
        state["active_page"] = "sessions"
        state["selected_session_name"] = session_name.strip()
        state["selected_task_id"] = cleaned_task_id
        state["load_session_rows"] = True
        state["load_session_files"] = False
        state["load_session_detail"] = False
        state["load_task_list"] = True
        state["load_task_detail"] = False
        state["load_weixin_bindings"] = True
        jump_to("sessions")

    def _switch_weixin_binding_backend(sender_id: str, backend: str) -> None:
        result = switch_weixin_session_backend(sender_id, backend)
        _notify(result.message)

    def _reset_weixin_binding(sender_id: str) -> None:
        result = reset_weixin_conversation(sender_id)
        _notify(result.message)

    def open_sidebar() -> None:
        ui.run_javascript("document.body.classList.toggle('cb-sidebar-open')")

    def close_sidebar() -> None:
        ui.run_javascript("document.body.classList.remove('cb-sidebar-open')")

    def _stream_sidebar_task_limit() -> int:
        try:
            return max(STREAM_SIDEBAR_PAGE_SIZE, int(state.get("stream_sidebar_task_limit") or STREAM_SIDEBAR_PAGE_SIZE))
        except (TypeError, ValueError):
            return STREAM_SIDEBAR_PAGE_SIZE

    def _load_more_sidebar_sessions() -> None:
        state["stream_sidebar_task_limit"] = _stream_sidebar_task_limit() + STREAM_SIDEBAR_PAGE_SIZE
        sidebar_sessions_view.refresh()

    def _load_sidebar_sessions() -> None:
        state["sidebar_content_loaded"] = True
        right_sidebar_view.refresh()

    def _sidebar_codex_threads() -> list[dict[str, object]]:
        threads = state.get("stream_sidebar_codex_threads")
        return [thread for thread in threads if isinstance(thread, dict)] if isinstance(threads, list) else []

    def _load_sidebar_codex_threads() -> None:
        state["stream_sidebar_codex_loaded"] = True
        if bool(state.get("stream_sidebar_codex_done")):
            sidebar_sessions_view.refresh()
            return
        archived = bool(state.get("stream_sidebar_codex_archived"))
        cursor = str(state.get("stream_sidebar_codex_cursor") or "").strip()
        payload = load_codex_threads_page(cursor=cursor, archived=archived)
        error = str(payload.get("error") or "").strip()
        state["stream_sidebar_codex_error"] = error
        if error:
            sidebar_sessions_view.refresh()
            return
        loaded_threads = _sidebar_codex_threads()
        seen_ids = {str(thread.get("id") or thread.get("session_id") or "").strip() for thread in loaded_threads}
        page_threads = payload.get("threads") if isinstance(payload.get("threads"), list) else []
        for thread in page_threads:
            if not isinstance(thread, dict):
                continue
            thread_id = str(thread.get("id") or thread.get("session_id") or "").strip()
            if not thread_id or thread_id in seen_ids:
                continue
            seen_ids.add(thread_id)
            loaded_threads.append(thread)
        state["stream_sidebar_codex_threads"] = loaded_threads
        next_cursor = str(payload.get("next_cursor") or "").strip()
        if next_cursor:
            state["stream_sidebar_codex_cursor"] = next_cursor
        elif archived:
            state["stream_sidebar_codex_done"] = True
            state["stream_sidebar_codex_cursor"] = ""
        else:
            state["stream_sidebar_codex_archived"] = True
            state["stream_sidebar_codex_cursor"] = ""
        sidebar_sessions_view.refresh()

    @ui.refreshable
    def sidebar_navigation_view() -> None:
        with ui.column().classes("w-full gap-2"):
            ui.label(t("ui.web.shell.navigation", "导航")).classes("cb-sidebar-section-title")
            for page in PRIMARY_PAGES:
                active = page.key == state["active_page"]
                props = "color=primary text-color=white unelevated" if active else "outline"
                icon = {
                    "home": "dashboard",
                    "sessions": "forum",
                    "mobile": "qr_code_2",
                    "stream": "dynamic_feed",
                    "diagnostics": "monitor_heart",
                }.get(page.key, "radio_button_unchecked")
                ui.button(
                    page_label(page.key, page.title),
                    on_click=lambda anchor=page.anchor: jump_to(anchor),
                    icon=icon,
                ).props(props).classes(f"cb-nav-button {'cb-nav-active' if active else ''}")

        with ui.column().classes("w-full gap-2"):
            ui.label(t("ui.web.field.language", "语言")).classes("cb-sidebar-section-title")
            ui.toggle(
                {"zh-CN": "中文", "en-US": "English"},
                value=state["language"],
                on_change=lambda event: switch_language(str(event.value or "")),
                clearable=False,
            ).props("unelevated color=dark text-color=white toggle-color=primary toggle-text-color=dark").classes("cb-language-toggle w-full")

        with ui.column().classes("w-full gap-2"):
            ui.label(t("ui.web.field.theme", "主题")).classes("cb-sidebar-section-title")
            ui.toggle(
                {
                    "dark": t("ui.web.theme.dark", "深色"),
                    "light": t("ui.web.theme.light", "浅色"),
                    "forest": t("ui.web.theme.forest", "护眼"),
                },
                value=state["theme"],
                on_change=lambda event: switch_theme(str(event.value or "")),
                clearable=False,
            ).props("unelevated color=dark text-color=white toggle-color=primary toggle-text-color=dark").classes("cb-language-toggle cb-theme-toggle w-full")

    @ui.refreshable
    def sidebar_sessions_view() -> None:
        mobile_state = build_stream_sidebar_state_snapshot(
            task_limit=_stream_sidebar_task_limit(),
            include_codex_threads=False,
        )
        tasks = mobile_state.get("tasks") if isinstance(mobile_state.get("tasks"), list) else []
        visible_tasks = [task for task in tasks if isinstance(task, dict)]
        session_counts = mobile_state.get("session_task_counts") if isinstance(mobile_state.get("session_task_counts"), dict) else {}
        try:
            session_total_count = int(mobile_state.get("session_total_count") or len(session_counts))
        except (TypeError, ValueError):
            session_total_count = len(session_counts)
        sessions: dict[str, list[dict[str, object]]] = {}
        session_order: list[str] = []
        for task in visible_tasks:
            session_name = str(task.get("session_name") or "default")
            if session_name not in sessions:
                sessions[session_name] = []
                session_order.append(session_name)
            sessions[session_name].append(task)
        session_order = _stream_session_order(sessions, session_order)
        qq_current_session = stream_qq_current_session_name()
        if qq_current_session and qq_current_session not in sessions:
            sessions[qq_current_session] = []
            session_counts[qq_current_session] = 0
            session_order.insert(0, qq_current_session)
            session_total_count += 1
        selected_sidebar_session = str(state["selected_session_name"] or "").strip()
        if selected_sidebar_session and not codex_thread_id_from_session_name(selected_sidebar_session) and selected_sidebar_session not in sessions:
            sessions[selected_sidebar_session] = []
            session_order.insert(0, selected_sidebar_session)

        with ui.column().classes("w-full gap-2 min-h-0"):
            with ui.row().classes("w-full items-center justify-between gap-2"):
                ui.label(t("ui.web.mobile.stream_sessions", "会话")).classes("cb-sidebar-section-title")
                ui.button(
                    t("ui.web.mobile.refresh_session_list", "刷新会话列表"),
                    on_click=sidebar_sessions_view.refresh,
                    icon="refresh",
                ).props("flat dense").classes("cb-stream-refresh-session-list-button")
            if not session_order:
                with ui.element("div").classes("cb-panel w-full p-3"):
                    ui.label(t("ui.web.mobile.stream_empty", "暂无任务输出。")).classes("text-sm cb-muted")
            else:
                with ui.column().classes("w-full gap-2 pr-1"):
                    for session_name in session_order:
                        session_items = sessions[session_name]
                        latest = max(
                            session_items,
                            key=_stream_task_order_key,
                        ) if session_items else {}
                        status = str(latest.get("status") or "idle")
                        selected = session_name == state["selected_session_name"] or (
                            not state["selected_session_name"] and session_name == session_order[0]
                        )
                        props = "unelevated" if selected else "outline"
                        with ui.button(
                            "",
                            on_click=lambda session_name=session_name: _open_stream_session(session_name),
                        ).props(f"{props} data-stream-session-link={quote(session_name, safe='')}").classes("w-full cb-stream-task-button"):
                            with ui.column().classes("w-full items-stretch gap-1 text-left"):
                                with ui.row().classes("w-full items-start justify-between gap-2 flex-wrap"):
                                    ui.label(session_name).classes("font-semibold break-all text-left min-w-0 flex-1")
                                    ui.label(t(f"bridge.task.status.{status}", status)).classes(f"{_stream_status_badge_class(status)} flex-shrink-0")
                                ui.label(
                                    t(
                                        "ui.web.mobile.stream_session_meta",
                                        "{count} 轮对话 | Agent: {agent}",
                                        count=str(session_counts.get(session_name, len(session_items))),
                                        agent=str(latest.get("agent_name") or latest.get("agent_id") or t("ui.web.value.unselected", "(未选择)")),
                                    )
                                ).classes("text-xs cb-muted break-all text-left")
                    if len(session_order) < session_total_count:
                        ui.button(
                            t(
                                "ui.web.mobile.load_more_sessions",
                                "加载更多会话 ({shown}/{total})",
                                shown=str(len(session_order)),
                                total=str(session_total_count),
                            ),
                            on_click=_load_more_sidebar_sessions,
                            icon="expand_more",
                        ).props("flat dense").classes("w-full cb-stream-load-older-button")

            codex_threads = _sidebar_codex_threads()
            codex_threads_error = str(state.get("stream_sidebar_codex_error") or "").strip()
            codex_threads_done = bool(state.get("stream_sidebar_codex_done"))
            ui.label(t("ui.web.mobile.codex_threads", "Codex 会话")).classes("cb-sidebar-section-title mt-3")
            if not state.get("stream_sidebar_codex_loaded"):
                ui.button(
                    t("ui.web.mobile.load_codex_threads", "加载 Codex 会话"),
                    on_click=_load_sidebar_codex_threads,
                    icon="sync",
                ).props("outline dense").classes("w-full")
            elif codex_threads_error:
                with ui.element("div").classes("cb-panel w-full p-3"):
                    ui.label(t("ui.web.mobile.codex_threads_error", "Codex 会话读取失败：{error}", error=codex_threads_error)).classes("text-sm cb-muted")
                    ui.button(
                        t("ui.web.mobile.retry_codex_threads", "重新加载 Codex 会话"),
                        on_click=_load_sidebar_codex_threads,
                        icon="refresh",
                    ).props("outline dense").classes("w-full mt-2")
            elif not codex_threads and codex_threads_done:
                with ui.element("div").classes("cb-panel w-full p-3"):
                    ui.label(t("ui.web.mobile.codex_threads_empty", "没有发现 Codex 会话。")).classes("text-sm cb-muted")
            else:
                workspace_groups = group_codex_threads_by_workspace(codex_threads)
                with ui.column().classes("w-full gap-2 pr-1"):
                    for group in workspace_groups:
                        group_threads = group.get("threads") if isinstance(group.get("threads"), list) else []
                        if not group_threads:
                            continue
                        group_project = str(group.get("project") or "").strip()
                        group_cwd = str(group.get("cwd") or "").strip()
                        group_label = group_project or t("ui.web.mobile.codex_workspace_unknown", "未指定工作区")
                        latest_thread = max(
                            (thread for thread in group_threads if isinstance(thread, dict)),
                            key=_codex_thread_updated_key,
                            default={},
                        )
                        latest_at = str(latest_thread.get("updated_at") or "").strip() if isinstance(latest_thread, dict) else ""
                        selected_in_group = any(
                            isinstance(thread, dict)
                            and str(thread.get("session_name") or "").strip() == state["selected_session_name"]
                            for thread in group_threads
                        )
                        details_props = "open" if selected_in_group else ""
                        with ui.element("details").props(details_props).classes("cb-codex-workspace w-full"):
                            with ui.element("summary"):
                                with ui.column().classes("min-w-0 flex-1 gap-1"):
                                    with ui.row().classes("w-full items-start justify-between gap-2 flex-wrap"):
                                        ui.label(group_label).classes("font-semibold break-all text-left")
                                        ui.label(
                                            t(
                                                "ui.web.mobile.codex_workspace_count",
                                                "{count} 个会话",
                                                count=str(len(group_threads)),
                                            )
                                        ).classes("cb-chip cb-chip-ok")
                                    meta = " | ".join(item for item in [group_cwd, latest_at] if item)
                                    if meta:
                                        ui.label(meta).classes("text-xs cb-muted break-all text-left")
                            with ui.column().classes("cb-codex-workspace-body w-full gap-2"):
                                for thread in group_threads:
                                    if not isinstance(thread, dict):
                                        continue
                                    thread_session_name = str(thread.get("session_name") or "").strip()
                                    thread_id = str(thread.get("id") or "").strip()
                                    if not thread_session_name or not thread_id:
                                        continue
                                    selected = thread_session_name == state["selected_session_name"]
                                    props = "unelevated" if selected else "outline"
                                    with ui.button(
                                        "",
                                        on_click=lambda session_name=thread_session_name: _open_stream_session(session_name),
                                    ).props(f"{props} data-stream-session-link={quote(thread_session_name, safe='')}").classes("w-full cb-stream-task-button"):
                                        with ui.column().classes("w-full items-stretch gap-1 text-left"):
                                            ui.label(str(thread.get("title") or thread_id)).classes("font-semibold break-all text-left")
                                            with ui.row().classes("w-full gap-1 items-center flex-wrap"):
                                                if bool(thread.get("archived")):
                                                    ui.label(t("ui.web.mobile.codex_thread_archived", "已归档")).classes("cb-chip cb-chip-warn")
                                                if str(thread.get("branch") or "").strip():
                                                    ui.label(str(thread.get("branch") or "")).classes("cb-chip cb-chip-ok")
                                            updated_at = str(thread.get("updated_at") or "").strip()
                                            ui.label(updated_at or thread_id).classes("text-xs cb-muted break-all text-left")
                    if not codex_threads_done:
                        next_label = (
                            "ui.web.mobile.load_more_codex_threads_archived"
                            if bool(state.get("stream_sidebar_codex_archived"))
                            else "ui.web.mobile.load_more_codex_threads"
                        )
                        next_fallback = "加载更多归档 Codex 会话" if bool(state.get("stream_sidebar_codex_archived")) else "加载更多 Codex 会话"
                        ui.button(
                            t(next_label, next_fallback),
                            on_click=_load_sidebar_codex_threads,
                            icon="expand_more",
                        ).props("flat dense").classes("w-full cb-stream-load-older-button")

    @ui.refreshable
    def right_sidebar_view() -> None:
        current_client = context.client
        if getattr(current_client, "_deleted", False):
            return
        with ui.column().classes("w-full gap-4 p-4 cb-sidebar-content"):
            with ui.row().classes("w-full items-center justify-between gap-2"):
                ui.label(t("ui.web.shell.panel_title", "控制面板")).classes("text-base font-bold cb-ink")
                ui.button("", icon="close").props("flat round dense data-sidebar-close-action=1")
            sidebar_navigation_view()
            with ui.column().classes("w-full gap-2"):
                ui.label(t("ui.web.mobile.new_session_title", "新建会话")).classes("cb-sidebar-section-title")
                new_session_input = ui.input(
                    placeholder=t("ui.web.mobile.new_session_placeholder", "输入新会话名"),
                ).classes("w-full cb-sidebar-new-session-input")
                ui.button(
                    t("ui.web.mobile.new_session_action_short", "新建会话"),
                    on_click=lambda input_box=new_session_input: _open_stream_session_from_input(input_box),
                    icon="add",
                ).props("outline").classes("w-full cb-sidebar-new-session-button")
            if not state.get("sidebar_content_loaded"):
                ui.button(
                    t("ui.web.mobile.load_sidebar_sessions", "加载会话列表"),
                    on_click=_load_sidebar_sessions,
                    icon="forum",
                ).props("outline").classes("w-full")
            else:
                sidebar_sessions_view()

    def _stream_status_badge_class(status: str) -> str:
        if status == "succeeded":
            return "cb-chip cb-chip-ok"
        if status == "failed":
            return "cb-chip cb-chip-danger"
        return "cb-chip cb-chip-warn"

    def _open_stream_session_from_input(input_box) -> None:
        session_name = str(getattr(input_box, "value", "") or "").strip()
        if not session_name:
            ui.notify(t("ui.web.mobile.new_session_name_required", "请输入新会话名"), position="top")
            return
        _open_stream_session(session_name)
        if hasattr(input_box, "set_value"):
            input_box.set_value("")

    def _open_stream_session(session_name: str) -> None:
        cleaned_session_name = str(session_name or "").strip() or "default"
        encoded_session_name = quote(cleaned_session_name, safe="")
        was_stream_page = state["active_page"] == "stream"
        current_session_name = str(state.get("selected_session_name") or "").strip()
        if was_stream_page and current_session_name == cleaned_session_name:
            ui.run_javascript("document.body.classList.remove('cb-sidebar-open')")
            return
        try:
            switch_sequence = int(state.get("stream_switch_sequence") or 0) + 1
        except (TypeError, ValueError):
            switch_sequence = 1
        state["stream_switch_sequence"] = switch_sequence
        state["stream_switch_refresh_pending"] = True
        state["active_page"] = "stream"
        state["selected_session_name"] = cleaned_session_name
        state["session_page"] = 1
        state["task_page"] = 1
        state["load_session_detail"] = False
        state["load_task_detail"] = False
        state["stream_force_bottom_session"] = cleaned_session_name
        state["stream_force_bottom_next"] = True
        ui.run_javascript(
            f"""
            (() => {{
                const url = new URL(window.location.href);
                url.searchParams.set('session', {cleaned_session_name!r});
                window.history.replaceState(null, '', url.toString());
                const panel = document.querySelector('.cb-agent-panel');
                if (panel) panel.dataset.streamKey = {encoded_session_name!r};
                if (window.__cbStreamScrollStateByKey) delete window.__cbStreamScrollStateByKey[{cleaned_session_name!r}];
                window.__cbStreamForceBottomUntil = Date.now() + 1200;
                document.body.classList.remove('cb-sidebar-open');
            }})();
            """
        )
        if was_stream_page:
            def refresh_selected_session() -> None:
                if (
                    str(state.get("selected_session_name") or "").strip() != cleaned_session_name
                    or state.get("stream_switch_sequence") != switch_sequence
                ):
                    return
                try:
                    _refresh_stream_parts()
                finally:
                    if state.get("stream_switch_sequence") == switch_sequence:
                        state["stream_switch_refresh_pending"] = False

            ui.timer(0.01, refresh_selected_session, once=True)
        else:
            sidebar_navigation_view.refresh()
            content_view.refresh()
            if state.get("stream_switch_sequence") == switch_sequence:
                state["stream_switch_refresh_pending"] = False

    def shell_view() -> None:
        with ui.element("div").classes("cb-sidebar-backdrop").on("click", lambda _event: close_sidebar()):
            pass
        with ui.element("aside").classes("cb-sidebar cb-sidebar-shell"):
            right_sidebar_view()
        with ui.page_sticky(position="top-right", x_offset=16, y_offset=16):
            ui.button("", icon="menu").props("round color=primary").classes("cb-sidebar-toggle").on(
                "click",
                js_handler="() => document.body.classList.toggle('cb-sidebar-open')",
            )

    def install_ui_error_logger() -> None:
        ui.run_javascript(
            """
            (() => {
                const sendUiLog = (event, payload = {}) => {
                    try {
                        const body = JSON.stringify({
                            event,
                            url: window.location.href,
                            ...payload,
                        });
                        if (navigator.sendBeacon) {
                            navigator.sendBeacon('/api/ui/stream-log', new Blob([body], { type: 'application/json' }));
                            return;
                        }
                        fetch('/api/ui/stream-log', {
                            method: 'POST',
                            headers: { 'content-type': 'application/json' },
                            body,
                            keepalive: true,
                        }).catch(() => {});
                    } catch {}
                };
                window.__cbUiLog = sendUiLog;
                if (window.__cbUiErrorLogInstalled === '1') return;
                window.__cbUiErrorLogInstalled = '1';
                window.addEventListener('error', (event) => {
                    sendUiLog('browser_error', {
                        message: String(event.message || '').slice(0, 1200),
                        source: String(event.filename || '').slice(0, 400),
                        line: event.lineno || 0,
                        column: event.colno || 0,
                        stack: String(event.error?.stack || '').slice(0, 1200),
                    });
                });
                window.addEventListener('unhandledrejection', (event) => {
                    const reason = event.reason;
                    sendUiLog('browser_unhandledrejection', {
                        message: String(reason?.message || reason || '').slice(0, 1200),
                        stack: String(reason?.stack || '').slice(0, 1200),
                    });
                });
            })();
            """
        )

    def scroll_stream_to_bottom(active_session_name: str = "", *, force_bottom: bool = False, preserve_top: bool = False) -> None:
        active_key = str(active_session_name or "").strip()
        current_client = context.client
        client_key = str(getattr(current_client, "id", id(current_client)))
        installed_clients = state.get("stream_scroll_runtime_clients")
        if not isinstance(installed_clients, set):
            installed_clients = set()
            state["stream_scroll_runtime_clients"] = installed_clients
        patch_options = {
            "activeKey": active_key,
            "forceBottom": bool(force_bottom),
            "preserveTop": bool(preserve_top),
            "labels": {
                "fileLinkTitle": t("ui.web.mobile.copy_file_path", "Copy file path"),
                "imageLightboxOpenLabel": t("ui.web.mobile.open_image_preview", "Open image preview"),
                "imageLightboxCloseLabel": t("ui.web.mobile.close_image_preview", "Close image preview"),
                "copyCodeLabel": t("ui.web.mobile.copy_code", "Copy code"),
                "copiedLabel": t("ui.web.mobile.copied", "Copied"),
            },
        }
        patch_options_json = json.dumps(patch_options, ensure_ascii=False)
        if client_key in installed_clients:
            start_time = time.perf_counter()
            _append_stream_ui_log(
                "scroll_after_patch_start",
                client_id=client_key,
                active_session=active_key,
                installed=True,
                force_bottom=bool(force_bottom),
                preserve_top=bool(preserve_top),
            )
            try:
                ui.run_javascript(f"window.__cbStreamAfterPatch?.({patch_options_json});")
                _append_stream_ui_log(
                    "scroll_after_patch_ok",
                    client_id=client_key,
                    active_session=active_key,
                    installed=True,
                    elapsed_ms=round((time.perf_counter() - start_time) * 1000, 3),
                )
            except Exception as exc:
                _append_stream_ui_log(
                    "scroll_after_patch_error",
                    client_id=client_key,
                    active_session=active_key,
                    installed=True,
                    error=repr(exc),
                    traceback=traceback.format_exc(),
                    elapsed_ms=round((time.perf_counter() - start_time) * 1000, 3),
                )
                raise
            return
        installed_clients.add(client_key)
        script = """
            (() => {
                if (window.__cbStreamPatchRuntimeReady !== '5') {
                window.__cbStreamPatchRuntimeReady = '5';
                window.__cbStreamAfterPatch = (options = {}) => {
                try {
                    if (window.history && 'scrollRestoration' in window.history) {
                        window.history.scrollRestoration = 'manual';
                    }
                } catch {}
                const labels = options.labels || {};
                const decodeStreamKey = (value) => {
                    try {
                        return decodeURIComponent(value || '');
                    } catch {
                        return value || '';
                    }
                };
                const readRenderedActiveKey = () => {
                    const panelKey = decodeStreamKey(document.querySelector('.cb-agent-panel')?.dataset?.streamKey || '').trim();
                    if (panelKey) return panelKey;
                    return (document.querySelector('.cb-agent-titlebar .font-bold')?.textContent || '').trim();
                };
                const requestedActiveKey = String(options.activeKey || '');
                const activeKey = requestedActiveKey || readRenderedActiveKey();
                const forceBottom = options.forceBottom === true;
                const preserveTop = options.preserveTop === true;
                if (forceBottom) {
                    window.__cbStreamForceBottomUntil = Date.now() + 1200;
                    if (window.__cbStreamScrollStateByKey) delete window.__cbStreamScrollStateByKey[activeKey];
                }
                window.__cbStreamDesiredActiveKey = activeKey;
                const nearBottomLimit = 120;
                const nearHistoryStartLimit = 96;
                const userScrollAwayLimit = 0;
                const fileLinkTitle = labels.fileLinkTitle || 'Copy file path';
                const imageLightboxOpenLabel = labels.imageLightboxOpenLabel || 'Open image preview';
                const imageLightboxCloseLabel = labels.imageLightboxCloseLabel || 'Close image preview';
                const copyCodeLabel = labels.copyCodeLabel || 'Copy code';
                const copiedLabel = labels.copiedLabel || 'Copied';
                const reasoningStorageKey = 'cb_reasoning_open_state';
                const reasoningOpenState = window.__cbReasoningOpenState || (() => {
                    try {
                        return JSON.parse(localStorage.getItem(reasoningStorageKey) || '{}') || {};
                    } catch {
                        return {};
                    }
                })();
                window.__cbReasoningOpenState = reasoningOpenState;
                const persistReasoningOpenState = () => {
                    try {
                        localStorage.setItem(reasoningStorageKey, JSON.stringify(reasoningOpenState));
                    } catch {}
                };
                const setupReasoningDisclosures = () => {
                    document.querySelectorAll('.cb-stream-reasoning[data-reasoning-key]').forEach((details) => {
                        const key = details.getAttribute('data-reasoning-key') || '';
                        if (!key) return;
                        if (reasoningOpenState[key] === true) details.open = true;
                        if (details.dataset.cbReasoningReady === '1') return;
                        details.dataset.cbReasoningReady = '1';
                        const summary = details.querySelector('summary');
                        summary?.addEventListener('click', () => {
                            reasoningOpenState[key] = !details.open;
                            persistReasoningOpenState();
                        });
                    });
                };
                const commandStorageKey = 'cb_command_open_state';
                const commandOpenState = window.__cbCommandOpenState || (() => {
                    try {
                        return JSON.parse(localStorage.getItem(commandStorageKey) || '{}') || {};
                    } catch {
                        return {};
                    }
                })();
                window.__cbCommandOpenState = commandOpenState;
                const persistCommandOpenState = () => {
                    try {
                        localStorage.setItem(commandStorageKey, JSON.stringify(commandOpenState));
                    } catch {}
                };
                const setupCommandDisclosures = () => {
                    document.querySelectorAll('.cb-stream-command[data-command-key]').forEach((details) => {
                        const key = details.getAttribute('data-command-key') || '';
                        if (!key) return;
                        if (commandOpenState[key] === true) details.open = true;
                        if (details.dataset.cbCommandReady === '1') return;
                        details.dataset.cbCommandReady = '1';
                        const summary = details.querySelector('summary');
                        summary?.addEventListener('click', () => {
                            commandOpenState[key] = !details.open;
                            persistCommandOpenState();
                        });
                    });
                };
                const readDelta = (scroller) => Math.max(0, scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight);
                const readScrollTop = (scroller) => Math.max(0, Number(scroller.scrollTop) || 0);
                const maxScrollTop = (scroller) => Math.max(0, scroller.scrollHeight - scroller.clientHeight);
                const clampScrollTop = (scroller, value) => Math.min(maxScrollTop(scroller), Math.max(0, Number(value) || 0));
                const programmaticScrollers = window.__cbStreamProgrammaticScrollers || new WeakSet();
                window.__cbStreamProgrammaticScrollers = programmaticScrollers;
                const markProgrammaticScroll = (scroller) => {
                    if (!scroller) return;
                    programmaticScrollers.add(scroller);
                    window.requestAnimationFrame(() => programmaticScrollers.delete(scroller));
                };
                const acceptUserScroll = (scroller) => {
                    programmaticScrollers.delete(scroller);
                };
                const isProgrammaticScroll = (scroller) => programmaticScrollers.has(scroller);
                const scrollWindowToBottom = () => {
                    const height = Math.max(
                        document.body?.scrollHeight || 0,
                        document.documentElement?.scrollHeight || 0,
                    );
                    window.scrollTo(0, height);
                };
                const scrollToBottom = (scroller) => {
                    markProgrammaticScroll(scroller);
                    scroller.scrollTop = scroller.scrollHeight;
                    scroller.scrollTop = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
                    scrollWindowToBottom();
                };
                window.__cbStreamScrollStateByKey = window.__cbStreamScrollStateByKey || {};
                const scrollStateFor = (key) => {
                    const safeKey = key || '';
                    const states = window.__cbStreamScrollStateByKey;
                    if (!states[safeKey]) {
                        states[safeKey] = {
                            delta: 0,
                            top: 0,
                            nearBottom: true,
                            userScrolledAway: false,
                            restoreTopPending: false,
                        };
                    }
                    return states[safeKey];
                };
                const updateScrollState = (scroller, source = 'script', keepSavedTop = false) => {
                    const key = window.__cbStreamActiveKey || window.__cbStreamDesiredActiveKey || activeKey || readRenderedActiveKey();
                    const delta = readDelta(scroller);
                    const state = scrollStateFor(key);
                    state.delta = delta;
                    state.nearBottom = delta <= nearBottomLimit;
                    window.__cbStreamScrollDelta = delta;
                    window.__cbStreamWasNearBottom = state.nearBottom;
                    if (source === 'user' && isProgrammaticScroll(scroller)) {
                        source = 'script';
                    }
                    if ((!keepSavedTop && !state.restoreTopPending) || source === 'user') {
                        state.top = readScrollTop(scroller);
                        state.restoreTopPending = false;
                    }
                    if (source === 'user') {
                        window.__cbStreamForceBottomUntil = 0;
                        state.userScrolledAway = delta > userScrollAwayLimit;
                        window.__cbStreamUserScrolledAway = state.userScrolledAway;
                    } else {
                        window.__cbStreamUserScrolledAway = state.userScrolledAway;
                    }
                    const button = document.querySelector('.cb-scroll-bottom-button');
                    if (button) {
                        button.classList.toggle('cb-scroll-bottom-button-visible', delta > nearBottomLimit);
                    }
                };
                const revealPositionedStream = () => {
                    document.querySelector('.cb-agent-panel')?.removeAttribute('data-stream-pending');
                    document.querySelector('.cb-agent-stream')?.removeAttribute('data-stream-pending');
                };
                const setupFooterLabelReveal = () => {
                    if (window.__cbStreamFooterRevealDelegateReady === '1') return;
                    window.__cbStreamFooterRevealDelegateReady = '1';
                    const reveal = (wrap) => {
                        if (wrap.dataset.cbFooterRevealTimer) {
                            window.clearTimeout(Number(wrap.dataset.cbFooterRevealTimer));
                        }
                        wrap.classList.add('cb-stream-footer-label-revealed');
                        const timer = window.setTimeout(() => {
                            wrap.classList.remove('cb-stream-footer-label-revealed');
                            delete wrap.dataset.cbFooterRevealTimer;
                        }, 3000);
                        wrap.dataset.cbFooterRevealTimer = String(timer);
                    };
                    document.addEventListener('click', (event) => {
                        const wrap = event.target?.closest?.('.cb-stream-footer-label-wrap[data-footer-toggle="1"]');
                        if (!wrap) return;
                        event.preventDefault();
                        event.stopPropagation();
                        reveal(wrap);
                    }, true);
                    document.addEventListener('keydown', (event) => {
                        if (event.key !== 'Enter' && event.key !== ' ') return;
                        const wrap = event.target?.closest?.('.cb-stream-footer-label-wrap[data-footer-toggle="1"]');
                        if (!wrap) return;
                        event.preventDefault();
                        event.stopPropagation();
                        reveal(wrap);
                    }, true);
                };
                const formatElapsed = (totalSeconds) => {
                    const safeSeconds = Math.max(0, Math.floor(totalSeconds));
                    const hours = Math.floor(safeSeconds / 3600);
                    const minutes = Math.floor((safeSeconds % 3600) / 60);
                    const seconds = safeSeconds % 60;
                    const pad = (value) => String(value).padStart(2, '0');
                    if (hours > 0) return `${hours}:${pad(minutes)}:${pad(seconds)}`;
                    return `${minutes}:${pad(seconds)}`;
                };
                const updateLiveElapsed = () => {
                    document.querySelectorAll('.cb-stream-live-elapsed[data-started-at]').forEach((node) => {
                        const startedAt = Date.parse(node.dataset.startedAt || '');
                        if (!Number.isFinite(startedAt)) return;
                        node.textContent = formatElapsed((Date.now() - startedAt) / 1000);
                    });
                };
                const setupCopyFeedback = () => {
                    if (window.__cbStreamCopyFeedbackDelegateReady === '1') return;
                    window.__cbStreamCopyFeedbackDelegateReady = '1';
                    document.addEventListener('click', (event) => {
                        const button = event.target?.closest?.('.cb-stream-copy-button');
                        if (!button) return;
                        const icon = button.querySelector('.q-icon, i');
                        const originalIcon = icon?.textContent || 'content_copy';
                        if (button.dataset.cbCopyFeedbackTimer) {
                            window.clearTimeout(Number(button.dataset.cbCopyFeedbackTimer));
                        }
                        button.classList.add('cb-stream-copy-button-copied');
                        if (icon) icon.textContent = 'check';
                        const timer = window.setTimeout(() => {
                            button.classList.remove('cb-stream-copy-button-copied');
                            if (icon) icon.textContent = originalIcon;
                            delete button.dataset.cbCopyFeedbackTimer;
                        }, 1500);
                        button.dataset.cbCopyFeedbackTimer = String(timer);
                    }, true);
                };
                const setupFileLinkFeedback = () => {
                    const normalizeFileHref = (value) => {
                        const href = value || '';
                        if (/^[A-Za-z]:[\\\\/]/.test(href)) return href;
                        if (/^\\/[A-Za-z]:[\\\\/]/.test(href)) return href.slice(1);
                        if (href.toLowerCase().startsWith('file://')) {
                            try {
                                const url = new URL(href);
                                const decoded = decodeURIComponent(url.pathname || '');
                                return /^\\/[A-Za-z]:[\\\\/]/.test(decoded) ? decoded.slice(1) : decoded;
                            } catch {
                                return href;
                            }
                        }
                        return href;
                    };
                    const readFilePath = (anchor) => {
                        const href = anchor.getAttribute('href') || '';
                        if (href.startsWith('#chatbridge-file=')) {
                            const encoded = href.slice('#chatbridge-file='.length);
                            try {
                                return normalizeFileHref(decodeURIComponent(encoded));
                            } catch {
                                return normalizeFileHref(encoded);
                            }
                        }
                        const normalized = normalizeFileHref(href);
                        return normalized === href && !/^[A-Za-z]:[\\\\/]/.test(href) && !/^\\/[A-Za-z]:[\\\\/]/.test(href) && !href.toLowerCase().startsWith('file://') ? '' : normalized;
                    };
                    const markCopied = (anchor) => {
                        if (anchor.dataset.cbFileLinkTimer) {
                            window.clearTimeout(Number(anchor.dataset.cbFileLinkTimer));
                        }
                        anchor.classList.add('cb-stream-file-link-copied');
                        const timer = window.setTimeout(() => {
                            anchor.classList.remove('cb-stream-file-link-copied');
                            delete anchor.dataset.cbFileLinkTimer;
                        }, 1400);
                        anchor.dataset.cbFileLinkTimer = String(timer);
                    };
                    const copyAnchorPath = (anchor, event) => {
                        const path = readFilePath(anchor);
                        if (!path) return;
                        event.preventDefault();
                        event.stopPropagation();
                        const fallbackCopy = () => {
                            const textarea = document.createElement('textarea');
                            textarea.value = path;
                            textarea.setAttribute('readonly', 'true');
                            textarea.style.position = 'fixed';
                            textarea.style.opacity = '0';
                            textarea.style.pointerEvents = 'none';
                            document.body.appendChild(textarea);
                            textarea.select();
                            try {
                                document.execCommand('copy');
                            } finally {
                                textarea.remove();
                            }
                        };
                        if (navigator.clipboard?.writeText) {
                            navigator.clipboard.writeText(path).catch(fallbackCopy);
                        } else {
                            fallbackCopy();
                        }
                        window.__cbLastCopiedFilePath = path;
                        document.body.dataset.cbLastCopiedFilePath = path;
                        markCopied(anchor);
                    };
                    if (window.__cbStreamFileLinkDelegateReady !== '1') {
                        window.__cbStreamFileLinkDelegateReady = '1';
                        document.addEventListener('click', (event) => {
                            const anchor = event.target?.closest?.('.cb-stream-markdown a[href^="#chatbridge-file="]');
                            if (!anchor) return;
                            copyAnchorPath(anchor, event);
                        }, true);
                        document.addEventListener('auxclick', (event) => {
                            const anchor = event.target?.closest?.('.cb-stream-markdown a[href^="#chatbridge-file="]');
                            if (!anchor) return;
                            event.preventDefault();
                            event.stopPropagation();
                        }, true);
                        document.addEventListener('keydown', (event) => {
                            if (event.key !== 'Enter' && event.key !== ' ') return;
                            const anchor = event.target?.closest?.('.cb-stream-markdown a[href^="#chatbridge-file="]');
                            if (!anchor) return;
                            copyAnchorPath(anchor, event);
                        }, true);
                    }
                };
                    const setupImageLightbox = () => {
                        const decodeAttr = (value) => {
                            try {
                                return decodeURIComponent(value || '');
                            } catch {
                                return value || '';
                            }
                        };
                        const assignLightboxKeys = () => {
                            const keyBySource = new Map();
                            let nextKey = 0;
                            document.querySelectorAll('.cb-stream-image-lightbox-trigger[data-lightbox-src]').forEach((trigger) => {
                                const source = trigger.getAttribute('data-lightbox-src') || '';
                                if (!source) return;
                                if (!keyBySource.has(source)) {
                                    keyBySource.set(source, `lb-${nextKey}`);
                                    nextKey += 1;
                                }
                                trigger.setAttribute('data-lightbox-key', keyBySource.get(source));
                            });
                        };
                        const prepareMarkdownImages = () => {
                            document.querySelectorAll('.cb-stream-markdown img').forEach((image) => {
                                const source = image.getAttribute('src') || '';
                                if (!source || image.dataset.cbLightboxReady === '1') return;
                                const label = image.getAttribute('alt') || source.split('/').pop() || '';
                                image.dataset.cbLightboxReady = '1';
                                image.classList.add('cb-stream-image-lightbox-trigger');
                                image.setAttribute('data-lightbox-src', encodeURIComponent(source));
                                image.setAttribute('data-lightbox-label', encodeURIComponent(label));
                                image.setAttribute('role', 'button');
                                image.setAttribute('tabindex', '0');
                                image.setAttribute('title', imageLightboxOpenLabel);
                            });
                        };
                        const prepareLightboxTriggers = () => {
                            document.querySelectorAll('.cb-stream-image-lightbox-trigger[data-lightbox-src]').forEach((trigger) => {
                                const source = trigger.getAttribute('data-lightbox-src') || '';
                                const label = trigger.getAttribute('data-lightbox-label') || '';
                                trigger.querySelectorAll?.('img')?.forEach((image) => {
                                    if (image.dataset.cbLightboxReady === '1') return;
                                    image.dataset.cbLightboxReady = '1';
                                    image.classList.add('cb-stream-image-lightbox-trigger');
                                    image.setAttribute('data-lightbox-src', source);
                                    image.setAttribute('data-lightbox-label', label);
                                    image.setAttribute('role', 'button');
                                    image.setAttribute('tabindex', '0');
                                    image.setAttribute('title', imageLightboxOpenLabel);
                                });
                            });
                            assignLightboxKeys();
                        };
                        const ensureLightbox = () => {
                        let overlay = document.querySelector('.cb-image-lightbox');
                        if (overlay) return overlay;
                        overlay = document.createElement('div');
                        overlay.className = 'cb-image-lightbox';
                        overlay.setAttribute('role', 'dialog');
                        overlay.setAttribute('aria-modal', 'true');
                        overlay.innerHTML = `
                            <div class="cb-image-lightbox-panel">
                                <button type="button" class="cb-image-lightbox-close" aria-label="${imageLightboxCloseLabel}" title="${imageLightboxCloseLabel}">X</button>
                                <button type="button" class="cb-image-lightbox-nav cb-image-lightbox-nav-prev" data-lightbox-nav="prev" aria-label="Previous image" title="Previous image">&lsaquo;</button>
                                <button type="button" class="cb-image-lightbox-nav cb-image-lightbox-nav-next" data-lightbox-nav="next" aria-label="Next image" title="Next image">&rsaquo;</button>
                                <div class="cb-image-lightbox-stage">
                                    <img class="cb-image-lightbox-image" alt="">
                                </div>
                                <div class="cb-image-lightbox-caption"></div>
                                <div class="cb-image-lightbox-controls">
                                    <button type="button" class="cb-image-lightbox-control" data-lightbox-zoom="out" aria-label="Zoom out" title="Zoom out">-</button>
                                    <button type="button" class="cb-image-lightbox-control" data-lightbox-zoom="reset" aria-label="Reset zoom" title="Reset zoom">1:1</button>
                                    <button type="button" class="cb-image-lightbox-control" data-lightbox-zoom="in" aria-label="Zoom in" title="Zoom in">+</button>
                                </div>
                            </div>
                        `;
                        document.body.appendChild(overlay);
                        const state = {
                            scale: 1,
                            x: 0,
                            y: 0,
                            pointers: new Map(),
                            dragStart: null,
                            pinchStart: null,
                            swipeStart: null,
                            swipeLast: null,
                            currentIndex: -1,
                        };
                        overlay.__cbLightboxState = state;
                        const image = overlay.querySelector('.cb-image-lightbox-image');
                        const stage = overlay.querySelector('.cb-image-lightbox-stage');
                        const caption = overlay.querySelector('.cb-image-lightbox-caption');
                        const lightboxItems = () => {
                            assignLightboxKeys();
                            return Array.from(document.querySelectorAll('.cb-stream-image-lightbox-trigger[data-lightbox-src]'))
                                .map((trigger) => ({
                                    key: trigger.getAttribute('data-lightbox-key') || '',
                                    source: decodeAttr(trigger.getAttribute('data-lightbox-src') || ''),
                                    label: decodeAttr(trigger.getAttribute('data-lightbox-label') || ''),
                                    trigger,
                                }))
                                .filter((item) => item.key && item.source)
                                .filter((item, index, items) => items.findIndex((candidate) => candidate.key === item.key) === index);
                        };
                        const apply = () => {
                            if (!image) return;
                            image.style.setProperty('--cb-lightbox-scale', String(state.scale));
                            image.style.setProperty('--cb-lightbox-x', `${state.x}px`);
                            image.style.setProperty('--cb-lightbox-y', `${state.y}px`);
                        };
                        const reset = () => {
                            state.scale = 1;
                            state.x = 0;
                            state.y = 0;
                            state.swipeStart = null;
                            state.swipeLast = null;
                            apply();
                        };
                        const setScale = (nextScale, centerX = 0, centerY = 0) => {
                            const previous = state.scale || 1;
                            const scale = Math.max(1, Math.min(6, nextScale));
                            if (scale === previous) return;
                            if (scale === 1) {
                                state.scale = 1;
                                state.x = 0;
                                state.y = 0;
                                apply();
                                return;
                            }
                            const ratio = scale / previous;
                            state.x = (state.x - centerX) * ratio + centerX;
                            state.y = (state.y - centerY) * ratio + centerY;
                            state.scale = scale;
                            apply();
                        };
                        const openAt = (index) => {
                            const items = lightboxItems();
                            if (!items.length) return;
                            const safeIndex = ((index % items.length) + items.length) % items.length;
                            const item = items[safeIndex];
                            state.currentIndex = safeIndex;
                            reset();
                            if (image) {
                                image.setAttribute('src', item.source);
                                image.setAttribute('alt', item.label);
                            }
                            if (caption) {
                                caption.textContent = item.label;
                            }
                            overlay.classList.add('cb-image-lightbox-open');
                        };
                        const moveBy = (delta) => {
                            const items = lightboxItems();
                            if (items.length < 2) return;
                            openAt((state.currentIndex < 0 ? 0 : state.currentIndex) + delta);
                        };
                        const openTrigger = (trigger) => {
                            assignLightboxKeys();
                            const key = trigger?.getAttribute?.('data-lightbox-key') || '';
                            const items = lightboxItems();
                            const index = items.findIndex((item) => item.key === key);
                            openAt(index >= 0 ? index : 0);
                        };
                        overlay.__cbLightboxOpenAt = openAt;
                        overlay.__cbLightboxOpenTrigger = openTrigger;
                        overlay.__cbLightboxMoveBy = moveBy;
                        overlay.__cbLightboxReset = reset;
                        if (stage) {
                            stage.addEventListener('wheel', (event) => {
                                event.preventDefault();
                                const rect = stage.getBoundingClientRect();
                                setScale(state.scale * (event.deltaY < 0 ? 1.18 : 0.84), event.clientX - rect.left - rect.width / 2, event.clientY - rect.top - rect.height / 2);
                            }, { passive: false });
                            stage.addEventListener('dblclick', (event) => {
                                event.preventDefault();
                                const rect = stage.getBoundingClientRect();
                                setScale(state.scale > 1 ? 1 : 2.5, event.clientX - rect.left - rect.width / 2, event.clientY - rect.top - rect.height / 2);
                            });
                            stage.addEventListener('pointerdown', (event) => {
                                stage.setPointerCapture?.(event.pointerId);
                                state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
                                if (state.pointers.size === 1) {
                                    state.dragStart = { x: event.clientX, y: event.clientY, baseX: state.x, baseY: state.y };
                                    state.swipeStart = { x: event.clientX, y: event.clientY };
                                    state.swipeLast = { x: event.clientX, y: event.clientY };
                                } else if (state.pointers.size === 2) {
                                    state.swipeStart = null;
                                    state.swipeLast = null;
                                    const points = Array.from(state.pointers.values());
                                    state.pinchStart = {
                                        distance: Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y) || 1,
                                        scale: state.scale,
                                    };
                                }
                            });
                            stage.addEventListener('pointermove', (event) => {
                                if (!state.pointers.has(event.pointerId)) return;
                                state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
                                if (state.pointers.size === 1 && state.swipeStart) {
                                    state.swipeLast = { x: event.clientX, y: event.clientY };
                                }
                                if (state.pointers.size === 2 && state.pinchStart) {
                                    const points = Array.from(state.pointers.values());
                                    const distance = Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y) || 1;
                                    setScale(state.pinchStart.scale * distance / state.pinchStart.distance);
                                    return;
                                }
                                if (state.scale <= 1 || !state.dragStart) return;
                                state.x = state.dragStart.baseX + event.clientX - state.dragStart.x;
                                state.y = state.dragStart.baseY + event.clientY - state.dragStart.y;
                                apply();
                            });
                            const endPointer = (event) => {
                                if (state.scale <= 1 && state.swipeStart && state.pointers.size === 1) {
                                    const point = state.swipeLast || state.pointers.get(event.pointerId) || { x: event.clientX, y: event.clientY };
                                    const dx = point.x - state.swipeStart.x;
                                    const dy = point.y - state.swipeStart.y;
                                    if (Math.abs(dx) >= 54 && Math.abs(dx) > Math.abs(dy) * 1.35) {
                                        event.preventDefault();
                                        moveBy(dx < 0 ? 1 : -1);
                                    }
                                }
                                state.pointers.delete(event.pointerId);
                                state.dragStart = null;
                                state.pinchStart = null;
                                state.swipeStart = null;
                                state.swipeLast = null;
                                if (state.pointers.size === 1) {
                                    const point = Array.from(state.pointers.values())[0];
                                    state.dragStart = { x: point.x, y: point.y, baseX: state.x, baseY: state.y };
                                    state.swipeStart = { x: point.x, y: point.y };
                                    state.swipeLast = { x: point.x, y: point.y };
                                }
                            };
                            stage.addEventListener('pointerup', endPointer);
                            stage.addEventListener('pointercancel', endPointer);
                        }
                        const close = () => {
                            overlay.classList.remove('cb-image-lightbox-open');
                            const image = overlay.querySelector('.cb-image-lightbox-image');
                            if (image) image.removeAttribute('src');
                            overlay.__cbLightboxReset?.();
                            overlay.__cbLightboxState?.pointers?.clear?.();
                        };
                        overlay.addEventListener('click', (event) => {
                            if (event.target === overlay || event.target?.closest?.('.cb-image-lightbox-close')) {
                                close();
                                return;
                            }
                            const zoom = event.target?.closest?.('[data-lightbox-zoom]')?.getAttribute('data-lightbox-zoom');
                            const nav = event.target?.closest?.('[data-lightbox-nav]')?.getAttribute('data-lightbox-nav');
                            if (nav === 'next') {
                                moveBy(1);
                                return;
                            }
                            if (nav === 'prev') {
                                moveBy(-1);
                                return;
                            }
                            if (zoom === 'in') {
                                setScale(state.scale * 1.35);
                            } else if (zoom === 'out') {
                                setScale(state.scale / 1.35);
                            } else if (zoom === 'reset') {
                                reset();
                            }
                        });
                        const preventBrowserGesture = (event) => {
                            if (overlay.classList.contains('cb-image-lightbox-open')) {
                                event.preventDefault();
                            }
                        };
                        overlay.addEventListener('touchmove', preventBrowserGesture, { passive: false });
                        overlay.addEventListener('gesturestart', preventBrowserGesture, { passive: false });
                        overlay.addEventListener('gesturechange', preventBrowserGesture, { passive: false });
                        document.addEventListener('keydown', (event) => {
                            if (!overlay.classList.contains('cb-image-lightbox-open')) return;
                            if (event.key === 'Escape') {
                                close();
                            } else if (event.key === 'ArrowRight') {
                                event.preventDefault();
                                moveBy(1);
                            } else if (event.key === 'ArrowLeft') {
                                event.preventDefault();
                                moveBy(-1);
                            }
                        });
                        return overlay;
                    };
                        if (window.__cbImageLightboxDelegateReady !== '1') {
                            window.__cbImageLightboxDelegateReady = '1';
                        const openLightbox = (event) => {
                            const trigger = event.target?.closest?.('.cb-stream-image-lightbox-trigger[data-lightbox-src]');
                            if (!trigger) return;
                            event.preventDefault();
                            event.stopPropagation();
                            const source = decodeAttr(trigger.getAttribute('data-lightbox-src') || '');
                            if (!source) return;
                            const overlay = ensureLightbox();
                            overlay.__cbLightboxOpenTrigger?.(trigger);
                        };
                        document.addEventListener('click', openLightbox, true);
                        document.addEventListener('pointerup', openLightbox, true);
                        document.addEventListener('keydown', (event) => {
                            if (event.key !== 'Enter' && event.key !== ' ') return;
                            openLightbox(event);
                        }, true);
                    }
                    prepareMarkdownImages();
                    prepareLightboxTriggers();
                };
                const setupCodeBlockCopy = () => {
                    if (window.__cbStreamCodeCopyDelegateReady === '1') return;
                    window.__cbStreamCodeCopyDelegateReady = '1';
                    const ensureCodeCopyButton = (pre) => {
                        if (!pre || pre.dataset.cbCodeCopyReady === '1') return null;
                        pre.dataset.cbCodeCopyReady = '1';
                        const button = document.createElement('button');
                        button.type = 'button';
                        button.className = 'cb-stream-code-copy-button';
                        button.textContent = 'content_copy';
                        button.setAttribute('aria-label', copyCodeLabel);
                        button.setAttribute('title', copyCodeLabel);
                        pre.appendChild(button);
                        return button;
                    };
                    const copyPreCode = (pre, button) => {
                        const code = pre.querySelector('code');
                        const copyValue = code
                            ? code.textContent || ''
                            : Array.from(pre.childNodes)
                                .filter((node) => node !== button)
                                .map((node) => node.textContent || '')
                                .join('');
                        const fallbackCopy = () => {
                            const textarea = document.createElement('textarea');
                            textarea.value = copyValue;
                            textarea.setAttribute('readonly', 'true');
                            textarea.style.position = 'fixed';
                            textarea.style.opacity = '0';
                            textarea.style.pointerEvents = 'none';
                            document.body.appendChild(textarea);
                            textarea.select();
                            try {
                                document.execCommand('copy');
                            } finally {
                                textarea.remove();
                            }
                        };
                        if (navigator.clipboard?.writeText) {
                            navigator.clipboard.writeText(copyValue).catch(fallbackCopy);
                        } else {
                            fallbackCopy();
                        }
                        if (button.dataset.cbCodeCopyTimer) {
                            window.clearTimeout(Number(button.dataset.cbCodeCopyTimer));
                        }
                        button.textContent = 'check';
                        button.classList.add('cb-stream-code-copy-button-copied');
                        button.setAttribute('aria-label', copiedLabel);
                        button.setAttribute('title', copiedLabel);
                        const timer = window.setTimeout(() => {
                            button.textContent = 'content_copy';
                            button.classList.remove('cb-stream-code-copy-button-copied');
                            button.setAttribute('aria-label', copyCodeLabel);
                            button.setAttribute('title', copyCodeLabel);
                            delete button.dataset.cbCodeCopyTimer;
                        }, 1500);
                        button.dataset.cbCodeCopyTimer = String(timer);
                    };
                    const ensureFromEvent = (event) => {
                        const pre = event.target?.closest?.('.cb-stream-markdown pre');
                        if (!pre) return;
                        ensureCodeCopyButton(pre);
                    };
                    document.addEventListener('pointerover', ensureFromEvent, true);
                    document.addEventListener('pointerdown', ensureFromEvent, true);
                    document.addEventListener('focusin', ensureFromEvent, true);
                    document.addEventListener('click', (event) => {
                        const button = event.target?.closest?.('.cb-stream-code-copy-button');
                        if (!button) return;
                        const pre = button.closest('.cb-stream-markdown pre');
                        if (!pre) return;
                        event.preventDefault();
                        event.stopPropagation();
                        copyPreCode(pre, button);
                    }, true);
                };
                const setupComposerSubmit = () => {
                    document.querySelectorAll('.cb-composer-box').forEach((composer) => {
                        const textarea = composer.querySelector('.cb-composer-input textarea');
                            const sendButton = composer.querySelector('.cb-composer-send-button[data-composer-mode="send"]');
                            if (!textarea || !sendButton || textarea.dataset.cbComposerSubmitReady === '1') return;
                            textarea.dataset.cbComposerSubmitReady = '1';
                            const updateSendState = () => {
                                const hasText = textarea.value.trim().length > 0;
                                sendButton.disabled = !hasText;
                                sendButton.setAttribute('aria-disabled', hasText ? 'false' : 'true');
                                sendButton.classList.toggle('cb-composer-send-disabled', !hasText);
                                sendButton.classList.toggle('cb-composer-send-ready', hasText);
                        };
                        textarea.addEventListener('input', updateSendState);
                        textarea.addEventListener('change', updateSendState);
                        textarea.addEventListener('keydown', (event) => {
                            if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
                            event.preventDefault();
                            if (textarea.value.trim().length === 0) return;
                            sendButton.click();
                        });
                        updateSendState();
                    });
                };
                const setupComposerUploadPanel = () => {
                    const panel = document.querySelector('.cb-composer-upload-panel');
                    const button = document.querySelector('.cb-composer-upload-button');
                    if (!panel || !button) return;
                    const open = window.__cbComposerUploadOpen === true;
                    panel.classList.toggle('cb-composer-upload-panel-hidden', !open);
                    const icon = button.querySelector('.q-icon, i');
                    if (icon) icon.textContent = open ? 'close' : 'add';
                };
                const updateComposerMetrics = () => {
                    const composerZone = document.querySelector('.cb-composer-zone');
                    const height = composerZone ? Math.ceil(composerZone.getBoundingClientRect().height) : 0;
                    document.documentElement.style.setProperty('--cb-composer-height', `${height}px`);
                };
                const liveTextByKey = window.__cbStreamLiveTextByKey || new Map();
                window.__cbStreamLiveTextByKey = liveTextByKey;
                const collectLiveTextNodes = (root) => {
                    const nodes = [];
                    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
                        acceptNode: (node) => {
                            const parent = node.parentElement;
                            if (!parent || parent.closest('.cb-stream-code-copy-button')) {
                                return NodeFilter.FILTER_REJECT;
                            }
                            return NodeFilter.FILTER_ACCEPT;
                        },
                    });
                    while (walker.nextNode()) {
                        nodes.push({ node: walker.currentNode, text: walker.currentNode.nodeValue || '' });
                    }
                    return nodes;
                };
                const setLiveTextPrefix = (segments, length) => {
                    let remaining = Math.max(0, length);
                    for (const segment of segments) {
                        const next = segment.text.slice(0, remaining);
                        if (segment.node.nodeValue !== next) {
                            segment.node.nodeValue = next;
                        }
                        remaining -= segment.text.length;
                        if (remaining < 0) remaining = 0;
                    }
                };
                const animateLiveText = (element, key, fullText, fromLength, placeholder) => {
                    const previous = liveTextByKey.get(key) || {};
                    if (previous.timer) {
                        window.clearInterval(previous.timer);
                    }
                    const state = {
                        text: fullText.slice(0, fromLength),
                        target: fullText,
                        element,
                        placeholder,
                        timer: 0,
                    };
                    liveTextByKey.set(key, state);
                    element.dataset.streamFullText = fullText;
                    const segments = collectLiveTextNodes(element);
                    let cursor = Math.max(0, Math.min(fromLength, fullText.length));
                    setLiveTextPrefix(segments, cursor);
                    const step = () => {
                        cursor = Math.min(fullText.length, cursor + Math.max(1, Math.ceil((fullText.length - cursor) / 240)));
                        setLiveTextPrefix(segments, cursor);
                        state.text = fullText.slice(0, cursor);
                        if (cursor >= fullText.length) {
                            window.clearInterval(state.timer);
                            state.timer = 0;
                            state.text = fullText;
                        }
                    };
                    state.timer = window.setInterval(step, 16);
                    step();
                };
                let liveTextScheduled = false;
                const syncLiveText = () => {
                    liveTextScheduled = false;
                    const seen = new Set();
                    document.querySelectorAll('[data-stream-live="1"][data-stream-text-key]').forEach((element) => {
                        const key = element.getAttribute('data-stream-text-key') || '';
                        if (!key) return;
                        seen.add(key);
                        const storedFullText = element.dataset.streamFullText || '';
                        const domText = element.textContent || '';
                        const fullText = storedFullText && (domText.length < storedFullText.length || storedFullText.startsWith(domText)) ? storedFullText : domText;
                        const placeholder = element.dataset.streamPlaceholder === '1';
                        const state = liveTextByKey.get(key);
                        if (!state) {
                            liveTextByKey.set(key, { text: fullText, target: fullText, element, placeholder, timer: 0 });
                            element.dataset.streamFullText = fullText;
                            return;
                        }
                        if (state.element === element && state.target === fullText && state.timer) {
                            return;
                        }
                        const currentText = String(state.text || '');
                        const canAppend = fullText.startsWith(currentText) && fullText.length > currentText.length;
                        const canReplacePlaceholder = state.placeholder && fullText.trim() && fullText !== currentText;
                        if (canAppend || canReplacePlaceholder) {
                            animateLiveText(element, key, fullText, canAppend ? currentText.length : 0, placeholder);
                            return;
                        }
                        if (state.timer) {
                            window.clearInterval(state.timer);
                        }
                        liveTextByKey.set(key, { text: fullText, target: fullText, element, placeholder, timer: 0 });
                        element.dataset.streamFullText = fullText;
                    });
                    for (const [key, state] of liveTextByKey.entries()) {
                        if (seen.has(key)) continue;
                        if (state.timer) {
                            window.clearInterval(state.timer);
                        }
                        liveTextByKey.delete(key);
                    }
                };
                const scheduleLiveTextSync = () => {
                    if (liveTextScheduled) return;
                    liveTextScheduled = true;
                    (window.queueMicrotask || ((callback) => Promise.resolve().then(callback)))(syncLiveText);
                };
                const setupLiveTypewriter = () => {
                    window.__cbStreamTypewriterSync = scheduleLiveTextSync;
                    scheduleLiveTextSync();
                };
                const ensureLiveTypewriterObserver = (streamContent) => {
                    if (!streamContent || window.__cbStreamTypewriterObservedContent === streamContent) return;
                    if (window.__cbStreamTypewriterObserver) {
                        window.__cbStreamTypewriterObserver.disconnect();
                    }
                    window.__cbStreamTypewriterObserver = new MutationObserver(scheduleLiveTextSync);
                    window.__cbStreamTypewriterObserver.observe(streamContent, {
                        childList: true,
                        subtree: true,
                        characterData: true,
                    });
                    window.__cbStreamTypewriterObservedContent = streamContent;
                };
                const setupStreamBehavior = () => {
                    setupStreamWheelFallback();
                    setupFooterLabelReveal();
                    setupCopyFeedback();
                    setupFileLinkFeedback();
                    setupImageLightbox();
                    setupCodeBlockCopy();
                    setupComposerUploadPanel();
                    setupComposerSubmit();
                    setupReasoningDisclosures();
                    setupCommandDisclosures();
                    updateComposerMetrics();
                    updateLiveElapsed();
                    setupLiveTypewriter();
                };
                const setupStreamWheelFallback = () => {
                    if (window.__cbStreamWheelFallbackReady === '4') return;
                    window.__cbStreamWheelFallbackReady = '4';
                    const handleWheel = (event) => {
                        if (event.defaultPrevented || event.ctrlKey || event.metaKey) return;
                        const target = event.target;
                        if (target?.closest?.('.cb-sidebar-shell, .cb-composer-zone, .q-menu, .q-dialog')) return;
                        const scroller = document.querySelector('.cb-agent-stream');
                        if (!scroller) return;
                        const deltaY = Number(event.deltaY) || (Number(event.wheelDelta) ? -Number(event.wheelDelta) : 0);
                        if (!deltaY) return;
                        const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? scroller.clientHeight : 1;
                        const nextTop = clampScrollTop(scroller, scroller.scrollTop + deltaY * unit);
                        if (nextTop === scroller.scrollTop) return;
                        acceptUserScroll(scroller);
                        event.preventDefault();
                        scroller.scrollTop = nextTop;
                        updateScrollState(scroller, 'user');
                    };
                    const handleScrollbarPointerDown = (event) => {
                        if (event.button !== undefined && event.button !== 0) return;
                        const scroller = document.querySelector('.cb-agent-stream');
                        if (!scroller) return;
                        const rect = scroller.getBoundingClientRect();
                        const scrollbarWidth = Math.max(18, rect.width - scroller.clientWidth);
                        const inScrollbar = event.clientX >= rect.right - scrollbarWidth
                            && event.clientX <= rect.right
                            && event.clientY >= rect.top
                            && event.clientY <= rect.bottom;
                        if (inScrollbar) acceptUserScroll(scroller);
                    };
                    document.addEventListener('wheel', handleWheel, { capture: true, passive: false });
                    document.addEventListener('mousewheel', handleWheel, { capture: true, passive: false });
                    document.addEventListener('pointerdown', handleScrollbarPointerDown, true);
                    document.addEventListener('mousedown', handleScrollbarPointerDown, true);
                };
                const attachScrollListener = (scroller) => {
                    const desiredActiveKey = window.__cbStreamDesiredActiveKey || activeKey || readRenderedActiveKey();
                    const streamChanged = window.__cbStreamActiveKey !== desiredActiveKey;
                    if (streamChanged) {
                        window.__cbStreamActiveKey = desiredActiveKey;
                        const hasKnownState = Object.prototype.hasOwnProperty.call(
                            window.__cbStreamScrollStateByKey || {}, desiredActiveKey,
                        );
                        const state = scrollStateFor(desiredActiveKey);
                        if (forceBottom || !hasKnownState) {
                            state.delta = 0;
                            state.top = 0;
                            state.nearBottom = true;
                            state.userScrolledAway = false;
                            state.restoreTopPending = false;
                            window.__cbStreamScrollDelta = state.delta;
                            window.__cbStreamWasNearBottom = state.nearBottom;
                            window.__cbStreamUserScrolledAway = state.userScrolledAway;
                            window.__cbStreamForceBottomUntil = Date.now() + 1200;
                        } else {
                            window.__cbStreamForceBottomUntil = 0;
                        }
                        window.__cbStreamComposerFocusedKey = '';
                    }
                    if (scroller.dataset.cbScrollReady !== '1') {
                        scroller.dataset.cbScrollReady = '1';
                        scroller.addEventListener('scroll', () => {
                            updateScrollState(scroller, 'user');
                        }, { passive: true });
                    }
                    if (scroller.dataset.cbWheelReady !== '1') {
                        scroller.dataset.cbWheelReady = '1';
                        scroller.addEventListener('wheel', (event) => {
                            if (event.defaultPrevented || event.deltaY === 0) return;
                            const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? scroller.clientHeight : 1;
                            const nextTop = clampScrollTop(scroller, scroller.scrollTop + event.deltaY * unit);
                            if (nextTop === scroller.scrollTop) return;
                            acceptUserScroll(scroller);
                            event.preventDefault();
                            scroller.scrollTop = nextTop;
                            updateScrollState(scroller, 'user');
                        }, { passive: false });
                    }
                    return streamChanged;
                };
                const positionScroller = (scroller) => {
                    const streamChanged = attachScrollListener(scroller);
                    const key = window.__cbStreamActiveKey || window.__cbStreamDesiredActiveKey || activeKey || readRenderedActiveKey();
                    const state = scrollStateFor(key);
                    const previousTop = Number(state.top);
                    const restorePreviousTop = () => {
                        const restoredTop = clampScrollTop(scroller, previousTop);
                        state.restoreTopPending = restoredTop !== previousTop;
                        scroller.scrollTop = restoredTop;
                        return state.restoreTopPending;
                    };
                    const loadOlderAnchor = window.__cbStreamLoadOlderAnchor;
                    if (
                        loadOlderAnchor
                        && loadOlderAnchor.key === key
                        && Number.isFinite(Number(loadOlderAnchor.scrollHeight))
                        && Number.isFinite(Number(loadOlderAnchor.scrollTop))
                    ) {
                        markProgrammaticScroll(scroller);
                        state.restoreTopPending = false;
                        if (loadOlderAnchor.stickToBottom === true) {
                            scrollToBottom(scroller);
                        } else {
                            scroller.scrollTop = Math.max(
                                0,
                                scroller.scrollHeight - Number(loadOlderAnchor.scrollHeight) + Number(loadOlderAnchor.scrollTop),
                            );
                        }
                        delete window.__cbStreamLoadOlderAnchor;
                        updateScrollState(scroller);
                        revealPositionedStream();
                        return;
                    }
                    if (forceBottom) {
                        state.delta = 0;
                        state.nearBottom = true;
                        state.userScrolledAway = false;
                        state.restoreTopPending = false;
                    }
                    const shouldStickToBottom = forceBottom
                        || Date.now() < Number(window.__cbStreamForceBottomUntil || 0)
                        || (!state.userScrolledAway && state.nearBottom === true)
                        || !Number.isFinite(previousTop);
                    if (preserveTop && !shouldStickToBottom) {
                        markProgrammaticScroll(scroller);
                        const topWasClamped = restorePreviousTop();
                        updateScrollState(scroller, 'script', topWasClamped);
                        revealPositionedStream();
                        return;
                    }
                    if (shouldStickToBottom) {
                        scrollToBottom(scroller);
                    } else {
                        markProgrammaticScroll(scroller);
                        const topWasClamped = restorePreviousTop();
                        updateScrollState(scroller, 'script', topWasClamped);
                        revealPositionedStream();
                        return;
                    }
                    updateScrollState(scroller);
                    revealPositionedStream();
                };
                const focusComposerIfNeeded = () => {
                    if (window.innerWidth < 768) return;
                    const desiredActiveKey = window.__cbStreamDesiredActiveKey || activeKey || readRenderedActiveKey();
                    const textarea = document.querySelector('.cb-composer-input textarea');
                    if (!textarea) return;
                    if (window.__cbStreamComposerFocusedKey === desiredActiveKey && document.activeElement === textarea) return;
                    const activeElement = document.activeElement;
                    const activeTag = activeElement?.tagName?.toLowerCase() || '';
                    const isEditing = activeElement?.isContentEditable || ['input', 'textarea', 'select'].includes(activeTag);
                    if (isEditing) return;
                    textarea.focus({ preventScroll: true });
                    if (document.activeElement === textarea) {
                        window.__cbStreamComposerFocusedKey = desiredActiveKey;
                    }
                };
                const applyPosition = () => {
                    const scroller = document.querySelector('.cb-agent-stream');
                    setupStreamBehavior();
                    if (!scroller) return;
                    positionScroller(scroller);
                    focusComposerIfNeeded();
                };
                let positionRaf = 0;
                const scheduleApplyPosition = () => {
                    if (positionRaf) return;
                    positionRaf = window.requestAnimationFrame(() => {
                        positionRaf = 0;
                        applyPosition();
                    });
                };
                const ensureStreamBehaviorLoop = () => {
                    if (window.__cbStreamAutoScrollTimer) {
                        window.clearInterval(window.__cbStreamAutoScrollTimer);
                        window.__cbStreamAutoScrollTimer = null;
                    }
                    if (window.__cbStreamBehaviorTimer) {
                        window.clearInterval(window.__cbStreamBehaviorTimer);
                    }
                    window.__cbStreamBehaviorTimer = window.setInterval(() => {
                        updateComposerMetrics();
                        updateLiveElapsed();
                    }, 1000);
                };
                const ensureAutoScrollObserver = () => {
                    const streamContent = document.querySelector('.cb-agent-stream-content') || document.querySelector('.cb-agent-stream');
                    if (!streamContent) return;
                    ensureLiveTypewriterObserver(streamContent);
                    if (window.__cbStreamObservedContent !== streamContent) {
                        if (window.__cbStreamAutoScrollObserver) {
                            window.__cbStreamAutoScrollObserver.disconnect();
                        }
                        window.__cbStreamAutoScrollObserver = new MutationObserver(() => {
                            scheduleApplyPosition();
                        });
                        window.__cbStreamAutoScrollObserver.observe(streamContent, {
                            childList: true,
                            subtree: true,
                        });
                        window.__cbStreamObservedContent = streamContent;
                    }
                    const resizeNodes = [
                        document.querySelector('.cb-agent-panel'),
                        document.querySelector('.cb-agent-stream'),
                        document.querySelector('.cb-agent-stream-content'),
                        document.querySelector('.cb-composer-zone'),
                    ].filter(Boolean);
                    const resizeKey = resizeNodes.map((node) => node.className || node.tagName).join('|');
                    if (window.ResizeObserver && window.__cbStreamResizeObserverKey !== resizeKey) {
                        if (window.__cbStreamResizeObserver) {
                            window.__cbStreamResizeObserver.disconnect();
                        }
                        window.__cbStreamResizeObserver = new ResizeObserver(() => {
                            scheduleApplyPosition();
                        });
                        resizeNodes.forEach((node) => window.__cbStreamResizeObserver.observe(node));
                        window.__cbStreamResizeObserverKey = resizeKey;
                    }
                };
                ensureStreamBehaviorLoop();
                ensureAutoScrollObserver();
                [0, 80, 180, 360, 700, 1200].forEach((delay) => {
                    setTimeout(scheduleApplyPosition, delay);
                });
                };
                }
                window.__cbStreamAfterPatch(__CB_PATCH_OPTIONS__);
            })();
            """.replace("__CB_PATCH_OPTIONS__", patch_options_json)
        start_time = time.perf_counter()
        _append_stream_ui_log(
            "scroll_runtime_install_start",
            client_id=client_key,
            active_session=active_key,
            force_bottom=bool(force_bottom),
            preserve_top=bool(preserve_top),
        )
        try:
            ui.run_javascript(script)
            _append_stream_ui_log(
                "scroll_runtime_install_ok",
                client_id=client_key,
                active_session=active_key,
                elapsed_ms=round((time.perf_counter() - start_time) * 1000, 3),
            )
        except Exception as exc:
            _append_stream_ui_log(
                "scroll_runtime_install_error",
                client_id=client_key,
                active_session=active_key,
                error=repr(exc),
                traceback=traceback.format_exc(),
                elapsed_ms=round((time.perf_counter() - start_time) * 1000, 3),
            )
            raise

    def install_stream_refresh_timer() -> None:
        client = context.client
        timer_ref = {"initial_timer": None, "timer": None}

        def install_initial_stream_behavior() -> None:
            if getattr(client, "_deleted", False):
                return
            installed_clients = state.get("stream_scroll_runtime_clients")
            if isinstance(installed_clients, set):
                installed_clients.discard(str(getattr(client, "id", id(client))))
            if state["active_page"] == "stream":
                stream_state = _stream_state_snapshot()
                active_stream_session = _resolve_stream_active_session(stream_state)
                selected_stream_session = str(state["selected_session_name"] or "").strip()
                next_hub_file_signature = (
                    None if codex_thread_id_from_session_name(selected_stream_session) else stream_hub_state_file_signature()
                )
                next_signature = _stream_signature_snapshot()
                state["stream_refresh_signature"] = next_signature
                state["stream_force_bottom_next"] = True
                _refresh_stream_parts(
                    stream_state,
                    active_stream_session,
                    refresh_signature=next_signature,
                    hub_file_signature=next_hub_file_signature,
                )

        def refresh_stream() -> None:
            timer = timer_ref["timer"]
            if getattr(client, "_deleted", False) or not client.has_socket_connection:
                _append_stream_ui_log(
                    "refresh_timer_cancel",
                    client_id=str(getattr(client, "id", id(client))),
                    deleted=bool(getattr(client, "_deleted", False)),
                    has_socket=bool(getattr(client, "has_socket_connection", False)),
                )
                if timer is not None:
                    timer.cancel(with_current_invocation=True)
                return
            if state["active_page"] == "stream":
                try:
                    if state.get("stream_switch_refresh_pending"):
                        return
                    selected_stream_session = str(state["selected_session_name"] or "").strip()
                    if not codex_thread_id_from_session_name(selected_stream_session):
                        next_hub_file_signature = stream_hub_state_file_signature()
                        if next_hub_file_signature == state.get("stream_hub_state_file_signature"):
                            return
                    else:
                        next_hub_file_signature = None
                    next_signature = _stream_signature_snapshot()
                    if next_hub_file_signature is not None:
                        state["stream_hub_state_file_signature"] = next_hub_file_signature
                    if next_signature != state.get("stream_refresh_signature"):
                        stream_state = _stream_state_snapshot()
                        active_stream_session = _resolve_stream_active_session(stream_state)
                        next_composer_signature = _stream_composer_signature(stream_state, active_stream_session)
                        state["stream_refresh_signature"] = next_signature
                        if next_composer_signature != state.get("stream_composer_signature"):
                            state["stream_composer_signature"] = next_composer_signature
                            _refresh_stream_parts(
                                stream_state,
                                active_stream_session,
                                refresh_signature=next_signature,
                                hub_file_signature=next_hub_file_signature,
                            )
                        else:
                            _refresh_stream_parts(
                                stream_state,
                                active_stream_session,
                                refresh_composer=False,
                                refresh_signature=next_signature,
                                hub_file_signature=next_hub_file_signature,
                            )
                except RuntimeError as exc:
                    _append_stream_ui_log(
                        "refresh_timer_runtime_error",
                        client_id=str(getattr(client, "id", id(client))),
                        error=repr(exc),
                        traceback=traceback.format_exc(),
                    )
                    if "client this element belongs to has been deleted" not in str(exc):
                        raise
                    if timer is not None:
                        timer.cancel(with_current_invocation=True)
                except Exception as exc:
                    _append_stream_ui_log(
                        "refresh_timer_error",
                        client_id=str(getattr(client, "id", id(client))),
                        error=repr(exc),
                        traceback=traceback.format_exc(),
                    )
                    raise

        client.on_connect(install_initial_stream_behavior)
        timer_ref["initial_timer"] = ui.timer(0.1, install_initial_stream_behavior, once=True)
        timer_ref["timer"] = ui.timer(1.0, refresh_stream)

        def cancel_timer() -> None:
            _append_stream_ui_log(
                "client_disconnect",
                client_id=str(getattr(client, "id", id(client))),
                deleted=bool(getattr(client, "_deleted", False)),
                has_socket=bool(getattr(client, "has_socket_connection", False)),
            )
            initial_timer = timer_ref["initial_timer"]
            timer = timer_ref["timer"]
            if initial_timer is not None:
                initial_timer.cancel(with_current_invocation=True)
            if timer is not None:
                timer.cancel(with_current_invocation=True)

        client.on_disconnect(cancel_timer)
        client.on_delete(cancel_timer)

    @ui.page("/")
    def index_page(request: Request) -> None:
        apply_request_language(request)
        apply_request_theme(request)
        apply_request_page(request)
        requested_mode = str(request.query_params.get("mode") or "").strip()
        if requested_mode in {"weixin", "qq"}:
            state["bridge_mode"] = requested_mode
            state["bridge_mode_selected"] = True
        else:
            state["bridge_mode_selected"] = False
        apply_request_session(request)
        _refresh_runtime_status_on_ui_entry()
        shell_view()
        content_view()
        ui.run_javascript(f"window.__cbApplyTheme && window.__cbApplyTheme({state['theme']!r})")
        install_ui_error_logger()
        install_stream_refresh_timer()

    @ui.page("/mobile-ui")
    def mobile_ui_page(request: Request) -> None:
        token = str(request.query_params.get("token") or "").strip()
        if not is_mobile_access_authorized(token):
            with ui.column().classes("w-full max-w-md mx-auto gap-3 p-5"):
                ui.label(t("ui.web.mobile.unauthorized_title", "未授权")).classes("text-xl font-bold cb-ink")
                ui.label(t("ui.web.mobile.unauthorized_body", "请从桌面 UI 的“手机入口”扫码打开。")).classes("text-sm cb-muted")
            return
        apply_request_language(request)
        state["active_page"] = "stream"
        state["bridge_mode_selected"] = False
        apply_request_page(request)
        apply_request_session(request)
        shell_view()
        content_view()
        ui.run_javascript(f"window.__cbApplyTheme && window.__cbApplyTheme({state['theme']!r})")
        install_ui_error_logger()
        install_stream_refresh_timer()


def run_ui(host: str = "0.0.0.0", port: int = 8765, native: bool = False) -> None:
    ui = _load_nicegui()
    from nicegui import app

    @app.post("/api/ui/stream-log")
    async def ui_stream_log(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception as exc:
            payload = {"event": "browser_log_parse_error", "error": repr(exc)}
        if not isinstance(payload, dict):
            payload = {"event": "browser_log_invalid_payload", "payload": repr(payload)[:1200]}
        event = str(payload.pop("event", "browser_event") or "browser_event")
        _append_stream_ui_log(event, **payload)
        return JSONResponse({"ok": True})

    install_mobile_routes(app, host=host, port=port)
    create_ui(host=host, port=port)
    ui.run(
        host=host,
        port=port,
        reload=False,
        native=native,
        show=False,
        title=f"{APP_SHELL.app_name} UI",
        reconnect_timeout=NICEGUI_RECONNECT_TIMEOUT_SECONDS,
        message_history_length=NICEGUI_MESSAGE_HISTORY_LENGTH,
    )
