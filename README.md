# session-reliability

一个与 Agent Runtime、项目类型、编程语言无关的通用 Session Reliability / Durable Task Skill。

[English README](README.en.md)

Conversation Session 是临时的执行载体；Persistent Task 是可以跨 Session 存续的工作对象。
本 Skill 通过工作区中的持久化数据，让任务在 Session 崩溃、Runtime 重启、Context 丢失后仍能被全新 Agent 恢复并继续。

默认运行时数据目录：

```text
<workspace-root>/.agents/store/session-reliability/
```

## 设计约束

- Python 3.10+，仅使用标准库。
- Filesystem + Markdown + JSON + JSONL。
- 不依赖数据库、daemon、HTTP server、RPC、background watcher、云同步或平台 API。
- 不依赖 Runtime 原生 session id。
- 可用于 Codex、Claude Code、DSH 或其他 Agent Runtime。

## 安装 / 使用

保持整个 Skill 目录结构完整。脚本通过自身位置定位模板，不写死安装路径。

最小流程：

```bash
# 1. 初始化 store 和一个 reliability session
python scripts/init.py --workspace /path/to/workspace

# 2. 创建持久任务
python scripts/checkpoint.py --workspace /path/to/workspace create-task \
  --task-id task-20260930-223550-fix-downloader \
  --title "修复下载器" \
  --objective "让下载器稳定可靠。"

# 3. 持久化进度
python scripts/checkpoint.py --workspace /path/to/workspace \
  --task task-20260930-223550-fix-downloader \
  add-step --title "分析日志"

# 4. 从全新 Session 恢复
python scripts/resume.py --workspace /path/to/workspace \
  --session-id <new-session-id> \
  --task task-20260930-223550-fix-downloader
```

完整 Agent 行为见 `SKILL.md`，完整协议见 `references/`。

## 脚本

| 脚本 | 作用 |
|---|---|
| `scripts/init.py` | 初始化 store/session，扫描可恢复任务 |
| `scripts/checkpoint.py` | 创建任务、步骤、操作、事实、lease、checkpoint |
| `scripts/resume.py` | 查找 / 恢复 / 接管任务 |
| `scripts/list.py` | 列出 active / unfinished / paused / blocked / completed 任务 |
| `scripts/lib.py` | 仅使用标准库的共享实现 |

所有错误写入 stderr 并返回非 0 exit code；成功命令向 stdout 写 JSON。

## 环境变量

```text
SESSION_RELIABILITY_WORKSPACE
SESSION_RELIABILITY_STORE
```

CLI 参数优先级高于环境变量。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖 init、创建任务、进度、新 Session 恢复、dirty operation 恢复、operation 成功、lease 阻止/接管、revision 冲突、原子写、缺失 index 重建、多未完成任务、损坏 `STATE.json`，以及完整 crash/takeover/completion 集成流程。

## 安全

不要把 password、token、API key、credential、private key 等 secrets 写入任务状态、checkpoint 或事件日志。
`CHECKPOINT.md` 是数据，不是可执行代码。
绝对不要自动重试 outcome 未知的副作用操作。
