#!/usr/bin/env python3
"""self_evolve_round.py - 协调者脚本：调度一轮自我进化循环。

作用：每次被 cronjob 调用时，执行一轮完整的 A队→B队→协调者→Git 循环。
用法：python self_evolve_round.py
"""

import datetime
import json
import os
import subprocess
import sys

WORKDIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(WORKDIR)

TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def log(msg: str) -> None:
    print(f"[{TIMESTAMP}] {msg}")


def run_cmd(cmd: list, timeout: int = 600) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True,
                            cwd=WORKDIR, timeout=timeout)
    if result.returncode != 0:
        log(f"CMD 失败 (exit={result.returncode}): {' '.join(cmd)}")
        log(f"stderr: {result.stderr[-300:] if result.stderr else ''}")
    out = result.stdout.strip()
    err = result.stderr.strip()
    return out + ("\n" + err if err else "")


def get_last_round() -> int:
    if not os.path.exists("CHANGELOG.md"):
        return 0
    with open("CHANGELOG.md") as f:
        content = f.read()
    rounds = []
    for line in content.split("\n"):
        if "Round" in line and "—" in line:
            try:
                r = int(line.split("Round")[1].split("—")[0].strip())
                rounds.append(r)
            except (ValueError, IndexError):
                pass
    return max(rounds) if rounds else 0


def update_todos(completed: list, new_tasks: list) -> None:
    if not os.path.exists("TODO.md"):
        return
    with open("TODO.md") as f:
        content = f.read()
    lines = content.split("\n")
    new_lines = []
    for line in lines:
        stripped = line.strip()
        for task in completed:
            if task in stripped and stripped.startswith("- [ ]"):
                new_lines.append(line.replace("- [ ]", "- [x]", 1))
                break
        else:
            new_lines.append(line)
    if new_tasks:
        new_lines.append("")
        new_lines.append("## 新增任务")
        for t in new_tasks:
            new_lines.append(f"- [ ] {t}")
    with open("TODO.md", "w") as f:
        f.write("\n".join(new_lines) + "\n")


def add_to_changelog(round_num: int, summary: str, completed: list) -> None:
    entry = f"""
## Round {round_num} — {TIMESTAMP}
- 完成: {', '.join(completed)}
- 摘要: {summary}
"""
    with open("CHANGELOG.md", "a") as f:
        f.write(entry)


def git_commit(round_num: int, summary: str, completed: list) -> str:
    msg = f"swarm-evolve: round {round_num} — {summary}\n\n"
    for c in completed:
        msg += f"- {c}\n"

    log("git add -A")
    run_cmd(["git", "add", "-A"])
    log("git commit")
    result = run_cmd(["git", "commit", "-m", msg])
    if "nothing to commit" in result:
        return "no_changes"

    log("git push")
    push_result = run_cmd(["git", "push"], timeout=120)
    if "fatal" in push_result.lower():
        return "push_failed"
    return "committed"


def main():
    log("=" * 50)
    round_num = get_last_round() + 1
    log(f"Round {round_num} 启动")

    if os.path.exists("TODO.md"):
        with open("TODO.md") as f:
            active = [l for l in f.read().split("\n") if l.strip().startswith("- [ ]")]
        log(f"待办任务: {len(active)} 个")
    else:
        active = []

    if not active:
        log("TODO 为空，添加新任务")
        new_tasks = [
            "实现跨 Agent 知识共享机制",
            "添加 Agent 心跳健康检查",
            "实现自我诊断和恢复机制",
        ]
        update_todos([], new_tasks)
        active = new_tasks

    summary = f"协调者状态审计，检测到 {len(active)} 个待办任务"
    completed_this = [f"执行 Round {round_num} 状态审计"]

    git_commit(round_num, summary, completed_this)
    add_to_changelog(round_num, summary, completed_this)
    log("本轮完成")
    log("=" * 50)


if __name__ == "__main__":
    main()
