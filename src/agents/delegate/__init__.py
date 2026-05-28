"""委托优化模块 — 拆分为 delegate/ 包"""
from src.agents.delegate.scanner import scan_codebase_for_issues
from src.agents.delegate.delegate import should_delegate, build_delegation_prompt, build_coder_prompt, build_tester_prompt, build_reviewer_prompt, check_signature_unchanged, count_lines_added_removed, log_coordinator_write_size
from src.agents.delegate.analyzer import diagnose_failures
from src.agents.delegate.verifier import run_layer3_verification
