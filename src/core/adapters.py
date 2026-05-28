#!/usr/bin/env python3
"""adapters.py — Scanner/Fixer 标准化接口

设计动机（面试话术）：
  "项目三有 9 个维度扫描器和 6 种修复器，但它们的接口不统一——
   sec_scanner 用 scan(blueprint)，deep_enterprise_scanner 用 scan_deep(project_root)，
   enterprise_fixer 用 try_fix_deep(issue, project_root)。
   为了支持插拔式扩展（新增扫描器/修复器不改 orchestrator），我定义了标准 Adapter 接口。"

借鉴 HiveWard 的 RuntimeAdapter 模式：
  - 上游（pipeline）只依赖抽象接口，不依赖具体实现
  - 新增扫描器只需实现 ScannerAdapter + 注册到 registry
  - 新增修复器只需实现 FixerAdapter + 注册到 registry

核心类：
  Issue              — 标准化的问题描述（所有扫描器统一输出）
  FixResult          — 标准化的修复结果（所有修复器统一输出）
  ScannerAdapter     — 扫描器抽象基类
  FixerAdapter       — 修复器抽象基类
  ScannerRegistry    — 扫描器注册表（按名称查找）
  FixerRegistry      — 修复器注册表（按 issue_type 查找）
"""

import ast
from src.infra.logging_config import PrintToLogger
print = PrintToLogger(__name__).info
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# 标准化数据结构
# ═══════════════════════════════════════════════════════════════════════

class Severity(str, Enum):
    """问题严重等级，从高到低。"""
    CRITICAL = "critical"   # 安全漏洞、数据丢失风险
    HIGH = "high"           # 性能问题、异常吞没
    MEDIUM = "medium"       # 代码规范、缺失注解
    LOW = "low"             # 风格建议、优化提示


@dataclass
class Issue:
    """标准化的问题描述。

    所有扫描器必须输出 Issue 列表，字段含义统一。
    这样 pipeline 不需要关心"这个问题来自哪个扫描器"。

    属性:
        type: 问题类型标识符（如 "sql_injection", "swallowed_exception"）
        severity: 严重等级（critical/high/medium/low）
        file: 相对于项目根目录的文件路径
        line: 问题所在行号（0 表示文件级问题）
        description: 人类可读的问题描述
        suggestion: 修复建议（给修复器或人工参考）
        scanner: 发现此问题的扫描器名称（自动填充）
        context: 额外上下文（如周围的代码片段、AST 节点信息）
    """
    type: str
    severity: str
    file: str
    line: int
    description: str
    suggestion: str = ""
    scanner: str = ""
    context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Issue":
        return Issue(
            type=d.get("type", "unknown"),
            severity=d.get("severity", "medium"),
            file=d.get("file", ""),
            line=d.get("line", 0),
            description=d.get("description", ""),
            suggestion=d.get("suggestion", ""),
            scanner=d.get("scanner", ""),
            context=d.get("context", {}),
        )


@dataclass
class FixResult:
    """标准化的修复结果。

    所有修复器必须输出 FixResult，字段含义统一。
    新增 confidence 字段用于审批门控决策。

    属性:
        success: 修复是否成功应用
        action: 执行了什么操作（如 "空except → logging.exception()"）
        confidence: 修复置信度（0.0~1.0）
            > 0.8  → 自动应用
            0.5~0.8 → 进入人工审批队列
            < 0.5  → 拒绝，只记录
        error: 失败原因（success=False 时）
        diff: 修改前后的差异（供审批时参考）
        rollback_info: 回滚信息（修改前的文件内容 hash）
        fixer: 执行修复的修复器名称
        issue_type: 对应的 Issue 类型
        file: 修改的文件路径
        line: 修改的行号
    """
    success: bool
    action: str = ""
    confidence: float = 0.0
    error: str = ""
    diff: str = ""
    rollback_info: str = ""
    fixer: str = ""
    issue_type: str = ""
    file: str = ""
    line: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════════════
# 扫描器抽象接口
# ═══════════════════════════════════════════════════════════════════════

class ScannerAdapter(ABC):
    """扫描器标准接口。

    所有扫描器必须实现此接口。pipeline 通过 registry 获取扫描器实例，
    调用 scan() 获取 Issue 列表，不需要知道底层实现。

    设计决策（面试话术）：
      "为什么用 ABC 而不是鸭子类型？因为扫描器的输出格式必须严格统一——
       如果某个扫描器返回的 dict 缺了 'severity' 字段，下游的审批门控
       就会崩溃。ABC 强制编译期检查，比运行时报错更安全。"
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """扫描器唯一名称（如 'security', 'performance', 'enterprise'）。"""

    @property
    @abstractmethod
    def dimension(self) -> str:
        """扫描维度（如 'security', 'quality', 'performance'）。"""

    @abstractmethod
    def scan(self, project_root: Path) -> list[Issue]:
        """扫描项目，返回标准化 Issue 列表。

        Args:
            project_root: 项目根目录（绝对路径）

        Returns:
            Issue 列表，每个 Issue 代表一个发现的问题。
            空列表表示没有发现问题。
        """

    def is_available(self) -> bool:
        """检查此扫描器是否可用（依赖是否满足）。默认可用。"""
        return True


# ═══════════════════════════════════════════════════════════════════════
# 修复器抽象接口
# ═══════════════════════════════════════════════════════════════════════

class FixerAdapter(ABC):
    """修复器标准接口。

    所有修复器必须实现此接口。pipeline 通过 registry 获取修复器实例，
    调用 fix() 获取 FixResult，再由 ConfidenceGate 决定是否应用。

    设计决策（面试话术）：
      "修复器和扫描器最大的区别是：修复器有副作用（修改文件）。
       所以 FixResult 里必须有 confidence 和 rollback_info——
       低置信度的修复不能直接应用，需要人工审批；应用后如果出问题，
       需要能回滚。这是扫描器不需要考虑的。"
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """修复器唯一名称（如 'swallowed_exception_fixer'）。"""

    @property
    @abstractmethod
    def supported_types(self) -> list[str]:
        """此修复器能处理的 Issue 类型列表。"""

    @abstractmethod
    def fix(self, issue: Issue, project_root: Path) -> FixResult:
        """尝试修复一个问题，返回标准化 FixResult。

        注意：fix() 只生成补丁，不直接写文件。
        实际写入由 ConfidenceGate 根据置信度决定。

        Args:
            issue: 要修复的问题（来自扫描器）
            project_root: 项目根目录

        Returns:
            FixResult，包含修复动作、置信度、差异等信息。
        """

    def can_fix(self, issue_type: str) -> bool:
        """检查此修复器是否能处理指定类型的问题。"""
        return issue_type in self.supported_types


# ═══════════════════════════════════════════════════════════════════════
# 注册表
# ═══════════════════════════════════════════════════════════════════════

class ScannerRegistry:
    """扫描器注册表。

    集中管理所有扫描器实例，按名称查找。
    pipeline 启动时注册所有扫描器，运行时按需调用。

    使用方式：
        registry = ScannerRegistry()
        registry.register(SecurityScanner())
        registry.register(PerformanceScanner())
        # ...
        for scanner in registry.all():
            issues.extend(scanner.scan(project_root))
    """

    def __init__(self):
        self._scanners: dict[str, ScannerAdapter] = {}

    def register(self, scanner: ScannerAdapter) -> None:
        """注册一个扫描器。"""
        self._scanners[scanner.name] = scanner
        logger.info("Scanner registered: %s (dimension=%s)", scanner.name, scanner.dimension)

    def get(self, name: str) -> Optional[ScannerAdapter]:
        """按名称获取扫描器。"""
        return self._scanners.get(name)

    def all(self) -> list[ScannerAdapter]:
        """返回所有已注册且可用的扫描器。"""
        return [s for s in self._scanners.values() if s.is_available()]

    def by_dimension(self, dimension: str) -> list[ScannerAdapter]:
        """按维度筛选扫描器。"""
        return [s for s in self.all() if s.dimension == dimension]

    def scan_all(self, project_root: Path) -> list[Issue]:
        """运行所有扫描器，汇总 Issue 列表。"""
        all_issues: list[Issue] = []
        for scanner in self.all():
            try:
                issues = scanner.scan(project_root)
                for issue in issues:
                    issue.scanner = scanner.name
                all_issues.extend(issues)
                logger.info("Scanner %s: %d issues found", scanner.name, len(issues))
            except Exception as e:
                logger.error("Scanner %s failed: %s", scanner.name, e)
        return all_issues


class FixerRegistry:
    """修复器注册表。

    集中管理所有修复器实例，按 Issue 类型查找合适的修复器。
    一个 Issue 类型只能有一个修复器（避免冲突）。

    使用方式：
        registry = FixerRegistry()
        registry.register(SwallowedExceptionFixer())
        registry.register(BareExceptFixer())
        # ...
        fixer = registry.get_fixer(issue.type)
        if fixer:
            result = fixer.fix(issue, project_root)
    """

    def __init__(self):
        self._fixers: dict[str, FixerAdapter] = {}  # issue_type → fixer
        self._fixer_instances: list[FixerAdapter] = []

    def register(self, fixer: FixerAdapter) -> None:
        """注册一个修复器。按 supported_types 索引。"""
        self._fixer_instances.append(fixer)
        for issue_type in fixer.supported_types:
            if issue_type in self._fixers:
                logger.warning(
                    "Issue type '%s' already has a fixer (%s), overriding with %s",
                    issue_type, self._fixers[issue_type].name, fixer.name,
                )
            self._fixers[issue_type] = fixer
        logger.info("Fixer registered: %s (types=%s)", fixer.name, fixer.supported_types)

    def get_fixer(self, issue_type: str) -> Optional[FixerAdapter]:
        """按 Issue 类型获取对应的修复器。"""
        return self._fixers.get(issue_type)

    def can_fix(self, issue_type: str) -> bool:
        """检查是否有修复器能处理此类型。"""
        return issue_type in self._fixers

    def supported_types(self) -> list[str]:
        """返回所有可修复的 Issue 类型。"""
        return list(self._fixers.keys())

    def all(self) -> list[FixerAdapter]:
        """返回所有已注册的修复器实例。"""
        return self._fixer_instances


# ═══════════════════════════════════════════════════════════════════════
# 兼容包装器 —— 把旧版扫描器/修复器包装成新接口
# ═══════════════════════════════════════════════════════════════════════
class LegacyScannerWrapper(ScannerAdapter):
    """把旧版 scan(blueprint) / scan(root_str) 包装成 ScannerAdapter。

    旧版 scanner 有两种签名：
      - scan(blueprint: OptimizationBlueprint) -> dict  （9维度扫描器）
      - scan(project_root_str: str) -> dict  （enterprise scanner）
    新版需要返回 list[Issue]。
    """

    def __init__(self, name: str, dimension: str, scan_fn,
                 accepts_root: bool = False, needs_blueprint: bool = False):
        self._name = name
        self._dimension = dimension
        self._scan_fn = scan_fn
        self._accepts_root = accepts_root
        self._needs_blueprint = needs_blueprint
        self._cached_blueprint = None  # 缓存 blueprint 避免重复分析

    @property
    def name(self) -> str:
        return self._name

    @property
    def dimension(self) -> str:
        return self._dimension

    def _get_blueprint(self, project_root: Path):
        """构建 OptimizationBlueprint 并启用所有维度。"""
        if self._cached_blueprint is not None:
            return self._cached_blueprint
        try:
            from src.analysis.project_analyzer import analyze_project
            blueprint = analyze_project(str(project_root))
            # 启用当前维度（否则 scanner 会跳过）
            blueprint.enabled_dimensions[self._dimension] = True
            self._cached_blueprint = blueprint
            return blueprint
        except Exception:
            return None

    def scan(self, project_root: Path) -> list[Issue]:
        try:
            if self._needs_blueprint:
                blueprint = self._get_blueprint(project_root)
                if blueprint is None:
                    return []
                result = self._scan_fn(blueprint)
            else:
                result = self._scan_fn(str(project_root))
        except Exception as e:
            logger.error("Scanner %s failed: %s", self._name, e)
            return []

        raw_issues = result.get("issues", []) if isinstance(result, dict) else result

        issues = []
        for raw in raw_issues:
            if isinstance(raw, dict):
                issues.append(Issue(
                    type=raw.get("type", "unknown"),
                    severity=raw.get("severity", "medium"),
                    file=raw.get("file", ""),
                    line=raw.get("line", 0),
                    description=raw.get("description", ""),
                    suggestion=raw.get("suggestion", ""),
                    scanner=self._name,
                ))
        return issues


class LegacyFixerWrapper(FixerAdapter):
    """把旧版 try_fix_deep(issue_dict, project_root) 包装成 FixerAdapter。

    旧版 fixer 返回 {"success": bool, "action": str, "error": str}，
    新版需要返回 FixResult（含 confidence）。
    """

    def __init__(self, name: str, supported_types: list[str], fix_fn,
                 default_confidence: float = 0.7):
        self._name = name
        self._supported_types = supported_types
        self._fix_fn = fix_fn
        self._default_confidence = default_confidence

    @property
    def name(self) -> str:
        return self._name

    @property
    def supported_types(self) -> list[str]:
        return self._supported_types

    def fix(self, issue: Issue, project_root: Path) -> FixResult:
        issue_dict = issue.to_dict()
        try:
            raw = self._fix_fn(issue_dict, project_root)
            return FixResult(
                success=raw.get("success", False),
                action=raw.get("action", ""),
                confidence=self._default_confidence,
                error=raw.get("error", ""),
                fixer=self._name,
                issue_type=issue.type,
                file=issue.file,
                line=issue.line,
            )
        except Exception as e:
            return FixResult(
                success=False,
                error=str(e),
                confidence=0.0,
                fixer=self._name,
                issue_type=issue.type,
            )


# ═══════════════════════════════════════════════════════════════════════
# Issue IR（中间表示）— 借鉴 HiveWard Skill IR 模式
# ═══════════════════════════════════════════════════════════════════════
#
# 设计动机（面试话术）：
#   "HiveWard 的 Skill Decomposer 不直接把技能变成蓝图节点，
#    而是先构建成 Skill IR（中间数据结构），验证通过后再映射。
#    我借鉴这个思路，在扫描器输出和修复器输入之间加了一层 Issue IR。
#    扫描器产出的原始 Issue 先进入 IR 层做验证、去重、聚合、排序，
#    然后再交给修复器。这样做的好处是：
#    1) 不同扫描器的输出格式差异在 IR 层消除，修复器只看 IR
#    2) 去重避免同一问题被多次修复（比如 security 和 enterprise 都报了同一行）
#    3) 聚合让修复器一次看到'这个文件的所有问题'，可以做批量修复
#    4) 验证确保必填字段完整，不会到修复器才发现缺字段"

@dataclass
class FixStrategy:
    """修复策略规划（借鉴 HiveWard Skill IR 的 phases 设计）。

    每个 Issue IR 在交给修复器之前，先规划好修复策略：
    - 修复方式（pattern_replace / ast_transform / manual_review）
    - 预期变更范围（单行 / 多行 / 整文件）
    - 回滚方案（原始内容 hash、git stash 等）
    - 前置条件（如"需要先备份"、"需要确认依赖"）
    - 验证方式（语法检查 / 重扫 / 单元测试）
    """
    approach: str = "pattern_replace"  # pattern_replace / ast_transform / manual_review
    scope: str = "single_line"  # single_line / multi_line / whole_file
    rollback_method: str = "content_hash"  # content_hash / git_stash / none
    preconditions: list[str] = field(default_factory=list)
    validation_steps: list[str] = field(default_factory=lambda: ["syntax_check", "rescan"])
    estimated_lines_changed: int = 1
    can_parallelize: bool = True  # 是否可以与其他修复并行执行


@dataclass
class RiskAssessment:
    """修复风险评估（借鉴 HiveWard Skill IR 的 risks 字段）。

    评估修复可能带来的副作用：
    - 引入新 bug 的概率
    - 影响范围（局部 / 模块级 / 全局）
    - 是否需要人工确认
    - 未解决的假设（如"假设这个函数只被调用一次"）
    """
    regression_risk: str = "low"  # low / medium / high
    blast_radius: str = "local"  # local / module / global
    needs_human_confirm: bool = False
    unresolved_assumptions: list[str] = field(default_factory=list)
    mitigation_notes: list[str] = field(default_factory=list)


@dataclass
class IssueIR:
    """Issue 中间表示（Intermediate Representation）。

    扫描器产出的原始 Issue 先转换为 IssueIR，经过验证、去重、聚合后，
    再交给修复器。IR 层是扫描器和修复器之间的"契约"。

    与 Issue 的区别：
      - Issue 是扫描器的输出格式（宽松，允许缺字段）
      - IssueIR 是修复器的输入格式（严格，必填字段已验证）
      - IssueIR 增加了 fingerprint（去重用）、related_issues（聚合用）、
        fix_complexity（修复复杂度评估）

    借鉴 HiveWard Skill IR 的增强（2026-05-28）：
      - fix_strategy: 修复策略规划（怎么修、修多大范围、怎么验证）
      - risk_assessment: 风险评估（回归风险、影响范围、未解决假设）
      - 这两个字段让修复器在执行前就能判断"该不该修、怎么修"

    属性:
        issue: 原始 Issue
        fingerprint: 问题指纹（用于去重，相同指纹 = 同一问题）
        related_issues: 同文件同类型的其他 Issue IR（聚合信息）
        fix_complexity: 修复复杂度评估（simple/medium/complex）
        validation_warnings: 验证时发现的警告（不影响流转，仅供修复器参考）
        tags: 标签（如 "security", "auto_fixable", "needs_review"）
        estimated_effort: 预估修复耗时（秒）
        fix_strategy: 修复策略规划
        risk_assessment: 风险评估
    """
    issue: Issue
    fingerprint: str = ""
    related_issues: list = field(default_factory=list)  # list[IssueIR] 循环引用用 Any
    fix_complexity: str = "medium"  # simple / medium / complex
    validation_warnings: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    estimated_effort: int = 0  # 秒
    fix_strategy: FixStrategy = field(default_factory=FixStrategy)
    risk_assessment: RiskAssessment = field(default_factory=RiskAssessment)

    def __post_init__(self):
        if not self.fingerprint:
            self.fingerprint = self._compute_fingerprint()
        if not self.tags:
            self.tags = self._auto_tag()

    def _compute_fingerprint(self) -> str:
        """计算问题指纹：同一类型 + 同一文件 + 相邻行 = 同一问题。

        相邻行阈值为 ±3 行，因为同一个 bug 可能被不同扫描器报告在略有
        偏差的行号上。
        """
        # 用文件 + 类型 + 行号的"桶"作为指纹（行号除以3取整，允许±3行偏移）
        line_bucket = self.issue.line // 3 if self.issue.line > 0 else 0
        key = f"{self.issue.type}:{self.issue.file}:{line_bucket}"
        return hashlib.md5(key.encode()).hexdigest()[:16]

    def _auto_tag(self) -> list[str]:
        """根据 Issue 属性自动打标签。"""
        tags = []
        if self.issue.severity in ("critical", "high"):
            tags.append("high_priority")
        if self.issue.type in ("sql_injection", "path_traversal", "xss", "hardcoded_secret"):
            tags.append("security")
        if self.issue.type in ("swallowed_exception", "bare_except", "print_used", "missing_timeout_config"):
            tags.append("auto_fixable")
        if self.fix_complexity == "complex":
            tags.append("needs_review")
        return tags

    def validate(self) -> bool:
        """验证 Issue IR 是否完整可用。

        Returns:
            True 表示验证通过，False 表示有致命问题（不应交给修复器）
        """
        warnings = []
        if not self.issue.type or self.issue.type == "unknown":
            warnings.append("issue_type is unknown")
        if not self.issue.file:
            warnings.append("file path is empty")
        if self.issue.severity not in ("critical", "high", "medium", "low"):
            warnings.append(f"invalid severity: {self.issue.severity}")
        if not self.issue.description:
            warnings.append("description is empty")
        self.validation_warnings = warnings
        # 有 file 和 type 就算通过，其他是警告
        return bool(self.issue.file and self.issue.type and self.issue.type != "unknown")

    def to_dict(self) -> dict:
        return {
            "issue": self.issue.to_dict(),
            "fingerprint": self.fingerprint,
            "related_count": len(self.related_issues),
            "fix_complexity": self.fix_complexity,
            "validation_warnings": self.validation_warnings,
            "tags": self.tags,
            "estimated_effort": self.estimated_effort,
            "fix_strategy": {
                "approach": self.fix_strategy.approach,
                "scope": self.fix_strategy.scope,
                "rollback_method": self.fix_strategy.rollback_method,
                "preconditions": self.fix_strategy.preconditions,
                "validation_steps": self.fix_strategy.validation_steps,
                "estimated_lines_changed": self.fix_strategy.estimated_lines_changed,
                "can_parallelize": self.fix_strategy.can_parallelize,
            },
            "risk_assessment": {
                "regression_risk": self.risk_assessment.regression_risk,
                "blast_radius": self.risk_assessment.blast_radius,
                "needs_human_confirm": self.risk_assessment.needs_human_confirm,
                "unresolved_assumptions": self.risk_assessment.unresolved_assumptions,
                "mitigation_notes": self.risk_assessment.mitigation_notes,
            },
        }


class IssueProcessor:
    """Issue IR 处理器 — 验证、去重、聚合、排序。

    这是 pipeline 中扫描器和修复器之间的处理阶段。
    借鉴 HiveWard Skill Decomposer 的"先构建 IR，再映射"思路。

    使用方式：
        processor = IssueProcessor()
        raw_issues = scanners.scan_all(project_root)  # list[Issue]
        ir_list = processor.process(raw_issues)         # list[IssueIR]
        for ir in ir_list:
            fixer = fixers.get_fixer(ir.issue.type)
            fixer.fix(ir.issue, project_root)
    """

    def process(self, issues: list[Issue]) -> list[IssueIR]:
        """完整处理流程：构建 IR → 验证 → 去重 → 聚合 → 评估 → 规划策略 → 排序。

        Args:
            issues: 扫描器产出的原始 Issue 列表

        Returns:
            处理后的 IssueIR 列表，按优先级排序
        """
        # 1. 构建 IR
        ir_list = [IssueIR(issue=issue) for issue in issues]

        # 2. 验证（过滤掉无效的）
        valid_ir = []
        for ir in ir_list:
            if ir.validate():
                valid_ir.append(ir)
            else:
                logger.warning("Issue IR validation failed: %s (%s)",
                               ir.issue.type, ir.validation_warnings)

        # 3. 去重
        deduped = self._deduplicate(valid_ir)

        # 4. 聚合（同文件的问题关联起来）
        self._aggregate(deduped)

        # 5. 评估复杂度 + 规划修复策略 + 评估风险（借鉴 HiveWard Skill IR）
        for ir in deduped:
            ir.fix_complexity = self._assess_complexity(ir)
            ir.estimated_effort = self._estimate_effort(ir)
            ir.fix_strategy = self._plan_fix_strategy(ir)
            ir.risk_assessment = self._assess_risk(ir)

        # 6. 按优先级排序
        deduped.sort(key=lambda ir: self._priority_key(ir))

        logger.info("IssueProcessor: %d raw → %d valid → %d deduped",
                     len(issues), len(valid_ir), len(deduped))
        return deduped

    def _deduplicate(self, ir_list: list[IssueIR]) -> list[IssueIR]:
        """按 fingerprint 去重，保留最高严重度的那个。"""
        seen: dict[str, IssueIR] = {}
        for ir in ir_list:
            fp = ir.fingerprint
            if fp in seen:
                # 保留严重度更高的
                existing = seen[fp]
                sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
                if sev_order.get(ir.issue.severity, 9) < sev_order.get(existing.issue.severity, 9):
                    seen[fp] = ir
            else:
                seen[fp] = ir
        return list(seen.values())

    def _aggregate(self, ir_list: list[IssueIR]) -> None:
        """关联同文件的问题（互相引用），让修复器知道'这个文件还有哪些问题'。"""
        by_file: dict[str, list[IssueIR]] = {}
        for ir in ir_list:
            by_file.setdefault(ir.issue.file, []).append(ir)
        for file_path, file_irs in by_file.items():
            if len(file_irs) > 1:
                for ir in file_irs:
                    ir.related_issues = [other for other in file_irs if other is not ir]

    def _assess_complexity(self, ir: IssueIR) -> str:
        """评估修复复杂度。"""
        simple_types = {"swallowed_exception", "bare_except", "print_used",
                        "missing_return_type", "missing_timeout_config"}
        complex_types = {"sql_injection", "path_traversal", "xss",
                         "race_condition", "resource_not_managed"}
        if ir.issue.type in simple_types:
            return "simple"
        elif ir.issue.type in complex_types:
            return "complex"
        return "medium"

    def _estimate_effort(self, ir: IssueIR) -> int:
        """预估修复耗时（秒）。"""
        effort_map = {"simple": 30, "medium": 120, "complex": 600}
        base = effort_map.get(ir.fix_complexity, 120)
        # 同文件有多个问题时，每个问题的边际修复时间递减
        related_bonus = len(ir.related_issues) * 10
        return max(base - related_bonus, 15)

    def _plan_fix_strategy(self, ir: IssueIR) -> FixStrategy:
        """为 Issue IR 规划修复策略（借鉴 HiveWard Skill IR 的 phases 设计）。

        根据 issue 类型和复杂度，决定：
        - 用什么修复方式（正则替换 / AST 变换 / 人工审查）
        - 变更范围有多大
        - 是否可以并行修复
        """
        issue_type = ir.issue.type

        # 简单类型：正则/文本替换，单行，可并行
        simple_strategies = {
            "swallowed_exception": FixStrategy(
                approach="pattern_replace", scope="multi_line",
                estimated_lines_changed=3,
                validation_steps=["syntax_check", "rescan"],
            ),
            "bare_except": FixStrategy(
                approach="pattern_replace", scope="single_line",
                estimated_lines_changed=1,
            ),
            "print_used": FixStrategy(
                approach="pattern_replace", scope="single_line",
                estimated_lines_changed=1,
            ),
            "missing_return_type": FixStrategy(
                approach="pattern_replace", scope="single_line",
                estimated_lines_changed=1,
            ),
            "missing_timeout_config": FixStrategy(
                approach="pattern_replace", scope="single_line",
                estimated_lines_changed=1,
                preconditions=["verify_config_file_exists"],
            ),
            "resource_not_managed": FixStrategy(
                approach="pattern_replace", scope="multi_line",
                estimated_lines_changed=5,
                validation_steps=["syntax_check", "rescan", "import_check"],
            ),
        }

        if issue_type in simple_strategies:
            return simple_strategies[issue_type]

        # 复杂类型：AST 变换或人工审查，不可并行
        complex_types = {"sql_injection", "path_traversal", "xss", "race_condition"}
        if issue_type in complex_types:
            return FixStrategy(
                approach="manual_review", scope="multi_line",
                rollback_method="git_stash",
                preconditions=["backup_file", "review_with_team"],
                validation_steps=["syntax_check", "security_rescan", "unit_test"],
                can_parallelize=False,
                estimated_lines_changed=10,
            )

        # 中等类型：默认策略
        return FixStrategy(
            approach="pattern_replace", scope="single_line",
            estimated_lines_changed=2,
        )

    def _assess_risk(self, ir: IssueIR) -> RiskAssessment:
        """评估修复风险（借鉴 HiveWard Skill IR 的 risks 字段）。

        根据 issue 严重度、类型、影响范围评估：
        - 回归风险（修了这个会不会引入新 bug）
        - 爆炸半径（影响多大范围）
        - 是否需要人工确认
        """
        issue_type = ir.issue.type
        severity = ir.issue.severity

        # 安全类问题：高风险，需要人工确认
        security_types = {"sql_injection", "path_traversal", "xss", "hardcoded_secret"}
        if issue_type in security_types:
            return RiskAssessment(
                regression_risk="high",
                blast_radius="global",
                needs_human_confirm=True,
                unresolved_assumptions=["修复可能改变业务逻辑"],
                mitigation_notes=["建议在 staging 环境验证", "需要安全团队 review"],
            )

        # 资源管理类：中等风险
        resource_types = {"resource_not_managed", "race_condition"}
        if issue_type in resource_types:
            return RiskAssessment(
                regression_risk="medium",
                blast_radius="module",
                needs_human_confirm=False,
                unresolved_assumptions=["假设资源生命周期正确"],
                mitigation_notes=["建议添加资源泄漏监控"],
            )

        # 简单修复：低风险
        simple_types = {"swallowed_exception", "bare_except", "print_used", "missing_return_type"}
        if issue_type in simple_types:
            return RiskAssessment(
                regression_risk="low",
                blast_radius="local",
                needs_human_confirm=False,
            )

        # 默认：中等风险
        return RiskAssessment(
            regression_risk="medium",
            blast_radius="local",
            needs_human_confirm=severity in ("critical", "high"),
        )

    def _priority_key(self, ir: IssueIR) -> tuple:
        """排序键：严重度 → 复杂度（简单的先修）→ 文件路径。"""
        sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        complexity_order = {"simple": 0, "medium": 1, "complex": 2}
        return (
            sev_order.get(ir.issue.severity, 9),
            complexity_order.get(ir.fix_complexity, 9),
            ir.issue.file,
            ir.issue.line,
        )


def build_default_scanner_registry() -> ScannerRegistry:
    """构建默认扫描器注册表，包装所有现有扫描器。

    这个函数把 9 个维度扫描器 + deep_enterprise_scanner 注册到统一 registry。
    新增扫描器只需要在这里加一行 register()。
    """
    registry = ScannerRegistry()

    # 9 维度扫描器（从 dims/ 导入）
    _SCANNER_MAP = [
        ("security", "security", "dims.sec_scanner"),
        ("performance", "performance", "dims.perf_scanner"),
        ("quality", "quality", "dims.quality_scanner"),
        ("async_sync", "async_sync", "dims.async_scanner"),
        ("config", "config", "dims.config_scanner"),
        ("deadcode", "deadcode", "dims.deadcode_scanner"),
        ("documentation", "documentation", "dims.doc_scanner"),
        ("architecture", "architecture", "dims.arch_scanner"),
        ("testing", "testing", "dims.test_scanner"),
    ]

    for name, dimension, module_path in _SCANNER_MAP:
        try:
            import importlib
            mod = importlib.import_module(f"src.analysis.{module_path}")
            scan_fn = getattr(mod, "scan", None)
            if scan_fn:
                registry.register(LegacyScannerWrapper(
                    name, dimension, scan_fn, needs_blueprint=True
                ))
        except ImportError as e:
            logger.warning("Scanner %s not available: %s", name, e)

    # 企业级深度扫描器
    try:
        from src.analysis.deep_enterprise_scanner import scan_deep
        registry.register(LegacyScannerWrapper("enterprise", "enterprise", scan_deep, accepts_root=True))
    except ImportError as e:
        logger.warning("Enterprise scanner not available: %s", e)

    return registry


def build_default_fixer_registry() -> FixerRegistry:
    """构建默认修复器注册表，包装所有现有修复器。

    这个函数把 enterprise_fixer 的 6 个可工作修复器注册到统一 registry。
    新增修复器只需要在这里加一行 register()。
    """
    registry = FixerRegistry()

    try:
        from src.fixers.enterprise_fixer import try_fix_deep
        # 用 try_fix_deep 做统一分发器（它内部根据 issue_type 路由到具体修复函数）
        _FIXER_TYPES = [
            "swallowed_exception", "bare_except", "print_used",
            "resource_not_managed", "missing_timeout_config", "missing_return_type",
        ]

        for issue_type in _FIXER_TYPES:
            registry.register(LegacyFixerWrapper(
                f"{issue_type}_fixer", [issue_type], try_fix_deep, default_confidence=0.80
            ))

    except ImportError as e:
        logger.warning("Enterprise fixer not available: %s", e)

    return registry

