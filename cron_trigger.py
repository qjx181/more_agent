#!/usr/bin/env python3
"""cron_trigger.py — 系统 cron 触发器，每 30 分钟调用 hermes 执行一轮自我进化。

使用方式（添加系统 cron）：
   crontab -e
   添加一行：*/30 * * * * /usr/bin/python3 /mnt/f/项目三：多Agent/cron_trigger.py >> /mnt/f/项目三：多Agent/logs/cron.log 2>&1

注意：这个脚本需要 Hermes Agent 的提供者（当前对话的模型）能够正常访问。
它通过 hermes CLI 发起一个自我进化循环的请求。
"""

import datetime
import os
import subprocess
import sys

WORKDIR = "/mnt/f/项目三：多Agent"
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_DIR = os.path.join(WORKDIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"cron_{TIMESTAMP}.log")


def log(msg: str) -> None:
    line = f"[{TIMESTAMP}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def run_self_evolve() -> bool:
    """执行 self_evolve_round.py（Git 提交 + 状态检查）。"""
    log("执行 self_evolve_round.py...")
    result = subprocess.run(
        [sys.executable, os.path.join(WORKDIR, "self_evolve_round.py")],
        capture_output=True, text=True, cwd=WORKDIR, timeout=600
    )
    if result.stdout:
        log(f"stdout: {result.stdout.strip()[-200:]}")
    if result.returncode != 0:
        log(f"错误: exit={result.returncode}, {result.stderr[-200:]}")
        return False
    return True


def main():
    log("=" * 50)
    log("系统 cron 触发 — 自我进化循环")

    success = run_self_evolve()

    if success:
        log("完成 ✅")
    else:
        log("失败 ❌")
    log("=" * 50)


if __name__ == "__main__":
    main()
