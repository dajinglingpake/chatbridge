# ChatBridge

[中文说明](README.zh-CN.md)

ChatBridge provides a single public entrypoint with parameter-controlled web mode and local native shell mode for:

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

If you want to prepare the environment manually:

```powershell
python -m pip install -r requirements.txt
```

The unified entry `main.py` now bootstraps the local environment automatically on first run through the shared UI module:

- create `.venv/`
- install `requirements.txt`
- relaunch itself from that virtualenv

## Validation

Recommended local regression checks:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python tools/smoke_weixin_bridge.py
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

Desktop mode:

```powershell
python .\main.py --native
```

Linux / headless web mode:

```bash
./start-chatbridge-web.sh
```

Or run web mode directly:

```bash
python3 ./main.py --host 0.0.0.0 --port 8765
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
2. Launch the unified entry with the desired mode
3. Let the app auto-detect and auto-repair missing dependencies
4. Complete WeChat login when prompted
   Put the WeChat account `json/sync` files into `accounts/`
5. Start the stack from the main button

Recommended entrypoints:

- `start-chatbridge-desktop.cmd`: desktop shortcut launcher
- `main.py`: unified primary entry
- `start-chatbridge-web.sh`: web shortcut launcher

Shared bootstrap module:

- `ui_main.py`

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
- Python bytecode cache

Persistent session files belong under:

- `sessions/`

These should not be committed.

## Local Directories

The repository keeps placeholder directories for:

- `accounts/`
- `sessions/`
- `workspace/`

Do not commit account JSON files or temporary workspace content.

## Git Ignore

The repo already ignores:

- `.runtime/`
- `sessions/*`
- `__pycache__/`
- `*.pyc`
- `*.pyo`
- `*.pyd`

## Main Files

- `main.py`: unified primary entry
- `ui_main.py`: shared UI bootstrap module
- `start-chatbridge-desktop.cmd`: primary desktop launcher
- `runtime_stack.py`: primary runtime and process control entry
- `env_tools.py`: primary environment check and install helper entry
- `agent_hub.py`: primary conversation backend entry with `codex` / `claude` / `opencode` support
- `config/agent_hub.json`: primary Agent Hub config
- `config/weixin_bridge.json`: primary WeChat bridge config
- `agent_backends/`: backend interface and isolated implementations; new `*_backend.py` files are auto-discovered
- `weixin_hub_bridge.py`: WeChat bridge

## Notes

- The project now has a shared UI direction for Windows, Linux, and headless mode
- `main.py` is the public entrypoint, and `ui_main.py` is the shared bootstrap module
- The preferred Node path is `nvm for Windows` with `Node.js 24.14.1`
- Hub and bridge communicate through local runtime IPC

## WeChat Commands

The bridge supports the following WeChat commands:

- `/help`
  Show help
- `/status`
  Show the current bridge agent, current session, current backend, and session count
- `/new <name>`
  Create and switch to a new session
- `/list`
  List all sessions for the current sender
- `/use <name>`
  Switch to the target session
- `/backend`
  Show the current session backend
- `/backend <codex|claude|opencode>`
  Switch the current session backend
- `/agent`
  Show the current default bridge agent
- `/agent <name>`
  Switch the default bridge agent
- `/notify`
  Show system notice status
- `/notify on|off`
  Toggle all notices at once
- `/notify service-on|service-off`
  Toggle service lifecycle notices
- `/notify config-on|config-off`
  Toggle configuration change notices
- `/notify task-on|task-off`
  Toggle task notices
- `/task <task_id>`
  Show details for a specific task
- `/last`
  Show the latest task for the current sender
- `/close`
  Close the current session
- `/reset`
  Reset the current sender session state

When asynchronous tasks finish, task notices are pushed back into WeChat and include follow-up commands like `/task <task_id>` or `/last`.
