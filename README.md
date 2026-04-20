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
- `/context`
  Show how Agent / Session / Backend / Model / Project relate to each other and which values are currently effective
- `/new <name>`
  Create and switch to a new session
- `/list`
  List session summaries for the current sender
- `/sessions [page]`
  View paged session summaries
- `/sessions search <keyword>`
  Filter sessions by name or recent summary
- `/sessions delete <a,b,c>`
  Delete multiple target sessions
- `/sessions clear-empty`
  Remove empty sessions that have no task history
- `/preview [name]`
  Show recent rounds for the current or target session
- `/history [name]`
  Show the history summary for the current or target session
- `/export [name]`
  Export the current or target session history to a local Markdown file
- `/use <name>`
  Switch to the target session
- `/rename <new>` / `/rename <old> <new>`
  Rename the current or target session
- `/delete <name>`
  Delete a specific session
- `/cancel [task_id]`
  Cancel the latest queued or running task for the current sender, or a specific task
- `/retry [task_id]`
  Retry the latest task for the current sender, or a specific task
- `/model`
  Show the model bound to the current session
- `/model <name>`
  Switch the model for the current session
- `/model reset`
  Revert to the current agent default model
- `/project`
  Show the project directory bound to the current session
- `/project list`
  List available project directories
- `/project <name|path>`
  Switch the project directory for the current session
- `/project reset`
  Revert to the current agent default project directory
- `/backend`
  Show the current session backend
- `/backend <codex|claude|opencode>`
  Switch the current session backend
- `/agent`
  Show details of the current default bridge agent
- `/agent list`
  List all agents with backend, model, and workdir summaries
- `/agent help`
  Show how to query commands supported by the current agent
- `/agent <name>`
  Switch the default bridge agent
- `/notify`
  Show system notice status
- `/notify on|off`
  Toggle all notices at once
- `/notify service-on|service-off`
  Toggle service lifecycle notices

## MCP Management Assistant

The project now also ships an MCP stdio server for external agents:

- Launch with:
  `python3 tools/chatbridge_mcp_server.py`
- Or use:
  `./start-chatbridge-mcp.sh`
  `start-chatbridge-mcp.cmd`

This MCP server is designed as an isolated control plane instead of reusing ordinary WeChat session state. That means:

- The management assistant does not depend on normal WeChat `/use`, `/backend`, or `/model` session switching for its own context
- Any bridge command execution must explicitly target a `target_sender_id`
- Delegating work to other agents or starting new agent sessions will not implicitly move the management assistant into another session

To reduce accidental state changes, the management assistant must explicitly enter and exit control mode:

- `enter_control_mode`
  Enables state-changing operations and agent delegation
- `exit_control_mode`
  Leaves management mode and falls back to read-only queries

Current MCP tools include:

- `get_manager_guide`
  Show control-plane rules and the recommended workflow
- `get_management_snapshot`
  Show the global overview, or a specific sender snapshot via `target_sender_id`
- `run_sender_command`
  Execute a bridge slash command for a specific sender
- `start_agent_session`
  Start a fresh agent session and send the first instruction
- `delegate_task`
  Delegate a new instruction to a target agent
- `list_agents`
  Show agent summaries
- `get_task`
  Show task details

The runtime context model is fixed:

- `Agent`
  Defines the default backend, default model, default project(workdir), and prompt prefix
- `Session`
  Belongs to one sender only; `/use` changes only that sender
- `Backend`
  Decides which CLI backend executes the task
- `Model`
  Follows the agent by default; if the session has `/model`, the session override wins
- `Project`
  Follows the agent workdir by default; if the session has `/project`, the session override wins
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

If you need to forward a slash command to the current agent unchanged, use double-slash passthrough:

- `//help`
  Ask the current agent to show its own supported commands
- `//status`
  Query the current agent's internal status

When asynchronous tasks finish, task notices are pushed back into WeChat and include follow-up commands like `/task <task_id>` or `/last`.
