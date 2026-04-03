# ChatBridge Deployment

This document describes the intended deployment model for ChatBridge on a new Windows machine.

## Deployment Policy

This software is **not distributed as a packaged executable**.

Current policy:

- keep the project as normal Python source
- allow direct debugging on the target machine
- require Python to be installed in advance
- use the desktop app as the only user-facing control surface

Minimum prerequisite:

- Python 3.11 or newer

That is the only manual prerequisite the user must satisfy before first launch.

## 1. Install Python

Install Python 3.11+ on the target machine.

Recommended verification:

```powershell
python --version
```

If this command works, the minimum prerequisite is satisfied.

## 2. Copy The Project

Copy the full project directory to the target machine.

Example location:

```text
D:/projects/chatbridge
```

Any path is fine, as long as the config files use the correct local paths.

## 3. Start The App

Preferred:

```bat
start-codex-wechat-desktop.cmd
```

Alternative:

```powershell
python .\main.py
```

## 4. What Happens On First Run

On first run, the app will automatically:

- create `.runtime/`
- auto-install missing desktop Python dependencies
  - `PySide6`
  - `psutil`
- run environment detection
- try to auto-install missing Windows toolchain components when possible
  - `nvm for Windows`
  - `Node.js 24.14.1`
  - `Codex CLI`

After that, the desktop app will tell the user what the next step is.

## 5. Remaining Dependencies

The project still depends on these external tools:

- Node.js / npm
- Codex CLI

The preferred installation path is:

- `winget`
- `nvm for Windows`
- `Node.js 24.14.1`

The desktop app detects these items and should auto-repair them when the machine supports it.

## 6. WeChat Account Files

WeChat transport requires account files in the project runtime directory.

Expected location:

```text
accounts/
```

Expected files:

- `<account-id>.json`
- `<account-id>.sync.json`

The desktop app checks this directory automatically.

## 7. Config Files

The main config files are:

- `multi_codex_hub_config.json`
- `weixin_hub_bridge_config.json`

Typical fields that may need adjustment on a new machine:

- work directories
- session file paths
- WeChat account file path
- WeChat sync file path

## 8. Runtime Output

All generated runtime files should stay under:

```text
.runtime/
```

This includes:

- logs
- pid files
- state files
- session files
- Python bytecode cache

These are disposable runtime artifacts and should not be committed.

## 9. Recommended User Message

For end users, the simplified message is:

1. Install Python first
2. Double-click the desktop launcher
3. Let the app auto-install missing dependencies
4. Put the WeChat account `json/sync` files into `accounts/`

That is the intended deployment experience.
