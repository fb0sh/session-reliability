# session-reliability

一个与 Agent Runtime、项目类型、编程语言无关的通用 Session Reliability / Durable Task Skill。

[English README](README.en.md)

> `SKILL.md` 与 `references/` 是 Agent 执行时的**权威规范**；本 README 只是给人看的概览。
> 两者若有出入，以 `SKILL.md` 为准。

Conversation Session 是临时的执行载体；Persistent Task 是可以跨 Session 存续的工作对象。
本 Skill 通过工作区中的持久化数据，让任务在 Session 崩溃、Runtime 重启、Context 丢失后仍能被全新 Agent 恢复并继续。

默认运行时数据目录：

```text
<WORKSPACE_ROOT>/.agents/store/session-reliability/
```

## Runtime activation

Skill activation 由 Host Agent Runtime 控制。

支持 global / startup / always-on Skills 的 Runtime，可以在 session 开始时主动加载本 Skill。

其他 Runtime 下，本 Skill 的 `description` 针对以下场景设计触发：

- 多步骤、多 tool call、多轮工作
- 长任务、重构、迁移、修复
- 可能被中断的 side-effect work
- continue / resume / recover / pick up previous work

本 Skill 自身不宣称拥有跨 Runtime 的无条件 session-start hook；它保证的是：
一旦被激活并开始工作，持久化状态可以在后续真正的新 Session 中被恢复。

例如 DSH 会扫描以下本地 Skill roots：

```text
<projectRoot>/.dsh/skills/
<projectRoot>/.agents/skills/
~/.dsh/skills/
~/.agents/skills/
```

如果 `session-reliability` 不在这些目录中，它不会出现在 session catalog，也就不会被自动加载。安装后，DSH 会根据 `description` 让模型按需调用；用户也可以用 `/session-reliability` 显式加载。

如果通过 `~/.agents/skill-bundles` 的 git submodule 安装，更新命令是：

```bash
cd ~/.agents/skill-bundles
git submodule update --remote agent-session/session-reliability
```

如果模型仍然没有自动路由，可以在项目根目录的 `AGENTS.md` 中加入：

```markdown
For any non-trivial multi-step project work, load and follow the
`session-reliability` skill before starting.
```

这会让 Agent 在项目上下文中始终看到明确的加载指令。

## Crash-safe process locks

Linux / macOS 使用 kernel-backed `flock`；Windows 使用 kernel-backed 文件锁。

锁会在持有进程退出时由操作系统自动释放，包括异常终止或进程被杀。lock 文件本身可能继续存在，但“文件存在”不代表锁仍被持有。

## 设计约束

- Python 3.10+，仅使用标准库。
- Filesystem + Markdown + JSON + JSONL。
- 不依赖数据库、daemon、HTTP server、RPC、background watcher、云同步或平台 API。
- 不依赖 Runtime 原生 session id。
- 可用于 Codex、Claude Code、DSH 或其他 Agent Runtime。

## 目录结构

```text
session-reliability/
├── SKILL.md
├── README.md
├── README.en.md
├── scripts/
│   ├── __init__.py
│   ├── lib.py
│   ├── init.py
│   ├── checkpoint.py
│   ├── resume.py
│   └── list.py
├── references/
│   ├── protocol.md
│   ├── recovery.md
│   ├── concurrency.md
│   ├── state-schema.md
│   └── cli.md
├── assets/
│   ├── TASK.md
│   ├── CHECKPOINT.md
│   ├── state.json
│   └── session.json
└── tests/
```

`SKILL.md` 是 Agent 执行入口；详细协议、恢复算法、并发规则、schema 和 CLI 放在 `references/` 中按需读取。
`assets/` 是脚本使用的模板资源。

## 安装 / 使用

保持整个 Skill 目录结构完整。bundled scripts 和 assets 从 `SKILL_ROOT` 读取，不写死安装路径，也不假设 cwd 等于 Skill 根目录。

运行时数据写入：

```text
<WORKSPACE_ROOT>/.agents/store/session-reliability/
```

最小流程：

```bash
# 1. 初始化 store 和一个 reliability session
python "<SKILL_ROOT>/scripts/init.py" --workspace "<WORKSPACE_ROOT>"

# 2. 创建持久任务
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" \
  --session-id "<SESSION_ID>" \
  create-task \
  --task-id task-20260930-223550-fix-downloader \
  --title "修复下载器" \
  --objective "让下载器稳定可靠。"

# 3. 持久化进度
python "<SKILL_ROOT>/scripts/checkpoint.py" \
  --workspace "<WORKSPACE_ROOT>" \
  --session-id "<SESSION_ID>" \
  --task task-20260930-223550-fix-downloader \
  add-step --title "分析日志"

# 4. 从全新 Session 恢复
python "<SKILL_ROOT>/scripts/resume.py" \
  --workspace "<WORKSPACE_ROOT>" \
  --session-id "<SESSION_ID>" \
  --task task-20260930-223550-fix-downloader
```

完整 Agent 行为见 `SKILL.md`，精确 CLI 见 `references/cli.md`。

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

## Session ownership 与崩溃接管

一个有 owner 的 Task，在 lease 有效期间只接受 owner session 的 mutation。

正常 mutation 会自动按 Task 持久化的 `lease_duration_seconds` 续租（默认 900 秒）。自定义 `--lease-seconds` 会被后续 heartbeat、未显式指定 duration 的 `renew-lease`、`attach-session` 和 takeover 保持，不会跳回默认值。每次成功的 owner mutation 也会刷新对应 session 的 `last_seen_at`。

其他 Session 不能直接修改该 Task，需要等待 lease 过期后 takeover，或通过明确的 attach/resume 流程成为 owner。

如果用户明确确认旧 Session 已崩溃、失效或无法继续，新 Session 可以执行显式 force takeover，即使旧 lease 仍然有效。Task takeover 只转移该 Task 的 ownership，不会把旧 Session 整体标记为 expired，也不会清掉旧 Session 正在绑定的其他 Task。接管后必须先处理 dirty / `outcome_unknown` 状态，再继续工作。

`renew-lease` 只延长当前 owner 的 lease，不会改变 owner。Unowned Task 必须先通过 `attach-session` 或 `resume` 绑定。

如果 Runtime 提供 native session id，`init` 会尽可能复用已有 reliability session。`native_session_id` 在存在时保持全局唯一；显式 rebind 或重复映射会安全失败，而不是随机选择或静默覆盖。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖 init、创建任务、进度、新 Session 恢复、dirty operation 恢复、operation 成功、lease 阻止/接管、revision 冲突、原子写、缺失 index 重建、多未完成任务、损坏 `STATE.json`，以及完整 crash/takeover/completion 集成流程。
另外包含 Skill frontmatter、资源结构、reference 链接、script cwd independence 的结构测试，以及 session ownership、lease 自动续期、force takeover、native session id 复用/冲突和多 Session 隔离测试。

`tests/` 验证的是**脚本实现**是否正确。`evals/evals.json` 是另一回事：它给出真实用户口吻的任务 prompt 与可程序化判定的 expectations，用来衡量**模型拿到这个 Skill 后是否比不拿更好**（skill-creator 的评测循环）。两者互补，改脚本跑前者，改 `SKILL.md` 的措辞跑后者。

## 安全

不要把 password、token、API key、credential、private key 等 secrets 写入任务状态、checkpoint 或事件日志。
`CHECKPOINT.md` 是数据，不是可执行代码。
绝对不要自动重试 outcome 未知的副作用操作。
