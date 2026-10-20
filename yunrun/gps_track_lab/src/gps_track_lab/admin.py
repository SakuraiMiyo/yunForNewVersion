# -*- coding: utf-8 -*-
"""引导式管理前台：从零配置 → 取令牌 → 下历史 → 标定 → 锁场地 → 人脸库 → 跑步。

设计原则（与跑步控制台 console.py 同源）：

* **引导式**。一个新用户要走的六步在这里按顺序摆出来，每步都能自动检测
  完成状态（总览页是一张检查表），点「去完成」跳到对应面板。
* **危险动作仍然是两阶段的**。本页不提供任何绕过确认闸的入口——真正提交
  记录仍然只发生在跑步控制台（/ui）的确认弹窗之后；这里的一切服务器交互
  都是只读请求（getHomeRunInfo / 学期列表 / 历史明细 / 学校目录），或
  本地操作（写配置 / 标定 / 人脸库）。
* **子进程隔离**。ADB 拉令牌、账号密码登录、标定这三件事跑在子进程里流式
  回传输出（AdminJob），不污染本服务进程；登录/标定脚本自带 chdir 副作用。
* **人脸库**。多张照片导入（RetinaFace 自动检测 + 取景质量门），激活一张
  作为执行时的人脸源——控制台的 bridge 在下次构建时自动带上（与原
  机器人接入的 face_photo/face_detection/face_mirror 三元组同一语义）。

路由（全部仅本机回环可用，与 /ui 同一道闸）：

    GET  /admin                      引导页
    GET  /api/admin/overview         六步检查表
    GET  /api/admin/config           配置（脱敏）
    POST /api/admin/config           保存配置字段
    GET  /api/admin/schools          公共学校目录全量列表（只读）
    POST /api/admin/school-discover  按学校名查公共目录（只读）
    POST /api/admin/verify-token     只读验证 token（getHomeRunInfo）
    POST /api/admin/token-adb        启动 ADB 拉取令牌子进程任务
    POST /api/admin/token-login      启动账号密码登录子进程任务
    GET  /api/admin/history/terms    学期列表（只读）
    POST /api/admin/history/runs     某学期的记录列表（只读）
    POST /api/admin/history/download 下载所选记录明细到 tasks_else（只读）
    GET  /api/admin/history/local    本地已存历史文件
    GET  /api/admin/calibration      当前标定摘要
    POST /api/admin/calibrate        启动标定子进程任务
    POST /api/admin/stadium-lock     场地锁定核对（只读）
    GET  /api/admin/face             人脸库条目 + 摘要
    POST /api/admin/face/upload      上传一张照片（原图字节）
    POST /api/admin/face/delete      删除条目
    POST /api/admin/face/activate    激活条目（并重置控制台 bridge）
    GET  /api/admin/face/photo/<id>  预览照片
    GET  /api/admin/jobs/<id>        子进程任务进度（since 增量）
"""
from __future__ import annotations

import configparser
import json
import os
import tempfile
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import web

ADMIN_PATH = "/admin"
API_PREFIX = "/api/admin"

MAX_JOB_LINES = 4000
JOB_TTL_SECONDS = 30 * 60
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# 配置表单可写的键（协议常量与 token 不在表单里——token 只由 ADB/登录流程写入）
WRITABLE_CONFIG: dict[str, list[str]] = {
    "Yun": ["yun_host", "school_host", "school_id", "school_name", "school_login_url"],
    "User": ["device_name", "sys_edition", "sys_version", "map_key"],
    "Login": ["username", "password"],
}
# 只读展示（脱敏）的键
MASKED_CONFIG = {("Login", "password")}


class AdminJob:
    """一个子进程任务的追加式输出缓冲（ConsoleJob 的子进程版）。"""

    def __init__(self, job_id: str, kind: str, command: list[str], cwd: Path,
                 env: dict[str, str]) -> None:
        self.id = job_id
        self.kind = kind
        self.command = command
        self.status = "running"
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.exit_code: int | None = None
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            command, cwd=str(cwd), env=env, text=True, encoding="utf-8",
            errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL)
        threading.Thread(target=self._pump, name=f"yun-admin-{job_id}", daemon=True).start()

    def _pump(self) -> None:
        proc = self._proc
        try:
            for line in proc.stdout or []:
                self.note(line.rstrip("\r\n"))
        finally:
            code = proc.wait()
            with self._lock:
                self.exit_code = code
                self.status = "done" if code == 0 else "failed"
                self.finished_at = time.time()

    def note(self, message: str) -> None:
        with self._lock:
            self._lines.append(str(message))
            del self._lines[:-MAX_JOB_LINES]

    def snapshot(self, *, since: int = 0) -> dict[str, Any]:
        with self._lock:
            lines = self._lines[since:] if since < len(self._lines) else []
            payload = {
                "job_id": self.id,
                "kind": self.kind,
                "status": self.status,
                "exit_code": self.exit_code,
                "total_lines": len(self._lines),
                "lines": lines,
                "elapsed_s": round((self.finished_at or time.time()) - self.started_at, 1),
            }
        return payload


class Admin:
    """引导页后端：配置 / 令牌 / 历史 / 标定 / 场地锁定 / 人脸库。"""

    def __init__(self, console) -> None:
        # 与跑步控制台共享同一个 Console 实例（同一进程）：bridge 复用、令牌互通，
        # 人脸库激活后还能 reset 它的 bridge 缓存让新配置生效。
        self.console = console
        self.project_dir = Path(console.project_dir).resolve()
        self.release_root = self.project_dir.parent
        self.config_path = Path(console._config_path or self.project_dir / "config.ini").resolve()
        self.history_dir = self.project_dir / "tasks_else"
        self.track_path = self.project_dir / "gps_track_lab" / "config" / "track.json"
        self._jobs: dict[str, AdminJob] = {}
        self._jobs_lock = threading.Lock()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _ok(**data: Any) -> dict[str, Any]:
        return {"ok": True, **data}

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {"ok": False, "error": {"code": code, "message": message}}

    def _config(self) -> configparser.ConfigParser:
        """读配置；config.ini 还不存在时，以 config.example.ini 为底稿。

        全新 clone 里没有 config.ini，而网页是唯一的配置入口：如果从空文件起笔，
        第一次「保存配置」只写进表单那几个键，`[Run]` 与协议常量（publickey /
        cipherkey / md5key …）通通缺失，后面每一步都会 NoOptionError。所以缺文件时
        先拿模板当底。

        ``interpolation=None``：配置里有密码、URL 这类可能出现 % 的值，默认的
        BasicInterpolation 会把它当占位符，读一次就抛 InterpolationSyntaxError。
        """
        conf = configparser.ConfigParser(interpolation=None)
        if not self.config_path.exists():
            example = self.config_path.parent / "config.example.ini"
            if example.is_file():
                conf.read(example, encoding="utf-8-sig")
            return conf
        conf.read(self.config_path, encoding="utf-8-sig")
        return conf

    def _bridge(self):
        return self.console.bridge()

    def _live_runner(self):
        """加载 runner 并按当前配置构建客户端（只读请求用；不发网络）。"""
        bridge = self._bridge()
        runner = bridge._runner()
        runner.set_args(str(self.config_path))
        return runner

    def _runner_tools_path(self) -> None:
        """只把 import 路径铺好（登录前的查询用：学校目录）。

        学校目录 / 按名查学校只需要协议常量，不读 token 与学校信息；所以这里
        不调用 ``set_args``（它要求 config.ini 齐备），全新 clone 还没有
        config.ini 时也能列学校、点选后再保存配置。
        """
        for sub in ("", "runner", os.path.join("runner", "tools")):
            path = str(self.project_dir / sub) if sub else str(self.project_dir)
            if path not in sys.path:
                sys.path.insert(0, path)

    def _spawn(self, kind: str, command: list[str], cwd: Path | None = None) -> dict[str, Any]:
        """启动一个流式回传的子进程任务。"""
        cwd = (cwd or self.project_dir).resolve()
        env = dict(os.environ)
        parts = [str(self.project_dir),
                 str(self.project_dir / "gps_track_lab" / "src"),
                 str(self.project_dir / "runner"),
                 str(self.project_dir / "runner" / "tools"),
                 str(self.project_dir / "tools")]
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(parts + ([existing] if existing else []))
        env["PYTHONUNBUFFERED"] = "1"
        import secrets as _secrets
        job_id = _secrets.token_hex(6)
        try:
            job = AdminJob(job_id, kind, command, cwd, env)
        except OSError as exc:
            return self._error("spawn_failed", f"无法启动子进程: {exc}")
        with self._jobs_lock:
            self._sweep_jobs_locked()
            self._jobs[job_id] = job
        return self._ok(job_id=job_id, kind=kind, poll=f"{API_PREFIX}/jobs/{job_id}")

    def _sweep_jobs_locked(self) -> None:
        cutoff = time.time() - JOB_TTL_SECONDS
        for job_id, job in list(self._jobs.items()):
            if job.finished_at and job.finished_at < cutoff:
                self._jobs.pop(job_id, None)

    def _job(self, job_id: str, since: int) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(str(job_id))
        if job is None:
            return self._error("unknown_job", "任务不存在或已过期")
        return self._ok(**job.snapshot(since=max(0, int(since))))

    # ------------------------------------------------------------------ 总览

    def overview(self) -> dict[str, Any]:
        """六步检查表（全部本地判断，不联网）。"""
        try:
            conf = self._config()
            school_name = conf.get("Yun", "school_name", fallback="").strip()
            school_host = conf.get("Yun", "school_host", fallback="").strip()
            school_id = conf.get("Yun", "school_id", fallback="").strip()
            token = conf.get("User", "token", fallback="").strip()
            username = conf.get("Login", "username", fallback="").strip()
        except Exception as exc:  # noqa: BLE001
            return self._error("config_unreadable", f"config.ini 无法读取: {exc}")

        history_files = sorted(p.name for p in self.history_dir.glob("tasklist_*.json")) \
            if self.history_dir.is_dir() else []
        calibration = self.calibration_summary().get("calibration") or {}
        try:
            import face_lib
            face = face_lib.summary()
        except Exception as exc:  # noqa: BLE001
            face = {"count": 0, "usable": 0, "active": "", "detector_ready": False,
                    "detector_error": str(exc), "model": ""}

        steps = [
            {"id": "config", "title": "学校与账号", "index": 1,
             "done": bool(school_host and school_id and school_name),
             "detail": school_name and f"{school_name} · {school_host} · id={school_id}"
                     or "尚未配置学校名称 / 服务器地址 / 学校 id"},
            {"id": "token", "title": "获取登录令牌", "index": 2,
             "done": bool(token),
             "detail": (f"token 已写入（{token[:4]}***，是否有效需验证）" if token
                        else "尚未获取 token（推荐 ADB 拉取，不影响手机登录）")
                     + ("" if token or not username else "；已保存账号密码，可用账号密码登录")},
            {"id": "history", "title": "下载历史跑步记录", "index": 3,
             "done": len(history_files) >= 3,
             "detail": f"本地已有 {len(history_files)} 份历史记录"
                     + ("（标定建议 ≥3 份同一操场）" if len(history_files) < 3 else "（可标定）")},
            {"id": "calibration", "title": "标定操场", "index": 4,
             "done": bool(calibration.get("calibrated")),
             "detail": calibration.get("summary_line") or "尚未标定（用历史记录拟合操场模型）"},
            {"id": "face", "title": "人脸库", "index": 5,
             "done": bool(face.get("active")),
             "detail": (f"{face.get('count', 0)} 张照片 / {face.get('usable', 0)} 张可用 / "
                        + (f"已激活 {face.get('active')}" if face.get("active") else "未激活"))
                     if face.get("count") else "库为空（学校开启人脸核验时需要）"},
            {"id": "run", "title": "仿真跑步与打表上传", "index": 6,
             "done": False, "always_open": True,
             "detail": "到跑步控制台生成轨迹 → 核对卡片 → 确认开跑（两阶段确认）"},
        ]
        return self._ok(steps=steps, history_files=history_files[-10:],
                        history_count=len(history_files), calibration=calibration,
                        face=face, school_host=school_host, token_set=bool(token))

    # ------------------------------------------------------------------ 配置

    def read_config(self) -> dict[str, Any]:
        try:
            conf = self._config()
        except Exception as exc:  # noqa: BLE001
            return self._error("config_unreadable", str(exc))
        out: dict[str, dict[str, Any]] = {}
        for section, keys in WRITABLE_CONFIG.items():
            out[section] = {}
            for key in keys:
                value = conf.get(section, key, fallback="")
                if (section, key) in MASKED_CONFIG:
                    out[section][key] = ""
                    out[section][key + "__set"] = bool(value)
                else:
                    out[section][key] = value
        return self._ok(config=out,
                        token_set=bool(conf.get("User", "token", fallback="").strip()))

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """只写白名单键；键存在即写入（空字符串 = 清空该键）。"""
        if not isinstance(payload, dict):
            return self._error("bad_payload", "请求体必须是 JSON 对象")
        conf = self._config()
        changed = []
        for section, keys in WRITABLE_CONFIG.items():
            incoming = payload.get(section)
            if not isinstance(incoming, dict):
                continue
            if section not in conf.sections():
                conf.add_section(section)
            for key in keys:
                if key not in incoming:
                    continue
                value = str(incoming[key]).strip()
                conf.set(section, key, value)
                changed.append(f"{section}.{key}")
        if not changed:
            return self._error("nothing_to_write", "没有可写入的字段")
        # 这个文件里放着 token / 设备标识 / 账号密码，直接 "w" 覆写遇到断电或杀进程
        # 就留下半截文件；先写临时文件再 os.replace。
        #
        # 临时文件必须是**唯一名**（mkstemp）：管理前台的「保存配置」和 ADB 拉令牌脚本
        # 会往同一个目录写配置，用固定的 config.ini.tmp 时两者会互相截断；而且固定名
        # 一旦崩溃残留，就是一份能被 `git add -A` 直接带进仓库的明文 token 文件。
        # mkstemp 顺带把权限压到 0600（POSIX）。
        tmp = None
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.config_path.parent),
                                       prefix=".config-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                conf.write(handle)
            os.replace(tmp, self.config_path)
            tmp = None
        except OSError as exc:
            return self._error("write_failed", f"config.ini 写入失败: {exc}")
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return self._ok(changed=changed)

    def discover_school(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按学校名精确查公共学校目录（只读、不需要 token）。"""
        name = str((payload or {}).get("school_name") or "").strip()
        if not name:
            return self._error("missing_name", "请填写学校名称")
        try:
            self._runner_tools_path()             # 保证 runner/tools 在 sys.path 上
            from getUrl_Id import getschool_Url_Id
            url, school_id = getschool_Url_Id(name, conf_path=str(self.config_path))
        except Exception as exc:  # noqa: BLE001
            return self._error("discover_failed", f"{type(exc).__name__}: {exc}")
        if not url or not school_id:
            return self._error("not_found",
                               f"学校目录里没有找到「{name}」：请用「加载学校列表」点选（名称需完全一致）")
        return self._ok(school_url=url, school_id=school_id,
                        note="已查到；点「保存配置」写入 config.ini")

    def schools(self) -> dict[str, Any]:
        """全量学校目录（不预设任何学校）：live 双路线 → 离线缓存 data/schools.json。"""
        try:
            self._runner_tools_path()             # 保证 runner/tools 在 sys.path 上
            from getUrl_Id import list_schools
            raw, source = list_schools(conf_path=str(self.config_path))
        except Exception as exc:  # noqa: BLE001
            return self._error("schools_failed", f"{type(exc).__name__}: {exc}")
        schools = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("schoolName") or "").strip()
            if not name:
                continue
            schools.append({
                "name": name,
                "id": item.get("schoolId") or item.get("schoolCode") or "",
                "url": str(item.get("schoolUrl") or "").rstrip("/"),
            })
        schools.sort(key=lambda s: s["name"])
        fetched_at = ""
        try:
            cache = json.loads(
                (self.project_dir / "data" / "schools.json").read_text(encoding="utf-8"))
            fetched_at = str(cache.get("fetched_at") or "")
        except (OSError, ValueError):
            pass
        return self._ok(count=len(schools), source=source, fetched_at=fetched_at,
                        schools=schools,
                        note=("在线获取" if source.startswith("live") else
                              f"离线缓存（{fetched_at}），联网后可点「重新加载」"))

    def verify_token(self) -> dict[str, Any]:
        """只读验证：用当前 token 调一次 /run/getHomeRunInfo（不创建任何记录）。"""
        # 配置未填时给出可操作的指引，而不是让底层抛一个 No scheme supplied。
        try:
            conf = self._config()
            if not conf.get("Yun", "school_host", fallback="").strip():
                return self._error(
                    "school_not_configured",
                    "服务器地址为空：请先完成「① 学校与账号」（加载学校列表 → 点选 → 保存配置），"
                    "再回来验证令牌。")
            if not conf.get("User", "token", fallback="").strip():
                return self._error(
                    "token_missing", "还没有令牌：先在本步用 ADB 拉取或账号密码登录。")
        except Exception:  # noqa: BLE001 - 读不了配置就按原路径报错
            pass
        try:
            runner = self._live_runner()
            raw = runner.default_post("/run/getHomeRunInfo", "")
            obj = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            return self._error("verify_failed",
                               f"{type(exc).__name__}: {exc}（服务器不可达或配置未填）")
        code = obj.get("code") if isinstance(obj, dict) else None
        cralist = ((obj.get("data") or {}).get("cralist") or []) if isinstance(obj, dict) else []
        if code == 200:
            return self._ok(valid=True, code=code, tasks=len(cralist),
                            note="token 有效（只读请求，未创建任何记录）")
        return self._ok(valid=False, code=code, msg=str(obj.get("msg", "") if isinstance(obj, dict) else ""),
                        note="token 无效或已过期：重新拉取一次即可")

    # ------------------------------------------------------------------ 令牌任务

    def token_adb(self) -> dict[str, Any]:
        script = self.project_dir / "refresh_token_adb.py"
        if not script.is_file():
            return self._error("missing_script", f"找不到 {script}")
        return self._spawn("token-adb", [sys.executable, "-X", "utf8", str(script)])

    def token_login(self) -> dict[str, Any]:
        script = self.release_root / "scripts" / "login_once.py"
        if not script.is_file():
            return self._error("missing_script", f"找不到 {script}")
        return self._spawn("token-login",
                           [sys.executable, "-X", "utf8", str(script),
                            "--config", str(self.config_path)])

    # ------------------------------------------------------------------ 历史

    def history_terms(self) -> dict[str, Any]:
        try:
            runner = self._live_runner()
            client = runner.default_client()
            obj = client.post_json("/run/listXnYearXqByStudentId", "")
        except Exception as exc:  # noqa: BLE001
            return self._error("terms_failed", f"{type(exc).__name__}: {exc}")
        if obj.get("code") != 200:
            return self._error("terms_failed", f"code={obj.get('code')} msg={obj.get('msg')}")
        terms = obj.get("data") or []
        return self._ok(terms=[{"key": t.get("key"), "value": t.get("value"),
                                "sjd": t.get("sjd")} for t in terms if isinstance(t, dict)])

    def history_runs(self, payload: dict[str, Any]) -> dict[str, Any]:
        table = str((payload or {}).get("term_table") or "").strip()
        if not table:
            return self._error("missing_term", "请先选择学期")
        try:
            runner = self._live_runner()
            client = runner.default_client()
            obj = client.post_json("/run/crsReocordInfoList",
                                   json.dumps({"tableName": table}))
        except Exception as exc:  # noqa: BLE001
            return self._error("runs_failed", f"{type(exc).__name__}: {exc}")
        if obj.get("code") != 200:
            return self._error("runs_failed", f"code={obj.get('code')} msg={obj.get('msg')}")
        runs = []
        for month in (obj.get("data") or {}).get("rank", []) or []:
            for run in month.get("rankList", []) or []:
                if isinstance(run, dict):
                    runs.append({"id": run.get("id"),
                                 "end_time": run.get("endTime", ""),
                                 "mileage": run.get("recordMileage", "0")})
        return self._ok(runs=runs)

    def history_download(self, payload: dict[str, Any]) -> dict[str, Any]:
        """下载所选记录明细（crsReocordInfo 是只读查询；保存到 tasks_else）。"""
        table = str((payload or {}).get("term_table") or "").strip()
        ids = (payload or {}).get("ids") or []
        if not table or not isinstance(ids, list) or not ids:
            return self._error("bad_payload", "需要 term_table 与非空 ids 列表")
        try:
            runner = self._live_runner()
            client = runner.default_client()
        except Exception as exc:  # noqa: BLE001
            return self._error("client_failed", f"{type(exc).__name__}: {exc}")
        self.history_dir.mkdir(parents=True, exist_ok=True)
        saved, failed = [], []
        for run_id in ids[:50]:
            try:
                text = client.post("/run/crsReocordInfo",
                                   json.dumps({"id": run_id, "tableName": table}))
                obj = json.loads(text)
                if obj.get("code") != 200:
                    failed.append({"id": run_id, "reason": f"code={obj.get('code')}"})
                    continue
                saved.append(self._save_history_record(text))
            except Exception as exc:  # noqa: BLE001
                failed.append({"id": run_id, "reason": str(exc)})
        return self._ok(saved=saved, failed=failed,
                        note="明细查询是只读请求；文件按 history.py 的 tasklist_N.json 编号落盘")

    def _save_history_record(self, text: str) -> str:
        """runner/history.py save_history_record 的同实现：递增编号 tasklist_N.json。"""
        files = os.listdir(self.history_dir)
        last = 0
        for file in files:
            if file.startswith("tasklist_") and file.endswith(".json"):
                try:
                    last = max(last, int(file[9:-5]) + 1)
                except ValueError:
                    continue
        target = self.history_dir / f"tasklist_{last}.json"
        target.write_text(text, encoding="utf-8")
        return target.name

    def history_local(self) -> dict[str, Any]:
        files = sorted(p.name for p in self.history_dir.glob("tasklist_*.json")) \
            if self.history_dir.is_dir() else []
        return self._ok(files=files, count=len(files), dir=str(self.history_dir))

    # ------------------------------------------------------------------ 标定

    def calibration_summary(self) -> dict[str, Any]:
        try:
            data = json.loads(self.track_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._ok(calibration={"calibrated": False,
                                         "summary_line": "track.json 不存在或不可读——先完成标定"})
        # 真实标定（recalibrate_track.py 产物）没有 "calibrated" 键，默认视为已标定；
        # 占位文件显式写 "calibrated": false。
        calibrated = ("calibrated" not in data) or bool(data.get("calibrated"))
        line = None
        if calibrated:
            line = (f"中心 ({float(data.get('center_lon')):.6f},{float(data.get('center_lat')):.6f})"
                    f" · 圈长 {float(data.get('lap_length_m')):.0f} m"
                    f" · 半径 {float(data.get('curve_radius_m')):.1f} m"
                    f" · 朝向 {float(data.get('heading_degrees')):.1f}°"
                    f" · 残差 {float(data.get('fit_residual_m')):.2f} m"
                    f" · {data.get('source_points')} 点")
        return self._ok(calibration={
            "calibrated": calibrated,
            "summary_line": line or "占位标定（虚构坐标，仅保证程序可运行）：尚未用你自己的历史记录标定",
            **{k: data.get(k) for k in (
                "center_lon", "center_lat", "heading_degrees", "straight_half_length_m",
                "curve_radius_m", "lap_length_m", "fit_residual_m", "source_points")}})

    def calibrate(self, payload: dict[str, Any]) -> dict[str, Any]:
        script = self.project_dir / "gps_track_lab" / "tools" / "recalibrate_track.py"
        if not script.is_file():
            return self._error("missing_script", f"找不到 {script}")
        files = list(self.history_dir.glob("tasklist_*.json")) if self.history_dir.is_dir() else []
        if not files:
            return self._error("no_history",
                               "tasks_else 里还没有历史记录：先到「下载历史记录」一步下载")
        try:
            lap = float((payload or {}).get("lap_length") or 400)
            cluster = int((payload or {}).get("cluster_index") or 0)
        except (TypeError, ValueError):
            return self._error("bad_payload", "lap_length / cluster_index 必须是数字")
        command = [sys.executable, "-X", "utf8", str(script),
                   "--input", str(self.history_dir),
                   "--output", str(self.track_path),
                   "--robust", "--lap-length", str(lap),
                   "--cluster-index", str(cluster), "--compare"]
        return self._spawn("calibrate", command)

    # ------------------------------------------------------------------ 场地锁定

    def stadium_lock(self) -> dict[str, Any]:
        """只读核对：几何锁定会选哪个任务、为什么（getHomeRunInfo 不创建记录）。"""
        try:
            bridge = self._bridge()
            stadium = bridge.stadium_status()
            rules = bridge.task_rules()
        except Exception as exc:  # noqa: BLE001
            return self._error("lock_failed", f"{type(exc).__name__}: {exc}")
        return self._ok(stadium=stadium, tasks=rules)

    # ------------------------------------------------------------------ 人脸库

    def face_status(self) -> dict[str, Any]:
        try:
            import face_lib
            return self._ok(entries=face_lib.entries(), summary=face_lib.summary())
        except Exception as exc:  # noqa: BLE001
            return self._error("face_lib_failed", f"{type(exc).__name__}: {exc}")

    def face_upload(self, data: bytes, name: str) -> dict[str, Any]:
        try:
            import face_lib
            entry = face_lib.add_photo(data, name)
        except (ValueError, RuntimeError, OSError) as exc:
            return self._error("upload_failed", str(exc))
        return self._ok(entry=entry)

    def face_delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            import face_lib
            return face_lib.remove(str((payload or {}).get("entry") or ""))
        except FileNotFoundError as exc:
            return self._error("not_found", str(exc))
        except (ValueError, OSError) as exc:
            return self._error("delete_failed", str(exc))

    def face_activate(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            import face_lib
            result = face_lib.set_active(str((payload or {}).get("entry") or ""))
        except (ValueError, FileNotFoundError) as exc:
            return self._error("activate_failed", str(exc))
        # 激活指针变了：让控制台下次构建 bridge 时带上新的人脸源
        # （bridge 在构造时读取 face_config，与接入方同一时机）。
        try:
            self.console.reset_bridge()
        except Exception:  # noqa: BLE001 - 重置失败不影响激活本身
            pass
        return result

    def face_photo_bytes(self, entry_id: str) -> bytes:
        import face_lib
        path = face_lib._entry_path(entry_id) / "photo.jpg"
        if not path.is_file():
            raise FileNotFoundError(entry_id)
        return path.read_bytes()

    # ------------------------------------------------------------------ 路由

    def handle_get(self, path: str, query: dict[str, list[str]]) -> tuple[int, Any, str]:
        if path == ADMIN_PATH:
            return 200, ADMIN_HTML.encode("utf-8"), "text/html; charset=utf-8"
        if path == f"{API_PREFIX}/overview":
            return 200, self.overview(), "application/json"
        if path == f"{API_PREFIX}/config":
            return 200, self.read_config(), "application/json"
        if path == f"{API_PREFIX}/schools":
            return 200, self.schools(), "application/json"
        if path == f"{API_PREFIX}/history/terms":
            return 200, self.history_terms(), "application/json"
        if path == f"{API_PREFIX}/history/local":
            return 200, self.history_local(), "application/json"
        if path == f"{API_PREFIX}/calibration":
            return 200, self.calibration_summary(), "application/json"
        if path == f"{API_PREFIX}/face":
            return 200, self.face_status(), "application/json"
        if path.startswith(f"{API_PREFIX}/face/photo/"):
            entry_id = unquote(path.rsplit("/", 1)[-1])
            try:
                return 200, self.face_photo_bytes(entry_id), "image/jpeg"
            except (FileNotFoundError, ValueError):
                return 404, {"error": "photo not found"}, "application/json"
        if path.startswith(f"{API_PREFIX}/jobs/"):
            since = 0
            raw = (query.get("since") or ["0"])[0]
            try:
                since = int(raw)
            except ValueError:
                since = 0
            job_id = unquote(path.rsplit("/", 1)[-1])
            payload = self._job(job_id, since)
            return (200 if payload.get("ok") else 404), payload, "application/json"
        return 404, {"error": "not found"}, "application/json"

    def handle_post(self, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        if path == f"{API_PREFIX}/config":
            result = self.save_config(payload)
        elif path == f"{API_PREFIX}/school-discover":
            result = self.discover_school(payload)
        elif path == f"{API_PREFIX}/verify-token":
            result = self.verify_token()
        elif path == f"{API_PREFIX}/token-adb":
            result = self.token_adb()
        elif path == f"{API_PREFIX}/token-login":
            result = self.token_login()
        elif path == f"{API_PREFIX}/history/runs":
            result = self.history_runs(payload)
        elif path == f"{API_PREFIX}/history/download":
            result = self.history_download(payload)
        elif path == f"{API_PREFIX}/calibrate":
            result = self.calibrate(payload)
        elif path == f"{API_PREFIX}/stadium-lock":
            result = self.stadium_lock()
        elif path == f"{API_PREFIX}/face/delete":
            result = self.face_delete(payload)
        elif path == f"{API_PREFIX}/face/activate":
            result = self.face_activate(payload)
        else:
            return 404, {"error": "not found"}
        return (200 if result.get("ok") else 400), result


# --------------------------------------------------------------------- 页面
from .ui import render_page

ADMIN_HTML = render_page("admin")
