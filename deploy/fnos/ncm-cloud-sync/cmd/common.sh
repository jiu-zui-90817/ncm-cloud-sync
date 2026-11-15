#!/bin/bash
# 原生应用生命周期与启动脚本共用的函数。
set -euo pipefail

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fail() {
  printf "%s\n" "$*" >&2
  if [ -n "${TRIM_TEMP_LOGFILE:-}" ]; then
    printf "%s\n" "$*" >>"${TRIM_TEMP_LOGFILE}" 2>/dev/null || true
  fi
  exit 1
}

validate_int() {
  local value="$1" min="$2" max="$3"
  case "$value" in
    ""|*[!0-9]*) return 1 ;;
  esac
  [ "$value" -ge "$min" ] && [ "$value" -le "$max" ]
}

normalize_music_dir() {
  local dir="$1"
  # 从聊天/文件管理器粘贴时经常带换行，去掉后再校验。
  dir="$(printf '%s' "$dir" | tr -d '\r\n')"
  dir="${dir#"${dir%%[![:space:]]*}"}"
  dir="${dir%"${dir##*[![:space:]]}"}"
  printf '%s' "$dir"
}

music_dir_error() {
  local dir="$1"
  [ -n "$dir" ] || { printf '路径为空'; return 1; }
  case "$dir" in
    /*) ;;
    *) printf '必须是绝对路径，以 / 开头'; return 1 ;;
  esac
  # JSON 字符串里最容易出问题的字符，直接拒绝，避免手工拼 JSON 出错。
  case "$dir" in
    *'"'*|*\\*) printf '路径不能包含双引号或反斜杠'; return 1 ;;
  esac
  [ -e "$dir" ] || { printf '目录不存在'; return 1; }
  [ -d "$dir" ] || { printf '这不是目录'; return 1; }
  [ -r "$dir" ] || { printf '应用用户没有读取权限，请在应用中心授权该目录，或给应用用户 ncm-cloud-sync 读权限'; return 1; }
  return 0
}

validate_log_level() {
  case "$1" in
    DEBUG|INFO|WARNING|ERROR) return 0 ;;
    *) return 1 ;;
  esac
}

write_settings() {
  local path="$1" music="$2" interval="$3" dry_run="$4" log_level="$5"
  local bool="false"
  [ "$dry_run" = "true" ] && bool="true"
  umask 077
  printf '{\n  "version": 1,\n  "music_dirs": ["%s"],\n  "interval": %s,\n  "dry_run": %s,\n  "log_level": "%s"\n}\n' \
    "$music" "$interval" "$bool" "$log_level" >"$path"
  chmod 600 "$path"
}

write_login_hint() {
  local vardir="$1" appdir="$2"
  umask 077
  printf "%s\n" \
    "NCM 云盘同步已安装。" \
    "" \
    "打开飞牛 NAS 桌面上的「NCM 云盘同步」图标即可使用。" \
    "首次进入后点击「生成登录二维码」，用网易云音乐 App 扫码登录。" \
    "" \
    "数据目录：$vardir/data" \
    "设置文件：$vardir/settings.json" \
    "日志文件：$vardir/app.log" \
    "程序目录：$appdir" \
    >"$vardir/login.txt"
  chmod 600 "$vardir/login.txt"
}

apply_wizard_settings() {
  local appdir="$1" vardir="$2"
  local music interval dry_run log_level reason
  music="$(normalize_music_dir "${wizard_music_dir:-/vol1/1000/Music}")"
  interval="${wizard_interval:-3600}"
  dry_run="${wizard_dry_run:-false}"
  log_level="${wizard_log_level:-INFO}"

  if ! reason="$(music_dir_error "$music")"; then
    if [ -n "${TRIM_DATA_ACCESSIBLE_PATHS:-}" ]; then
      reason="$reason。应用当前已授权的目录：${TRIM_DATA_ACCESSIBLE_PATHS//:/、}"
    fi
    fail "音乐目录不可用：$music（$reason）"
  fi
  validate_int "$interval" 60 86400 || fail "同步间隔必须是 60-86400 秒之间的整数"
  case "$dry_run" in true|false) ;; *) fail "演练模式只能是 true 或 false" ;; esac
  validate_log_level "$log_level" || fail "日志级别只能是 DEBUG/INFO/WARNING/ERROR"

  mkdir -p "$vardir/data"
  chmod 700 "$vardir/data" 2>/dev/null || true
  local python
  if python="$(find_python "$appdir" 2>/dev/null)" && [ -n "$python" ]; then
    "$python" "$COMMON_DIR/settings_merge.py" "$vardir/settings.json" "$music" "$interval" "$dry_run" "$log_level"
  else
    write_settings "$vardir/settings.json" "$music" "$interval" "$dry_run" "$log_level"
  fi
  write_login_hint "$vardir" "$appdir"
}

find_arch() {
  case "$(uname -m)" in
    x86_64|amd64) printf "%s" "x86_64" ;;
    aarch64|arm64) printf "%s" "aarch64" ;;
    *) return 1 ;;
  esac
}

find_python() {
  local appdir="$1" candidate=""
  if [ -n "${NCM_CLOUD_SYNC_PYTHON:-}" ] && [ -x "${NCM_CLOUD_SYNC_PYTHON}" ]; then
    printf "%s" "${NCM_CLOUD_SYNC_PYTHON}"
    return 0
  fi
  # fnOS 的 python312 依赖通常装在 /var/apps/python312/target。
  # 同时兼容 TRIM_APPDEST 是 /var/apps/<app>/target 或 /var/apps/<app> 两种情况。
  local base
  for base in "/var/apps" "$appdir/../.." "$appdir/.."; do
    candidate="$(cd "$base" 2>/dev/null && pwd)/python312/target/bin/python3"
    if [ -x "$candidate" ]; then
      printf "%s" "$candidate"
      return 0
    fi
  done
  if candidate="$(command -v python3 2>/dev/null)" && \
     "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1; then
    printf "%s" "$candidate"
    return 0
  fi
  return 1
}

is_running() {
  local pidfile="$1" socket="$2" pid=""
  [ -f "$pidfile" ] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  case "$pid" in ""|*[!0-9]*) return 1 ;; esac
  kill -0 "$pid" 2>/dev/null || return 1
  [ -S "$socket" ]
}