#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/deploy/fnos/wy-music-cloud"
CTX="$APP/app/docker/context"
DIST="$ROOT/dist"

TMP_ROOT="$ROOT/.tmp/fnpack"
rm -rf "$TMP_ROOT" "$CTX"
mkdir -p "$TMP_ROOT" "$CTX/deploy/docker" "$DIST"
chmod 777 "$TMP_ROOT"
if command -v cygpath >/dev/null 2>&1; then
  TMP_FOR_FNPACK="$(cygpath -w "$TMP_ROOT")"
else
  TMP_FOR_FNPACK="$TMP_ROOT"
fi
export TMPDIR="$TMP_FOR_FNPACK" TMP="$TMP_FOR_FNPACK" TEMP="$TMP_FOR_FNPACK"

cp "$ROOT/pyproject.toml" "$ROOT/README.md" "$ROOT/LICENSE" "$CTX/"
cp -R "$ROOT/src" "$CTX/src"
cp "$ROOT/deploy/docker/Dockerfile" "$CTX/Dockerfile"
cp "$ROOT/deploy/docker/entrypoint.sh" "$CTX/deploy/docker/entrypoint.sh"
find "$CTX" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true

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
for fpk in "$APP"/*.fpk "$ROOT"/*.fpk; do
  mv "$fpk" "$DIST/"
done

FPK_COUNT="$(find "$DIST" -maxdepth 1 -name '*.fpk' | wc -l)"
if [ "$FPK_COUNT" -eq 0 ]; then
  echo "fnpack 没有生成 fpk，请检查上方错误。" >&2
  exit 1
fi

PYTHON_BIN=python
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN=python3
fi
FPK_FILE="$(find "$DIST" -maxdepth 1 -name '*.fpk' -print -quit)"
"$PYTHON_BIN" "$ROOT/scripts/fix-fpk-modes.py" "$FPK_FILE"

echo "构建完成："
find "$DIST" -maxdepth 1 -name '*.fpk' -exec ls -lh {} +