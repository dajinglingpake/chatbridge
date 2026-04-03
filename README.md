# ChatBridge

ChatBridge is a desktop control app for:

- WeChat transport
- multiple Codex conversations
- lifecycle control for Hub / Bridge / Codex child processes

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

Install desktop dependencies before launch if you do not want the app to auto-install them:

```powershell
python -m pip install -r requirements.txt
```

## First Run

Preferred launcher:

```bat
start-codex-wechat-desktop.cmd
```

Direct Python launch also works:

```powershell
python .\main.py
```

On startup, the desktop app will:

- create `.runtime/` if needed
- auto-install desktop Python dependencies if missing
  - `PySide6`
  - `psutil`
- auto-detect the rest of the environment
- auto-repair the Windows toolchain when possible
  - `winget`
  - `nvm for Windows`
  - `Node.js 24.14.1`
  - `Codex CLI`

## User Expectation

For a new user, the intended flow is:

1. Install Python
2. Launch the desktop app
3. Let the app auto-detect and auto-repair missing dependencies
4. Complete WeChat login when prompted
   Put the WeChat account `json/sync` files into `accounts/`
5. Start the stack from the main button

The desktop app should be the only normal user entrypoint.

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

- `main.py`: desktop UI entry
- `codex_wechat_runtime.py`: Python runtime and process control
- `codex_wechat_bootstrap.py`: environment checks and install helpers
- `multi_codex_hub.py`: conversation backend
- `weixin_hub_bridge.py`: WeChat bridge
- `start-codex-wechat-desktop.cmd`: desktop launcher

## Notes

- This project currently assumes Windows
- Closing the desktop window is not the same as a clean backend stop unless the app stops the stack
- The desktop app is responsible for showing the current state and the next recommended step
- The preferred Node path is `nvm for Windows` with `Node.js 24.14.1`
- Hub and bridge communicate through local runtime IPC

## Suggested First Commit Flow

```powershell
git add .
git commit -m "chore: initialize chatbridge repository"
```
