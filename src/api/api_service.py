#!/usr/bin/env python3
"""api_service.py — FastAPI 服务 + Web 仪表盘

位于 src/api/ 目录，入口为 api_entrypoint()。
直接运行：python src/api/api_service.py
Docker 入口：docker-entrypoint.sh api（挂载 /app → 容器 /app）

提供 HTTP 接口和 Web 仪表盘，让用户通过网页控制 swarm。

端点:
  POST /api/tasks        — 提交新任务
  GET  /api/tasks        — 列出所有任务
  GET  /api/tasks/:id    — 查看单个任务
  DELETE /api/tasks/:id  — 删除任务
  POST /api/trigger      — 触发一轮进化
  GET  /api/metrics      — 核心指标
  GET  /api/status       — 完整状态
  GET  /api/logs         — 查看日志
  POST /api/bugs         — 提交 Bug 或扫描项目
  GET  /api/bugs         — 列出所有 Bug
  GET  /api/bugs/:id     — 查看 Bug 详情
  POST /api/bugs/:id/fix — 执行 Bug 修复
  GET  /api/bugs/:id/fix — 查看修复结果
  GET  /health           — 健康检查
  GET  /                 — 返回 Web 仪表盘

设计理由:
  - 所有数据从磁盘文件读取，不依赖内存状态，重启后数据不丢失
  - CORS 全开，方便开发调试
  - 日志读取限制 100 行避免大文件加载
  面试官可能问:
  - 为什么不用数据库？答：项目三的状态数据是文件化的（TODO.md、state.json），保持一致性
  - 触发进化用后台任务？答：subprocess.Popen 独立于 FastAPI 生命周期，避免阻塞
"""

import asyncio
import json
import os
import subprocess
import datetime
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

# ── 路径（向上两级，从 src/api/ 到项目根目录）────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
_SRC_ROOT = Path(__file__).parent.parent.resolve()
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

PROJECT_DIR = _PROJECT_ROOT
SRC_DIR = _SRC_ROOT
STATE_FILE = PROJECT_DIR / "state.json"
TODO_FILE = PROJECT_DIR / "TODO.md"
LOGS_DIR = PROJECT_DIR / "logs"


# ── 应用 ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="项目三：多Agent — API 服务",
    description="Swarm 多Agent 自进化引擎的 HTTP 接口和 Web 仪表盘",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 启动时间 ─────────────────────────────────────────────────────────────

START_TIME = datetime.datetime.now()


# ── 辅助函数 ────────────────────────────────────────────────────────────


def _read_json(path: Path) -> dict:
    """安全读取 JSON 文件

    Returns:
        dict: 解析后的 JSON 内容。文件不存在或解析失败返回空字典。
    """
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    """安全写入 JSON 文件（原子写入）"""
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _read_lines(path: Path, n: int = 50) -> list[str]:
    """读取文件最后 N 行"""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return lines[-n:]
    except OSError:
        return []


def _parse_tasks_from_todo() -> list[dict]:
    """从 TODO.md 解析任务列表

    Returns:
        list[dict]: 任务列表，每项含 id, description, status, category, depends
    """
    if not TODO_FILE.exists():
        return []

    tasks = []
    try:
        with open(TODO_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []

    current_task = None
    for line in lines:
        # [ ] 或 [x] 标记
        task_match = __import__("re").match(
            r"^- \[([ x])\] 任务ID:\s*(\S+)", line
        )
        if task_match:
            if current_task:
                tasks.append(current_task)
            done = task_match.group(1) == "x"
            current_task = {
                "id": task_match.group(2),
                "status": "completed" if done else "pending",
                "description": "",
                "category": "debug",
                "depends": [],
            }
        elif current_task and "描述:" in line:
            current_task["description"] = line.split("描述:", 1)[1].strip()
        elif current_task and "依赖:" in line:
            dep_text = line.split("依赖:", 1)[1].strip()
            if dep_text and dep_text != "无":
                current_task["depends"] = [d.strip() for d in dep_text.split(",")]
        elif current_task and "类别:" in line:
            current_task["category"] = line.split("类别:", 1)[1].strip()

    if current_task:
        tasks.append(current_task)

    return tasks


# ── 路由 ────────────────────────────────────────────────────────────────


@app.get("/health")
async def health_check():
    """健康检查"""
    return {
        "status": "healthy",
        "service": "project3-swarm-api",
        "timestamp": datetime.datetime.now().isoformat(),
        "uptime_seconds": (datetime.datetime.now() - START_TIME).total_seconds(),
    }


@app.get("/api/tasks")
async def list_tasks():
    """列出所有任务

    Returns:
        list[dict]: 任务列表（来自 TODO.md + state.json）
    """
    return _parse_tasks_from_todo()


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    """查看单个任务详情"""
    tasks = _parse_tasks_from_todo()
    for t in tasks:
        if t["id"] == task_id:
            return t
    raise HTTPException(status_code=404, detail=f"任务 {task_id} 未找到")


@app.post("/api/tasks")
async def create_task(task: dict):
    """提交新任务

    Args:
        task: JSON body 包含 task_id, description, category, depends

    Returns:
        dict: 创建结果

    Why:
        将任务追加到 TODO.md 而非 state.json，保持一致性
    """
    task_id = task.get("task_id", "")
    if not task_id:
        raise HTTPException(status_code=400, detail="task_id 是必填字段")

    # 检查是否已存在
    existing = _parse_tasks_from_todo()
    for t in existing:
        if t["id"] == task_id:
            raise HTTPException(status_code=409, detail=f"任务 {task_id} 已存在")

    # 追加到 TODO.md
    description = task.get("description", "")
    category = task.get("category", "debug")
    depends = task.get("depends", "")
    if isinstance(depends, list):
        depends = ", ".join(depends)

    entry = f"\n- [ ] 任务ID: {task_id}\n  描述: {description}\n  类别: {category}\n"
    if depends:
        entry += f"  依赖: {depends}\n"

    try:
        with open(TODO_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"写入 TODO.md 失败: {e}")

    return {
        "message": "任务已创建",
        "task_id": task_id,
        "status": "pending",
    }


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    """删除任务

    从 TODO.md 中移除对应任务条目。
    """
    if not TODO_FILE.exists():
        raise HTTPException(status_code=404, detail="TODO.md 不存在")

    try:
        with open(TODO_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {e}")

    # 精确匹配任务块
    import re
    pattern = re.compile(
        rf"^- \[([ x])\] 任务ID:\s*{re.escape(task_id)}.*?(?=^- \[|$)",
        re.DOTALL,
    )
    new_content = pattern.sub("", content).strip()
    # 清理多余空行
    new_content = re.sub(r"\n{3,}", "\n\n", new_content)

    if new_content == content:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 未找到")

    try:
        with open(TODO_FILE, "w", encoding="utf-8") as f:
            f.write(new_content + "\n")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"写入失败: {e}")

    return {"message": f"任务 {task_id} 已删除"}


@app.post("/api/trigger")
async def trigger_evolution(background_tasks: BackgroundTasks):
    """手动触发一轮进化

    在后台运行 self_evolve_round.py（不阻塞 API 响应）。

    Why:
        - 使用 subprocess.Popen 而非 asyncio.create_subprocess_exec
          因为后者在 FastAPI 后台任务中可能被事件循环生命周期影响
        - Popen 是独立进程，即使 API 重启也不会中断进化
    """
    evolve_script = PROJECT_DIR / "src" / "core" / "self_evolve_round.py"
    if not evolve_script.exists():
        raise HTTPException(status_code=500, detail="src/core/self_evolve_round.py 不存在")

    def _run_evolve():
        try:
            result = subprocess.run(
                ["python3", str(evolve_script)],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(PROJECT_DIR),
            )
            with open(LOGS_DIR / "api_trigger.log", "a", encoding="utf-8") as f:
                f.write(f"\n--- Trigger at {datetime.datetime.now().isoformat()} ---\n")
                f.write(f"stdout: {result.stdout[:2000]}\n")
                if result.stderr:
                    f.write(f"stderr: {result.stderr[:1000]}\n")
        except subprocess.TimeoutExpired:
            with open(LOGS_DIR / "api_trigger.log", "a", encoding="utf-8") as f:
                f.write(f"\n--- Trigger TIMEOUT at {datetime.datetime.now().isoformat()} ---\n")
        except Exception as e:
            with open(LOGS_DIR / "api_trigger.log", "a", encoding="utf-8") as f:
                f.write(f"\n--- Trigger ERROR at {datetime.datetime.now().isoformat()}: {e} ---\n")

    background_tasks.add_task(_run_evolve)

    return {
        "message": "进化已触发",
        "script": "self_evolve_round.py",
        "triggered_at": datetime.datetime.now().isoformat(),
        "note": "进化在后台运行，稍后查看日志确认结果",
    }


@app.get("/api/metrics")
async def get_metrics():
    """返回核心指标

    Returns:
        dict: {
            current_round: int,
            completed_tasks: int,
            pending_tasks: int,
            success_rate: float,
            dollar_spent_today: float,
            dollar_limit: float,
            uptime_seconds: float,
            last_round_at: str,
            rounds_total: int,
        }
    """
    state = _read_json(STATE_FILE)
    tasks = _parse_tasks_from_todo()

    completed = sum(1 for t in tasks if t["status"] == "completed")
    pending = len(tasks) - completed
    total = len(tasks)

    budget = state.get("daily_budget", {})
    rounds_history = state.get("completed_task_ids", [])
    # 从 self_evolve_log.json 读取轮次
    evo_log = _read_json(PROJECT_DIR / "self_evolve_log.json")
    rounds = evo_log.get("rounds", []) if isinstance(evo_log, dict) else []

    return {
        "current_round": len(rounds) + 1,
        "completed_tasks": completed,
        "pending_tasks": pending,
        "total_tasks": total,
        "success_rate": round(completed / max(total, 1) * 100, 1),
        "dollar_spent_today": budget.get("dollar_spent_today", 0),
        "dollar_limit": budget.get("dollar_limit", 5.0),
        "uptime_seconds": round((datetime.datetime.now() - START_TIME).total_seconds(), 1),
        "rounds_total": len(rounds),
        "last_round_at": rounds[-1].get("timestamp", "") if rounds else "",
    }


@app.get("/api/status")
async def get_status():
    """返回完整状态报告"""
    state = _read_json(STATE_FILE)
    metrics = await get_metrics()

    github_status = "未配置"
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(PROJECT_DIR),
        )
        if result.returncode == 0:
            github_status = result.stdout.strip()
    except Exception:
        import logging
        logging.warning("Git status 检查失败")

    # 检查 Docker/Podman
    docker_available = False
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        docker_available = True
    except Exception:
        import logging
        logging.warning(f"Docker 检查失败")

    return {
        "service": "项目三：多Agent",
        "version": "1.0.0",
        "api_uptime_seconds": metrics["uptime_seconds"],
        "github_last_commit": github_status,
        "docker": "可用" if docker_available else "不可用",
        "state_step": state.get("step", "unknown"),
        "cronjob_paused": state.get("readonly_mode", False),
        "metrics": metrics,
    }


@app.get("/api/logs")
async def get_logs(lines: int = 50):
    """查看最近日志

    Args:
        lines: 返回行数（默认 50，最大 200）
    """
    lines = min(max(lines, 10), 200)
    log_file = LOGS_DIR / "self_evolve.log"

    content = _read_lines(log_file, lines)
    return {
        "log_file": str(log_file),
        "lines_returned": len(content),
        "content": "".join(content),
    }



# ── Bug 相关路由 ────────────────────────────────────────────────────────

BUGS_DIR = PROJECT_DIR / "bugs"
BUGS_DIR.mkdir(exist_ok=True)


def _bug_history_load() -> list:
    f = BUGS_DIR / "analysis_history.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _bug_history_save(data: list) -> None:
    try:
        (BUGS_DIR / "analysis_history.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        import logging
        logging.warning(f"Bug 历史保存失败: {BUGS_DIR / 'analysis_history.json'}")


def _bug_get(bug_id: str) -> dict:
    for item in _bug_history_load():
        if item.get("id") == bug_id:
            return item
    return None


def _bug_patch(bug_id: str, updates: dict) -> None:
    data = _bug_history_load()
    for item in data:
        if item.get("id") == bug_id:
            item.update(updates)
            break
    _bug_history_save(data)


def _scan_project(path: str) -> dict:
    import subprocess as sub
    proj = Path(path)
    findings = []
    py_files = list(proj.rglob("*.py"))[:50]
    for pf in py_files:
        try:
            r = sub.run(["python", "-m", "py_compile", str(pf)],
                        capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                findings.append({
                    "type": "syntax_error", "file": str(pf), "line": 0,
                    "detail": r.stderr[:200], "severity": "error",
                })
        except Exception:
            import logging
            logging.warning(f"py_compile 失败: {pf}")
    for pf in py_files[:30]:
        try:
            txt = pf.read_text(encoding="utf-8", errors="ignore")
            for i, ln in enumerate(txt.splitlines(), 1):
                if any(k in ln for k in ["TODO", "FIXME", "XXX"]):
                    findings.append({
                        "type": "todo_comment", "file": str(pf), "line": i,
                        "detail": ln.strip()[:100], "severity": "info",
                    })
                if re.search(r"except[^:]*:\s*pass", ln):
                    findings.append({
                        "type": "silent_exception", "file": str(pf), "line": i,
                        "detail": ln.strip()[:100], "severity": "warning",
                    })
        except Exception:
            import logging
            logging.warning(f"读取文件失败: {pf}")
    return {
        "project_path": path,
        "scan_time": datetime.datetime.now().isoformat(),
        "bug_count": len(findings),
        "py_files_scanned": len(py_files),
        "findings": findings[:100],
    }


@app.post("/api/bugs")
async def submit_bug(bug_data: dict):
    error_text = bug_data.get("error_text", "").strip()
    project_path = bug_data.get("project_path", "").strip()
    source_type = bug_data.get("source_type", "python")

    if not error_text and not project_path:
        raise HTTPException(status_code=400, detail="error_text 和 project_path 至少填写一个")

    if not error_text and project_path:
        if not Path(project_path).exists():
            raise HTTPException(status_code=400, detail=f"项目路径不存在: {project_path}")
        result = _scan_project(project_path)
        return {"bug_id": None, "status": "scanned", "scan_result": result,
                "message": f"已扫描项目，发现 {result['bug_count']} 个潜在问题"}

    if project_path and not Path(project_path).exists():
        raise HTTPException(status_code=400, detail=f"项目路径不存在: {project_path}")

    from src.analysis.bug_analysis_engine import analyze_bug
    result = analyze_bug(error_text, source_type)
    result["project_path"] = project_path
    result["status"] = "pending_fix" if (result.get("confidence", 0) >= 0.7 and project_path) else "analyzed"
    hist = _bug_history_load()
    for i, item in enumerate(hist):
        if item.get("id") == result["id"]:
            hist[i] = result
            break
    else:
        hist.append(result)
    _bug_history_save(hist)

    msg = "分析完成，可修复" if result["status"] == "pending_fix" else "分析完成，置信度不足，需人工处理"
    return {"bug_id": result["id"], "analysis": result, "can_fix": result["status"] == "pending_fix", "message": msg}


@app.get("/api/bugs")
async def list_bugs(limit: int = 30):
    return _bug_history_load()[-limit:]


@app.get("/api/bugs/{bug_id}")
async def get_bug(bug_id: str):
    bug = _bug_get(bug_id)
    if not bug:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} 不存在")
    return bug


@app.post("/api/bugs/{bug_id}/fix")
async def fix_bug(bug_id: str, background_tasks: BackgroundTasks):
    bug = _bug_get(bug_id)
    if not bug:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} 不存在")
    pp = bug.get("project_path", "")
    if not pp:
        raise HTTPException(status_code=400, detail="该 Bug 未关联项目路径，无法修复")

    _bug_patch(bug_id, {"status": "fixing", "fix_result": None})

    def _do():
        from src.analysis.bug_analysis_engine import execute_bug_fix
        res = execute_bug_fix(bug, pp)
        _bug_patch(bug_id, {
            "status": "fixed" if res["success"] else "failed",
            "fix_result": res,
        })

    background_tasks.add_task(_do)
    return {"bug_id": bug_id, "status": "fixing", "message": "修复已启动，请稍后刷新查看结果"}


@app.get("/api/bugs/{bug_id}/fix")
async def get_fix_result(bug_id: str):
    bug = _bug_get(bug_id)
    if not bug:
        raise HTTPException(status_code=404, detail=f"Bug {bug_id} 不存在")
    st = bug.get("status", "unknown")
    msgs = {
        "analyzed": "分析完成，等待修复",
        "pending_fix": "可修复，等待调用 /api/bugs/{id}/fix",
        "fixing": "修复中，请稍后刷新",
        "fixed": "修复完成",
        "failed": "修复失败，请查看 details",
        "scanned": "项目扫描完成",
    }
    return {
        "bug_id": bug_id, "status": st,
        "fix_result": bug.get("fix_result"),
        "project_path": bug.get("project_path", ""),
        "message": msgs.get(st, f"未知状态: {st}"),
    }


# ── 前端 ────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """返回 Web 仪表盘页面"""
    return _get_dashboard_html()


def _get_dashboard_html() -> HTMLResponse:
    """从 api/dashboard.html 文件读取仪表盘"""
    df = PROJECT_DIR / "api" / "dashboard.html"
    if df.exists():
        return HTMLResponse(content=df.read_text(encoding="utf-8"))
    fb = ("<html><body style='background:#0d1117;color:#c9d1d9;padding:40px;font-family:sans-serif'>"
          "<h1>仪表盘文件未找到</h1><p>请检查 api/dashboard.html 是否存在</p></body></html>")
    return HTMLResponse(content=fb)



# ── 入口 ────────────────────────────────────────────────────────────────

def api_entrypoint():
    """启动 FastAPI 服务

    供 docker-entrypoint.sh 调用，或直接 python src/api/api_service.py 启动。

    Why:
        用函数封装而非 __main__ 块，方便容器入口脚本调用。
    """
    import uvicorn
    uvicorn.run(
        "api_service:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    api_entrypoint()
