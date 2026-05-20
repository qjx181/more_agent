# ═══════════════════════════════════════════════════════════════════════
# Dockerfile — 项目三：多Agent 容器镜像
# ═══════════════════════════════════════════════════════════════════════
# 基于 Python 3.11-slim，最小化镜像体积。
# 构建： docker build -t swarm-agent:latest .
# 运行： docker compose up -d
# ═══════════════════════════════════════════════════════════════════════

FROM python:3.11-slim AS base

# ─── 系统依赖 ──────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ─── 工作目录 ──────────────────────────────────────────────────────────
WORKDIR /app

# ─── 依赖缓存层 ────────────────────────────────────────────────────────
# 将 requirements.txt 单独复制，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ─── 项目代码 ──────────────────────────────────────────────────────────
COPY . .

# ─── 运行时目录 ────────────────────────────────────────────────────────
RUN mkdir -p /app/logs /app/heartbeats /app/tmp_agent /app/metrics

# ─── 非 root 用户运行 ─────────────────────────────────────────────────
RUN useradd -m -u 1000 swarm && chown -R swarm:swarm /app
USER swarm

# ─── 健康检查 ──────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:9090/health || exit 1

# ─── 默认命令 ──────────────────────────────────────────────────────────
EXPOSE 9090
ENTRYPOINT ["python3", "/app/scripts/docker/docker-entrypoint.sh"]
CMD ["swarm"]
