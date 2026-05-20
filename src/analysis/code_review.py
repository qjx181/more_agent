#!/usr/bin/env python3
"""code_review.py — PR 代码审查 Agent 模块

自动审查代码变更，检测安全、性能、代码质量问题，输出质量报告。

模块结构:
  - SecurityReviewer    安全审查（SQL注入、命令注入、密钥泄露、XSS）
  - PerformanceReviewer 性能审查（N+1查询、sync I/O阻塞、内存泄漏）
  - QualityReviewer     代码质量审查（未用import、过深嵌套、硬编码值）
  - PRReviewer          综合 PR 审查（调用三个审查器，输出总评分和审核结论）

设计理由:
  - 纯 Python 标准库，无外部依赖，开箱即用
  - 正则 + AST 双重检测：正则捕获字面量，AST 捕获结构问题
  - 按严重级别分级（critical/high/medium/low），下游可灵活决策

面试官可能问:
  - 为什么不用 flake8/pylint？答：它们只检查质量不检查安全；此模块兼做安全和性能
  - 覆盖 JavaScript 吗？答：XSS 检测部分覆盖 JS，完整 JS 支持需要补充
  - 误报率怎么控制？答：白名单 + AST 分析减少正则误报
"""

import ast
import re
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════
# SecurityReviewer — 安全审查
# ═══════════════════════════════════════════════════════════════════════


class SecurityReviewer:
    """安全审查器

    检测 SQL 注入、命令注入、密钥泄露、XSS 四类安全问题。
    每项检测返回格式：{
        "type": str,              # 检测类型
        "severity": str,          # critical/high/medium/low
        "line": int,              # 行号
        "code": str,              # 问题代码片段
        "description": str,       # 问题描述
        "suggestion": str,        # 修复建议
    }
    """

    # 常见密钥关键词（白名单排除测试密钥）
    SECRET_KEYWORDS = re.compile(
        r"(?i)(api_key|secret_key|api_secret|access_key|secret_access|"
        r"private_key|password|passwd|token|auth_token|"
        r"aws_secret|db_password|jwt_secret|openai_key|app_secret)",
    )
    # 被视为测试/占位的值，跳过
    SKIP_VALUES = re.compile(
        r"(your_|example_|test_|changeme|placeholder|xxx|"
        r"sk-[A-Za-z0-9]{5,10}|'[^']{1,5}')",
        re.IGNORECASE,
    )

    @staticmethod
    def check_sql_injection(code: str) -> list[dict]:
        """检测 SQL 注入风险

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: SQL 注入问题列表

        Why:
            - 覆盖 f-string SQL、.format() SQL、% 格式化 SQL、字符串拼接 SQL 四种模式
            - 使用 row 内的 execute/sql 函数名作为锚点，减少误报
        """
        issues = []

        # 模式1: f-string SQL → f"SELECT * FROM {user_input}"
        fstring_sql = re.finditer(
            r'(?:execute|executescript|cursor\.execute|\.sql)\s*\(\s*f["\']',
            code,
            re.IGNORECASE,
        )
        for m in fstring_sql:
            line = code[: m.start()].count("\n") + 1
            issues.append({
                "type": "sql_injection",
                "severity": "critical",
                "line": line,
                "code": code.split("\n")[line - 1].strip()[:80],
                "description": "使用 f-string 拼接 SQL 查询，存在 SQL 注入风险",
                "suggestion": "使用参数化查询：cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))",
            })

        # 模式2: .format() SQL
        format_sql = re.finditer(
            r"(?:execute|cursor\.execute|\.sql)\s*\(\s*['\"].*?\{.*?\}.*?['\"]\s*\.\s*format\b",
            code,
        )
        for m in format_sql:
            line = code[: m.start()].count("\n") + 1
            issues.append({
                "type": "sql_injection",
                "severity": "critical",
                "line": line,
                "code": code.split("\n")[line - 1].strip()[:80],
                "description": "使用 .format() 拼接 SQL 查询参数",
                "suggestion": "使用参数化查询替代 .format()",
            })

        # 模式3: % 格式化 SQL
        pct_sql = re.finditer(
            r"(?:execute|cursor\.execute|\.sql)\s*\(\s*['\"].*?%[sd].*?['\"]\s*%\s*\(",
            code,
        )
        for m in pct_sql:
            line = code[: m.start()].count("\n") + 1
            issues.append({
                "type": "sql_injection",
                "severity": "high",
                "line": line,
                "code": code.split("\n")[line - 1].strip()[:80],
                "description": "使用 % 格式化拼接 SQL 参数",
                "suggestion": "使用参数化查询：cursor.execute('...', (param,))",
            })

        # 模式4: SQL 字符串 + 变量拼接
        concat_sql = re.finditer(
            r"""['"](SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b.*?['"]\s*\+""",
            code,
            re.IGNORECASE,
        )
        for m in concat_sql:
            line = code[: m.start()].count("\n") + 1
            issues.append({
                "type": "sql_injection",
                "severity": "high",
                "line": line,
                "code": code.split("\n")[line - 1].strip()[:80],
                "description": "通过字符串拼接构建 SQL 查询",
                "suggestion": "使用参数化查询或 ORM 框架",
            })

        return issues

    @staticmethod
    def check_command_injection(code: str) -> list[dict]:
        """检测命令注入风险

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 命令注入问题列表

        Why:
            - subprocess.run/shell=True 是最危险的组合
            - os.system 和 os.popen 直接调用 shell，同样危险
            - 检测 String 模板中是否混入用户输入变量
        """
        issues = []

        patterns = [
            (r"subprocess\.run\(.*shell\s*=\s*True", "subprocess.run + shell=True", "critical"),
            (r"subprocess\.Popen\(.*shell\s*=\s*True", "subprocess.Popen + shell=True", "critical"),
            (r"subprocess\.call\(.*shell\s*=\s*True", "subprocess.call + shell=True", "critical"),
            (r"os\.system\s*\(", "os.system() 调用", "high"),
            (r"os\.popen\s*\(", "os.popen() 调用", "high"),
            (r"commands\.getoutput\s*\(", "commands.getoutput() 调用", "medium"),
            (r"shutil\.which\s*\(.*\+\s*", "shutil.which 参数拼接", "medium"),
        ]

        for pattern, desc, severity in patterns:
            for m in re.finditer(pattern, code):
                line = code[: m.start()].count("\n") + 1
                # 检查是否使用了变量（用户输入）
                var_in_cmd = re.search(
                    r'["\'].*?\b(f|format|\%|{|\+|join)\b',
                    code.split("\n")[line - 1],
                )
                if var_in_cmd or severity == "critical":
                    issues.append({
                        "type": "command_injection",
                        "severity": severity,
                        "line": line,
                        "code": code.split("\n")[line - 1].strip()[:80],
                        "description": f"检测到 {desc}，存在命令注入风险",
                        "suggestion": "使用 subprocess.run(cmd, shell=False, capture_output=True) 替代，避免 shell 解释",
                    })

        return issues

    @staticmethod
    def check_secret_leak(code: str) -> list[dict]:
        """检测密钥泄露

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 密钥泄露问题列表

        Why:
            - 使用正则匹配常见密钥变量名
            - 跳过测试密钥和占位符，减少误报
            - 不检测 .env 文件引用（那是正确实践）
        """
        issues = []

        lines = code.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # 跳过注释和 import
            if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("/*"):
                continue
            if "import " in stripped:
                continue

            match = SecurityReviewer.SECRET_KEYWORDS.search(stripped)
            if match:
                key = match.group()
                value_match = re.search(r'=\s*["\'](.+?)["\']', stripped)
                if value_match:
                    value = value_match.group(1)
                    # 跳过测试/占位值
                    if SecurityReviewer.SKIP_VALUES.search(value):
                        continue
                    # 真正的密钥
                    issues.append({
                        "type": "secret_leak",
                        "severity": "critical" if "password" in key.lower() or "secret" in key.lower() else "high",
                        "line": i,
                        "code": stripped[:80],
                        "description": f"在代码中硬编码了 {key}，值: '{value[:20]}...'",
                        "suggestion": "将敏感信息移至 .env 文件，通过 os.getenv() 读取",
                    })

        return issues

    @staticmethod
    def check_xss(code: str) -> list[dict]:
        """检测 XSS 风险

        Args:
            code: 源代码字符串（Python/JavaScript/HTML 混合）

        Returns:
            list[dict]: XSS 风险问题列表
        """
        issues = []

        patterns = [
            (r"innerHTML\s*=\s*[\"']", "innerHTML 设置用户内容", "high"),
            (r"outerHTML\s*=\s*[\"']", "outerHTML 设置用户内容", "high"),
            (r"document\.write\s*\(", "document.write() 写入内容", "high"),
            (r"\bmark_safe\s*\(", "Django mark_safe 绕过转义", "high"),
            (r"\|safe\b", "Jinja2 safe 过滤器（跳过转义）", "medium"),
            (r"insertAdjacentHTML\s*\(", "insertAdjacentHTML 未转义", "high"),
            (r"v-html\s*=", "Vue v-html（不转义）", "medium"),
        ]

        for pattern, desc, severity in patterns:
            for m in re.finditer(pattern, code):
                line = code[: m.start()].count("\n") + 1
                line_text = code.split("\n")[line - 1].strip()
                # 跳过已知安全的模式
                if "escaped" in line_text.lower() or "sanitize" in line_text.lower():
                    continue
                issues.append({
                    "type": "xss",
                    "severity": severity,
                    "line": line,
                    "code": line_text[:80],
                    "description": f"检测到 {desc}，可能导致 XSS",
                    "suggestion": "使用 textContent 替代 innerHTML，或对用户输入做 escape/DOMPurify 处理",
                })

        return issues

    @classmethod
    def review_all(cls, code: str) -> list[dict]:
        """综合安全审查

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 所有安全问题，按严重级别排序（critical → low）
        """
        issues = []
        issues.extend(cls.check_sql_injection(code))
        issues.extend(cls.check_command_injection(code))
        issues.extend(cls.check_secret_leak(code))
        issues.extend(cls.check_xss(code))
        issues.sort(key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(x["severity"], 99))
        return issues


# ═══════════════════════════════════════════════════════════════════════
# PerformanceReviewer — 性能审查
# ═══════════════════════════════════════════════════════════════════════


class PerformanceReviewer:
    """性能审查器

    检测 N+1 查询、同步 I/O 阻塞、内存泄漏等性能问题。
    """

    @staticmethod
    def check_n_plus_one(code: str) -> list[dict]:
        """检测 N+1 查询模式

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: N+1 查询问题列表

        Why:
            - N+1 的经典模式：for 循环内执行 SQL/API 查询
            - 使用行级近似判断：循环体内有查询调用
        """
        issues = []
        lines = code.split("\n")

        in_loop = False
        loop_start = 0
        loop_indent = 0

        for i, line in enumerate(lines, 1):
            stripped = line.rstrip()
            indent = len(line) - len(line.lstrip())

            # 检测循环开始
            if re.match(r"\s*(for|while)\s", stripped) and ":" in stripped:
                in_loop = True
                loop_start = i
                loop_indent = indent

            if in_loop:
                # 循环体结束：缩进回到循环级别或更低
                if indent <= loop_indent and i > loop_start:
                    in_loop = False
                    continue

                # 在循环体内检测 SQL 查询
                if re.search(
                    r"(execute|\.query|\.get\b|\.filter\b|\.all\b|fetchone|fetchall)",
                    stripped,
                    re.IGNORECASE,
                ):
                    # 检查是否真的是 N+1（循环内的查询）
                    if re.search(
                        r"(for|while|list comp|generator|map\()",
                        stripped[:40],
                    ):
                        continue  # 这本身是循环定义，不是查询
                    issues.append({
                        "type": "n_plus_one",
                        "severity": "high",
                        "line": i,
                        "code": stripped[:80],
                        "description": "在循环体中执行数据库查询，可能导致 N+1 问题",
                        "suggestion": "将查询移到循环外部，使用 IN 查询或 select_related/prefetch_related 预加载",
                    })

        return issues

    @staticmethod
    def check_sync_io_in_async(code: str) -> list[dict]:
        """检测 async 函数中的同步 I/O 阻塞

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 同步阻塞问题列表

        Why:
            - async def 中使用 requests/print/time.sleep 会阻塞事件循环
            - httpx/asyncio.to_thread/aiohttp 是异步替代品
        """
        issues = []
        lines = code.split("\n")
        in_async_def = False
        async_indent = 0

        # 已知的同步 I/O 模式
        sync_io_patterns = [
            (r"^import requests|^from requests", "requests 库（同步 HTTP）", "high"),
            (r"time\.sleep\s*\(", "time.sleep()（同步阻塞）", "high"),
            (r"subprocess\.run\s*\(", "subprocess.run()（同步进程调用）", "high"),
            (r"subprocess\.call\s*\(", "subprocess.call()（同步）", "high"),
            (r"os\.system\s*\(", "os.system()（同步阻塞）", "high"),
            (r"\.read\s*\(\s*\)", "同步 .read() 调用", "medium"),
            (r"\.write\s*\(\s*\)", "同步 .write() 调用", "medium"),
            (r"json\.load\s*\(", "json.load()（同步文件读取）", "low"),
        ]

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())

            if stripped.startswith("async def "):
                in_async_def = True
                async_indent = indent
                continue

            if in_async_def:
                # async def 块结束
                if indent <= async_indent and i > 1 and stripped:
                    in_async_def = False
                    continue

                for pattern, desc, severity in sync_io_patterns:
                    if re.search(pattern, stripped):
                        # 排除 asyncio 相关调用
                        if "asyncio" in stripped:
                            continue
                        issues.append({
                            "type": "sync_io_in_async",
                            "severity": severity,
                            "line": i,
                            "code": stripped[:80],
                            "description": f"async 函数中使用 {desc}，会阻塞事件循环",
                            "suggestion": "使用 asyncio.to_thread() 包装同步调用，或使用 httpx/aiohttp 等异步库替代",
                        })

        return issues

    @staticmethod
    def check_memory_leak(code: str) -> list[dict]:
        """检测潜在内存泄漏

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 内存泄漏问题列表

        Why:
            - 全局列表/字典无限增长是最常见的 Python 内存泄漏模式
            - 没有上限的缓存（lru_cache 等）在长期运行中也会泄漏
        """
        issues = []
        lines = code.split("\n")

        # 检查全局/类级别的可增长集合
        global_collections = []
        in_class = False

        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            indent = len(line) - len(line.lstrip())

            # 检测全局列表/字典
            m = re.match(r"(\w+)\s*=\s*\[\s*\]", stripped)
            if m and indent == 0:
                global_collections.append((m.group(1), i, "list"))
            m = re.match(r"(\w+)\s*=\s*\{\s*\}", stripped)
            if m and indent == 0:
                global_collections.append((m.group(1), i, "dict"))
            m = re.match(r"(\w+)\s*=\s*set\(\s*\)", stripped)
            if m and indent == 0:
                global_collections.append((m.group(1), i, "set"))

        # 检查是否有 .append()/.add() 但无清理逻辑
        for name, decl_line, ctype in global_collections:
            # 找到所有对该变量的修改
            appends = list(re.finditer(rf"\b{re.escape(name)}\.append\(|{re.escape(name)}\.add\(", code))
            pops = list(re.finditer(rf"\b{re.escape(name)}\.pop\(|{re.escape(name)}\.discard\(", code))
            del_ops = list(re.finditer(
                rf"del\s+{re.escape(name)}\[|{re.escape(name)}\.clear\(|{re.escape(name)}\s*=\s*\[\s*\]",
                code,
            ))

            if len(appends) > 3 and len(pops) + len(del_ops) < 2:
                first_append_line = code[: appends[0].start()].count("\n") + 1
                issues.append({
                    "type": "memory_leak",
                    "severity": "medium",
                    "line": first_append_line,
                    "code": appends[0].group()[:60],
                    "description": f"全局 {ctype} '{name}' 有 {len(appends)} 次添加操作但几乎无清理，可能无限增长",
                    "suggestion": f"为 {name} 设置上限（如使用 collections.deque(maxlen=1000)），或定期清理过期元素",
                })

        # 检查 @lru_cache 无 maxsize
        for m in re.finditer(r"@lru_cache\b(?!\s*\(\s*maxsize)", code):
            line = code[: m.start()].count("\n") + 1
            issues.append({
                "type": "memory_leak",
                "severity": "low",
                "line": line,
                "code": "@lru_cache (无 maxsize)",
                "description": "@lru_cache 未指定 maxsize，默认 128，运行中可能增长",
                "suggestion": "显式设置 @lru_cache(maxsize=128)",
            })

        return issues

    @classmethod
    def review_all(cls, code: str) -> list[dict]:
        """综合性能审查"""
        issues = []
        issues.extend(cls.check_n_plus_one(code))
        issues.extend(cls.check_sync_io_in_async(code))
        issues.extend(cls.check_memory_leak(code))
        issues.extend(AsyncSyncBoundaryChecker.review_all(code))
        issues.sort(key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(x["severity"], 99))
        return issues


# ═══════════════════════════════════════════════════════════════════════
# AsyncSyncBoundaryChecker — async/sync 边界检测
# ═══════════════════════════════════════════════════════════════════════


class AsyncSyncBoundaryChecker:
    """async/sync 边界检测器

    检测三类 async/sync 边界模式：
      1) sync-wrapper-raises: sync 包装器在检测到运行中事件循环时 raise（Critical）
      2) sync-calls-async: sync 函数调用 async 函数但未在独立线程运行（High）
      3) asyncio-run-in-loop: asyncio.run() 在非入口 sync 函数中被调用（High）

    Why AST:
      - 正则无法可靠区分 sync def 内部的函数作用域
      - AST 能精确判断函数边界、async 修饰符、调用链

    面试官可能问:
      - 为什么不用 mypy/pyright? 答：它们检查类型安全，不检查运行时 async/sync 边界
      - 误报怎么控制？答：白名单入口函数 + 名称前缀过滤
      - 能处理嵌套函数吗？答：AST 递归遍历，子函数也可检测
    """

    # 入口函数白名单——这些函数中的 asyncio.run() 降级为警告
    _IGNORE_ENTRY_POINTS: set[str] = {"main", "setup", "manage", "cli", "run", "init"}

    @staticmethod
    def check_sync_wrapper_raises(code: str) -> list[dict]:
        """检测 sync-wrapper-raises 模式（Critical）

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 问题列表，每条含 type/severity/line/code/description/suggestion

        检测目标:
          sync def 函数中调用 asyncio.get_running_loop() 检测事件循环，
          并在检测到循环运行时 raise RuntimeError。

        Why:
          这种模式是"假 sync 包装器"：看起来是同步函数，实际上内部做了事件循环检测，
          在已有事件循环的上下文中（如 FastAPI/uvicorn）会直接崩溃。
          真正的 sync 包装器应该用 asyncio.run()（在新事件循环中运行），
          而不是检测到循环就抛异常。

        面试官可能问:
          - 为什么不直接检测 raise 语句？答：raise 的具体内容重要，只检测裸 raise 会误报
          - 为什么是 critical 级别？答：会导致运行时直接崩溃，不是性能问题
        """
        issues: list[dict] = []

        try:
            tree = ast.parse(code)
        except SyntaxError:
            return issues

        for node in ast.walk(tree):
            # 只处理同步函数定义（非 async def）
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                if isinstance(dec, ast.Name) and dec.id in ("classmethod", "staticmethod"):
                    continue

            # AST 中 async def 的函数 node 有 async 标记
            # 在 Python 3.8+ 中，async def 的 FunctionDef 节点没有 async 属性直接标记
            # 但 ast.FunctionDef 在 Python 3.8+ 有 lineno 和 col_offset
            # 我们需要检查源代码中该函数定义是否以 "async def" 开头
            func_source_lines = code.split("\\n")
            func_def_line = func_source_lines[node.lineno - 1].strip() if node.lineno <= len(func_source_lines) else ""
            if func_def_line.startswith("async "):
                continue  # async def 不算 sync 包装器

            # 在 sync 函数体内搜索 asyncio.get_running_loop()
            has_get_running_loop = False
            has_is_running_check = False
            has_raise = False

            for child in ast.walk(node):
                # asyncio.get_running_loop()
                if isinstance(child, ast.Call):
                    func = child.func
                    if (isinstance(func, ast.Attribute)
                            and func.attr == "get_running_loop"
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "asyncio"):
                        has_get_running_loop = True

                # Raises 中的 RuntimeError
                if isinstance(child, ast.Raise):
                    if child.exc and isinstance(child.exc, ast.Call):
                        exc_func = child.exc.func
                        if isinstance(exc_func, ast.Name) and exc_func.id == "RuntimeError":
                            has_raise = True

                # loop.is_running()
                if isinstance(child, ast.Call):
                    func = child.func
                    if (isinstance(func, ast.Attribute)
                            and func.attr == "is_running"
                            and isinstance(func.value, ast.Attribute)
                            and func.value.attr == "is_running") or (
                            isinstance(func, ast.Attribute)
                            and func.attr == "is_running"
                    ):
                        # Verify it's called on a loop variable by checking parent context
                        has_is_running_check = True

            if has_get_running_loop and has_raise:
                issues.append({
                    "type": "sync_wrapper_raises",
                    "severity": "critical",
                    "line": node.lineno,
                    "code": f"def {node.name}(...):",
                    "description": f"同步函数 '{node.name}' 检测到运行中事件循环后主动抛出异常（sync-wrapper-raises），"
                                   f"在 async 上下文（如 FastAPI）中调用时会直接崩溃",
                    "suggestion": f"将 '{node.name}' 改为 async def 让调用方用 await 调用；"
                                   f"或使用 asyncio.run() 在新事件循环中执行而不阻塞",
                })

        return issues

    @staticmethod
    def check_asyncio_run_in_func(code: str) -> list[dict]:
        """检测 asyncio-run-in-loop 模式（High）

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 问题列表

        检测目标:
          在同步函数中调用 asyncio.run(coro)，且该函数不是入口函数。
          尤其关注函数名含 _sync / _wrap_ 的包装器函数。

        Why:
          asyncio.run() 在已有事件循环（如 FastAPI/uvicorn/Jupyter）中抛出
          "RuntimeError: asyncio.run() cannot be called from a running event loop"。
          入口函数（main/setup）中调用是合理的，但包装器函数中调用则是设计问题。

        面试官可能问:
          - asyncio.run() 和 loop.run_until_complete() 的区别？
            答：asyncio.run() 总是创建新事件循环，run_until_complete 运行在已有循环上
          - 误报主要来源？答：测试代码和 CLI 入口（已通过白名单控制）
        """
        issues: list[dict] = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return issues

        lines = code.split("\\n")

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue

            func_name = node.name

            # 跳过 async def 函数
            func_source_line = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
            if func_source_line.startswith("async "):
                continue

            # 跳过入口函数白名单
            if func_name.lower() in AsyncSyncBoundaryChecker._IGNORE_ENTRY_POINTS:
                continue

            # 检查函数体内是否有 asyncio.run() 调用
            has_asyncio_run = False
            asyncio_run_line = 0
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    # asyncio.run(coro)
                    if (isinstance(func, ast.Attribute)
                            and func.attr == "run"
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "asyncio"):
                        has_asyncio_run = True
                        asyncio_run_line = child.lineno
                        break
                    # 局部 import asyncio 后的 asyncio.run()
                    if (isinstance(func, ast.Attribute)
                            and func.attr == "run"
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "asyncio"):
                        has_asyncio_run = True
                        asyncio_run_line = child.lineno
                        break

            if not has_asyncio_run:
                continue

            # 判断函数名含 _sync 或 _wrap_ 的包装器 → high
            is_wrapper = "_sync" in func_name or "_wrap_" in func_name or func_name.startswith("_sync_")

            severity = "high" if is_wrapper else "medium"
            desc = (
                f"同步包装器 '{func_name}' 中调用 asyncio.run()，如果被 async 上下文调用会抛出 'already running' 错误"
                if is_wrapper
                else f"同步函数 '{func_name}' 中调用 asyncio.run()，可能在已有事件循环中运行"
            )

            issues.append({
                "type": "asyncio_run_in_loop",
                "severity": severity,
                "line": asyncio_run_line,
                "code": lines[asyncio_run_line - 1].strip()[:80] if asyncio_run_line <= len(lines) else "",
                "description": desc,
                "suggestion": (
                    f"将 {func_name} 改为 async def，调用方用 await 替代；"
                    f"或确保只在主线程/新线程中调用"
                ),
            })

        return issues

    @classmethod
    def check_sync_calls_async_def(cls, code: str) -> list[dict]:
        """检测 sync-calls-async 模式——同步函数中 asyncio.run() 调用了 async 函数（High）

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 问题列表

        检测目标:
          在 sync def 函数中，asyncio.run() 的参数是对 async def 函数的调用。
          这种模式意味着调用方依赖 asyncio.run() 在同步上下文中执行 async 函数，
          但如果在已有事件循环中调用就会崩溃。

        Why:
          模式2 比模式3 更进一步：模式3 只看"调用了 asyncio.run()"，
          模式2 检查"被 asyncio.run() 包裹的函数是否是 async def"。
          如果是调用一个同步函数，asyncio.run() 也能工作但无意义；
          但如果是 async 函数，说明设计者在故意 async→sync 桥接。

        面试官可能问:
          - 和 check_asyncio_run_in_func 区别？答：前者只检测'存在 asyncio.run'，
            后者检测'asyncio.run 包裹了 async 函数'，两者可同时触发但原因不同
          - 如何知道目标函数是 async？答：在当前代码的 AST 中搜索同名 async def
        """
        issues: list[dict] = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return issues

        lines = code.split("\\n")

        # 第一步：收集所有 async def 函数名
        async_func_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                async_func_names.add(node.name)

        if not async_func_names:
            return issues

        # 第二步：在 sync def 函数中搜索 asyncio.run(async_func(...))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue

            func_source_line = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
            if func_source_line.startswith("async "):
                continue  # 跳过 async def

            func_name = node.name
            if func_name.lower() in cls._IGNORE_ENTRY_POINTS:
                continue  # 跳过入口函数

            # 在函数体中搜索 asyncio.run(...)
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                func = child.func
                # 检查是否为 asyncio.run(...)
                is_asyncio_run = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "run"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "asyncio"
                )
                if not is_asyncio_run:
                    continue

                # 检查 asyncio.run() 的参数是否是一个函数调用
                if child.args:
                    arg = child.args[0]  # asyncio.run(arg)
                    # arg 可能是 ast.Call（直接调用）或 ast.Await（但 await 在 sync 中不可能）
                    if isinstance(arg, ast.Call):
                        called_func = arg.func
                        # 检查调用的函数名是否是 async def
                        if isinstance(called_func, ast.Name):
                            called_name = called_func.id
                        elif isinstance(called_func, ast.Attribute):
                            called_name = called_func.attr
                        else:
                            continue

                        if called_name in async_func_names:
                            is_wrapper = "_sync" in func_name or "_wrap_" in func_name or func_name.startswith("_sync_")
                            issues.append({
                                "type": "sync_calls_async",
                                "severity": "high" if is_wrapper else "high",
                                "line": child.lineno,
                                "code": lines[child.lineno - 1].strip()[:80] if child.lineno <= len(lines) else "",
                                "description": (
                                    f"同步函数 '{func_name}' 通过 asyncio.run() 调用 async 函数 '{called_name}'，"
                                    f"在已有事件循环中会抛出 RuntimeError"
                                ),
                                "suggestion": (
                                    f"将 '{func_name}' 改为 async def 并用 await {called_name}(...) 替代；"
                                    f"或将调用移至独立线程"
                                ),
                            })

        return issues

    @staticmethod
    def check_codebase_import_chain(files: list[str]) -> list[dict]:
        """跨文件导入链分析——检测 sync 函数被导入到 async 调用方

        Args:
            files: Python 文件路径列表

        Returns:
            list[dict]: 导入链问题列表

        检测目标:
          追踪 sync 函数通过 import 语句被导入到另一个模块，
          在该模块中被当作 async 函数调用（但调用处没有 await）。

        Why:
          单文件分析无法发现跨模块的 async/sync 不匹配问题。
          一个 sync 函数被 `from A import sync_func` 到 B 模块，
          然后在 B 的 async 函数中被无 await 地调用——这在运行时不会直接崩溃，
          但违背了 async 代码的设计意图，可能阻塞事件循环。

        面试官可能问:
          - 为什么不做跨文件追踪？答：需要解析所有依赖模块的 AST 并建立导入图，
            对大型项目扫描成本较高。这里提供的是轻量级版本：只检查 import 语句
            和调用方的函数类型
          - 局限性？答：不能处理动态导入、__init__.py 重导出、条件导入
        """
        issues: list[dict] = []

        # 收集所有文件中定义的 sync 函数名
        sync_funcs_by_file: dict[str, set[str]] = {}
        file_asts: dict[str, ast.Module] = {}

        for filepath in files:
            try:
                with open(filepath, encoding="utf-8") as f:
                    code_text = f.read()
            except (FileNotFoundError, IOError, OSError):
                continue

            try:
                tree = ast.parse(code_text)
            except SyntaxError:
                continue

            file_asts[filepath] = tree
            lines = code_text.split("\\n")
            sync_funcs: set[str] = set()

            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    func_def_line = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                    if not func_def_line.startswith("async "):
                        sync_funcs.add(node.name)

            if sync_funcs:
                sync_funcs_by_file[filepath] = sync_funcs

        # 对每个文件，检查 import 语句是否导入了其他文件的 sync 函数
        # 并且在本文件的 async 函数中被无 await 地调用
        for filepath, tree in file_asts.items():
            lines_for_file: list[str] = []
            try:
                with open(filepath, encoding="utf-8") as f:
                    lines_for_file = f.read().split("\\n")
            except (FileNotFoundError, IOError, OSError):
                continue

            # 收集所有 import 语句
            imports: dict[str, str] = {}  # local_name → source
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for alias in node.names:
                        local_name = alias.asname or alias.name
                        imports[local_name] = f"{module}.{alias.name}"
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        local_name = alias.asname or alias.name
                        imports[local_name] = alias.name

            # 检查每个异步函数中是否有对导入的 sync 函数无 await 调用
            for node in ast.walk(tree):
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue

                # 在 async 函数体中搜索 name(...) 调用（无 await）
                for child in ast.walk(node):
                    if not isinstance(child, ast.Call):
                        continue
                    if isinstance(child.func, ast.Name):
                        called_name = child.func.id
                    elif isinstance(child.func, ast.Attribute):
                        called_name = child.func.attr
                    else:
                        continue

                    # 检查这个被调用的名字是否来自 import
                    if called_name not in imports:
                        continue

                    source = imports[called_name]
                    # 检查该函数是否是 sync 函数（跨文件匹配）
                    for src_file, sync_funcs in sync_funcs_by_file.items():
                        # 简单匹配：import 源的最后一部分是函数名
                        imported_func = source.split(".")[-1]
                        if imported_func in sync_funcs:
                            issues.append({
                                "type": "sync_calls_async_cross_file",
                                "severity": "high",
                                "line": child.lineno,
                                "code": lines_for_file[child.lineno - 1].strip()[:80] if child.lineno <= len(lines_for_file) else "",
                                "description": (
                                    f"async 函数 '{node.name}' 中无 await 调用了 sync 函数 '{called_name}'"
                                    f"（来自 {source}），可能阻塞事件循环"
                                ),
                                "suggestion": (
                                    f"使用 asyncio.to_thread({called_name}, ...) 包装同步调用，"
                                    f"或将函数改为 async def"
                                ),
                            })

        return issues

    @classmethod
    def review_all(cls, code: str) -> list[dict]:
        """综合 async/sync 边界检测

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 所有 async/sync 边界问题，按严重级别排序
        """
        issues = []
        issues.extend(cls.check_sync_wrapper_raises(code))
        issues.extend(cls.check_asyncio_run_in_func(code))
        issues.extend(cls.check_sync_calls_async_def(code))
        issues.sort(key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(x["severity"], 99))
        return issues


# ═══════════════════════════════════════════════════════════════════════
# QualityReviewer — 代码质量审查
# ═══════════════════════════════════════════════════════════════════════


class QualityReviewer:
    """代码质量审查器

    检测未使用的 import、过深嵌套、硬编码值、缺失异常处理、过长函数。
    """

    @staticmethod
    def check_unused_import(code: str) -> list[dict]:
        """检测未使用的 import

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 未使用 import 问题列表

        Why:
            - 通过 AST 解析精确检测，比正则更可靠
            - 只检测简单 import（from X import Y），不处理动态导入
        """
        issues = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return issues

        # 收集所有 import
        imports = {}  # {name: line_number}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name
                    imports[name] = node.lineno
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname or alias.name
                    imports[name] = node.lineno

        if not imports:
            return issues

        # 收集所有名字引用
        used_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                used_names.add(node.attr)

        # 检测未使用的 import
        for name, lineno in imports.items():
            # 跳过 __all__ 中的名字
            if name.startswith("_"):
                continue
            if name not in used_names:
                issues.append({
                    "type": "unused_import",
                    "severity": "low",
                    "line": lineno,
                    "code": "",
                    "description": f"导入了 '{name}' 但未使用",
                    "suggestion": f"移除 import {name} 或确认是否需要保留",
                })

        return issues

    @staticmethod
    def check_deep_nesting(code: str, max_depth: int = 4) -> list[dict]:
        """检测过深嵌套

        Args:
            code: 源代码字符串
            max_depth: 最大允许嵌套深度（默认 4）

        Returns:
            list[dict]: 过深嵌套问题列表

        Why:
            - 基于缩进级别检测，适用于 Python
            - 嵌套深度超过 4 层通常意味着需要重构
        """
        issues = []
        lines = code.split("\n")

        current_depth = 0
        depth_stack = []  # [(depth, line_number, keyword)]
        in_multiline = False

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            # 跳过空行和注释
            if not stripped or stripped.startswith("#") or stripped.startswith('"""'):
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    in_multiline = not in_multiline
                continue
            if in_multiline:
                continue

            indent = len(line) - len(line.lstrip())
            indent_level = indent // 4  # 假设 4 空格缩进

            # 检测嵌套语句
            nesting_keywords = [
                r"if\s", r"elif\s", r"else\s*:", r"for\s", r"while\s",
                r"def\s", r"class\s", r"try\s*:", r"except\s", r"finally\s*:",
                r"with\s", r"async\s+def\s", r"async\s+for\s", r"async\s+with\s",
            ]

            for kw in nesting_keywords:
                if re.match(rf"\s*{kw}", line):
                    depth_stack.append((indent_level, i, kw[:10]))
                    break

        # 分析嵌套深度
        for depth, line_num, keyword in depth_stack:
            if depth > max_depth:
                issues.append({
                    "type": "deep_nesting",
                    "severity": "medium",
                    "line": line_num,
                    "code": lines[line_num - 1].strip()[:60],
                    "description": f"嵌套深度 {depth} 层（{keyword}），超过建议的 {max_depth} 层",
                    "suggestion": "提取为独立函数、使用 guard clause 提前返回，或合并条件",
                })

        return issues

    @staticmethod
    def check_hardcoded_values(code: str) -> list[dict]:
        """检测硬编码值（魔数、硬编码 URL、硬编码路径）

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 硬编码值问题列表
        """
        issues = []
        lines = code.split("\n")

        # 硬编码魔数：赋值语句中的整数，排除 0, 1, -1, 常用的 timeout/port
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or "import " in stripped:
                continue

            # 硬编码数字
            magic_num = re.search(r"=\s*(\d{4,})\s*(?:#|$|\])", stripped)
            if magic_num:
                val = int(magic_num.group(1))
                if val not in (0, 1, -1, 80, 443, 8080, 8000, 3000, 5000, 6379, 3306, 5432):
                    issues.append({
                        "type": "hardcoded_value",
                        "severity": "low",
                        "line": i,
                        "code": stripped[:60],
                        "description": f"硬编码魔数 {val}",
                        "suggestion": "提取为命名常量：MAX_RETRIES = 3",
                    })

            # 硬编码 URL（排除 localhost 和常见 CDN）
            url_match = re.search(r'["\'](https?://[^"\']+)["\']', stripped)
            if url_match:
                url = url_match.group(1)
                if "localhost" not in url and "127.0.0.1" not in url and not re.search(
                    r"example\.com|test|cdn\.",
                    url,
                ):
                    issues.append({
                        "type": "hardcoded_value",
                        "severity": "low",
                        "line": i,
                        "code": stripped[:60],
                        "description": f"硬编码 URL: {url[:40]}...",
                        "suggestion": "将 URL 移至配置文件或环境变量",
                    })

        return issues

    @staticmethod
    def check_missing_error_handling(code: str) -> list[dict]:
        """检测缺失异常处理

        Args:
            code: 源代码字符串

        Returns:
            list[dict]: 缺失异常处理问题列表
        """
        issues = []

        # 检查常见需要异常处理的调用
        dangerous_calls = [
            (r"int\(|float\(|bool\(", "类型转换", "输入值非预期格式"),
            (r"open\(|\.read\(|\.write\(", "文件 I/O", "文件不存在或权限不足"),
            (r"json\.loads\(|json\.dump\(", "JSON 解析", "JSON 格式错误"),
            (r"requests\.(get|post|put|delete)\(", "HTTP 请求", "网络异常或超时"),
            (r"\.execute\(", "数据库查询", "SQL 执行错误"),
            (r"subprocess\.", "子进程", "子进程执行失败"),
        ]

        lines = code.split("\n")
        for pattern, op_name, reason in dangerous_calls:
            for m in re.finditer(pattern, code):
                line = code[: m.start()].count("\n") + 1
                # 检查该行是否在 try 块内（简单近似：行前 10 行内是否有 try:）
                try_nearby = False
                for j in range(max(0, line - 12), line - 1):
                    if re.match(r"\s*try\s*:", lines[j]) if j < len(lines) else False:
                        try_nearby = True
                        break

                if not try_nearby:
                    issues.append({
                        "type": "missing_error_handling",
                        "severity": "medium",
                        "line": line,
                        "code": code.split("\n")[line - 1].strip()[:60],
                        "description": f"检测到 {op_name} 调用但未在 try/except 中保护（可能原因：{reason}）",
                        "suggestion": f"用 try/except 包装 {op_name} 调用，处理可能的异常",
                    })

        return issues

    @staticmethod
    def check_long_functions(code: str, max_lines: int = 80) -> list[dict]:
        """检测过长函数

        Args:
            code: 源代码字符串
            max_lines: 函数最大建议行数（默认 80）

        Returns:
            list[dict]: 过长函数问题列表
        """
        issues = []
        lines = code.split("\n")

        i = 0
        while i < len(lines):
            line = lines[i]
            m = re.match(r"\s*def\s+(\w+)\s*\(", line)
            if m:
                func_name = m.group(1)
                func_start = i
                func_indent = len(line) - len(line.lstrip())
                i += 1
                while i < len(lines):
                    l = lines[i]
                    indent = len(l) - len(l.lstrip())
                    if l.strip() and indent <= func_indent:
                        break
                    i += 1
                func_lines = i - func_start
                if func_lines > max_lines and not func_name.startswith("_"):
                    issues.append({
                        "type": "long_function",
                        "severity": "low",
                        "line": func_start + 1,
                        "code": f"def {func_name}()",
                        "description": f"函数 '{func_name}' 共 {func_lines} 行，超过建议的 {max_lines} 行",
                        "suggestion": "将函数拆分成多个小函数，每个函数只做一件事",
                    })
            else:
                i += 1

        return issues

    @classmethod
    def review_all(cls, code: str) -> list[dict]:
        """综合代码质量审查"""
        issues = []
        issues.extend(cls.check_unused_import(code))
        issues.extend(cls.check_deep_nesting(code))
        issues.extend(cls.check_hardcoded_values(code))
        issues.extend(cls.check_missing_error_handling(code))
        issues.extend(cls.check_long_functions(code))
        issues.sort(key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(x["severity"], 99))
        return issues


# ═══════════════════════════════════════════════════════════════════════
# PRReviewer — 综合 PR 审查
# ═══════════════════════════════════════════════════════════════════════


class PRReviewer:
    """综合 PR 审查器

    对 PR 的 diff 或文件变更进行全方面审查，输出质量报告。
    """

    def __init__(self):
        self.security = SecurityReviewer()
        self.performance = PerformanceReviewer()
        self.quality = QualityReviewer()

    def review_pr(self, diff_text: str, changed_files: list[str]) -> dict:
        """综合 PR 审查

        Args:
            diff_text: PR 的 diff 文本
            changed_files: 变更文件路径列表

        Returns:
            dict: {
                "summary": str,           # 审查概述
                "security_issues": [...],  # 安全问题
                "performance_issues": [...],
                "quality_issues": [...],
                "overall_score": int,       # 0-100
                "verdict": str,            # approve/needs_changes/reject
                "comments": str,           # GitHub PR review 评论
            }

        Why:
            - overall_score 基于问题严重程度加权扣分
            - verdict 让 CI/CD 系统能自动决定 merge/block
            - comments 可直接粘贴到 GitHub PR 页面
        """
        all_issues = []
        all_issues.extend(self.security.review_all(diff_text))
        all_issues.extend(self.performance.review_all(diff_text))
        all_issues.extend(self.quality.review_all(diff_text))

        # 计算分数
        severity_scores = {"critical": -30, "high": -15, "medium": -5, "low": -2}
        score = 100
        for issue in all_issues:
            score += severity_scores.get(issue.get("severity", "low"), -2)
        score = max(0, min(100, score))

        # 判定结论
        critical_count = sum(1 for i in all_issues if i["severity"] == "critical")
        high_count = sum(1 for i in all_issues if i["severity"] == "high")

        if critical_count > 0:
            verdict = "reject"
        elif high_count > 3:
            verdict = "reject"
        elif high_count > 0:
            verdict = "needs_changes"
        elif score >= 85:
            verdict = "approve"
        else:
            verdict = "needs_changes"

        # 生成总结
        total = len(all_issues)
        summary_parts = [
            f"## PR 审查报告",
            f"",
            f"**审核结论**: {'✅ 批准' if verdict == 'approve' else '⚠️ 需要修改' if verdict == 'needs_changes' else '❌ 拒绝'}",
            f"**综合评分**: {score}/100",
            f"",
            f"### 发现问题 ({total} 项)",
        ]

        if all_issues:
            by_severity = {"critical": 0, "high": 0, "medium": 0, "low": 0}
            for i in all_issues:
                by_severity[i["severity"]] = by_severity.get(i["severity"], 0) + 1
            summary_parts.append("| 级别 | 数量 |")
            summary_parts.append("|------|:----:|")
            for sev in ["critical", "high", "medium", "low"]:
                if by_severity[sev] > 0:
                    summary_parts.append(f"| {sev} | {by_severity[sev]} |")

        summary_parts.append("")
        for issue in all_issues:
            emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}.get(
                issue.get("severity", "low"), "⚪"
            )
            summary_parts.append(
                f"{emoji} **{issue['type']}** (L{issue['line']}): {issue['description']}"
            )
            summary_parts.append(f"   - 建议: {issue['suggestion']}")
            if issue.get("code"):
                summary_parts.append(f"   - 代码: `{issue['code'][:60]}`")

        comments = "\n".join(summary_parts)

        return {
            "summary": f"审查了 {len(changed_files)} 个文件，发现 {total} 个问题。评分 {score}/100，结论: {verdict}。",
            "security_issues": [i for i in all_issues if i["type"] in (
                "sql_injection", "command_injection", "secret_leak", "xss"
            )],
            "performance_issues": [i for i in all_issues if i["type"] in (
                "n_plus_one", "sync_io_in_async", "memory_leak",
                "sync_wrapper_raises", "asyncio_run_in_loop", "sync_calls_async",
                "sync_calls_async_cross_file",
            )],
            "quality_issues": [i for i in all_issues if i["type"] in (
                "unused_import", "deep_nesting", "hardcoded_value",
                "missing_error_handling", "long_function"
            )],
            "overall_score": score,
            "verdict": verdict,
            "comments": comments,
        }

    @staticmethod
    def generate_comment(issues: list[dict]) -> str:
        """生成 GitHub PR Review 评论文本

        Args:
            issues: 问题列表

        Returns:
            str: 格式化的 Review 评论

        Why:
            - 格式与 GitHub PR Review 兼容，可以粘贴到 GitHub 的 Review 文本框
            - 按严重级别分组，便于 reviewer 优先处理 critical 问题
        """
        if not issues:
            return "✅ 代码审查通过，未发现问题。"

        by_type = {}
        for issue in issues:
            t = issue["type"]
            if t not in by_type:
                by_type[t] = []
            by_type[t].append(issue)

        lines = ["## 🤖 Swarm Code Review Report", ""]
        for issue_type, type_issues in by_type.items():
            sev_emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}
            lines.append(f"### {sev_emoji.get(type_issues[0].get('severity','low'), '⚪')} {issue_type}")
            lines.append("")
            for issue in type_issues:
                lines.append(f"- **L{issue['line']}** [{issue['severity']}]: {issue['description']}")
                lines.append(f"  - 💡 {issue['suggestion']}")
                if issue.get("code"):
                    lines.append(f"  - ```{issue['code'][:80]}```")
            lines.append("")

        if not any(i.get("severity") in ("critical", "high") for i in issues):
            lines.append("---")
            lines.append("> ⚠️ 未发现严重问题，建议修复以上 low/medium 问题后合并。")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════════════════════════


def check_python_file(path: str) -> dict:
    """单个 Python 文件的完整审查

    Args:
        path: Python 文件路径

    Returns:
        dict: {
            "file": str,
            "total_issues": int,
            "issues": [各项问题],
            "overall_score": int,
        }
    """
    filepath = Path(path)
    if not filepath.exists():
        return {"file": path, "error": "文件不存在", "issues": [], "total_issues": 0}

    try:
        code = filepath.read_text(encoding="utf-8")
    except Exception as e:
        return {"file": path, "error": str(e), "issues": [], "total_issues": 0}

    reviewer = PRReviewer()
    result = reviewer.review_pr(code, [path])

    return {
        "file": path,
        "total_issues": len(result["security_issues"]) + len(result["performance_issues"]) + len(result["quality_issues"]),
        "security_issues": result["security_issues"],
        "performance_issues": result["performance_issues"],
        "quality_issues": result["quality_issues"],
        "overall_score": result["overall_score"],
    }


def review_project(path: str) -> dict:
    """对整个项目目录进行全面的代码审查

    Args:
        path: 项目根目录路径

    Returns:
        dict: {
            "project": str,
            "files_checked": int,
            "total_issues": int,
            "file_reports": [每个文件的审查结果],
            "overall_score": int,
        }
    """
    root = Path(path)
    if not root.is_dir():
        return {"project": path, "error": "目录不存在"}

    py_files = list(root.rglob("*.py"))
    # 排除 __pycache__ 和测试临时文件
    py_files = [f for f in py_files if "__pycache__" not in str(f) and not f.name.startswith("_")]

    file_reports = []
    total_issues = 0
    for f in py_files:
        report = check_python_file(str(f))
        file_reports.append(report)
        total_issues += report.get("total_issues", 0)

    scores = [r.get("overall_score", 100) for r in file_reports if "overall_score" in r]
    avg_score = sum(scores) // len(scores) if scores else 100

    return {
        "project": path,
        "files_checked": len(py_files),
        "total_issues": total_issues,
        "file_reports": file_reports,
        "overall_score": avg_score,
    }


# ═══════════════════════════════════════════════════════════════════════
# GitHub Webhook 处理
# ═══════════════════════════════════════════════════════════════════════


def handle_github_webhook(payload: dict) -> dict:
    """处理 GitHub Webhook 请求

    Args:
        payload: GitHub Webhook 的 JSON payload（pull_request 事件）

    Returns:
        dict: 审查结果，包含 review_comment

    Why:
        - 可以在 FastAPI 的路由中调用此函数
        - 返回的 review_comment 可直接通过 GitHub API 提交到 PR
    """
    action = payload.get("action", "")
    if action not in ("opened", "synchronize"):
        return {"skipped": True, "reason": f"action={action}，不需要审查"}

    pr = payload.get("pull_request", {})
    pr_number = pr.get("number", "?")
    pr_title = pr.get("title", "")
    pr_body = pr.get("body", "")
    pr_diff_url = pr.get("diff_url", "")

    # GitHub webhook 不直接包含 diff 内容
    # 需要另外调用 GitHub API 获取 diff
    # 这里返回一个模拟结果，实际集成时需要配合 GitHub API 使用

    diff_text = f"PR #{pr_number}: {pr_title}\n\n{pr_body}"
    changed_files = [f.get("filename", "") for f in payload.get("pull_request", {}).get("files", [])]

    reviewer = PRReviewer()
    result = reviewer.review_pr(diff_text, changed_files)

    result["pr_number"] = pr_number
    result["pr_title"] = pr_title
    result["review_comment"] = reviewer.generate_comment(
        result.get("security_issues", [])
        + result.get("performance_issues", [])
        + result.get("quality_issues", [])
    )

    return result


if __name__ == "__main__":
    # 快速自测
    test_code = """import os
import sys

def get_user(user_id):
    # SQL 注入
    query = f"SELECT * FROM users WHERE id = {user_id}"
    cursor.execute(query)
    return cursor.fetchone()

def run_cmd(cmd):
    os.system("ping " + cmd)

API_KEY = "sk-1234567890abcdef1234567890abcdef"
"""
    reviewer = PRReviewer()
    result = reviewer.review_pr(test_code, ["test.py"])
    print(f"评分: {result['overall_score']}/100")
    print(f"结论: {result['verdict']}")
    print(f"发现问题: {len(result['security_issues'])} 安全 + {len(result['performance_issues'])} 性能 + {len(result['quality_issues'])} 质量")
    for i in result["security_issues"]:
        print(f"  [{i['severity']}] L{i['line']} {i['type']}: {i['description'][:50]}")
    print(f"\n{result['comments'][:500]}")

