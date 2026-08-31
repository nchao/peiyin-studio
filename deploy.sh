#!/usr/bin/env bash
# 一键部署到群晖 NAS：Mac 本地构建镜像 → save → 传 NAS → load → 起容器。
#
# 为什么这么做：NAS 那颗 Celeron J4125 太弱，本地构建（尤其前端 npm build
# 和 pip 编译扩展）要几分钟；而 Mac 的 i7 只要几秒。镜像同为 amd64，Mac 上
# 构建好直接拿到 NAS 跑。Docker 分层缓存让改代码后只重传变化的层，很快。
#
# 用法：
#   ./deploy.sh            # 构建 + 传输 + 部署
#   ./deploy.sh --no-build # 跳过构建，用现有本地镜像直接传+部署
#
# 依赖：Mac 上 docker 可用；已配好到 NAS 的 SSH 免密与 sudo docker 免密。
set -euo pipefail

# ---- 配置 ----
NAS_HOST="user@your-nas-host"
NAS_PORT="22"
NAS_DIR="/volume1/docker/peiyin"
IMAGE="peiyin:latest"
DOCKER="/usr/local/bin/docker"          # NAS 上 docker 的绝对路径（sudoers 白名单按此匹配）
PROXY="http://127.0.0.1:PROXY_PORT"           # Mac 本地构建拉基础镜像用的代理
COMPOSE_FILES="-f docker-compose.yml -f docker-compose.deploy.yml"

cd "$(dirname "$0")"

ssh_nas() { ssh -p "$NAS_PORT" "$NAS_HOST" "$@"; }

# ---- 1. 本地构建 ----
if [[ "${1:-}" != "--no-build" ]]; then
  echo "▶ [1/4] Mac 本地构建镜像 $IMAGE ..."
  https_proxy="$PROXY" http_proxy="$PROXY" all_proxy="socks5://127.0.0.1:7897" \
    docker build -t "$IMAGE" -f Dockerfile .
else
  echo "▶ [1/4] 跳过构建，使用现有本地镜像 $IMAGE"
fi

# ---- 2. 传输并 load（分层缓存：只传 NAS 上没有的层）----
echo "▶ [2/4] save + 传输 + load 到 NAS（只传变化的层，首次较慢）..."
docker save "$IMAGE" | gzip | ssh_nas "gunzip | sudo $DOCKER load"

# ---- 3. 同步部署所需的 compose 文件 ----
echo "▶ [3/4] 同步 compose 文件 ..."
tar czf - docker-compose.yml docker-compose.deploy.yml .env.example \
  | ssh_nas "cd '$NAS_DIR' && tar xzf -"

# ---- 4. 起容器（不带 --build，直接用刚 load 的镜像）----
echo "▶ [4/4] 在 NAS 上重启容器 ..."
ssh_nas "cd '$NAS_DIR' && sudo $DOCKER compose $COMPOSE_FILES up -d"

# ---- 健康检查 ----
echo "▶ 等待健康检查 ..."
for i in $(seq 1 10); do
  sleep 3
  status=$(ssh_nas "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8756/api/auth-status" || echo 000)
  if [[ "$status" == "200" ]]; then
    echo "✅ 部署完成，服务已就绪。"
    ssh_nas "sudo $DOCKER ps --filter name=peiyin --format '   {{.Status}}'"
    exit 0
  fi
done
echo "⚠ 健康检查未在预期时间内通过，去 NAS 看日志：sudo $DOCKER compose logs --tail 50"
exit 1
