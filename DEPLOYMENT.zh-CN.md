# ChatBridge 部署说明

[English Deployment Guide](DEPLOYMENT.md)

本文说明 ChatBridge 在统一 UI 入口下的预期部署方式。

## 部署策略

这个软件当前**不以打包可执行文件**的形式分发。

当前策略：

- 以普通 Python 源码项目形式保留
- 允许在目标机器上直接调试
- 要求用户预先安装 Python
- 以统一 UI 作为主要用户可见控制面

最低前置条件：

- Python 3.11 或更高版本

这也是首次运行前用户唯一必须手动满足的条件。

## 1. 安装 Python

在目标机器上安装 Python 3.11+。

建议验证：

```powershell
python --version
```

如果命令可用，就说明最低前置条件已经满足。

## 2. 拷贝项目

将完整项目目录复制到目标机器上。

例如：

```text
D:/projects/chatbridge
```

路径本身不限，只要配置文件里的本地路径正确即可。

## 3. 启动应用

推荐桌面 / 本地壳方式：

```bat
start-codex-wechat-desktop.cmd
```

备选本地壳方式：

```powershell
python .\ui_main.py --native
```

Web 方式：

```bash
python3 ./ui_main.py --host 127.0.0.1 --port 8765
```

## 4. 首次运行时会发生什么

首次运行时，应用会自动：

- 创建 `.runtime/`
- 执行环境检测
- 在可能的情况下自动安装或补齐缺失的 Windows 工具链
  - `nvm for Windows`
  - `Node.js 24.14.1`
  - `Codex CLI`
  - `OpenCode CLI`

之后统一 UI 会继续告诉用户下一步应该做什么。

## 5. 剩余依赖

项目仍依赖以下外部工具：

- Node.js / npm
- Codex CLI
- OpenCode CLI

推荐安装路径：

- `winget`
- `nvm for Windows`
- `Node.js 24.14.1`

统一 UI 会检测这些项目，并在机器支持时尝试自动修复。

## 6. 微信账号文件

微信传输依赖项目运行目录中的账号文件。

预期目录：

```text
accounts/
```

预期文件：

- `<account-id>.json`
- `<account-id>.sync.json`

统一 UI 会自动检查这个目录。

## 7. 配置文件

主要配置文件有：

- `agent_hub_config.json`
- `weixin_bridge_config.json`

在新机器上通常需要确认的字段：

- 工作目录
- 会话文件路径
- 微信账号文件路径
- 微信同步文件路径
- 语言选择（`auto`、`zh-CN`、`en-US`）

## 8. 运行时输出

所有运行期生成文件都应保存在：

```text
.runtime/
```

包括：

- 日志
- PID 文件
- 状态文件
- 会话文件
- Python 字节码缓存

这些都是可丢弃的运行时产物，不应被提交。

## 9. 建议给用户的简化说明

面向最终用户时，可以简化成：

1. 先安装 Python
2. 启动统一 UI
3. 让应用自动安装缺失依赖
4. 将微信账号 `json/sync` 文件放进 `accounts/`

这就是当前项目预期的部署体验。
