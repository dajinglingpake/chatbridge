# ChatBridge

[中文说明](README.zh-CN.md)

ChatBridge is moving to a unified UI entry that can run both as a web app and as a local native shell for:

- WeChat transport
- multiple AI conversations
- lifecycle control for Hub / Bridge / multiple agent CLI child processes

## Repository Status

This repository is intended to be pushed to GitHub as normal source code.

Included:

- application source
- config files
- launcher scripts
- deployment docs

Excluded from Git:

- runtime output under `.runtime/`
- local WeChat account files under `accounts/`
- temporary workspace files under `workspace/`
- IDE metadata

## Positioning

This project is intentionally **not packaged** into a standalone executable.

Reason:

- local debugging is easier
- the code stays transparent
- the minimum environment requirement is simple enough

The only required preinstalled runtime is:

- Python 3.11 or newer

Everything else is handled by the app or by the app-guided setup flow.

## Minimum Requirement

Before first run, the target machine must already have:

- Python 3.11+

Recommended check:

```powershell
python --version
```

If Python is missing, install it first.

## Install Python Dependencies

Install Python dependencies before launch:

```powershell
python -m pip install -r requirements.txt
```

## Language

The project now supports a file-based bilingual path for bridge responses.

- Default behavior: auto-detect from the system locale
- Override with env var: `CHATBRIDGE_LANG=zh-CN` or `CHATBRIDGE_LANG=en-US`
- Bridge config also supports `"language": "auto" | "zh-CN" | "en-US"`

## First Run

Preferred launcher:

```bat
start-chatbridge-desktop.cmd
```

Unified UI entry:

```powershell
python .\ui_main.py --native
```

Linux / headless web mode:

```bash
python3 ./ui_main.py --host 127.0.0.1 --port 8765
```

On startup, the unified UI will:

- create `.runtime/` if needed
- rely on Python dependencies from `requirements.txt`
- auto-detect the rest of the environment
- auto-repair the Windows toolchain when possible
  - `winget`
  - `nvm for Windows`
  - `Node.js 24.14.1`
  - `Codex CLI`
  - `Claude Code`
  - `OpenCode CLI`

## User Expectation

For a new user, the intended flow is:

1. Install Python
2. Launch the unified UI
3. Let the app auto-detect and auto-repair missing dependencies
4. Complete WeChat login when prompted
   Put the WeChat account `json/sync` files into `accounts/`
5. Start the stack from the main button

Recommended entrypoints:

- `start-chatbridge-desktop.cmd`: desktop shortcut launcher
- `ui_main.py`: unified primary entry
- `start-chatbridge-web.sh`: web shortcut launcher

Compatibility wrappers:

- `main.py`
- `web_main.py`

## Runtime Files

Keep in repo root:

- source files
- config files
- docs
- icon
- launcher scripts

Generated files belong under:

- `.runtime/`

This includes:

- logs
- pid files
- state snapshots
- session files
- Python bytecode cache

These should not be committed.

## Local Directories

The repository keeps placeholder directories for:

- `accounts/`
- `workspace/`

Do not commit account JSON files or temporary workspace content.

## Git Ignore

The repo already ignores:

- `.runtime/`
- `__pycache__/`
- `*.pyc`
- `*.pyo`
- `*.pyd`

## Main Files

- `ui_main.py`: unified UI primary entry
- `start-chatbridge-desktop.cmd`: primary desktop launcher
- `main.py`: legacy desktop compatibility entry
- `web_main.py`: legacy web compatibility entry
- `runtime_stack.py`: primary runtime and process control entry
- `env_tools.py`: primary environment check and install helper entry
- `agent_hub.py`: primary conversation backend entry with `codex` / `claude` / `opencode` support
- `agent_backends/`: backend interface and isolated implementations for agent runtimes
- `weixin_hub_bridge.py`: WeChat bridge

## Notes

- The project now has a unified UI direction for Windows, Linux, and headless mode
- `main.py` and `web_main.py` are compatibility wrappers around the unified UI
- The preferred Node path is `nvm for Windows` with `Node.js 24.14.1`
- Hub and bridge communicate through local runtime IPC

## WeChat Commands

The bridge supports per-session backend switching:

- `/help`
- `/status`
- `/new <name>`
- `/list`
- `/use <name>`
- `/backend`
- `/backend <codex|claude|opencode>`
- `/close`
- `/reset`
