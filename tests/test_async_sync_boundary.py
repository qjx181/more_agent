"""test_async_sync_boundary.py — AsyncSyncBoundaryChecker 测试

覆盖 7 个测试用例，包含 3 种检测模式和误报控制：
  1) sync_wrapper_raises        — 模式1: sync-wrapper-raises（Critical）
  2) asyncio_run_in_sync_func   — 模式3: asyncio-run-in-loop（High）
  3) asyncio_run_in_wrapper     — 模式3: _sync 包装器调用 asyncio.run（High）
  4) sync_calls_async           — 模式2: sync 函数 call async 函数（High）
  5) no_issue_async_function    — 纯 async 函数：零误报
  6) no_issue_entry_point       — 入口函数 main/setup：零误报
  7) no_issue_pure_sync         — 纯 sync 函数无 async 操作：零误报
  8) cross_file_import_chain    — 跨文件导入链分析
"""

import sys
import tempfile
import os
from pathlib import Path

import pytest

# 自动计算项目根目录（不依赖硬编码路径）
PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(PROJECT_DIR))

from src.analysis.code_review import AsyncSyncBoundaryChecker, PRReviewer


# ═══════════════════════════════════════════════════════════════════════
# 模式1: sync-wrapper-raises 测试
# ═══════════════════════════════════════════════════════════════════════


class TestSyncWrapperRaises:
    """模式1: sync 包装器检测到运行中事件循环后主动 raise"""

    def test_detects_sync_build_context_pattern(self):
        """典型 BUG-001 模式：_sync_build_context 调用 get_running_loop + raise"""
        code = '''
import asyncio

def _sync_build_context(query, role):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        raise RuntimeError("build_context is now async")
    return asyncio.run(build_context(query, role))
'''
        issues = AsyncSyncBoundaryChecker.check_sync_wrapper_raises(code)
        assert len(issues) >= 1
        assert issues[0]["severity"] == "critical"
        assert issues[0]["type"] == "sync_wrapper_raises"
        assert "_sync_build_context" in issues[0]["description"]

    def test_detects_any_sync_func_with_get_running_loop_and_raise(self):
        """任何 sync 函数中同时有 get_running_loop + raise RuntimeError 都应命中"""
        code = '''
import asyncio

def my_wrapper():
    loop = asyncio.get_running_loop()
    if loop.is_running():
        raise RuntimeError("cannot call in running loop")
'''
        issues = AsyncSyncBoundaryChecker.check_sync_wrapper_raises(code)
        assert len(issues) >= 1
        assert issues[0]["severity"] == "critical"

    def test_no_false_positive_for_async_function(self):
        """async def 函数不应被检测"""
        code = '''
import asyncio

async def handler():
    loop = asyncio.get_running_loop()
    return loop
'''
        issues = AsyncSyncBoundaryChecker.check_sync_wrapper_raises(code)
        assert len(issues) == 0

    def test_no_false_positive_get_running_loop_without_raise(self):
        """只有 get_running_loop 但没有 raise 不应被报（非 wrapper 模式）"""
        code = '''
import asyncio

def check_loop():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    return loop is not None
'''
        issues = AsyncSyncBoundaryChecker.check_sync_wrapper_raises(code)
        assert len(issues) == 0


# ═══════════════════════════════════════════════════════════════════════
# 模式3: asyncio-run-in-loop 测试
# ═══════════════════════════════════════════════════════════════════════


class TestAsyncioRunInLoop:
    """模式3: 同步函数中调用 asyncio.run()"""

    def test_wrapper_sync_function_detected_as_high(self):
        """_sync 包装器调用 asyncio.run() 应为 high"""
        code = '''
import asyncio

def _sync_fetch(url):
    return asyncio.run(fetch_data(url))
'''
        issues = AsyncSyncBoundaryChecker.check_asyncio_run_in_func(code)
        assert len(issues) >= 1
        assert issues[0]["severity"] == "high"
        assert issues[0]["type"] == "asyncio_run_in_loop"

    def test_regular_sync_function_detected_as_medium(self):
        """普通 sync 函数调用 asyncio.run() 应为 medium"""
        code = '''
import asyncio

def get_data():
    return asyncio.run(fetch_data())
'''
        issues = AsyncSyncBoundaryChecker.check_asyncio_run_in_func(code)
        assert len(issues) >= 1
        # 非包装器应为 medium
        assert issues[0]["severity"] == "medium"

    def test_entry_point_ignored(self):
        """main() 等入口函数应被白名单忽略"""
        code = '''
import asyncio

def main():
    return asyncio.run(start_app())

def setup():
    return asyncio.run(configure())
'''
        issues = AsyncSyncBoundaryChecker.check_asyncio_run_in_func(code)
        # 入口函数应被忽略
        names_found = [i.get("code", "") for i in issues]
        assert len(issues) == 0, f"入口函数不应被检出: {names_found}"

    def test_async_function_ignored(self):
        """async def 函数不应被检测"""
        code = '''
import asyncio

async def handler():
    data = await fetch()
    return data
'''
        issues = AsyncSyncBoundaryChecker.check_asyncio_run_in_func(code)
        assert len(issues) == 0


# ═══════════════════════════════════════════════════════════════════════
# 模式2: sync-calls-async 测试
# ═══════════════════════════════════════════════════════════════════════


class TestSyncCallsAsync:
    """模式2: sync 函数通过 asyncio.run() 调用 async 函数"""

    def test_detects_sync_calling_async_via_run(self):
        """sync 函数通过 asyncio.run(async_func()) 调用 async 函数"""
        code = '''
import asyncio

async def fetch_data(url):
    return "data"

def get():
    return asyncio.run(fetch_data("http://example.com"))
'''
        issues = AsyncSyncBoundaryChecker.check_sync_calls_async_def(code)
        assert len(issues) >= 1
        assert issues[0]["type"] == "sync_calls_async"
        assert "fetch_data" in issues[0]["description"]

    def test_sync_calling_sync_not_flagged(self):
        """sync 函数调用另一个 sync 函数不应触发"""
        code = '''
import asyncio

def helper():
    return 42

def process():
    return asyncio.run(helper())
'''
        issues = AsyncSyncBoundaryChecker.check_sync_calls_async_def(code)
        # helper 不是 async def，应无匹配
        assert len(issues) == 0

    def test_entry_point_not_flagged(self):
        """入口函数 mai n() 中 asyncio.run(async_func()) 不应报"""
        code = '''
import asyncio

async def start():
    return "ok"

def main():
    return asyncio.run(start())
'''
        issues = AsyncSyncBoundaryChecker.check_sync_calls_async_def(code)
        assert len(issues) == 0


# ═══════════════════════════════════════════════════════════════════════
# 跨文件导入链分析
# ═══════════════════════════════════════════════════════════════════════


class TestCrossFileImportChain:
    """跨文件导入链分析"""

    def test_detects_sync_imported_into_async_context(self, tmp_path):
        """sync 函数被导入到 async 函数中无 await 调用"""
        # 文件A: sync 函数
        file_a = tmp_path / "module_a.py"
        file_a.write_text("""
def sync_processor(data):
    return data * 2
""")
        # 文件B: 导入并在 async 函数中调用
        file_b = tmp_path / "module_b.py"
        file_b.write_text("""
from module_a import sync_processor

async def handle():
    result = sync_processor(42)
    return result
""")
        files = [str(file_a), str(file_b)]
        issues = AsyncSyncBoundaryChecker.check_codebase_import_chain(files)
        assert len(issues) >= 1
        assert issues[0]["type"] == "sync_calls_async_cross_file"
        assert "handle" in issues[0]["description"]
        assert "sync_processor" in issues[0]["description"]

    def test_no_false_positive_when_properly_awaited(self, tmp_path):
        """async 函数中正确 await 的调用不应触发"""
        file_a = tmp_path / "module_a.py"
        file_a.write_text("""
async def async_worker(data):
    return data * 2
""")
        file_b = tmp_path / "module_b.py"
        file_b.write_text("""
from module_a import async_worker

async def handle():
    result = await async_worker(42)
    return result
""")
        files = [str(file_a), str(file_b)]
        issues = AsyncSyncBoundaryChecker.check_codebase_import_chain(files)
        # async_worker 是 async def，不在 sync_funcs 集合中，所以不应报
        assert len(issues) == 0


# ═══════════════════════════════════════════════════════════════════════
# 综合测试（review_all + PRReviewer 集成）
# ═══════════════════════════════════════════════════════════════════════


class TestIntegration:
    """综合集成测试"""

    def test_review_all_includes_all_modes(self):
        """review_all 应包含所有3种模式"""
        code = '''
import asyncio

async def fetch_data(url):
    return "data"

def _sync_build_context():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        raise RuntimeError("use async")
    return asyncio.run(fetch_data("http://example.com"))
'''
        issues = AsyncSyncBoundaryChecker.review_all(code)
        types = {i["type"] for i in issues}
        assert "sync_wrapper_raises" in types  # 模式1
        assert "asyncio_run_in_loop" in types   # 模式3
        assert "sync_calls_async" in types      # 模式2

    def test_prreviewer_includes_async_sync_issues(self):
        """PRReviewer 的输出中应包含 async/sync 边界问题"""
        code = '''
import asyncio

def _sync_build_context():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        raise RuntimeError("cannot call")
    return asyncio.run(some_func())
'''
        reviewer = PRReviewer()
        result = reviewer.review_pr(code, ["test.py"])
        types = {i["type"] for i in result["performance_issues"]}
        assert "sync_wrapper_raises" in types
        assert result["overall_score"] < 100

    def test_clean_code_no_issues(self):
        """干净代码应无任何 async/sync 边界问题"""
        code = '''
async def handler(data):
    result = await process(data)
    return result

def helper():
    return 42

class Calculator:
    def add(self, a, b):
        return a + b
'''
        issues = AsyncSyncBoundaryChecker.review_all(code)
        assert len(issues) == 0

    def test_prreviewer_verdict_reject_on_critical(self):
        """critical 级别的 async/sync 问题应导致 PR 被 reject"""
        code = '''
import asyncio

def _sync_build_context():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        raise RuntimeError("cannot call in running loop")
    return asyncio.run(some_func())
'''
        reviewer = PRReviewer()
        result = reviewer.review_pr(code, ["test.py"])
        assert result["verdict"] == "reject"


# ═══════════════════════════════════════════════════════════════════════
# 边界情况测试
# ═══════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """边界情况"""

    def test_empty_code_returns_empty(self):
        """空代码应返回空列表"""
        assert AsyncSyncBoundaryChecker.review_all("") == []
        assert AsyncSyncBoundaryChecker.check_sync_wrapper_raises("") == []
        assert AsyncSyncBoundaryChecker.check_asyncio_run_in_func("") == []
        assert AsyncSyncBoundaryChecker.check_sync_calls_async_def("") == []

    def test_syntax_error_returns_empty(self):
        """语法错误的代码应返回空列表"""
        bad_code = "def broken( "
        assert AsyncSyncBoundaryChecker.review_all(bad_code) == []

    def test_no_asyncio_import_no_issues(self):
        """没有导入 asyncio 的代码不应有问题"""
        code = '''
def compute(x, y):
    return x + y

async def process():
    return 42
'''
        assert len(AsyncSyncBoundaryChecker.review_all(code)) == 0
