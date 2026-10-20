#!/usr/bin/env bash
# ============================================================================
# 拉取 token（macOS / Linux 版）—— 等价于 Windows 的 pull_token.bat
#
# 用途：手机 USB 连着、云运动处于登录状态、开了 USB 调试，
#      本脚本会重启云运动 → 自动点开「我的」页 → 从 logcat 抓最新 token
#      → 写进 yunrun/config.ini → 再用它调一次只读接口验证。
#      全程不发登录请求，手机上的会话不受影响。
#
# 前置：
#   adb 在 PATH（brew install --cask android-platform-tools）
#   手机弹窗「允许 USB 调试」要点同意
#
# 用法：
#   双击本文件                      自动跑
#   ./pull_token.command -s RSA...   多台手机时指定序列号
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PY=""
for cand in python3 python3.13 python3.12 python3.11 python3.10 python; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "[FAIL] 找不到 python3。安装：brew install python@3.12"
  echo
  printf '按回车关闭窗口…'; read -r _ || true
  exit 1
fi

# adb 预检：给出比「脚本跑到一半才报错」更有用的提示
if ! command -v adb >/dev/null 2>&1; then
  for cand in /opt/homebrew/bin/adb /usr/local/bin/adb "$HOME/Library/Android/sdk/platform-tools/adb"; do
    if [ -x "$cand" ]; then PATH="$(dirname "$cand"):$PATH"; break; fi
  done
fi
if ! command -v adb >/dev/null 2>&1; then
  echo "[FAIL] 找不到 adb —— 管理前台第 ② 步需要它"
  echo "  安装：brew install --cask android-platform-tools"
  echo "  Apple Silicon 提示：Google 只发 x86_64 版，靠 Rosetta 跑，首次需在系统设置里允许"
  echo
  printf '按回车关闭窗口…'; read -r _ || true
  exit 1
fi
echo "[OK] adb：$(command -v adb)"
echo "[OK] python：$PY"
adb devices | sed -n '1,4p'
echo

# 用项目自带的虚拟环境（若 run.command 已经建过），否则用系统解释器
if [ -x "$HERE/../.venv-mac/bin/python" ]; then
  PY="$HERE/../.venv-mac/bin/python"
elif [ -x "$HERE/.venv-mac/bin/python" ]; then
  PY="$HERE/.venv-mac/bin/python"
fi

# 这个脚本最后会打 ✅，个别终端会因此抛 UnicodeEncodeError，这里先统一成 UTF-8
export PYTHONIOENCODING=utf-8

# 注意：set -e 下不能直接 `cmd` 再取 $?，否则失败会立刻退出，诊断信息打不出来
rc=0
"$PY" refresh_token_adb.py "$@" || rc=$?
echo
if [ "$rc" -ne 0 ]; then
  echo "[FAIL] 退出码 $rc"
else
  echo "[OK] 完成"
fi
# 双击运行时窗口不要一闪就没
if [ "$#" -eq 0 ] && [ -t 0 ]; then
  printf '按回车关闭窗口…'; read -r _ || true
fi
exit "$rc"
