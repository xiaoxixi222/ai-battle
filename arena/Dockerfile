# AI 容器镜像（M3）：python + opencode（serve 与 run 都要用）。
#
# 构建（在仓库根目录）：
#   docker build -t ai-battle-ai:latest arena/
#
# python 是必要的：侦察轮里 AI 要读、要跑工作区里的 server/ 副本（纯 python）。
#
# 注意：不走官方安装脚本（https://opencode.ai/install 的证书链在部分网络环境
# 验不过）；直接从 npmmirror 的 npm 镜像取 CLI 压缩包。二进制落在
# /root/.opencode/bin/opencode（不在 PATH；内核用全路径调用）。
FROM python:latest

ARG OPENCODE_VERSION=2.0.32

RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
           amd64) pkg="cli-linux-x64" ;; \
           arm64) pkg="cli-linux-arm64" ;; \
           *) echo "不支持架构：$arch" >&2; exit 1 ;; \
       esac \
    && mkdir -p /root/.opencode/bin /tmp/o \
    && curl -fSL -o /tmp/o/cli.tgz \
       "https://registry.npmmirror.com/@opencode/${pkg}/-/${pkg}-${OPENCODE_VERSION}.tgz" \
    && tar -xzf /tmp/o/cli.tgz -C /tmp/o \
    && install -m 755 /tmp/o/package/bin/opencode /root/.opencode/bin/opencode \
    && rm -rf /tmp/o \
    && /root/.opencode/bin/opencode --version
