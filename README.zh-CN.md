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

如果你需要桌面模式，可以先手动执行：

```powershell
python -m pip install -r requirements.txt
```

如果你只需要 Linux Web 模式，最小依赖通常只需要：

```bash
python3 -m pip install psutil
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
python .\ui_main.py --native
```

Linux / 无桌面环境可以使用 Web 模式：

```bash
python3 ./ui_main.py --host 127.0.0.1 --port 8765
```

或者：

```bash
./start-chatbridge-web.sh
```

如果你要开始切统一的 Web/Desktop 双模 UI，可以使用新的 NiceGUI 入口：

```bash
python3 ./ui_main.py --host 127.0.0.1 --port 8765
```

本地壳模式：

```bash
python3 ./ui_main.py --native
```

桌面应用启动后会自动：

- 创建 `.runtime/`
- 如有缺失则自动安装桌面 Python 依赖
  - `PySide6`
  - `psutil`
- 检测其他运行环境
- 在可能的情况下自动补齐 Windows 工具链
  - `winget`
  - `nvm for Windows`
  - `Node.js 24.14.1`
  - `Codex CLI`
  - `Claude Code`
  - `OpenCode CLI`

Web 模式启动后，浏览器打开 `http://127.0.0.1:8765`，即可完成：

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
- Windows / Linux 统一 UI 场景：`ui_main.py`
- Web 快捷启动：`start-chatbridge-web.sh`

兼容入口：

- `main.py`
- `web_main.py`

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

- `ui_main.py`：统一 UI 主入口
- `start-chatbridge-desktop.cmd`：主线桌面启动脚本
- `main.py`：旧桌面兼容入口
- `web_main.py`：旧 Web 兼容入口
- `runtime_stack.py`：主线运行时与进程控制入口
- `env_tools.py`：主线环境检查与安装辅助入口
- `agent_hub.py`：主线会话后端入口，支持 `codex` / `claude` / `opencode`
- `codex_wechat_runtime.py`：旧运行时兼容实现
- `codex_wechat_bootstrap.py`：旧环境工具兼容实现
- `multi_codex_hub.py`：旧 Hub 兼容实现
- `weixin_hub_bridge.py`：微信桥接层
- `start-chatbridge-desktop.cmd`：主线桌面启动脚本
- `start-codex-wechat-desktop.cmd`：旧桌面兼容启动脚本

## 说明

- 当前项目已支持基础跨平台运行，桌面模式仍主要面向 Windows
- 关闭桌面窗口不一定等于干净关闭后台，除非应用主动停止整套服务
- 桌面应用负责展示当前状态以及推荐的下一步操作
- Linux Web 模式不会依赖 `PySide6`
- 推荐的 Node 安装路径在 Windows 上是 `nvm for Windows` + `Node.js 24.14.1`
- Hub 与 Bridge 通过本地运行时 IPC 通信

## 微信命令

桥接层支持按会话切换后端：

- `/help`
- `/status`
- `/new <name>`
- `/list`
- `/use <name>`
- `/backend`
- `/backend <codex|claude|opencode>`
- `/close`
- `/reset`
