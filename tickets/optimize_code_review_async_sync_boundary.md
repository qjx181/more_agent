# code_review 增强：async/sync 边界检测 — 开发工单

> 项目三自身优化工单
> 创建日期：2026-05-19
> 状态：⬜ 待开发

---

## 一、背景

在 2026-05-19 的 P1 Bug 扫描中，发现了一个真实的**临界 Bug**：
- `chat_service._build_messages(sync)` → `build_context_sync` → `_sync_build_context` 在检测到运行中事件循环时主动 `raise RuntimeError`
- 该 Bug 导致每次 chat 请求在运行时崩溃
- **项目三现有的 code_review 模块完全未检测到该问题**（311 个候选问题全是误报）

暴露的短板：code_review 的 `PerformanceReviewer` 只能检查 `check_sync_io_in_async`（同步 I/O 在 async 函数中），但无法检测更隐蔽的**async/sync 边界错误**——即 sync 包装器把自己的 async 内部函数用 `asyncio.run()` 包裹，然后在事件循环中调用时主动异常。

## 二、目标

在 code_review.py 的 `PerformanceReviewer` 中新增 **async/sync 边界分析器** (`AsyncSyncBoundaryChecker`)，能静态检测以下三类模式：

| 模式 | 说明 | 风险等级 |
|------|------|----------|
| **sync-wrapper-raises** | sync 包装器检测运行中事件循环后 raise | Critical |
| **sync-calls-async** | sync 函数调用 async 函数但未在单独线程/进程中运行 | High |
| **asyncio-run-in-loop** | `asyncio.run()` 在已有事件循环的上下文中被调用 | High |

## 三、技术方案

### 3.1 核心设计

新增类 `AsyncSyncBoundaryChecker`，使用 **AST 分析 + 启发式规则** 双重检测：

```
AsyncSyncBoundaryChecker
├── check_sync_wrapper_raises(code)     → 检测模式1
├── check_sync_calls_async_def(code)    → 检测模式2
├── check_asyncio_run_in_func(code)     → 检测模式3
└── check_codebase_import_chain(files)  → 跨文件导入链分析（追踪 sync 函数被导入到 async 调用方）
```

### 3.2 检测模式详解

**模式1: sync-wrapper-raises**
- AST 匹配：在 sync `def` 函数中查找 `asyncio.get_running_loop()` 调用
- 如果随后有 `if loop and loop.is_running(): raise RuntimeError` 模式
- → 输出 `severity: critical` 警告

```python
# 检测目标（来自 retrieval.py 的真实代码）
def _sync_build_context(query, role):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        raise RuntimeError("build_context is now async. ...")  # ← 命中
    return asyncio.run(build_context(query, role))
```

**模式2: sync-calls-async**
- 在 sync `def` 函数中检测 `asyncio.run(some_async_func(...))`
- 如果该 sync 函数被标记为在 async 上下文中调用（通过跨文件导入链分析）
- → 输出 `severity: high` 警告

**模式3: asyncio-run-in-loop**
- 在任何 sync `def` 中检测 `asyncio.run(coro)` 调用
- 如果函数定义在 `_sync_`, `_sync_` 包装器、或 `_wrap_` 开头的函数中
- → 输出 `severity: high` 警告
- 简单启发式：名字含 `_sync` 的 sync 函数内调用 `asyncio.run()` 就是潜在问题

**跨文件导入链分析（进阶）**
- 扫描 `from X import Y as Z` 和 `from X import Y` 语句
- 追踪 sync 函数被当成 async 函数的别名导入到另一个模块
- 检查调用方是否在 async 函数中（AST 搜索 `await Z(...)` 在调用方代码中不存在）
- 如果 sync 函数被导入到 async 模块但调用处没有 `await` → 报 `high`

### 3.3 与现有模块的集成

- 新检查器集成到 `PerformanceReviewer.check_all()` 中
- 输出格式与现有审查器一致（`type/severity/line/code/description/suggestion`）
- 不破坏现有测试，新增专门的测试文件 `tests/test_async_sync_boundary.py`

### 3.4 测试用例

需要覆盖以下场景：

| 测试用例 | 输入 | 预期输出 |
|----------|------|----------|
| sync_wrapper_with_get_running_loop | `_sync_build_context` 示例代码 | critical 级别告警 |
| asyncio_run_in_sync_func | `def sync_fn(): asyncio.run(...)` | high 级别告警 |
| asyncio_run_in_regular_func | `def normal(): asyncio.run(coro)` | 无告警（需要导入链信息） |
| no_issue_normal_async | 纯 async 函数 | 无告警 |
| no_issue_sync_no_asyncio | sync 函数无 async 操作 | 无告警 |

### 3.5 误报控制

- 白名单模式：函数名若包含 `main`、`setup`、`init`、`run` 等代表"入口"的词汇，降低告警级别
- `asyncio.run()` 只在**非入口函数**中告警
- 全局标志 `_ignore_entry_points=["main", "setup", "manage", "cli"]` 可配置

## 四、涉及文件

| 文件 | 改动内容 |
|------|----------|
| `code_review.py` | 新增 `AsyncSyncBoundaryChecker` 类，集成到 `PerformanceReviewer.check_all()` |
| `tests/test_async_sync_boundary.py` | 新增测试文件，覆盖 5+ 场景 |
| `performance_profile.json`（可选） | 更新性能基线 |

## 五、验收标准

| 验收项 | 通过标准 |
|--------|----------|
| 能检测 sync-wrapper-raises | 用 `_sync_build_context` 示例测试 → 返回 critical 级别告警 |
| 能检测 asyncio-run-in-loop | 用 `def f(): asyncio.run(g())` 示例测试 → 返回 high 级别告警 |
| 跨文件导入链 | 提供两个文件模拟 → 检测 sync 被导入 async 调用方无 await |
| 误报率 | 对纯 async 项目和简单 sync 代码 0 误报 |
| 模块集成 | `PRReviewer` 输出中包含新增的 async/sync 边界问题 |
| 已有测试 | 不影响 `test_swarm_utils.py` 等现有测试 |

## 六、开发阶段

| 阶段 | 任务 | 预计耗时 | 里程碑 |
|------|------|----------|--------|
| D1 | 实现 AsyncSyncBoundaryChecker 基础类（模式1+模式3） | 20 分钟 | 3 种基础模式可检测 |
| D2 | 实现跨文件导入链分析（模式2 进阶版） | 20 分钟 | 跨文件检测可用 |
| D3 | 集成到 PRReviewer + 自行测试验证 | 15 分钟 | 可对整个项目一生成报告 |
| D4 | 编写测试文件 `test_async_sync_boundary.py` | 20 分钟 | 5+ 测试用例通过 |

## 七、交付物

1. `code_review.py` 中新增的 `AsyncSyncBoundaryChecker` 类
2. `tests/test_async_sync_boundary.py` 测试文件
3. 用新的扫描器重新扫描项目一，确认能检测到 BUG-001
4. 更新 TODO.md 标记完成
