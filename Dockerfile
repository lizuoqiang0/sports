# 生产后端：无 Playwright 浏览器；依赖可缓存，日常启动不重建
FROM python:3.12-slim AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60

# 国内源加速 apt（腾讯云 / Debian 官方镜像均可）
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g; s|security.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
      sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g; s|security.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list; \
    fi; \
    apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-prod.txt .
# 国内 PyPI + BuildKit 缓存，二次构建通常几十秒内完成
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -i https://mirrors.cloud.tencent.com/pypi/simple \
      --trusted-host mirrors.cloud.tencent.com \
      -r requirements-prod.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts
COPY __init__.py ./__init__.py

RUN mkdir -p /app/logs \
    && chmod +x /app/scripts/docker_entrypoint_backend.sh

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=8 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["/app/scripts/docker_entrypoint_backend.sh"]
