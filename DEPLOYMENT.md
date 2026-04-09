# ChatBridge Deployment

[中文部署说明](DEPLOYMENT.zh-CN.md)

This document describes the intended deployment model for ChatBridge with a single public entrypoint backed by a shared UI module.

## Deployment Policy

This software is **not distributed as a packaged executable**.

Current policy:

- keep the project as normal Python source
- allow direct debugging on the target machine
- require Python to be installed in advance
- use the unified entry as the main user-facing control surface

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

Preferred desktop/native shell:

```bat
start-chatbridge-desktop.cmd
```

Alternative native launch:

```powershell
python .\main.py --native
```

Web launch:

```bash
python3 ./main.py --host 0.0.0.0 --port 8765
```

## 4. What Happens On First Run

On first run, the app will automatically:

- create `.runtime/`
- run environment detection
- try to auto-install missing Windows toolchain components when possible
  - `nvm for Windows`
  - `Node.js 24.14.1`
  - `Codex CLI`
  - `OpenCode CLI`

After that, the shared UI layer will tell the user what the next step is.

## 5. Remaining Dependencies

The project still depends on these external tools:

- Node.js / npm
- Codex CLI
- OpenCode CLI

The preferred installation path is:

- `winget`
- `nvm for Windows`
- `Node.js 24.14.1`

The shared UI layer detects these items and should auto-repair them when the machine supports it.

## 6. WeChat Account Files

WeChat transport requires account files in the project runtime directory.

Expected location:

```text
accounts/
```

Expected files:

- `<account-id>.json`
- `<account-id>.sync.json`

The shared UI layer checks this directory automatically.

## 7. Config Files

The main config files are:

- `config/agent_hub.json`
- `config/weixin_bridge.json`

Typical fields that may need adjustment on a new machine:

- work directories
- session file paths
- WeChat account file path
- WeChat sync file path
- language selection (`auto`, `zh-CN`, `en-US`)

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
2. Start the unified entry with the desired mode
3. Let the app auto-install missing dependencies
4. Put the WeChat account `json/sync` files into `accounts/`

That is the intended deployment experience.

## 10. Remote WeChat Control

If WeChat and the Agent/UI are not on the same machine, prefer WeChat commands for remote inspection and control:

- `/status`
- `/agent`
- `/backend`
- `/notify`
- `/task <task_id>`
- `/last`

In practice, task results in WeChat should be consumed through summaries and follow-up commands rather than relying on local Web links.
