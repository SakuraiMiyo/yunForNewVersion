#!/usr/bin/env bash
# ============================================================================
# 云运动 · macOS 源码直跑入口
#
# 这是「现在就能在 Mac 上跑起来」的那条路：找一个能用的 python3，在项目内建一个
# 本地虚拟环境（.venv-mac/，不污染系统的 site-packages），装好 yunrun/requirements.txt，
# 然后直接跑 launcher.py —— 它会起本地服务并唤起系统浏览器打开管理前台。
#
# 用法：
#   双击本文件                     第一次会装依赖（需要联网），之后秒开
#   ./run.command --check      只体检：python 版本、依赖是否齐、adb、浏览器
#   ./run.command --port 8500  指定端口（被占用会自动换一个空闲的）
#
# 前置：
#   · python3（3.10+）。没有的话：brew install python@3.12 或 python.org 安装包
#   · 首次运行需要联网装依赖；之后可离线运行
#   · adb（管理前台第 ② 步要用）：brew install --cask android-platform-tools
#   · Edge / Chrome / Chromium（轨迹卡片截图要用）之一
#
# 若被 Gatekeeper 拦住：xattr -dr com.apple.quarantine run.command
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV="$HERE/.venv-mac"
MARKER="$VENV/.deps-ok"
CHECK_ONLY=0
PASS_ARGS=()

for arg in "$@"; do
  case "$arg" in
    --check|-c) CHECK_ONLY=1 ;;
    --help|-h)  sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          PASS_ARGS+=("$arg") ;;
  esac
done

if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_BAD=$'\033[31m'
  C_INFO=$'\033[90m'; C_HEAD=$'\033[36m'; C_OFF=$'\033[0m'
else
  C_OK=; C_WARN=; C_BAD=; C_INFO=; C_HEAD=; C_OFF=
fi
ok()   { printf '%s  [OK]   %s%s\n' "$C_OK" "$1" "$C_OFF"; }
warn() { printf '%s  [WARN] %s%s\n' "$C_WARN" "$1" "$C_OFF"; }
bad()  { printf '%s  [FAIL] %s%s\n' "$C_BAD" "$1" "$C_OFF"; }
info() { printf '%s  %s%s\n' "$C_INFO" "$1" "$C_OFF"; }
head2() { printf '\n%s%s%s\n' "$C_HEAD" "  $1" "$C_OFF"; }

head2 '云运动 · macOS 源码直跑'
info "项目目录：$HERE"

# ── 1. 找一个 python3（3.10+），优先挑「已经装了运行期依赖」的那个 ──
# 同一台机器上 python3 / python 可能指向不同安装（macOS 上装了 pyenv/conda 时尤其常见），
# 所以先探依赖、再退回「版本够新就交给 venv 去装」。
deps_ok() {  # 用指定解释器检查运行期依赖
  "$1" - <<'PYCODE' >/dev/null 2>&1
import importlib
for name in ("requests", "gmssl", "numpy", "onnxruntime", "PIL", "Crypto", "tqdm"):
    importlib.import_module(name)
PYCODE
}

PY=""
PY_ANY=""
SEEN=""
for cand in python3 python3.13 python3.12 python3.11 python3.10 python; do
  command -v "$cand" >/dev/null 2>&1 || continue
  case " $SEEN " in *" $cand "*) continue ;; esac
  SEEN="$SEEN $cand"
  "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null || continue
  [ -z "$PY_ANY" ] && PY_ANY="$cand"
  if deps_ok "$cand"; then PY="$cand"; break; fi
done
[ -z "$PY" ] && PY="$PY_ANY"
if [ -z "$PY" ]; then
  bad '找不到 python3（需要 3.10 以上）'
  info '  安装：brew install python@3.12   或   https://www.python.org/downloads/'
  exit 1
fi
PY_VER="$("$PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
PY_ARCH="$("$PY" -c 'import platform; print(platform.machine())')"
ok "python $PY_VER（$PY_ARCH，$PY）"

HOST_ARCH="$(uname -m)"
if [ "$HOST_ARCH" = "arm64" ] && [ "$PY_ARCH" != "arm64" ]; then
  warn '这台是 Apple Silicon，但 Python 是 x86_64（Rosetta 终端）'
  warn '  想让依赖都装成原生 arm64：换一个原生终端再跑本脚本'
fi

# ── 2. 依赖检查 / 安装 ──
if [ "$CHECK_ONLY" = 1 ]; then
  head2 '体检模式（不安装、不启动）'
  if [ -x "$VENV/bin/python" ] && deps_ok "$VENV/bin/python"; then
    ok '项目虚拟环境 .venv-mac 已就绪，依赖齐全'
  elif deps_ok "$PY"; then
    ok "系统解释器依赖齐全（$PY）"
  else
    warn '依赖未齐：双击本文件会自动在 .venv-mac 里安装（需要联网）'
    info '  等价命令：'"$PY"' -m venv .venv-mac && .venv-mac/bin/python -m pip install -r yunrun/requirements.txt'
  fi
  if command -v adb >/dev/null 2>&1; then
    ok "adb：$(command -v adb)"
  else
    warn '没找到 adb：管理前台第 ② 步「ADB 拉取令牌」不可用（brew install --cask android-platform-tools）'
  fi
  BROWSER=""
  for b in "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
           "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
           "/Applications/Chromium.app/Contents/MacOS/Chromium"; do
    [ -n "$BROWSER" ] && break
    [ -f "$b" ] && BROWSER="$b"
  done
  if [ -n "$BROWSER" ]; then ok "截图浏览器：$(basename "$BROWSER")"; else warn '没找到 Edge/Chrome/Chromium：轨迹卡片出图会失败'; fi
  [ "$(uname -s)" = "Darwin" ] || warn "当前不是 macOS（$(uname -s)）——体检逻辑可跑，真运行请到 Mac 上"
  exit 0
fi

RUN_PY="$PY"
if [ ! -x "$VENV/bin/python" ]; then
  head2 '创建项目虚拟环境 .venv-mac'
  info "> $PY -m venv .venv-mac"
  "$PY" -m venv "$VENV" || { bad '创建虚拟环境失败（某些发行版要先装 python3-venv）'; exit 1; }
  ok '虚拟环境已创建'
fi
RUN_PY="$VENV/bin/python"

if [ -f "$MARKER" ] && deps_ok "$RUN_PY"; then
  ok '依赖已就绪（跳过安装）'
else
  head2 '安装依赖（首次需要联网，约 1-3 分钟）'
  info '> pip install -r yunrun/requirements.txt'
  if "$RUN_PY" -m pip install --disable-pip-version-check -q -r "$HERE/yunrun/requirements.txt"; then
    deps_ok "$RUN_PY" && : > "$MARKER" && ok '依赖安装完成'
  else
    bad '依赖安装失败——多半是网络问题（PyPI 不通）'
    info '  重试：'"$RUN_PY"' -m pip install -r yunrun/requirements.txt'
    info '  换镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple'
    exit 1
  fi
  if ! deps_ok "$RUN_PY"; then
    warn '依赖装完了但仍缺某个模块，详见上面的输出'
  fi
fi

# ── 3. 起服务（launcher.py 会自动挑空闲端口并唤起浏览器） ──
head2 '启动（关掉这个终端窗口即停止）'
info '  提示：端口被占用时 launcher 会自动换一个；URL 见下面输出'
exec "$RUN_PY" "$HERE/launcher.py" "${PASS_ARGS[@]+"${PASS_ARGS[@]}"}"
