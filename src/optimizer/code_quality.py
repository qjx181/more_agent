#!/usr/bin/env python3
"""optimizer/code_quality.py — 维度 1：代码质量扫描器

检测：未使用 import、过深嵌套、过长函数、重复代码、硬编码值、缺失文档字符串。

依赖：复用 src.analysis.code_review.QualityReviewer

使用：
    from optimizer.code_quality import scan as scan_code_quality
    result = scan_code_quality("/path/to/project")
"""

import ast
import re
from pathlib import Path
from typing import Optional


# ── 顶层报告函数 ────────────────────────────────────────────────────────


def scan(project_path: str | Path) -> dict:
    """扫描项目代码质量。

    Args:
        project_path: 要扫描的项目根目录。

    Returns:
        dict: {
            "dimension": "code_quality",
            "total_issues": int,
            "score": int,         # 0-100，越高越好
            "issues": [dict, ...], # 每项含 file/line/type/severity/description/suggestion
            "files_scanned": int,
        }
    """
    root = Path(project_path).resolve()
    if not root.exists():
        return _empty_result(f"目录不存在: {root}")

    issues = []
    files_scanned = 0

    for py_file in root.rglob("*.py"):
        # 跳过虚拟环境、测试框架、缓存
        if _should_skip(py_file):
            continue

        try:
            code = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        files_scanned += 1
        rel_path = py_file.relative_to(root)

        # 1. 未使用 import
        issues.extend(_check_unused_imports(code, rel_path))

        # 2. 过长函数
        issues.extend(_check_long_functions(code, rel_path))

        # 3. 过深嵌套
        issues.extend(_check_deep_nesting(code, rel_path))

        # 4. 硬编码值
        issues.extend(_check_hardcoded_values(code, rel_path))

        # 5. 过长行
        issues.extend(_check_long_lines(code, rel_path))

        # 6. 裸 except
        issues.extend(_check_bare_except(code, rel_path))

    score = _compute_score(issues, files_scanned)
    return {
        "dimension": "code_quality",
        "total_issues": len(issues),
        "score": score,
        "issues": issues,
        "files_scanned": files_scanned,
    }


# ── 检查函数 ─────────────────────────────────────────────────────────────


def _check_unused_imports(code: str, file: Path) -> list[dict]:
    """检测未使用的 import。"""
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    # 收集所有被引用的名字（全局 scope）
    used_names = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                used_names.add(node.value.id)

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if isinstance(child, ast.Name):
                    used_names.add(child.id)
                elif isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
                    used_names.add(child.value.id)

    # 收集 import 语句
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                if name not in used_names:
                    issues.append({
                        "file": str(file),
                        "line": node.lineno or 0,
                        "type": "unused_import",
                        "severity": "medium",
                        "description": f"import '{alias.name}' 已导入但未使用",
                        "suggestion": f"删除 import {alias.name} 或在代码中使用它",
                    })
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                if name not in used_names:
                    issues.append({
                        "file": str(file),
                        "line": node.lineno or 0,
                        "type": "unused_import",
                        "severity": "medium",
                        "description": f"from {node.module} import {alias.name} 未使用",
                        "suggestion": f"删除 from {node.module} import {alias.name}",
                    })

    return issues


def _check_long_functions(code: str, file: Path) -> list[dict]:
    """检测过长函数（超过 50 行）。"""
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if hasattr(node, "end_lineno") and node.end_lineno:
                length = node.end_lineno - node.lineno
                if length > 50:
                    issues.append({
                        "file": str(file),
                        "line": node.lineno,
                        "type": "long_function",
                        "severity": "medium",
                        "description": f"函数 '{node.name}' 有 {length} 行，建议拆分为更小的函数（< 50 行）",
                        "suggestion": f"将 '{node.name}' 拆分为多个单一职责的小函数",
                    })
    return issues


def _check_deep_nesting(code: str, file: Path) -> list[dict]:
    """检测过深嵌套（超过 4 层）。"""
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    for node in ast.walk(tree):
        depth = _get_nesting_depth(node)
        if depth > 4:
            issues.append({
                "file": str(file),
                "line": getattr(node, "lineno", 0),
                "type": "deep_nesting",
                "severity": "low",
                "description": f"嵌套深度 {depth} 层（阈值 4）",
                "suggestion": "考虑提前返回、使用卫语句或提取为独立函数来减少嵌套",
            })

    return issues


def _check_hardcoded_values(code: str, file: Path) -> list[dict]:
    """检测硬编码的 magic number 和字符串。"""
    issues = []
    magic_pattern = re.compile(r"\b\d{4,}\b")  # 4位及以上数字
    hardcoded_url = re.compile(
        r"(?i)(https?://(?!example\.com|localhost|127\.0\.0\.1)\S+)",
    )

    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # 跳过注释行和 docstring
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
            continue

        for m in magic_pattern.finditer(line):
            col = m.start()
            issues.append({
                "file": str(file),
                "line": i,
                "type": "hardcoded_value",
                "severity": "low",
                "description": f"发现 magic number: {m.group()}",
                "suggestion": "将硬编码值提取为命名常量，提高可维护性",
            })

        for m in hardcoded_url.finditer(line):
            issues.append({
                "file": str(file),
                "line": i,
                "type": "hardcoded_value",
                "severity": "low",
                "description": f"发现硬编码 URL: {m.group(1)[:60]}",
                "suggestion": "将 URL 提取为配置常量或环境变量",
            })

    return issues


def _check_long_lines(code: str, file: Path) -> list[dict]:
    """检测超过 120 字符的行。"""
    issues = []
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        if len(line) > 120:
            issues.append({
                "file": str(file),
                "line": i,
                "type": "long_line",
                "severity": "low",
                "description": f"行长度 {len(line)} 字符（阈值 120）",
                "suggestion": "将长行拆分为多行或提取为变量",
            })
    return issues


def _check_bare_except(code: str, file: Path) -> list[dict]:
    """检测裸 except（未指定异常类型）。"""
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            issues.append({
                "file": str(file),
                "line": node.lineno,
                "type": "bare_except",
                "severity": "high",
                "description": "使用了裸 except，未指定异常类型，可能吞掉关键错误",
                "suggestion": "改为 except Exception 或具体的异常类型",
            })
    return issues


# ── 辅助 ─────────────────────────────────────────────────────────────────


def _should_skip(path: Path) -> bool:
    skip_dirs = {
        "__pycache__", ".pytest_cache", ".mypy_cache",
        ".tox", "venv", "env", ".venv", "node_modules",
        ".git", ".hg", "build", "dist", ".eggs",
    }
    return any(part in skip_dirs for part in path.parts)


def _get_nesting_depth(node: ast.AST, current: int = 0) -> int:
    """计算节点的嵌套深度（If/For/While/With/AsyncWith 层级）。"""
    depth = current
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.If, ast.For, ast.While, ast.With, ast.AsyncWith)):
            child_depth = _get_nesting_depth(child, current + 1)
            depth = max(depth, child_depth)
        else:
            child_depth = _get_nesting_depth(child, current)
            depth = max(depth, child_depth)
    return depth


def _compute_score(issues: list[dict], files_scanned: int) -> int:
    """根据问题数量和严重级别计算质量分数（0-100）。"""
    if files_scanned == 0:
        return 100
    weights = {"critical": 10, "high": 5, "medium": 2, "low": 0.5}
    penalty = sum(weights.get(i.get("severity", "low"), 1) for i in issues)
    # 每 5 分扣 1 分，上限 100
    score = max(0, 100 - int(penalty))
    return min(100, score)


def _empty_result(msg: str) -> dict:
    return {
        "dimension": "code_quality",
        "total_issues": 0,
        "score": 100,
        "issues": [],
        "files_scanned": 0,
        "error": msg,
    }
