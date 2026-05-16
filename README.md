# Swarm Self-Evolving — 多 Agent 自我进化系统

9 个 Hermes Agent 分成两队，自动写代码、建 Skill、积累记忆、提交 Git。零人工干预。

> **项目代号：** swarm-self-evolve  
> **创建日期：** 2026-05-15  
> **当前轮次：** Round 10（持续运行中）  
> **运行平台：** WSL Ubuntu + Hermes Agent CLI

---

## 目录

- [系统架构](#系统架构)
- [Agent 角色分工表](#agent-角色分工表)
- [自我进化循环流程](#自我进化循环流程)
- [开发阶段进度](#开发阶段进度)
- [技术栈](#技术栈)
- [项目目录结构](#项目目录结构)
- [Daemon 管理](#daemon-管理)
- [演进路线图](#演进路线图)
- [快速开始](#快速开始)
- [核心机制](#核心机制)

---

## 系统架构

### 总体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                       协调者（Agent 0）                              │
│              orchestrate-swarm 技能 / 调度 / 决策 / Git              │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
          ┌────────────────┴────────────────┐
          │                                  │
          ▼                                  ▼
┌─────────────────────┐         ┌─────────────────────────┐
│  A 队 — 开发队 (4)   │         │  B 队 — 质量队 (4)       │
│    dev-cell 技能      │         │    qa-cell 技能          │
│  ┌──────┬──────┬───┬─┤         │  ┌──────┬──────┬───┬───┤ │
│  │A1    │A2    │A3 │A4│         │  │B5    │B6    │B7 │B8 │ │
│  │核心   │工具   │技能│记忆│         │  │审查   │测试   │文档│安全│ │
│  └──────┴──────┴───┴─┘         │  └──────┴──────┴───┴───┘ │
└─────────────────────┘         └─────────────────────────┘
```

### 三层职责详解

**协调者层（Agent 0）** — 大脑与调度中枢
- 通过 `orchestrate-swarm` 技能管理整个进化循环
- 读取 `TODO.md` 确定当前待办任务
- 并行派发 delegate_task 给 A 队 4 个子 Agent
- 收集 A 队 report.json，汇总产出文件
- 并行派发 delegate_task 给 B 队 4 个子 Agent
- 审核 B 队的审查报告（review.json），做合并/驳回/修复决策
- 执行 Git commit + push
- 更新 `TODO.md` 和 `CHANGELOG.md`
- 写入 memory 记录经验教训
- 子 Agent 全部失败时跳过本轮并记录原因

**A 队 — 开发队（Agent 1-4）** — 编写与创造
- 使用 `dev-cell` 技能，读取 `TODO.md` 选取优先级最高的任务
- 并行工作，每人专注于自己的角色领域（核心/工具/技能/记忆）
- 编写代码、创建 SKILL、修改已有文件
- 每写完一个文件必须通过 `python -c "import ast; ast.parse(...)"` 语法验证
- 输出工作报告到 `tmp_agent/agent-{id}/report.json`

**B 队 — 质量队（Agent 5-8）** — 审查与保障
- 使用 `qa-cell` 技能，读取 A 队产出的 report.json 和输出文件
- 并行工作，每人从不同维度审查：
  - Agent 5 (qa-review): 代码质量、PEP 8、逻辑错误、边界情况
  - Agent 6 (qa-test): 写单元测试并运行，验证功能正确
  - Agent 7 (qa-docs): 注释完整性、README 一致性、遗留 TODO/FIXME
  - Agent 8 (qa-perf): 安全漏洞、性能瓶颈、资源泄漏
- 输出审查报告到 `tmp_agent/agent-{id}/review.json`
- 发现 critical 错误必须标记为 error，否则驳回

### 作业流程（每轮 ~30 分钟）

```
时间线

T+0min   协调者启动，读取 TODO.md / CHANGELOG.md
T+1min   A 队 4 个 Agent 并行启动（delegate_task batch）
T+1~8min A 队各自开发：选任务 → 读上下文 → 写代码 → 验证 → 输出 report.json
T+8min   协调者汇总 A 队产物
T+9min   B 队 4 个 Agent 并行启动（delegate_task batch）
T+9~16min B 队各自审查：读产物 → 审查 → 写测试 → 输出 review.json
T+16min  协调者汇总审查报告
T+17min  决策：合并 | 驳回 | 修复（多次驳回后强制合并防死锁）
T+18min  执行 git add → commit → push
T+19min  更新 TODO.md + CHANGELOG.md
T+20min  本轮完成，等待下一轮 cronjob 触发
```

### 通信与产物结构

```
tmp_agent/
├── agent-1/           # A 队 — 核心逻辑 (dev-core)
│   ├── output/        # 代码产出文件
│   └── report.json    # 任务完成报告
├── agent-2/           # A 队 — 工具/接口 (dev-tools)
│   ├── output/
│   └── report.json
├── agent-3/           # A 队 — 知识/Skill (dev-skills)
│   ├── output/
│   └── report.json
├── agent-4/           # A 队 — 记忆/配置 (dev-memory)
│   ├── output/
│   └── report.json
├── agent-5/           # B 队 — 代码审查 (qa-review)
│   ├── output/
│   └── review.json
├── agent-6/           # B 队 — 测试验证 (qa-test)
│   ├── output/
│   └── review.json
├── agent-7/           # B 队 — 文档审查 (qa-docs)
│   ├── output/
│   └── review.json
├── agent-8/           # B 队 — 安全/性能 (qa-perf)
│   ├── output/
│   └── review.json
└── orchestrate/       # 协调者总结
    └── round-N-report.md
```

### 子 Agent 隔离机制

所有子 Agent 运行在完全隔离的上下文中：

- ❌ 不能访问 memory（防止污染）
- ❌ 不能调用 delegate_task（防止无限嵌套）
- ❌ 不能问用户问题（完全自动）
- ❌ 不能操作 Git（只有协调者能提交）
- ✅ 仅访问 `tmp_agent/agent-{id}/` 自己的输出目录

---

## Agent 角色分工表

| 角色 | Agent ID | 技能 | 职责 | 约束 |
|------|----------|------|------|------|
| **协调者** | Agent 0 | orchestrate-swarm | 调度循环、分发任务、审核结果、执行 Git 提交、更新 TODO/CHANGELOG | 不直接写代码；有驳回/强制合并决策权 |
| **A队-核心逻辑** | Agent 1 (dev-core) | dev-cell | 编写算法核心函数、业务逻辑、项目主要 Python 文件 | 每函数 4 层注释；必须通过语法检查 |
| **A队-工具/接口** | Agent 2 (dev-tools) | dev-cell | 开发工具函数、API 接口、CLI 命令、文件操作封装 | 确保被其他 Agent 可调用 |
| **A队-知识/Skill** | Agent 3 (dev-skills) | dev-cell | 创建/更新 SKILL.md、编写文档、维护项目说明 | 只创建可复用的模式 |
| **A队-记忆/配置** | Agent 4 (dev-memory) | dev-cell | 管理配置文件、统计数据、分析 TODO 优先级 | 不能直接调 memory 工具，通过协调者代写 |
| **B队-代码审查** | Agent 5 (qa-review) | qa-cell | 审查代码质量、设计模式、代码异味、PEP 8 合规性 | 输出 critical/major/minor/suggestion 级别 |
| **B队-测试验证** | Agent 6 (qa-test) | qa-cell | 写单元测试、运行测试、验证功能正确性 | 测试文件命名 test_*.py |
| **B队-文档/注释** | Agent 7 (qa-docs) | qa-cell | 检查注释完整性、README 一致性、CHANGELOG 记录 | 检查 TODO/FIXME 遗留 |
| **B队-安全/性能** | Agent 8 (qa-perf) | qa-cell | 检查安全漏洞、性能瓶颈、资源泄漏、安全默认值 | 关注注入/路径遍历/命令执行风险 |

### 严重级别定义（B 队审查输出）

| 级别 | 含义 | 处理方式 |
|------|------|----------|
| critical | 代码有逻辑错误或语法错误 | 必须驳回 |
| major | 设计不合理但有解决方案 | 建议修复，协调者决策 |
| minor | 风格问题或可改进 | 记录，不阻塞合并 |
| suggestion | 未来优化方向 | 记录到 TODO |

---

## 自我进化循环流程

系统通过一个完整的闭环实现自我进化，每轮循环从触发到提交分为五个阶段。

### 阶段 0：触发

整个循环由定时器启动，有两种触发方式：

**方式一：Hermes Cronjob（推荐）**
- Hermes Agent 内建的 cronjob 调度器（`swarm-evolve-round`）
- 每 30 分钟自动加载 `orchestrate-swarm` 技能
- 依赖 Hermes Gateway 常驻运行（通过 tmux 守护）

```
系统时钟 --> Hermes cronjob 调度器 --> 加载 orchestrate-swarm 技能
                                            |
                                            ▼
                                      delegate_task batch
```

**方式二：系统 Cron（备用）**
- Linux crontab 每 30 分钟调用 `cron_trigger.py`
- cron_trigger.py 调用 `self_evolve_round.py`
- 局限性：只能执行 Git 提交 + 状态检查，不能派发子 Agent

```
系统 crontab --> cron_trigger.py --> self_evolve_round.py (状态审计)
```

**方式三：手动触发**
- 直接运行 `python self_evolve_round.py`
- 或 `python self_evolve_round.py --report` 生成详细状态报告
- 或 `python self_evolve_round.py --hermes-run` 通过 Hermes CLI 触发

### 阶段 1：A 队开发（Phase 1 — Develop）

协调者（Agent 0）通过 `delegate_task` 并行派发 4 个子任务给 A 队：

```
协调者 Agent 0
  │
  ├── delegate_task → Agent 1 (dev-core): 从 TODO 选 HIGH 任务，写核心代码
  ├── delegate_task → Agent 2 (dev-tools): 从 TODO 选工具/接口任务
  ├── delegate_task → Agent 3 (dev-skills): 创建/更新 SKILL、文档
  └── delegate_task → Agent 4 (dev-memory): 管理配置、统计数据
```

每个子 Agent 获得裁剪后的上下文（只传与其角色相关的 TODO 子集），在隔离环境中：
1. 读取 TODO.md 选取优先级最高的任务
2. 读取相关项目文件了解上下文
3. 编写代码或更新文件
4. 执行 `python -c "import ast; ast.parse(open('file.py').read())"` 验证语法
5. 输出 `report.json` 到 `tmp_agent/agent-{id}/`

### 阶段 2：B 队审查（Phase 2 — Review）

协调者收集 A 队的产出文件列表，再次通过 `delegate_task` 并行派发 4 个审查任务：

```
协调者 Agent 0
  │
  ├── delegate_task → Agent 5 (qa-review): 审查代码逻辑、风格、异常处理
  ├── delegate_task → Agent 6 (qa-test): 写单元测试并运行
  ├── delegate_task → Agent 7 (qa-docs): 审查注释、文档完整性
  └── delegate_task → Agent 8 (qa-perf): 审查安全漏洞、性能瓶颈
```

每个 B 队 Agent 审查产出文件，输出 `review.json`，包含 issues_found 列表（带 id、severity、category、line、description、suggestion）和总体评分。

### 阶段 3：协调者决策（Phase 3 — Decide）

| 审查结果 | 决策 |
|----------|------|
| 无 critical 问题 | 合并本轮所有改动 |
| 有 critical 且可修 | 派单给对应 A 队 Agent 修复 |
| 有 critical 且不可修 | 驳回本轮 |
| 连续 3 轮被驳回 | 强制合并（防死锁机制） |

### 阶段 4：Git 提交（Phase 4 — Commit）

```
git add -A
git diff --cached --quiet || git commit -m "swarm-evolve: round N — 摘要"
git push 2>&1 || echo "push_failed（跳过）"
```

- 只有协调者可以操作 Git
- Push 失败跳过（国内网络容错），本地 commit 已保证版本安全
- 提交后更新 TODO.md（完成任务标记 `[x]`）和 CHANGELOG.md（记录本轮摘要）
- 协调者将本轮经验写入 memory

### 完整时序图

```
        系统时钟    协调者(Agent0)    A队(1-4)    B队(5-8)    Git仓库    TODO/CHANGELOG
           │             │             │           │           │            │
           │──cronjob───>│             │           │           │            │
           │             │──batch─────>│ 并行开发   │           │            │
           │             │<──report────│           │           │            │
           │             │──batch─────────────────>│ 并行审查   │            │
           │             │<────────review─────────│           │            │
           │             │──决策───────────────────────────────────────────>│
           │             │────────────────────────────────> Git commit     │
           │             │────────────────────────────────────────────> 更新
           │<────done────│             │           │           │            │
```

---

## 开发阶段进度

### 当前状态

| 阶段 | 任务 | 状态 | 说明 |
|------|------|------|------|
| **D1** | 项目初始化 | ✅ 完成 | 创建目录结构、3 个 SKILL、README、TODO、CHANGELOG、Git 仓库、self_evolve_round.py、cron_trigger.py、swarm_utils.py |
| **D2** | 本地模型部署 (Ollama) | ⬜ 待办 | 需安装 Ollama + 下载 Qwen2.5-7B 模型 + 配置 Hermes provider |
| **D3** | Git 仓库配置 | ⬜ 待办 | 需配置 GitHub remote + credential.helper 实现自动 push |
| **D4** | 单 Agent 试跑 | ⬜ 待办 | 需手动触发一次 delegate_task 验证子 Agent 派发正常 |
| **D5** | 创建 cronjob | ⬜ 待办 | 需添加系统 crontab 每 30 分钟触发 `cron_trigger.py` |
| **D6** | 第一轮监控 | ⬜ 待办 | 查看第一轮自动运行产出，必要时调优 |
| **D7+** | 自动运行 | ⬜ 待办 | 零人工干预持续进化 |

### 实际运行情况

虽然 D2-D7 尚未完成，但协调者脚本 `self_evolve_round.py` 已在独立运行并完成 **10 轮状态审计**：

| 轮次 | 时间 | 内容 | Git 提交 |
|------|------|------|----------|
| Round 0 | D1 初始化 | 创建完整项目结构和初始提交 | ✅ |
| Round 1 | 2026-05-15 15:34 | 状态审计：7 个待办任务 | ✅ (本地) |
| Round 2 | 2026-05-15 21:00 | 状态审计：5 个待办任务 | ✅ (本地) |
| Round 3 | 2026-05-15 21:30 | 状态审计：5 个待办任务 | ✅ (本地) |
| Round 4 | 2026-05-15 22:00 | 状态审计：5 个待办任务 | ✅ (本地) |
| Round 5 | 2026-05-16 08:00 | 状态审计：5 个待办任务 | ✅ (本地) |
| Round 6 | 2026-05-16 08:30 | 状态审计：5 个待办任务 | ✅ (本地) |
| Round 7 | 2026-05-16 09:00 | 状态审计：5 个待办任务 | ✅ (本地) |
| Round 8 | 2026-05-16 10:00 | 状态审计：5 个待办任务 | ✅ (本地) |
| Round 9 | 2026-05-16 10:30 | 状态审计：5 个待办任务 | ✅ (本地) |
| Round 10 | 2026-05-16 11:00 | 状态审计：5 个待办任务 | ✅ (本地) |

> 注：Git 提交目前仅限本地（`git push` 因未配置 remote 暂不可用）。

### TODO 完成进度

| 优先级 | 任务 | 状态 |
|--------|------|------|
| HIGH | 实现 `swarm_utils.py` 基础工具函数集 | ✅ 已完成 |
| HIGH | 更新 README.md 补充架构说明 | ✅ 本轮完成 |
| MEDIUM | 实现 `swarm_logger.py` 日志记录工具 | ⬜ 待办 |
| MEDIUM | 创建一个 SKILL 描述如何安全地提交 Git | ⬜ 待办 |
| LOW | 设计 Agent 互相学习的交叉 Skill 导入机制 | ⬜ 待办 |
| LOW | 设计心跳检测机制，确认每个 Agent 健康运行 | ⬜ 待办 |

---

## 技术栈

| 层次 | 方案 | 选型理由 |
|------|------|----------|
| **Agent 框架** | Hermes Agent CLI | 支持 delegate_task 子任务派发、技能系统（SKILL）、cronjob 调度、memory 记忆 |
| **LLM 模型** | 本地开源模型（Ollama / llama.cpp GGUF） | 完全免费，数据不出本机，适合 24h 自动运行 |
| **调度方式** | cronjob + `cron_trigger.py`（系统 crontab） | 每 30 分钟触发 `self_evolve_round.py` |
| **子任务派发** | `delegate_task` | Hermes 原生支持，上下文隔离，batch 模式并行 |
| **技能系统** | SKILL.md（Hermes 原生） | 技能即记忆，可复用、可版本化 |
| **代码仓库** | Git（本地） | 版本控制 + 可见进化轨迹 |
| **Python 环境** | WSL 系统 Python 3 | 项目轻量，不需要额外依赖 |
| **消息队列** | `tmp_agent/` 文件系统 | 每轮产物通过 JSON 文件透传 |
| **后台服务** | tmux + Hermes Gateway | 守护进程自动运行 |

### 推荐本地模型

| 模型 | 运行方式 | 硬件要求 | 推荐场景 |
|------|----------|----------|----------|
| Qwen2.5-7B-Instruct Q4_K_M | Ollama | 8GB VRAM / 16GB RAM | 主 Agent 推理（高理解力） |
| Qwen2.5-Coder-7B Q4_K_M | Ollama | 8GB VRAM / 16GB RAM | 代码生成专用 |
| DeepSeek-Coder-V2-Lite GGUF | llama.cpp | 8GB VRAM / 16GB RAM | 备选 |
| Qwen2.5-3B-Instruct Q4_K_M | Ollama | 4GB VRAM / 8GB RAM | 子 Agent 快速推理 |

### 模型分配策略

| 角色 | 推荐模型 | 理由 |
|------|----------|------|
| 协调者（Agent 0） | 7B 模型 | 需要较强理解和决策能力 |
| A 队（开发） | Code 类模型 或 统一模型 | 代码生成能力优先 |
| B 队（审查） | 7B 模型 | 逻辑推理能力，不需要代码生成 |

> 当前 `delegate_task` 共用同一个 provider 配置，如需区分模型需在 Hermes 设置中配置 provider 切换。

---

## 项目目录结构

```
F:\项目三：多Agent\               # 项目根目录
│
├── README.md                    # 项目说明（本文档）
├── SWARM_RULES.md               # 架构和运行规则（核心文档）
├── TODO.md                      # 待办任务（Agent 驱动更新）
├── CHANGELOG.md                 # 进化日志（Agent 驱动更新）
├── 开发工单.md                   # 完整开发规划文档
├── 面试问答集.md                 # 面试准备 Q&A
├── HERMES_DAEMON.txt            # 一键启动 Hermes 守护进程说明
│
├── .gitignore                   # Git 忽略规则
├── .hermes/                     # Hermes Agent 配置（已 gitignore）
│   └── skills/
│       ├── orchestrate-swarm/   # 协调者调度技能（~/.hermes/skills/下）
│       ├── dev-cell/            # A 队开发技能
│       └── qa-cell/             # B 队质量技能
│
├── self_evolve_round.py         # 协调者脚本 — 每轮循环执行入口
│                                # 三种模式：默认(状态报告)、--report、--hermes-run
├── cron_trigger.py              # 系统 cron 触发器 — 调用 self_evolve_round.py
├── start_hermes_daemon.sh       # tmux 守护进程启动脚本（幂等防重复）
├── swarm_utils.py               # [H] 基础工具函数集（文件读写、日志辅助）
├── swarm_health.py              # [L] 心跳检测机制（HeartbeatPinger / HealthMonitor）
├── test_d4.py                   # [M] 单 Agent 试跑测试文件
│
├── tmp_agent/                   # 每轮工作产物（已 gitignore）
│   ├── agent-1/         # A队 dev-core   — report.json + output/
│   ├── agent-2/         # A队 dev-tools  — report.json + output/
│   ├── agent-3/         # A队 dev-skills — report.json + output/
│   ├── agent-4/         # A队 dev-memory — report.json + output/
│   ├── agent-5/         # B队 qa-review  — review.json + output/
│   ├── agent-6/         # B队 qa-test    — review.json + output/
│   ├── agent-7/         # B队 qa-docs    — review.json + output/
│   ├── agent-8/         # B队 qa-perf    — review.json + output/
│   └── orchestrate/     # 协调者总结     — round-N-report.md
│
├── heartbeats/                  # 心跳检测文件目录（swarm_health.py 生成）
│
├── logs/                        # cron 运行日志
│   ├── cron_stdout.log
│   ├── cron_20260516_*.log
│   └── ...
│
└── requirements.txt             # 依赖（待创建）
```

> **图例：** `[H]` = HIGH 优先级已完成，`[M]` = MEDIUM 优先级已完成，`[L]` = LOW 优先级已完成

---

## Daemon 管理

### 为什么需要 Daemon

Hermes Agent 的 cronjob 调度器是**会话级**的——关闭 Hermes 会话后 cronjob 不会触发。`cronjob list` 能看到 job，但 `last_run_at` 永远为 null。因此需要 tmux + Hermes Gateway 常驻运行。

### 启动守护进程

```bash
cd /mnt/f/项目三：多Agent/
bash start_hermes_daemon.sh
```

`start_hermes_daemon.sh` 脚本做了幂等处理：
- 如果 tmux 会话已存在，不重复启动
- 在 tmux 会话 `hermes-swarm` 中运行 `hermes gateway run`
- Hermes Gateway 常驻后，cronjob 调度器正常工作，每 30 分钟自动触发一轮进化

### 启动后检查

```bash
# 查看守护进程状态
tmux has-session -t hermes-swarm && echo "运行中" || echo "已停止"

# 查看运行日志
tmux attach -t hermes-swarm
# 注意：attach 会进入 tmux 界面，按 Ctrl+B 然后按 D 退出（不中断进程）
```

### 停止守护进程

```bash
tmux kill-session -t hermes-swarm
```

### 重启守护进程

```bash
tmux kill-session -t hermes-swarm 2>/dev/null
bash start_hermes_daemon.sh
```

### 备选方案：系统 Cron（无完整调度能力）

如果 tmux 不可行，可以用 Linux 系统 cron 执行状态检查：

```bash
echo '*/30 * * * * /usr/bin/python3 /mnt/f/项目三：多Agent/cron_trigger.py >> /mnt/f/项目三：多Agent/logs/cron_stdout.log 2>&1' | crontab -
```

但这个方案**只能执行 `self_evolve_round.py`（git commit + 状态检查），不能派发子 Agent**。完整 A→B→Git 闭环需要 Hermes 常驻。

### WSL 重启后的操作

每次重启 WSL 后，tmux 会话消失，需要重新运行：

```bash
bash /mnt/f/项目三：多Agent/start_hermes_daemon.sh
```

---

## 演进路线图

### 阶段划分

```
Phase 1: 基础设施搭建（D1-D3）     ── 当前阶段
┌──────────────────────────────────────────────┐
│ ✅ D1 项目初始化                              │
│ ⬜ D2 本地模型部署 (Ollama + Qwen2.5)         │
│ ⬜ D3 Git 远程仓库配置 (GitHub + credential)  │
└──────────────────────────────────────────────┘

Phase 2: 首次运行验证（D4-D6）     ── 下一阶段
┌──────────────────────────────────────────────┐
│ ⬜ D4 单 Agent 试跑 (delegate_task 验证)      │
│ ⬜ D5 创建 cronjob (每30分钟触发)              │
│ ⬜ D6 第一轮监控与调优                         │
└──────────────────────────────────────────────┘

Phase 3: 自动进化运行（D7+）
┌──────────────────────────────────────────────┐
│ ⬜ D7+ 零人工干预持续运行                      │
└──────────────────────────────────────────────┘
```

### 预期进化轨迹

| 轮次范围 | 预期成果 |
|----------|----------|
| **Round 0** | ✅ 项目结构、SKILL、初始 commit |
| **Round 1-2** | ⏳ 写出第一个工具函数、跑通完整 A→B→Git 调度 |
| **Round 3-5** | 🔜 代码有测试覆盖、B 队发现第一个 bug、Agent 从 memory 学到"上次测试失败因为 X" |
| **Round 6-10** | 🔜 自动重构重复代码、创建新 SKILL、系统自我诊断 |
| **Round 11-20** | 🔮 性能分析、自动优化、Agent 之间协作模式进化 |
| **Round 20+** | 🔮 可能的涌现行为：Agent 之间产生新的协作模式 |

### 防死循环设计

| 风险场景 | 防护措施 |
|----------|----------|
| A 队产出同一个文件冲突 | 每个 Agent 分配固定文件范围，避免重叠 |
| B 队永远不通过 | 协调者有驳回/通过的决策权，多次驳回后强制合并 |
| 无限创建无用 SKILL | 只创建可复用的模式，每次创建需说明理由 |
| Git 永远 no changes | 自动添加新 TODO 任务，确保总有待办 |
| cronjob 死锁 | 每轮 30 分钟，超时后自动下一轮 |
| Ollama 服务宕机 | 检测到 API 不可用后跳过本轮，等待下一轮 |
| TODO 为空 | 自动生成新任务（如"检查项目质量""添加新功能"） |
| 远程 Git 不可用 | 只做本地 commit，跳过 push，在 CHANGELOG 记录 |
| 子 Agent 全部失败 | 协调者跳过本轮，记录失败原因，下一轮继续 |
| 连续 5 轮无变化 | 协调者主动生成"破局任务"（重构、优化、新功能） |

---

## 快速开始

### 前提条件

- WSL Ubuntu（或 Linux 环境）
- Python 3.8+
- Hermes Agent CLI（已安装）
- [可选] Ollama（本地模型推理）

### 启动步骤

```bash
# 1. 进入项目目录
cd /mnt/f/项目三：多Agent/

# 2. 启动 Hermes 守护进程（tmux 后台运行）
bash start_hermes_daemon.sh

# 3. 手动触发一轮测试
python self_evolve_round.py

# 4. 查看产出
cat CHANGELOG.md   # 查看本轮记录
cat TODO.md        # 查看待办更新

# 5. 设置系统 cron（每30分钟自动运行）
crontab -e
# 添加：*/30 * * * * /usr/bin/python3 /mnt/f/项目三：多Agent/cron_trigger.py >> /mnt/f/项目三：多Agent/logs/cron_stdout.log 2>&1
```

### 本地模型部署（D2 待办）

```bash
# 安装 Ollama
curl -fsSL https://ollama.com/install.sh | sh

# 下载模型
ollama pull qwen2.5:7b-instruct-q4_K_M

# 配置 Hermes（~/.hermes/config.yaml）
# providers:
#   ollama:
#     api_base: http://localhost:11434/v1
#     model: qwen2.5:7b-instruct-q4_K_M
#     api_key: ollama
```

---

## 核心机制

### 1. 两队制衡

- **A 队（开发队）**：4 个 Agent 并行编写代码、创建 SKILL、管理知识
- **B 队（质量队）**：4 个 Agent 并行审查代码、写测试、检查文档和安全
- **协调者**：仲裁两队产出，决策合并/驳回/修复

### 2. 自我进化循环

每轮循环 = 开发 → 审查 → 决策 → 提交 → 记录，形成闭环。

```
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│ 触发     │ →  │ A 队开发  │ →  │ B 队审查  │ →  │ 协调者    │ →  │ Git 提交  │
│ cronjob  │    │ 写代码    │    │ 审查质量  │    │ 决策     │    │ + 更新    │
└──────────┘    └──────────┘    └──────────┘    └──────────┘    └──────────┘
```

### 3. 技能系统

Agent 发现可复用的模式 → 自动创建 SKILL → 后续 Agent 可加载使用 → 系统能力持续积累。

当前系统包含三个核心技能：
- **orchestrate-swarm**：协调者调度技能，管理完整的 A→B→Git 循环
- **dev-cell**：A 队开发技能，定义开发规范、验证要求、报告格式
- **qa-cell**：B 队质量技能，定义审查标准、严重级别、报告格式

### 4. 记忆系统

重复出现的失败模式 → 写入 memory → 后续 Agent 避免再犯 → 系统从错误中学习。

注意：只有协调者能操作 memory，子 Agent 不能直接访问。

### 5. 零人工干预

从任务选择、代码编写、质量审查到 Git 提交全部自动完成，用户只需观察进化轨迹。

---

## 相关文档

| 文档 | 用途 |
|------|------|
| [SWARM_RULES.md](./SWARM_RULES.md) | 架构和运行规则（核心） |
| [TODO.md](./TODO.md) | 待办任务清单（Agent 维护） |
| [CHANGELOG.md](./CHANGELOG.md) | 进化历史日志 |
| [开发工单.md](./开发工单.md) | 完整开发规划 |
| [面试问答集.md](./面试问答集.md) | 面试准备材料 |
| [self_evolve_round.py](./self_evolve_round.py) | 协调者脚本源码 |
| [swarm_utils.py](./swarm_utils.py) | 基础工具函数库 |
| [swarm_health.py](./swarm_health.py) | 心跳检测模块 |
| [cron_trigger.py](./cron_trigger.py) | cron 触发器脚本 |
| [start_hermes_daemon.sh](./start_hermes_daemon.sh) | 守护进程启动脚本 |

---

> **项目状态：** 基础设施阶段（D1 完成，D2-D7 待推进）  
> **已运行轮次：** 10 轮（状态审计模式）  
> **下个里程碑：** 部署本地模型 → 激活 A→B→Git 完整调度
