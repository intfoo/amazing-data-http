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

echo "::group::🛑 2. 停止并清理旧服务"
podman-compose down --remove-orphans || true
echo "::endgroup::"

echo "::group::📦 3. 拉取最新镜像"
podman-compose pull
echo "::endgroup::"

echo "::group::🚀 4. 启动新服务"
# compose 含 build: 段但部署时走预构建镜像，必须 --no-build
podman-compose up -d --no-build --remove-orphans
echo "::endgroup::"

echo "::group::🧹 5. 清理悬空镜像 (释放磁盘空间)"
podman image prune -f
echo "::endgroup::"

echo "::group::📊 6. 验证部署状态"
sleep 10
echo ""
echo "=== Compose 服务状态 ==="
podman-compose ps
echo ""
echo "=== 最近 20 行日志 ==="
podman-compose logs --tail=20
echo "::endgroup::"

echo "✅ 部署脚本执行完毕！"
