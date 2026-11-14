#!/bin/bash
# 生命周期脚本共用函数。

APP_DOCKER="${TRIM_APPDEST}/docker"
ENV_FILE="${APP_DOCKER}/.env"

fail() {
  echo "wy-music-cloud: $*" > "${TRIM_TEMP_LOGFILE:-/dev/stderr}"
  exit 1
}

validate_music_dir() {
  local dir="$1"
  case "$dir" in
    /*) ;;
    *) fail "音乐目录必须是绝对路径: $dir" ;;
  esac
  case "$dir" in
    *"'"*|*$'\n'*) fail "音乐目录不能包含单引号或换行: $dir" ;;
  esac
  if [ ! -d "$dir" ]; then
    fail "音乐目录不存在或不可访问: $dir"
  fi
}

validate_int() {
  local name="$1"
  local value="$2"
  case "$value" in
    ''|*[!0-9]*) fail "$name 必须是整数: $value" ;;
  esac
}

validate_uid() {
  local name="$1"
  local value="$2"
  validate_int "$name" "$value"
  if [ "$value" -lt 1 ] || [ "$value" -gt 65535 ]; then
    fail "$name 必须在 1-65535 之间: $value"
  fi
}

validate_timezone() {
  local value="$1"
  case "$value" in
    ''|*[!A-Za-z0-9_+/-]*) fail "时区格式不合法: $value" ;;
  esac
}

validate_dry_run() {
  case "$1" in
    true|false) ;;
    *) fail "试运行模式只能是 true 或 false: $1" ;;
  esac
}

read_env() {
  local key="$1"
  if [ ! -f "$ENV_FILE" ]; then
    return 1
  fi
  sed -n "s/^$key='\(.*\)'$/\1/p" "$ENV_FILE" | tail -n 1
}

write_env_file() {
  local music_dir="$1"
  local interval="$2"
  local timezone="$3"
  local dry_run="$4"
  local puid="$5"
  local pgid="$6"
  local version="$7"

  validate_music_dir "$music_dir"
  validate_int "扫描间隔" "$interval"
  if [ "$interval" -lt 60 ]; then
    fail "扫描间隔不能小于 60 秒: $interval"
  fi
  validate_timezone "$timezone"
  validate_dry_run "$dry_run"
  validate_uid "运行 UID" "$puid"
  validate_uid "运行 GID" "$pgid"

  mkdir -p "$APP_DOCKER"
  umask 077
  cat > "$ENV_FILE" <<EOF
WY_MUSIC_DIR='$music_dir'
WY_MUSIC_INTERVAL='$interval'
WY_MUSIC_DRY_RUN='$dry_run'
TZ='$timezone'
PUID='$puid'
PGID='$pgid'
WY_VERSION='$version'
EOF
  chmod 600 "$ENV_FILE"
}

write_login_hint() {
  mkdir -p "${TRIM_PKGVAR}"
  chmod 700 "${TRIM_PKGVAR}" 2>/dev/null || true
  umask 077
  cat > "${TRIM_PKGVAR}/login.txt" <<EOF
首次使用需要在 NAS 上执行一次扫码（或 Cookie）登录。

cd ${TRIM_APPDEST}/docker
sudo docker compose -p wy-music-cloud run --rm wy-music-cloud login --qr-png /data/qr.png

登录态保存在 Docker 数据卷 wy-music-cloud-data 中，升级/重装应用不会丢失。
EOF
  chmod 600 "${TRIM_PKGVAR}/login.txt" 2>/dev/null || true
}
