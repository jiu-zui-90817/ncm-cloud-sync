#!/bin/sh
set -eu

# 允许用 PUID/PGID 在运行时提示（镜像内已按构建参数创建用户）。
if [ "$(id -u)" = "0" ]; then
  echo "wy-music-cloud: 请以非 root 用户运行，或使用 docker compose 的 user 配置" >&2
  exit 1
fi

# 默认补上 -c，方便直接 docker run。
if [ "$#" -eq 0 ]; then
  set -- daemon
fi

case "$1" in
  -*) exec wy-music-cloud "$@" ;;
  login|logout|check|scan|sync|daemon|status|config|reset-failed|--help|-h|--version)
    exec wy-music-cloud "$@" ;;
  *) exec wy-music-cloud "$@" ;;
esac