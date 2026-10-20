# -*- coding: utf-8 -*-
"""非交互登录一次（管理前台「账号密码登录」按钮的子进程入口）。

为什么不直接调 Login.main：它是交互入口，缺字段时会 input()——在 Web 拉起的
子进程里 stdin 是 DEVNULL，input() 会直接 EOFError。这里先做与 Login.py
兜底一致的预填（设备名默认 Xiaomi、系统版本默认 14），必填项（学号/密码/
学校名）缺失时给出明确指引后退出，然后调用同一个 Login.main，逻辑零改动。

    python scripts/login_once.py --config <config.ini>
"""
from __future__ import annotations

import argparse
import configparser
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "yunrun"
for extra in (str(PROJECT / "runner"), str(PROJECT / "runner" / "tools")):
    if extra not in sys.path:
        sys.path.insert(0, extra)


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def main() -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="用 config.ini 的账号密码登录一次并写回 token")
    parser.add_argument("--config", default=str(PROJECT / "config.ini"))
    args = parser.parse_args()

    conf_path = Path(args.config)
    if not conf_path.is_file():
        print(f"[-] 配置文件不存在: {conf_path}")
        return 2
    conf = configparser.ConfigParser(interpolation=None)
    conf.read(conf_path, encoding="utf-8-sig")

    missing = []
    if not (conf.get("Login", "username", fallback="").strip()):
        missing.append("[Login] username（学号）")
    if not (conf.get("Login", "password", fallback="").strip()):
        missing.append("[Login] password（密码）")
    if not (conf.get("Yun", "school_name", fallback="").strip()):
        missing.append("[Yun] school_name（学校名称）")
    if missing:
        print("[-] 配置不完整，请先在管理前台「第 1 步 · 学校与账号」保存：")
        for item in missing:
            print("    · " + item)
        return 2

    # Login.main 的交互兜底改为预填（与其内部默认一致），避免子进程 input() 挂掉
    changed = False
    if not conf.get("User", "device_name", fallback="").strip():
        conf.set("User", "device_name", "Xiaomi")
        changed = True
    if not conf.get("User", "sys_edition", fallback="").strip():
        conf.set("User", "sys_edition", "14")
        changed = True
    if not conf.get("User", "sys_version", fallback="").strip():
        conf.set("User", "sys_version", "14")
        changed = True
    if changed:
        with open(conf_path, "w", encoding="utf-8") as handle:
            conf.write(handle)

    from Login import Login  # runner/tools/Login.py

    result = Login.main(str(conf_path))
    if result is None:
        print("[-] 登录失败（勿盲目重试：留意账号锁定）。")
        return 3
    print("[+] 登录完成，token/设备信息已写回 config.ini。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
