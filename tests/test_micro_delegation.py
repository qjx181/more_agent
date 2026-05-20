"""test_micro_delegation.py — micro_delegation.py 拆分/验证/聚合逻辑测试
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).parent.parent.resolve()
SRC_DIR = PROJECT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(PROJECT_DIR))

from src.agents.micro_delegation import (
    split_big_task,
    build_micro_goal,
    verify_micro_result,
    aggregate_micro_results,
    plan_micro_delegations,
    is_forbidden,
    load_task_registry,
    get_task_type,
)


# ═══════════════════════════════════════════════════════════════════════
# 1. 任务注册表
# ═══════════════════════════════════════════════════════════════════════


def test_load_task_registry():
    """测试 delegable_tasks.json 加载。"""
    registry = load_task_registry()
    assert len(registry["task_types"]) >= 10
    type_ids = [t["id"] for t in registry["task_types"]]
    for required in ["insert_line", "replace_string", "delete_line"]:
        assert required in type_ids, f"缺少必要类型: {required}"


def test_get_task_type():
    """测试按 ID 查找任务类型。"""
    tt = get_task_type("replace_string")
    assert tt is not None
    assert "allowed_tools" in tt
    assert "patch" in tt["allowed_tools"]


def test_is_forbidden():
    """测试禁区检查——查询禁区列表中的关键词。"""
    # 读取禁区列表的实际关键词
    registry = load_task_registry()
    forbidden_keywords = []
    for ft in registry.get("forbidden_tasks", []):
        forbidden_keywords.append(ft.get("keyword", "").lower())
    
    # 测试实际禁区关键词
    if forbidden_keywords:
        for kw in forbidden_keywords:
            if kw:
                forbidden, reason = is_forbidden(f"测试 {kw}")
                assert forbidden, f"关键词 '{kw}' 应命中禁区"
                assert reason
                break
    
    # 正常任务应通过
    forbidden, reason = is_forbidden("修改 config.yaml 添加字段")
    assert not forbidden, "修改配置文件不应命中禁区"


# ═══════════════════════════════════════════════════════════════════════
# 2. 拆分逻辑
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("task_id, expected_max, task_desc", [
    ("metrics_sqlite_storage", 5, "SQLite 指标存储"),
    ("delegation_validation_loop", 3, "委托验证闭环"),
    ("cost_tracker_persistence", 3, "成本跟踪持久化"),
    ("git_autopush_safety", 2, "git push 安全"),
    ("heartbeat_self_healing", 2, "心跳自愈"),
])
def test_split_big_task_known(task_id, expected_max, task_desc):
    """测试已知任务的预设拆分。"""
    micros = split_big_task(task_id, task_desc)
    assert len(micros) >= 1, f"{task_id}: 应至少拆出 1 个微任务"
    assert len(micros) <= expected_max, f"{task_id}: 不应超过 {expected_max} 个"
    for m in micros:
        assert "id" in m
        assert "task_type" in m
        assert "params" in m


def test_split_unknown_task():
    """测试未知任务的通用拆分。"""
    micros = split_big_task("unknown_task_xyz", "这是一个不存在的任务")
    assert isinstance(micros, list)


def test_split_forbidden_task():
    """测试禁区任务应返回跳过微任务。"""
    micros = split_big_task("create_test_file", "从零创建一个测试文件")
    assert len(micros) == 1
    assert micros[0]["task_type"] == "run_command" or micros[0]["task_type"] == "skip"


# ═══════════════════════════════════════════════════════════════════════
# 3. 构建委托 Goal
# ═══════════════════════════════════════════════════════════════════════


def test_build_micro_goal_replace_string():
    """测试 replace_string 类型的 goal。"""
    micro_task = {
        "id": "micro-001",
        "task_type": "replace_string",
        "params": {
            "file": "config.yaml",
            "old_string": "auto_push: false",
            "new_string": "auto_push: true",
        },
        "expected_outcome": "config.yaml 新增 auto_push",
    }
    goal = build_micro_goal(micro_task)
    assert goal
    assert "patch" in goal.lower() or "patch" in goal
    assert "auto_push" in goal


def test_build_micro_goal_no_task_type():
    """测试无匹配类型的 fallback。"""
    micro_task = {
        "id": "micro-099",
        "task_type": "nonexistent_type",
        "params": {"command": "echo test"},
        "expected_outcome": "无",
    }
    goal = build_micro_goal(micro_task)
    assert goal


# ═══════════════════════════════════════════════════════════════════════
# 4. 验证逻辑
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("summary,expected_status", [
    ("SUCCESS", "pass"),
    ("SUCCESS: patch 已完成", "pass"),
    ("FAIL", "fail"),
    ("FAIL: 未找到匹配文本", "fail"),
    ("ERROR: 无法读取文件", "fail"),
    ("正在处理中...", "pass"),
    ("", "pass"),
])
def test_verify_micro_result(summary, expected_status):
    """测试验证结果分类。"""
    micro_task = {"id": "micro-001", "expected_outcome": "test"}
    result = verify_micro_result(micro_task, summary)
    assert result["status"] == expected_status
    assert result["micro_id"] == "micro-001"
    assert "can_retry" in result


def test_verify_micro_result_can_retry():
    """测试失败时标记可重试。"""
    micro_task = {"id": "micro-002"}
    result = verify_micro_result(micro_task, "FAIL")
    assert result["can_retry"] is True


# ═══════════════════════════════════════════════════════════════════════
# 5. 聚合逻辑
# ═══════════════════════════════════════════════════════════════════════


def test_aggregate_all_pass():
    """测试全部通过。"""
    results = [
        {"micro_id": "m1", "status": "pass", "reason": "ok", "can_retry": False},
        {"micro_id": "m2", "status": "pass", "reason": "ok", "can_retry": False},
    ]
    agg = aggregate_micro_results(results)
    assert agg["all_passed"] is True
    assert agg["passed"] == 2
    assert agg["failed"] == 0


def test_aggregate_some_fail():
    """测试部分失败。"""
    results = [
        {"micro_id": "m1", "status": "pass", "reason": "ok", "can_retry": False},
        {"micro_id": "m2", "status": "fail", "reason": "文件不存在", "can_retry": True},
    ]
    agg = aggregate_micro_results(results)
    assert agg["all_passed"] is False
    assert agg["passed"] == 1
    assert agg["failed"] == 1
    assert len(agg["failure_details"]) == 1


def test_aggregate_empty():
    """测试空列表。"""
    agg = aggregate_micro_results([])
    assert agg["passed"] == 0
    assert agg["failed"] == 0
    assert agg["all_passed"] is False


# ═══════════════════════════════════════════════════════════════════════
# 6. 集成
# ═══════════════════════════════════════════════════════════════════════


def test_plan_micro_delegations():
    """测试 plan_micro_delegations() 不抛异常。"""
    state_file = PROJECT_DIR / "state.json"
    if not state_file.exists():
        pytest.skip("state.json 不存在")
    original = state_file.read_text()
    try:
        plan = plan_micro_delegations()
        if plan is not None:
            assert "tasks" in plan
    finally:
        state_file.write_text(original)
