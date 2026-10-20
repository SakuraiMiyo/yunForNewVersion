# -*- coding: utf-8 -*-
"""
从手机提取云运动最新登录 token

原理：云运动 App 自带 HttpLoggingInterceptor(BODY) 日志拦截器，
它发的每个请求头（token / deviceId / deviceName）都会原样打印到 logcat。
本脚本：连手机 → 重启云运动 App → 自动点开底部"我的"页触发一次业务请求
→ 从 logcat 抓最新 token → 写回 config.ini → 用服务器验证一次。
全程不发登录请求，App 用的是它自己已登录的会话。

这不等于「零风险」：本脚本只避开「重新登录」这一步带来的风控，成绩会不会被判违规
取决于服务器侧的检测（上游作者在 issue #82 里也还在查这件事），用之前先读 README
的风险说明。

用法：
    手机 USB 连电脑并开启 USB 调试（云运动保持登录状态），然后：
        python refresh_token_adb.py            # 全自动（多台设备时自动选装了云运动的那台）
        python refresh_token_adb.py -s 设备ID   # 手动指定设备
    Windows 可以直接双击同目录的 pull_token.bat；macOS / Linux 用命令行运行本脚本。

要求：adb 在 PATH（或下方 ADB_CANDIDATES 里能找到）；本机已装 Python 3.10+。
      macOS 上装 adb：brew install --cask android-platform-tools
      （Google 只发 x86_64 版 platform-tools，Apple Silicon 靠 Rosetta 跑，首次需在
       系统设置里允许；这一步与本脚本无关，是 adb 自身的事。）
"""
import argparse
import configparser
import os
import tempfile
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_INI = os.path.join(ROOT, "config.ini")
RUNNER_MAIN = os.path.join(ROOT, "runner", "main.py")


def _force_utf8_stdio():
    """Windows 控制台默认 GBK，print 遇到 ✅ 之类的字符会抛 UnicodeEncodeError。

    这个脚本最后一步的“验证通过 ✅”正好会触发它——token 其实已经写进 config.ini 了，
    却让整段以 traceback 收场，看起来像失败。这里显式把输出切到 UTF-8；
    个别不支持 reconfigure 的环境退回 replace，绝不因为打日志而中断流程。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass

PACKAGE = "com.yunzhi.tiyu"
LAUNCH_ACTIVITY = "com.yunzhi.tiyu/.module.SplashActivity"

# "我的"标签兜底坐标（按 1264x2780 分辨率估算；优先用 uiautomator
# 按文字定位，绝大多数机型走不到这个兜底）
FALLBACK_TAP_MINE = (1106, 2700)

# 常见 adb 位置（PATH 里找不到时逐个尝试）。三个平台都列上：不存在就跳过，成本为零。
# 注意 PATH 里的 `adb` 永远排第一——所以 macOS 上装了 platform-tools（Homebrew cask
# 或 Android Studio）之后，这里根本轮不到下面的兜底路径。
ADB_CANDIDATES = [
    "adb",
    # Windows
    r"C:\SoftWare\adb\adb.exe",
    r"C:\platform-tools\adb.exe",
    r"C:\Android\platform-tools\adb.exe",
    os.path.expanduser(r"~\AppData\Local\Android\Sdk\platform-tools\adb.exe"),
    # macOS（Homebrew 装在 /opt/homebrew 或 /usr/local；Android Studio 的 SDK 在 ~/Library）
    "/opt/homebrew/bin/adb",
    "/usr/local/bin/adb",
    os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
    # Linux
    "/usr/bin/adb",
    os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
]


def find_adb():
    """定位 adb 可执行文件。

    顺序：环境变量 YUNRUN_ADB（想指哪指哪，连 PATH 都不用配）→ PATH 里的 adb →
    各平台常见安装路径。**任何来源的 adb 都行**——它只是一个外部程序，
    项目只负责调用 `adb devices / logcat / shell ...`，与它是 brew 装的、
    Google 官方 zip 解压的、还是 Android Studio 自带的无关。
    """
    override = os.environ.get("YUNRUN_ADB")
    if override:
        candidate = os.path.expanduser(override)
        # 允许直接指向目录（自动补 adb / adb.exe），也允许指向文件
        if os.path.isdir(candidate):
            for name in ("adb", "adb.exe"):
                joined = os.path.join(candidate, name)
                if os.path.isfile(joined):
                    return joined
        elif os.path.isfile(candidate):
            return candidate
        print("[-] YUNRUN_ADB 指向的路径不存在：%s" % override)

    for cand in ADB_CANDIDATES:
        if cand == "adb" and shutil.which("adb"):
            return "adb"
        if cand != "adb" and os.path.isfile(cand):
            return cand
    print("[-] 找不到 adb。请安装 Android platform-tools 并加入 PATH：")
    print("    https://developer.android.com/tools/releases/platform-tools")
    print("    macOS: brew install --cask android-platform-tools")
    print("    已经有 adb 但不在 PATH？用环境变量指过去：")
    print("        export YUNRUN_ADB=/path/to/adb        # 也可以给目录")
    sys.exit(1)


def adb(adb_path, args, device=None):
    cmd = [adb_path] + (["-s", device] if device else []) + args
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)


def pick_device(adb_path):
    out = adb(adb_path, ["devices"]).stdout
    serials = [l.split("\t")[0] for l in out.splitlines()[1:] if "\tdevice" in l]
    if not serials:
        print("[-] 没有已连接的 adb 设备。请检查：")
        print("    1. 手机已 USB 连接且选择了「传输文件」")
        print("    2. 开发者选项里打开了「USB 调试」")
        print("    3. 手机上弹出的「允许 USB 调试」已确认")
        sys.exit(1)
    if len(serials) == 1:
        return serials[0]
    # 多台设备：选装了云运动的那台
    for s in serials:
        r = adb(adb_path, ["shell", "pm list packages"], s)
        if PACKAGE in (r.stdout or ""):
            print("[*] 检测到多台设备，自动选择装有云运动的：%s" % s)
            return s
    print("[!] 多台设备都没装云运动，默认用第一台：%s（可用 -s 指定）" % serials[0])
    return serials[0]


def locate_and_tap_mine(adb_path, device):
    """优先 uiautomator 按文字「我的」定位底部标签，失败则用兜底坐标。"""
    for _ in range(2):
        adb(adb_path, ["shell", "uiautomator", "dump", "/sdcard/ui.xml"], device)
        r = adb(adb_path, ["shell", "cat", "/sdcard/ui.xml"], device)
        m = re.search(r'text="我的"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                      r.stdout or "")
        if not m:  # 属性顺序可能相反（bounds 在 text 前）
            for node in re.finditer(r'<node[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*>', r.stdout or ""):
                if 'text="我的"' in node.group(0):
                    m = node
                    break
        if m:
            x = (int(m.group(1)) + int(m.group(3))) // 2
            y = (int(m.group(2)) + int(m.group(4))) // 2
            adb(adb_path, ["shell", "input", "tap", str(x), str(y)], device)
            return "uiautomator 文字定位 (%d,%d)" % (x, y)
        time.sleep(1)
    x, y = FALLBACK_TAP_MINE
    adb(adb_path, ["shell", "input", "tap", str(x), str(y)], device)
    return "兜底坐标 (%d,%d)" % (x, y)


def extract_latest(pattern, text):
    hits = re.findall(pattern, text)
    return hits[-1] if hits else None


def verify_token():
    """用 runner 的协议层实际调一次 /run/getHomeRunInfo（只读）。"""
    import importlib.util
    old_cwd = os.getcwd()
    spec = importlib.util.spec_from_file_location("yun_runner_verify", RUNNER_MAIN)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.chdir(old_cwd)
    mod.set_args(CONFIG_INI)
    raw = mod.default_post("/run/getHomeRunInfo", "")
    return raw


def main():
    _force_utf8_stdio()
    ap = argparse.ArgumentParser(description="从手机 App 日志提取云运动最新 token 并写回 config.ini")
    ap.add_argument("-s", "--serial", help="adb 设备序列号（多台手机时指定）")
    args = ap.parse_args()

    adb_path = find_adb()
    device = args.serial or pick_device(adb_path)
    print("[1/5] 设备就绪：%s" % device)

    adb(adb_path, ["logcat", "-c"], device)
    adb(adb_path, ["shell", "am", "force-stop", PACKAGE], device)
    time.sleep(1)
    adb(adb_path, ["shell", "am", "start", "-n", LAUNCH_ACTIVITY], device)
    print("[2/5] 云运动 App 已重启，等待首页加载…")
    time.sleep(15)

    how = locate_and_tap_mine(adb_path, device)
    print("[3/5] 已点开「我的」页（%s），等待业务请求… " % how)
    time.sleep(8)

    out = adb(adb_path, ["logcat", "-d"], device).stdout or ""
    token = extract_latest(r"okhttp\.OkHttpClient: token: ([0-9a-f-]{36})", out)
    device_id = extract_latest(r"okhttp\.OkHttpClient: deviceId: ([0-9a-f]{64})", out)
    device_name = extract_latest(r"okhttp\.OkHttpClient: deviceName: (\S+)", out)
    if not token:
        print("[-] 没抓到 token，常见原因：")
        print("    · App 没处于登录状态（先在手机上登录一次云运动）")
        print("    · 手机锁屏/弹窗挡住了页面（保持亮屏重试）")
        print("    · 刚抓过一次，日志被清了（直接再跑一次本脚本）")
        sys.exit(1)

    # config.ini 可能还不存在：全新 clone、或者用户直接从「② 获取登录令牌」开始、
    # 没走过「保存配置」。这时从空对象 set 会 NoSectionError——而报错点在 [4/5]，
    # 手机已经被重启、token 也抓到了，整轮白跑。所以缺文件时拿模板当底，
    # 再保证几个段存在。
    conf = configparser.ConfigParser(interpolation=None)
    if os.path.exists(CONFIG_INI):
        conf.read(CONFIG_INI, encoding="utf-8-sig")
    else:
        example = os.path.join(ROOT, "config.example.ini")
        if os.path.exists(example):
            conf.read(example, encoding="utf-8-sig")
        else:
            print("[!] 既没有 config.ini 也没有 config.example.ini，将只写入 token")
    for section in ("Yun", "User", "Run", "Login"):
        if not conf.has_section(section):
            conf.add_section(section)
    old = conf.get("User", "token", fallback="")
    conf.set("User", "token", token)
    if device_id:
        conf.set("User", "device_id", device_id)
    if device_name:
        conf.set("User", "device_name", device_name)
    # 同 admin.save_config：唯一名临时文件 + finally 清理（固定名崩溃残留时，
    # 那文件里是完整 token，而且不受任何忽略规则保护）。
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CONFIG_INI) or ".",
                                   prefix=".config-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            conf.write(f)
        os.replace(tmp, CONFIG_INI)
        tmp = None
    except OSError as exc:
        print("[-] 写入 config.ini 失败：%s" % exc)
        print("    token 已经抓到，先记在这里（下次重跑可覆盖）：%s…" % token[:6])
        sys.exit(1)
    print("[4/5] 已写入 config.ini：")
    # 管理前台把这个脚本的 stdout 原样显示在网页上，并且会留 30 分钟——
    # 所以这里只回显前后几位，完整 token 只落在 config.ini 里。
    shown = "（已写入）"
    if len(token) > 12:
        shown = "%s…%s（共 %d 位）" % (token[:6], token[-4:], len(token))
    print("      token      = %s%s" % (shown, "   (和之前相同)" if token == old else "   (已更新)"))
    if device_name:
        print("      deviceName = %s" % device_name)
    if device_id:
        print("      deviceId   = %s…" % device_id[:16])

    print("[5/5] 用新 token 调 /run/getHomeRunInfo 验证…")
    try:
        raw = verify_token()
        ok = '"code":200' in raw.replace(" ", "")
        if ok:
            print("[+] ✅ 验证通过，token 可用。跑步链路（网页控制台 / 命令行）直接使用即可。")
        else:
            print("[!] 验证返回异常：%s" % raw[:200])
    except Exception as e:  # noqa: BLE001
        print("[!] 验证环节出错（token 已写入，可稍后手动验证）：%s" % e)


if __name__ == "__main__":
    main()
