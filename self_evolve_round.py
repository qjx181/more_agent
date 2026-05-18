#!/usr/bin/env python3
"""自进化循环 - Git 后勤脚本（带 PID 文件 + flock + git pull --rebase + 重试 + 冲突处理）。

作用：
  Hermes cronjob 做主要 A→B→Git 调度，本脚本作为后勤兜底：
    1. 用 PID 文件防止重叠执行
    2. 用 fcntl.flock 保护 TODO.md 并发修改
    3. git pull --rebase 后再 commit，避免冲突
    4. 冲突发生时自动中止 rebase、标记冲突文件、暂停流程、等待人工
    5. 对 Git/IO 瞬态失败做指数退避重试（1s, 2s, 4s）
    6. 每步有绝对超时（timeout 参数），防止卡死
    7. 读写 state.json 追踪每步完成状态

为什么这样设计：
  - PID 文件：系统 cron 和 Hermes cron 可能重叠调用，需要互斥
  - flock：self_evolve_round.py 和 Hermes cronjob 可能同时改 TODO.md
  - git pull --rebase：多人/多 Agent 协作时，先同步再提交
  - 冲突中止：自动合并可能破坏代码，宁停勿乱
  - 重试：网络抖动/API 限流是高频故障，重试可自动恢复

面试可能追问：
  - Q: 为什么用 fcntl.flock 而不是文件锁库？ A: lockfile/portalocker 需额外安装，
    fcntl 是 Python 内置，Linux/WSL 开箱即用。
  - Q: PID 文件过时（stale PID）怎么处理？ A: 用 os.kill(pid, 0) 检测，
    如果进程不存在则清理旧 PID 文件并继续。
  - Q: git pull --rebase 和 git pull 什么区别？ A: rebase 保持线性历史，
    避免 merge commit 污染日志。但冲突时需手动处理。
  - Q: 为什么要暂停而不是跳过冲突文件？ A: 代码冲突可能隐藏逻辑错误，
    自动跳过可能导致上线后行为不一致。
"""

import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─── 日志配置 ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | s_evolve | %(message)s",
)
logger = logging.getLogger("self_evolve_round")

# ─── 路径常量 ──────────────────────────────────────────────────────────
PROJECT_ONE = Path("/mnt/c/Users/qjx/Desktop/agent-自进化版/项目一cursor版本/在线部分")
SWARM_DIR = Path("/mnt/f/项目三：多Agent")
TODO_PATH = SWARM_DIR / "TODO.md"
CHANGELOG_PATH = SWARM_DIR / "CHANGELOG.md"
STATE_PATH = SWARM_DIR / "state.json"
PID_FILE = Path("/tmp/swarm_evolve.pid")

# ─── 超时常量（秒）─
GIT_TIMEOUT = 120       # 单次 git 操作
STATE_TIMEOUT = 10      # state.json 读写
PULL_TIMEOUT = 60       # git pull --rebase
FILE_IO_TIMEOUT = 30    # TODO/CHANGELOG 读写

# ─── 重试参数 ──────────────────────────────────────────────────────────
RETRY_DELAYS = [1, 2, 4]  # 第1次重试等1s, 第2次等2s, 第3次等4s
MAX_RETRIES = len(RETRY_DELAYS)


# ═══════════════════════════════════════════════════════════════════════
# 第1层：PID 文件 — 防重叠执行
# ═══════════════════════════════════════════════════════════════════════

def acquire_pid_file() -> bool:
    """获取 PID 文件锁。已存在且进程存活 → 返回 False（不执行）；
    已存在但进程死 → 清理并创建新文件。
    """
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # 检查进程是否存在
            logger.warning("⛔ 前一轮进程 (PID=%d) 仍在运行，跳过本轮", pid)
            return False
        except (ProcessLookupError, OSError):
            logger.info("  → 前一轮进程已退出，清理 stale PID 文件")
            PID_FILE.unlink(missing_ok=True)
        except ValueError:
            PID_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    logger.info("  ✅ PID 文件已创建: PID=%d", os.getpid())
    return True


def release_pid_file():
    """释放 PID 文件。"""
    try:
        PID_FILE.unlink(missing_ok=True)
        logger.info("  ✅ PID 文件已释放")
    except Exception as e:
        logger.warning("  ⚠️ PID 文件释放失败: %s", e)


# ═══════════════════════════════════════════════════════════════════════
# 第2层：state.json — 步骤追踪 + 冲突标记 + 暂停状态
# ═══════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    """加载 state.json，失败时返回默认空状态。"""
    if not STATE_PATH.exists():
        return _default_state()
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("  ⚠️ state.json 读取失败: %s，返回默认状态", e)
        return _default_state()


def save_state(state: dict):
    """原子写入 state.json。"""
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _default_state() -> dict:
    return {
        "current_round": 0, "step": "idle",
        "project_one_step": "idle", "project_three_step": "idle",
        "started_at": None, "completed_at": None,
        "last_error": None, "conflict_files": [],
        "retry_counts": {},
        "paused_due_to_conflict": False,
        "paused_due_to_error": False,
        "manual_intervention_needed": False,
    }


def update_step(step: str, error: Optional[str] = None):
    """更新 state.json 的当前步骤。"""
    state = load_state()
    state["step"] = step
    if error:
        state["last_error"] = error
        state["paused_due_to_error"] = True
    save_state(state)


def mark_conflict(conflict_files: list[str]):
    """标记冲突，暂停流程。"""
    state = load_state()
    state["step"] = "conflict"
    state["conflict_files"] = list(set(state.get("conflict_files", []) + conflict_files))
    state["paused_due_to_conflict"] = True
    state["manual_intervention_needed"] = True
    save_state(state)


# ═══════════════════════════════════════════════════════════════════════
# 第3层：指数退避重试
# ═══════════════════════════════════════════════════════════════════════

def with_retry(fn, step_name: str = "unknown"):
    """执行 fn()，失败时以 1s-2s-4s 间隔重试，最多 3 次重试。
    每次重试前更新 state.json 的 retry_counts。
    """
    last_exception = None
    for attempt in range(MAX_RETRIES + 1):  # 首次 + 3次重试
        try:
            return fn()
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
                OSError, json.JSONDecodeError) as e:
            last_exception = e
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAYS[attempt]
                logger.warning("  ⚠️ [%s] 第%d次失败: %s，%ds后重试...",
                               step_name, attempt + 1, e, delay)
                # 记录重试计数
                state = load_state()
                retries = state.setdefault("retry_counts", {})
                retries[step_name] = attempt + 1
                save_state(state)
                time.sleep(delay)
            else:
                logger.error("  ❌ [%s] 重试%d次后仍失败: %s",
                             step_name, MAX_RETRIES, e)
    raise last_exception  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════
# 第4层：fcntl.flock — TODO.md 并发保护
# ═══════════════════════════════════════════════════════════════════════

def read_todo_with_flock() -> str:
    """用共享锁读 TODO.md，防止同时写时读到脏数据。"""
    if not TODO_PATH.exists():
        return ""
    with open(TODO_PATH, "r", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        content = f.read()
        fcntl.flock(f, fcntl.LOCK_UN)
    return content


def write_todo_with_flock(content: str):
    """用排他锁写 TODO.md，保证只有一个进程在修改。"""
    with open(TODO_PATH, "w", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f, fcntl.LOCK_UN)


def read_todo_first_task() -> str:
    """读取 TODO.md 的第一条未完成任务（带 flock 保护）。"""
    try:
        content = read_todo_with_flock()
        if not content:
            return "TODO.md 不存在或为空"
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("- [ ] "):
                return stripped.replace("- [ ] ", "").strip()
        return "所有待办已完成"
    except Exception as e:
        return f"读取失败: {e}"


# ═══════════════════════════════════════════════════════════════════════
# 第5层：Git 操作（pull --rebase + commit + 冲突检测）
# ═══════════════════════════════════════════════════════════════════════

def git_pull_rebase(repo_dir: Path) -> tuple[bool, list[str]]:
    """git pull --rebase，成功返回 (True, [])；
    冲突返回 (False, [冲突文件列表]) 且已执行 git rebase --abort。
    """
    result = subprocess.run(
        ["git", "pull", "--rebase"],
        cwd=str(repo_dir),
        capture_output=True, text=True,
        timeout=PULL_TIMEOUT,
    )
    output = result.stdout + result.stderr

    if result.returncode == 0:
        return True, []

    # 检测冲突
    conflict_files = []
    for line in output.split("\n"):
        if "CONFLICT" in line and "content" in line:
            # 格式: CONFLICT (content): Merge conflict in path/to/file.py
            parts = line.split(" in ")
            if len(parts) > 1:
                conflict_files.append(parts[-1].strip())

    if conflict_files:
        logger.error("  ❌ 检测到 Git 冲突！冲突文件: %s", conflict_files)
        # 中止 rebase，恢复干净状态
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=str(repo_dir),
            capture_output=True, timeout=30,
        )
        return False, conflict_files

    # 其他错误（网络、权限等）
    logger.warning("  ⚠️ git pull 失败 (rc=%d): %s", result.returncode, output[:300])
    return False, []


def run_git_commit(repo_dir: Path, message: str, skip_pull: bool = False) -> bool:
    """带 git pull --rebase + 冲突检测 + 重试的 commit 函数。

    流程：git pull --rebase → git add -A → git commit
    冲突时：中止 rebase，标记冲突文件到 state.json，暂停流程。
    """
    # ── Step A: git pull --rebase ──
    if not skip_pull:
        logger.info("  → git pull --rebase...")
        ok, conflict_files = git_pull_rebase(repo_dir)
        if not ok and conflict_files:
            mark_conflict(conflict_files)
            logger.error("  ⛔ 因 Git 冲突暂停流程，等待人工介入")
            return False
        if not ok:
            logger.warning("  ⚠️ git pull 非冲突失败，仍尝试直接 commit（可能落后 remote）")

    # ── Step B: git add -A ──
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(repo_dir), check=True, capture_output=True,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.CalledProcessError as e:
        logger.error("  ❌ git add 失败: %s", e)
        raise

    # ── Step C: 检查是否有变更 ──
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(repo_dir), capture_output=True, timeout=GIT_TIMEOUT,
    )
    if result.returncode == 0:
        logger.info("  → 无变更，跳过 commit")
        return True

    # ── Step D: git commit ──
    commit = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(repo_dir),
        capture_output=True, text=True,
        timeout=GIT_TIMEOUT,
    )
    if commit.returncode == 0:
        logger.info("  ✅ commit 成功: %s", commit.stdout.strip()[:120])
        return True
    else:
        logger.warning("  ⚠️ commit 失败: %s", commit.stderr.strip()[:200])
        return False


def run_git_commit_with_retry(repo_dir: Path, message: str,
                              repo_name: str = "project") -> bool:
    """包装 run_git_commit，加 3 次重试（1s-2s-4s）。"""
    def _do():
        return run_git_commit(repo_dir, message)

    try:
        return with_retry(_do, step_name=f"git_commit_{repo_name}")
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════

def main():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("=" * 60)
    logger.info("后勤脚本启动 — %s", timestamp)
    logger.info("=" * 60)

    # ── 0. PID 文件检查（防重叠）─
    if not acquire_pid_file():
        logger.info("本轮跳过（前一轮未结束）")
        return

    try:
        # ── 1. 检查暂停状态 ──
        state = load_state()
        if state.get("paused_due_to_conflict"):
            conflict_files = state.get("conflict_files", [])
            logger.error("⛔ 流程因 Git 冲突暂停，冲突文件: %s", conflict_files)
            logger.error("   请手动解决冲突后，将 state.json 中的 "
                         "'paused_due_to_conflict' 设为 false 再重试")
            return
        if state.get("manual_intervention_needed"):
            logger.error("⛔ 流程因等待人工介入而暂停")
            return

        # ── 2. 读取当前待办（带 flock 保护）─
        update_step("reading_status")
        current_task = read_todo_first_task()
        logger.info("当前待办: %s", current_task)

        # ── 3. 项目一 Git 后勤 ──
        update_step("project_one_sync")
        logger.info("=" * 30)
        logger.info("项目一 Git 后勤:")
        logger.info("=" * 30)
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(PROJECT_ONE),
                capture_output=True, text=True,
                timeout=10,
            )
            if status.returncode != 0:
                logger.warning("  ⚠️ git status 失败，跳过项目一")
            elif status.stdout.strip():
                lines = status.stdout.strip().split("\n")
                logger.info("  ⚠️ 有 %d 个未提交文件", len(lines))
                for line in lines:
                    logger.info("    %s", line)
                run_git_commit_with_retry(
                    PROJECT_ONE,
                    f"swarm-evolve: 后勤自动提交 — {timestamp[:10]}",
                    repo_name="project_one",
                )
            else:
                logger.info("  ✅ 工作区干净")
        except subprocess.TimeoutExpired:
            logger.error("  ❌ git status 超时（10s），跳过项目一同步")

        # ── 4. 项目三 Git 后勤 ──
        update_step("project_three_sync")
        logger.info("=" * 30)
        logger.info("项目三（Swarm）Git 后勤:")
        logger.info("=" * 30)
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(SWARM_DIR),
                capture_output=True, text=True,
                timeout=10,
            )
            if status.returncode != 0:
                logger.warning("  ⚠️ git status 失败，跳过项目三")
            elif status.stdout.strip():
                lines = status.stdout.strip().split("\n")
                logger.info("  ⚠️ 有 %d 个未提交文件", len(lines))
                for line in lines:
                    logger.info("    %s", line)
                run_git_commit_with_retry(
                    SWARM_DIR,
                    f"swarm-evolve: 后勤同步 — {timestamp[:10]}",
                    repo_name="swarm",
                )
            else:
                logger.info("  ✅ 工作区干净")
        except subprocess.TimeoutExpired:
            logger.error("  ❌ git status 超时（10s），跳过项目三同步")

        # ── 5. 更新 state.json 完成状态 ──
        state = load_state()
        state["step"] = "done"
        state["completed_at"] = timestamp
        if not state.get("started_at"):
            state["started_at"] = timestamp
        state["project_one_step"] = "done"
        state["project_three_step"] = "done"
        save_state(state)

        logger.info("")
        logger.info("提示：")
        logger.info("  - 主要 A→B→Git 由 Hermes cronjob 每30分钟自动执行")
        logger.info("  - self_evolve_round.py 仅做 Git 后勤兜底")
        logger.info("  - 如遇冲突，请手动解决后修改 state.json 恢复")

    finally:
        release_pid_file()

    logger.info("=" * 60)
    logger.info("后勤脚本完成 — %s", timestamp)


if __name__ == "__main__":
    main()
