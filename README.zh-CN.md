# ChatBridge

[English README](README.md)

ChatBridge 最初是一个桌面控制应用，现在也支持在无图形界面的 Linux 环境下通过 Web 控制台运行，主要用于：

- 微信消息传输
- 多个 AI 会话管理
- Hub / Bridge / 多种 Agent CLI 子进程生命周期控制

## 仓库状态

这个仓库旨在以普通源码仓库的方式托管到 GitHub。

仓库包含：

- 应用源码
- 配置文件
- 启动脚本
- 部署文档

不会提交到 Git 的内容：

- `.runtime/` 下的运行时输出
- `accounts/` 下的本地微信账号文件
- `workspace/` 下的临时工作区内容
- IDE 元数据

## 项目定位

这个项目当前刻意**不打包**为独立可执行文件。

原因：

- 便于本地调试
- 保持代码透明
- 最低环境要求足够简单

唯一需要预装的运行时是：

- Python 3.11 或更高版本

其他内容都由应用本身或引导流程处理。

## 最低要求

首次运行前，目标机器至少需要具备：

- Python 3.11+

建议先验证：

```powershell
python --version
```

如果 Python 缺失，请先安装。

## 安装 Python 依赖

如果你需要手动准备环境，可以先执行：

```powershell
python -m pip install -r requirements.txt
```

统一入口 `main.py` 会通过共享 UI 模块在首次运行时自动：

- 创建项目内 `.venv/`
- 安装 `requirements.txt`
- 用该虚拟环境重新启动自身

所以 Linux 上无论是直接运行还是走快捷脚本，都会触发同一套自举逻辑：

```bash
./start-chatbridge-web.sh
```

## 验证

建议在本地至少执行这两条回归检查：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python tools/smoke_weixin_bridge.py
python tools/run_product_acceptance.py
python tools/run_live_acceptance.py --send-notice
```

## 语言

当前项目已经支持基于文件的中英文桥接响应国际化。

- 默认行为：根据系统语言自动检测
- 也可以通过环境变量覆盖：`CHATBRIDGE_LANG=zh-CN` 或 `CHATBRIDGE_LANG=en-US`
- 桥接配置也支持：`"language": "auto" | "zh-CN" | "en-US"`

## 首次运行

桌面模式优先使用：

```bat
start-chatbridge-desktop.cmd
```

也可以直接运行：

```powershell
python .\main.py --native
```

Linux / 无桌面环境可以使用 Web 模式：

```bash
./start-chatbridge-web.sh
```

或者：

```bash
python3 ./main.py --host 0.0.0.0 --port 8765
```

如果你要开始切统一的 Web/Desktop 双模 UI，可以使用新的 NiceGUI 入口：

```bash
python3 ./main.py --host 0.0.0.0 --port 8765
```

本地壳模式：

```bash
python3 ./main.py --native
```

统一 UI 启动后会自动：

- 创建 `.runtime/`
- 如有缺失则自动安装 Python 依赖
  - `nicegui`
  - `psutil`
  - `qrcode`
- 检测其他运行环境
- 在可能的情况下自动补齐 Windows 工具链
  - `winget`
  - `nvm for Windows`
  - `Node.js 24.14.1`
  - `Codex CLI`
  - `Claude Code`
  - `OpenCode CLI`

Web 模式启动后，终端会打印本地地址和局域网地址，例如 `http://127.0.0.1:8765` 与 `http://192.168.x.x:8765`，即可快速访问：

- 查看运行状态
- 启动 / 停止 / 重启 Hub 与 Bridge
- 查看环境检查结果
- 直接向 Hub 提交测试任务

## 面向用户的预期流程

新用户推荐流程如下：

1. 先安装 Python
2. 在有图形界面的机器上启动桌面应用，或在 Linux 上启动 Web 控制台
3. 让应用检测并补齐缺失依赖
4. 按提示完成微信登录
   将微信账号 `json/sync` 文件放入 `accounts/`
5. 通过主按钮启动整套服务

当前推荐入口：

- Windows 桌面快捷启动：`start-chatbridge-desktop.cmd`
- 统一主入口：`main.py`
- Web 快捷启动：`start-chatbridge-web.sh`

共享启动模块：

- `ui_main.py`

## 运行时文件

应保留在仓库根目录中的内容：

- 源码文件
- 配置文件
- 文档
- 图标
- 启动脚本

所有生成内容应放在：

- `.runtime/`

包括：

- 日志
- PID 文件
- 状态快照
- 会话文件
- Python 字节码缓存

这些内容不应被提交。

## 本地目录

仓库会保留以下目录占位：

- `accounts/`
- `workspace/`

不要提交账号 JSON 文件，也不要提交临时工作区内容。

## Git 忽略

仓库当前已忽略：

- `.runtime/`
- `__pycache__/`
- `*.pyc`
- `*.pyo`
- `*.pyd`

## 主要文件

- `main.py`：统一主入口
- `ui_main.py`：共享 UI 启动模块
- `start-chatbridge-desktop.cmd`：主线桌面启动脚本
- `runtime_stack.py`：主线运行时与进程控制入口
- `env_tools.py`：主线环境检查与安装辅助入口
- `agent_hub.py`：主线会话后端入口，支持 `codex` / `claude` / `opencode`
- `config/agent_hub.json`：主线 Agent Hub 配置文件
- `config/weixin_bridge.json`：主线微信桥配置文件
- `agent_backends/`：Agent 后端接口与独立实现目录，新后端放入这里会被 registry 自动发现
- `weixin_hub_bridge.py`：微信桥接层

## 说明

- 当前项目已支持基础跨平台运行，桌面模式仍主要面向 Windows
- 关闭桌面窗口不一定等于干净关闭后台，除非应用主动停止整套服务
- 桌面应用负责展示当前状态以及推荐的下一步操作
- 当前统一 UI 主线不再依赖 `PySide6`
- 推荐的 Node 安装路径在 Windows 上是 `nvm for Windows` + `Node.js 24.14.1`
- Hub 与 Bridge 通过本地运行时 IPC 通信

## 微信命令

当前桥接层支持以下微信命令：

- `/help`
  查看帮助
- `/status`
  查看当前微信桥 Agent、当前会话、当前后端和会话数量
- `/context`
  查看 Agent / Session / Backend / Model / Project 的关系和当前生效值
- `/new <name>`
  新建会话并切换到该会话
- `/list`
  列出当前发送方的所有会话摘要
- `/sessions [page]`
  按页查看会话摘要
- `/sessions search <keyword>`
  按会话名或最近摘要过滤会话
- `/sessions delete <a,b,c>`
  批量删除指定会话
- `/sessions clear-empty`
  批量删除没有任务历史的空会话
- `/preview [name]`
  查看当前或指定会话最近几轮摘要
- `/history [name]`
  查看当前或指定会话的历史摘要
- `/export [name]`
  导出当前或指定会话历史到本地 Markdown 文件
- `/use <name>`
  切换到指定会话
- `/rename <new>` / `/rename <old> <new>`
  重命名当前或指定会话
- `/delete <name>`
  删除指定会话
- `/cancel [task_id]`
  取消当前发送方最近的排队中或运行中任务，或取消指定任务
- `/retry [task_id]`
  重试当前发送方最近任务，或重试指定任务
- `/model`
  查看当前会话绑定的模型
- `/model <name>`
  切换当前会话的模型
- `/model reset`
  恢复跟随当前 Agent 的默认模型
- `/project`
  查看当前会话绑定的工程目录
- `/project list`
  列出可选工程目录
- `/project <name|path>`
  切换当前会话的工程目录
- `/project reset`
  恢复跟随当前 Agent 的默认工程目录
- `/backend`
  查看当前会话后端
- `/backend <codex|claude|opencode>`
  切换当前会话后端
- `/agent`
  查看当前微信桥默认 Agent 详情
- `/agent list`
  列出所有 Agent 的后端、模型和工作目录摘要
- `/agent help`
  查看如何查询当前 Agent 自身支持的命令
- `/agent <name>`
  切换微信桥默认 Agent
- `/notify`
  查看系统通知开关状态
- `/notify on|off`
  一次性开关全部通知
- `/notify service-on|service-off`
  开关服务生命周期通知

## MCP 管理助手

项目现在额外提供了一个面向外部 Agent 的 MCP stdio server：

- 启动命令：
  `python3 tools/chatbridge_mcp_server.py`
- 或直接使用：
  `./start-chatbridge-mcp.sh`
  `start-chatbridge-mcp.cmd`

这个 MCP server 的设计是独立控制平面，不复用普通微信会话，因此：

- 管理助手自己的上下文不依赖微信 `/use`、`/backend`、`/model` 之类的会话切换
- 对目标发送方执行桥命令时，必须显式传入 `target_sender_id`
- 对其他 Agent 委派任务或启动新会话时，不会隐式把管理助手自己切到别的会话

为了避免误操作，管理助手必须显式进入和退出管理模式：

- `enter_control_mode`
  进入管理模式，之后才允许执行会修改状态或触发其他 Agent 的操作
- `exit_control_mode`
  退出管理模式，之后只保留只读查询

当前 MCP 主要工具包括：

- `get_manager_guide`
  查看管理助手规则和推荐流程
- `get_management_snapshot`
  查看全局总览，或按 `target_sender_id` 查看某个发送方的当前上下文
- `run_sender_command`
  对指定发送方执行桥接层 slash 命令
- `start_agent_session`
  显式启动一个新的 Agent 会话，并发送首条指令
- `delegate_task`
  向指定 Agent 委派新任务
- `list_agents`
  查看 Agent 摘要
- `get_task`
  查看任务详情

当前系统里的上下文关系是固定的：

- `Agent`
  定义默认 backend、默认 model、默认 project(workdir) 和提示词前缀
- `Session`
  只属于某个发送方，`/use` 只切这个发送方，不会影响其他发送方
- `Backend`
  决定这次任务实际走哪个 CLI 后端
- `Model`
  默认跟随 Agent；如果 Session 设置了 `/model`，则 Session 覆盖优先
- `Project`
  默认跟随 Agent workdir；如果 Session 设置了 `/project`，则 Session 覆盖优先
- `/notify config-on|config-off`
  开关配置变更通知
- `/notify task-on|task-off`
  开关任务通知
- `/task <task_id>`
  查看指定任务详情
- `/last`
  查看当前发送方最近任务
- `/close`
  结束当前会话
- `/reset`
  重置当前发送方的会话状态

如果你要把以 `/` 开头的消息原样发给当前 Agent，可以用双斜杠透传：

- `//help`
  让当前 Agent 返回自身支持的命令
- `//status`
  查询当前 Agent 的内部状态

异步任务执行完成后，任务通知会直接回推到微信，并提示可继续使用 `/task <task_id>` 或 `/last` 查看详情。
