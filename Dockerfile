# Muse Domestic Bridge — 飞书渠道容器镜像
# 上游: https://github.com/norlingdumar-arch/muse-domestic-bridge (Apache-2.0)
# 只安装 feishu 渠道的依赖，不装 weixin/qq 的。
FROM python:3.11-slim

# 容器内不写 pyc、输出实时刷新
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 只装 CA 证书。视频抽帧用 PyAV 的静态 ffmpeg（见 video_frames.py）：
# Debian 的 ffmpeg 包会经 libplacebo 拉入 libllvm19 + mesa-libgallium，
# 实测让镜像多出 449MB；PyAV wheel 只要约 104MB 且无系统依赖。
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates; \
    rm -rf /var/lib/apt/lists/*

COPY src/muse-domestic-bridge /tmp/bridge

RUN set -eux; \
    python -m pip install --only-binary=:all: "/tmp/bridge[feishu]"; \
    rm -rf /tmp/bridge; \
    # 冒烟测试：CLI 可用 + 关键模块能导入 + 视频解码器在位
    muse-bridge --version; \
    python -c "import muse_feishu_bridge.muse, muse_feishu_bridge.feishu, muse_feishu_bridge.video_frames, muse_feishu_bridge.reader, muse_feishu_bridge.markdown, lark_oapi; print('imports OK')"; \
    python -c "from muse_feishu_bridge import video_frames; assert video_frames.available(), 'video decoder missing'; print('video decode OK')"

# 非 root 运行。UID/GID 与宿主机目录属主保持一致（compose 里可覆盖）
ARG UID=10001
ARG GID=10001
RUN groupadd -g "${GID}" bridge && useradd -u "${UID}" -g "${GID}" -m -s /usr/sbin/nologin bridge

# 配置目录（含 secrets.json）与状态目录都用 volume 挂进来
RUN mkdir -p /config /state && chown -R "${UID}:${GID}" /config /state

USER ${UID}:${GID}
WORKDIR /state

ENTRYPOINT ["muse-bridge"]
# 默认跑 feishu 渠道；可用 docker compose run 覆盖成 configure/check/status/pair
CMD ["run", "feishu", "--config", "/config/config.json"]
