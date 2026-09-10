# Multi-stage build for gcli2api
FROM python:3.13-slim as base

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# Install tzdata, jemalloc and set timezone
# jemalloc 替代 glibc malloc，大幅减少内存碎片化
RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata libjemalloc2 && \
    ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 使用 jemalloc 替代 glibc malloc
# jemalloc 会主动将空闲内存归还给操作系统，避免 RSS 持续增长。
# libjemalloc.so.2 的安装路径随架构不同（x86_64-linux-gnu / aarch64-linux-gnu），
# 在构建期建立与架构无关的符号链接，保证 amd64 与 arm64 镜像都能正确加载。
ARG TARGETARCH
RUN case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
        amd64 | x86_64) JEMALLOC_TRIPLET="x86_64-linux-gnu" ;; \
        arm64 | aarch64) JEMALLOC_TRIPLET="aarch64-linux-gnu" ;; \
        *) echo "Unsupported architecture: ${TARGETARCH:-$(dpkg --print-architecture)}" >&2; exit 1 ;; \
    esac && \
    ln -sf "/usr/lib/${JEMALLOC_TRIPLET}/libjemalloc.so.2" /usr/lib/libjemalloc.so.2
ENV LD_PRELOAD=/usr/lib/libjemalloc.so.2
# jemalloc 配置：启用后台线程回收、脏页衰减时间 5 秒（默认 10 秒）
ENV MALLOC_CONF="background_thread:true,dirty_decay_ms:5000,muzzy_decay_ms:5000"

WORKDIR /app

# Copy only requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 7861

# Default command
CMD ["python", "web.py"]
