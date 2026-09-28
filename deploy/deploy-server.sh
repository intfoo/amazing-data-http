#!/bin/bash
# Restricted deployment script for the 'deployer' user (rootless podman).
# Two ways it is invoked:
#   - forced command (authorized_keys: command="/usr/local/bin/deploy.sh",restrict)
#     -> repository name comes in SSH_ORIGINAL_COMMAND
#   - direct call: bash /usr/local/bin/deploy.sh <repo>
# The GitHub Action's `script:` shows the latter for readability; the forced
# command guarantees only THIS script ever runs for the deployer key.

set -euo pipefail

# Defence: never run as root.
if [ "$(id -u)" = "0" ]; then
  echo "ERROR: deploy.sh must not run as root" >&2
  exit 1
fi

# Minimal, predictable PATH (non-login SSH shells may have a tiny PATH).
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Rootless podman runtime directory.
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
mkdir -p "$XDG_RUNTIME_DIR"

# Repository name: forced-command passes it via SSH_ORIGINAL_COMMAND; direct
# call passes it as $1. Accept bare, quoted, or `bash -c "repo"` wrappings.
RAW="${SSH_ORIGINAL_COMMAND:-${1:-}}"
REPO=$(printf '%s' "$RAW" | tr -s ' \t\r\n' '\n' | sed "s/^[\"']//; s/[\"']$//" | grep -E '^[A-Za-z0-9._-]+$' | grep -v '^-' | tail -1)
if [ -z "$REPO" ]; then
  echo "ERROR: no valid repository name in input: $RAW" >&2
  exit 1
fi

TARGET="/app/$REPO"
if [ ! -d "$TARGET" ]; then
  echo "ERROR: repository directory not found: $TARGET" >&2
  exit 1
fi

COMPOSE=""
for f in docker-compose.yml compose.yml docker-compose.yaml; do
  if [ -f "$TARGET/$f" ]; then COMPOSE="$TARGET/$f"; break; fi
done
if [ -z "$COMPOSE" ]; then
  echo "ERROR: no compose file found in $TARGET" >&2
  exit 1
fi

cd "$TARGET"

echo "::group::📦 2. 拉取最新镜像"
# 先拉取再重启: 拉取期间旧容器继续对外服务,
# 停机窗口只有下一步容器重建的几秒 (原顺序先 down 再 pull, 停机=整个拉取时长)

# 后台网速监控：速率长时间≈0 说明镜像源被限速/卡住，CI 日志一眼定位
# （2026-09-28：nju 镜像源限速至 39KB/s，靠此特征确认后切 chenby 源 3.8MB/s）。
monitor_speed() {
  local iface interval=5 prev cur
  iface=$(ip route show default | awk '{print $5; exit}')
  prev=$(awk -v i="$iface" '{sub(/:/," ")} $1==i{print $2}' /proc/net/dev)
  while sleep $interval; do
    cur=$(awk -v i="$iface" '{sub(/:/," ")} $1==i{print $2}' /proc/net/dev)
    echo "⬇️  $(date +%T)  $(( (cur - prev) / interval / 1024 )) KB/s"
    prev=$cur
  done
}
monitor_speed &
MON=$!
trap 'kill $MON 2>/dev/null' EXIT

# 单次尝试限时 300s + 重试（已下载 blob 保留，重试通常更快）；
# 第 2 次失败起追加镜像源 fallback（限速时重试同一源无意义）：
# ghcr.nju.edu.cn → ghcr.chenby.cn，拉完 tag 回原名，compose 无需改动。
pull_with_fallback() {
  local n img alt
  for n in 1 2 3; do
    if timeout 300 podman-compose pull 2>&1 | while IFS= read -r l; do echo "$(date +%T) $l"; done; then
      return 0
    fi
    echo "⚠️ 第 $n 次拉取失败或超时（300s）"
    if [ "$n" -ge 2 ]; then
      for img in $(awk '/^[[:space:]]*image:/{print $2}' "$COMPOSE" | grep '^ghcr.nju.edu.cn/' || true); do
        alt="${img/ghcr.nju.edu.cn/ghcr.chenby.cn}"
        echo "🔄 尝试备用镜像源: $alt"
        if timeout 300 podman pull "$alt" 2>&1 | while IFS= read -r l; do echo "$(date +%T) $l"; done; then
          podman tag "$alt" "$img"
          echo "✅ 备用源拉取成功，已 tag 回 $img"
        fi
      done
    fi
    sleep $((n * 5))
  done
  echo "❌ 拉取重试 3 次仍失败"
  return 1
}
pull_with_fallback
kill $MON 2>/dev/null || true
trap - EXIT
echo "::endgroup::"

echo "::group::🚀 3. 重启容器 (镜像有更新时自动重建)"
# 先拉取再重启: 拉取期间旧容器继续对外服务, 停机窗口只有 down+重建的几秒。
# up 前必须 down: rootless podman-compose 不会自动替换同名运行中容器
# (报 "container name is already in use ... use --replace"), down 后 up 即重建。
podman-compose down --remove-orphans || true
# compose 含 build: 段但部署时走预构建镜像，必须 --no-build
podman-compose up -d --no-build --remove-orphans
echo "::endgroup::"

echo "::group::🧹 4. 清理悬空镜像 (释放磁盘空间)"
podman image prune -f
echo "::endgroup::"

echo "::group::📊 5. 验证部署状态"
sleep 10
echo ""
echo "=== Compose 服务状态 ==="
podman-compose ps
echo ""
echo "=== 最近 20 行日志 ==="
podman-compose logs --tail=20
echo "::endgroup::"

echo "✅ 部署脚本执行完毕！"
