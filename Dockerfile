# 生产后端：无 Playwright 浏览器；依赖可缓存，日常启动不重建。
# 默认使用 DaoCloud 国内镜像；私有镜像仓库可通过 BASE_IMAGE_REGISTRY 覆盖。
ARG BASE_IMAGE_REGISTRY=docker.m.daocloud.io
FROM ${BASE_IMAGE_REGISTRY}/library/python:3.12-slim AS runtime

ARG PIP_INDEX_URL
ARG DEBIAN_MIRROR
ARG DEBIAN_SECURITY_MIRROR

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60

# 安装运行期健康检查工具。
RUN set -eux; \
    if [ -n "${DEBIAN_MIRROR:-}" ]; then \
        sed -i "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -n "${DEBIAN_SECURITY_MIRROR:-}" ]; then \
        sed -i "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# BuildKit 缓存保留已验证的下载包。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts

RUN mkdir -p /app/logs \
    && chmod +x /app/scripts/docker_entrypoint_backend.sh

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=8 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["/app/scripts/docker_entrypoint_backend.sh"]
