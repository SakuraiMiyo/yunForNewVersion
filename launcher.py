# -*- coding: utf-8 -*-
"""统一入口：``python launcher.py`` 起本地服务并唤起浏览器打开管理前台。

本仓库按源码运行，不附带打包流程。文件里的 ``sys.frozen`` 分支是给「自行把本文件
冻结成可执行文件」留的：那时项目本体仍以源码形态放在 exe 旁，靠 ``base_dir()``
定位，并由 ``--worker`` 自委托分派独立执行进程（见下方各函数说明）。
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path


def base_dir() -> Path:
    """项目根：放着 ``yunrun/`` 与 ``scripts/`` 的那一层。

    源码运行时就是本文件所在目录。若被冻结成可执行文件，位置取决于打包形态，
    不能写死一层：

    * Windows / macOS / Linux 的 onedir：``<dist>/<name>/<name>[.exe]``，
      同级目录就是项目根；
    * macOS 的 ``.app``：可执行文件在 ``Contents/MacOS/``，资源可能在
      ``Contents/Resources`` 或再上一层，所以逐层试探；

    都不匹配时退回可执行文件所在目录。
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        for candidate in (exe_dir, exe_dir.parent / "Resources", exe_dir.parent.parent):
            if (candidate / "yunrun" / "gps_track_lab").is_dir():
                return candidate
        return exe_dir
    return Path(__file__).resolve().parent


def setup_paths(base: Path) -> None:
    project = base / "yunrun"
    for rel in ("", "gps_track_lab/src", "runner", "runner/tools", "tools"):
        path = str(project / rel) if rel else str(project)
        if path not in sys.path:
            sys.path.insert(0, path)
    scripts = str(base / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)


def redirect_streams(base: Path) -> None:
    """窗口模式（--noconsole）下 sys.stdout/stderr 是 None，任何 print 都会炸。

    冻结后统一把两个流接到 logs/backend.log；源码运行时保持原样。
    """
    if not getattr(sys, "frozen", False):
        return
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir = base / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handle = open(log_dir / "backend.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = sys.stderr = handle


def script_target(argv: list[str]) -> tuple[Path, list[str]] | None:
    """识别「本 exe 被当成 python 解释器」的调用。

    冻结后 ``sys.executable`` 就是这个 exe，上游代码里形如
    ``[sys.executable, "-X", "utf8", "<script>.py", ...]`` 的调用会打到这里：
    取第一个存在的 .py 当脚本，其后的参数原样交给它，``-X utf8`` 这类解释器
    开关在冻结环境里没有意义，直接丢掉。
    """
    for index, item in enumerate(argv):
        if not item.lower().endswith(".py"):
            continue
        path = Path(item)
        if path.is_file():
            return path, argv[index + 1:]
    return None


def run_script(path: Path, argv: list[str]) -> int:
    """按源码执行一个脚本，等价于 ``python <script> ...``。"""
    base = base_dir()
    setup_paths(base)
    # 冻结后的 Python 以 isolated 模式启动，父进程设的 PYTHONUNBUFFERED 会被忽略，
    # 输出退化成块缓冲——管理前台靠管道实时读日志，不改成行缓冲就会一直「没反应」。
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(line_buffering=True, write_through=True)
            except (ValueError, OSError):
                pass
    sys.argv = [str(path)] + argv
    name = "yun_child_" + re.sub(r"\W+", "_", path.stem)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载脚本：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    entry = getattr(module, "main", None)
    if callable(entry):
        result = entry()
        return result if isinstance(result, int) else 0
    return 0


def run_worker(argv: list[str]) -> int:
    """分派独立执行进程（scripts/run_worker.py 的 main，参数原样）。"""
    base = base_dir()
    setup_paths(base)
    sys.argv = ["run_worker.py"] + argv
    spec_dir = base / "scripts"

    spec = importlib.util.spec_from_file_location("yun_run_worker", spec_dir / "run_worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module.main()


def pick_port(preferred: int) -> int:
    """挑一个真能监听的端口：优先用 ``preferred``，被占用就退到系统分配的空闲端口。

    默认端口一旦被别的实例占住，直接 bind 只会得到一个 OSError；先探一次，让启动
    失败退化成「换一个端口继续起」。
    """
    import socket

    for candidate in (preferred, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", candidate))
                return int(probe.getsockname()[1])
        except OSError:
            continue
    return preferred


USAGE = """用法：
    python launcher.py                 起本地服务并打开管理前台（默认 127.0.0.1:8437）
    python launcher.py --port 8500     指定端口（被占用会自动换一个空闲的）
    python launcher.py --no-browser    只起服务，不唤起浏览器
    python launcher.py --worker ...    内部使用：拉起独立执行进程（由业务层调用）
"""


def main() -> int:
    argv = sys.argv[1:]
    # 分派优先于「--help」：冻结后本程序会被当成 python 解释器调起
    # （``exe <script>.py --help``、``exe -X utf8 <script>.py ...``），
    # 这时 ``--help`` 是**子脚本**的，不能被我这里的 USAGE 吃掉。
    if argv and argv[0] == "--worker":
        return run_worker(argv[1:])

    target = script_target(argv)
    if target is not None:
        return run_script(*target)

    if "--help" in argv or "-h" in argv:
        print(USAGE, end="")
        return 0

    base = base_dir()
    redirect_streams(base)
    setup_paths(base)
    os.chdir(str(base / "yunrun" / "gps_track_lab"))

    host, port = "127.0.0.1", 8437
    open_admin = True
    if "--port" in argv:
        try:
            port = int(argv[argv.index("--port") + 1])
        except (IndexError, ValueError):
            pass
    if "--no-browser" in argv:
        open_admin = False

    requested = port
    port = pick_port(port)
    if port != requested:
        print(f"[提示] 端口 {requested} 已被占用，改用 {port}")

    # 挂在子目录下的模块对静态分析不可见，按名字动态加载：与脚本路径指向同一份源码，
    # 只是不依赖包内相对导入。
    web = importlib.import_module("gps_track_lab.web")
    web.run_server(host=host, port=port, open_browser=open_admin, admin=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
