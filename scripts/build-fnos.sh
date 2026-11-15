#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/deploy/fnos/ncm-cloud-sync"
VERSION="$(sed -n 's/^version=//p' "$APP/manifest" | head -n 1)"
if [ -z "$VERSION" ]; then
  echo "无法从 manifest 读取版本号。" >&2
  exit 1
fi
SERVER="$APP/app/server"
DIST="$ROOT/dist"
WHEEL_ROOT="$ROOT/.wheels/native"
TMP_ROOT="$ROOT/.tmp/fnpack-native"

if command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  PYTHON_BIN="python3"
fi

PIP=("$PYTHON_BIN" -m pip)
if [ -f "$ROOT/.pipfix/run_pip.py" ]; then
  PIP=("$PYTHON_BIN" "$ROOT/.pipfix/run_pip.py")
fi

rm -rf "$TMP_ROOT" "$SERVER"
rm -f "$DIST"/ncm-cloud-sync*.fpk "$ROOT"/*.fpk "$APP"/*.fpk
mkdir -p "$TMP_ROOT" "$DIST" "$SERVER/vendor" "$APP/app/ui/images"
chmod 777 "$TMP_ROOT"
if command -v cygpath >/dev/null 2>&1; then
  TMP_FOR_FNPACK="$(cygpath -w "$TMP_ROOT")"
else
  TMP_FOR_FNPACK="$TMP_ROOT"
fi
export TMPDIR="$TMP_FOR_FNPACK" TMP="$TMP_FOR_FNPACK" TEMP="$TMP_FOR_FNPACK"

# 1. 应用源码
cp -R "$ROOT/src/ncm_cloud_sync" "$SERVER/ncm_cloud_sync"
find "$SERVER" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$SERVER" -type f -name "*.pyc" -delete 2>/dev/null || true

# 2. 两个架构的离线依赖。Windows 上 python -m zipfile 也能解压 manylinux wheel。
for ARCH in x86_64 aarch64; do
  WHEEL_DIR="$WHEEL_ROOT/$ARCH"
  VENDOR="$SERVER/vendor/$ARCH"
  mkdir -p "$WHEEL_DIR" "$VENDOR"
  if ! ls "$WHEEL_DIR"/*.whl >/dev/null 2>&1; then
    echo "下载 $ARCH wheel..."
    "${PIP[@]}" download --dest "$WHEEL_DIR" --only-binary=:all: \
      --platform "manylinux2014_$ARCH" --python-version 312 \
      --implementation cp --abi cp312 --no-cache-dir --timeout 30 --retries 3 \
      -r "$ROOT/requirements-native.txt"
  fi
  for WHL in "$WHEEL_DIR"/*.whl; do
    "$PYTHON_BIN" -m zipfile -e "$WHL" "$VENDOR"
  done
  find "$VENDOR" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
  for MODULE in httpx waitress Crypto mutagen yaml qrcode; do
    if [ ! -e "$VENDOR/$MODULE" ]; then
      echo "缺少模块 $MODULE（$ARCH），请检查 wheel 下载。" >&2
      exit 1
    fi
  done
done

# 3. 图标（仓库已提交生成结果；缺失时再用 Pillow 生成）
if [ ! -f "$APP/ICON.PNG" ] || [ ! -f "$APP/ICON_256.PNG" ] || \
   [ ! -f "$APP/app/ui/images/icon_64.png" ] || [ ! -f "$APP/app/ui/images/icon_256.png" ] || \
   [ ! -f "$ROOT/src/ncm_cloud_sync/webui/favicon.png" ]; then
  "$PYTHON_BIN" "$ROOT/scripts/make_icons.py"
fi

# 4. 打包
if [ -x "$ROOT/.tools/fnpack.exe" ]; then
  FN="$ROOT/.tools/fnpack.exe"
elif [ -x "$ROOT/.tools/fnpack" ]; then
  FN="$ROOT/.tools/fnpack"
elif command -v fnpack >/dev/null 2>&1; then
  FN="$(command -v fnpack)"
else
  echo "找不到 fnpack。请把 fnpack 放到 .tools/，或加入 PATH。" >&2
  exit 1
fi

"$FN" build --directory "$APP"

shopt -s nullglob
for FPK in "$APP"/*.fpk "$ROOT"/*.fpk; do
  mv "$FPK" "$DIST/"
done
shopt -u nullglob

FPK_FILE="$DIST/ncm-cloud-sync-$VERSION.fpk"
if [ -f "$DIST/ncm-cloud-sync.fpk" ] && [ "$DIST/ncm-cloud-sync.fpk" != "$FPK_FILE" ]; then
  mv "$DIST/ncm-cloud-sync.fpk" "$FPK_FILE"
fi
if [ ! -f "$FPK_FILE" ]; then
  echo "fnpack 没有生成 $FPK_FILE，请检查上方错误。" >&2
  exit 1
fi

"$PYTHON_BIN" "$ROOT/scripts/fix-fpk-modes.py" "$FPK_FILE"

echo "构建完成：$FPK_FILE（版本 $VERSION）"
ls -lh "$FPK_FILE"