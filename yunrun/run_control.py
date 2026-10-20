# -*- coding: utf-8 -*-
"""Two-phase run control: prepare a staged run, then execute it after a human confirms.

Why two phases instead of one tool call. The failure this design prevents is a model
deciding on its own to submit a run: that would create a real campus record with no human
in the loop. So the destructive half is reachable only through a token minted by the
harmless half, and the only way to get a token is to run ``prepare_run()``, which touches
no network at all.

Flow:

    prepare_run()   -> generates a tasklist + renders a card. No network. Returns a token.
    (human sees the card and answers)
    execute_run()   -> refuses unless the token is pending AND confirmed=True.

Tokens live in memory, are single-use, and expire, so a stale token cannot be replayed
after a restart.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_TTL_SECONDS = 1800

# 跨进程读改写锁。三个入口（机器人插件 / 独立 MCP server / Web 控制台）各自持有一份
# 内存视图，写盘时必须排除并发，否则后写者会用自己那份陈旧字典覆盖先写者的新增条目。
# 使用操作系统锁，进程退出时自动释放；绝不因锁文件“太旧”而删除活跃锁。
_LOCK_TIMEOUT_S = 5.0


class RegistryError(RuntimeError):
    """Shared storage or locking failed; callers must not proceed with execution."""


@contextmanager
def file_mutex(path: Path, timeout: float = _LOCK_TIMEOUT_S):
    """Exclusive process lock. Failure is explicit; the lock file is never unlinked."""
    fd = None
    acquired = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RegistryError("shared lock unavailable; execution refused") from exc
                time.sleep(0.01)
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        raise RegistryError("cannot open shared lock; execution refused") from exc
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    try:
        yield
    finally:
        try:
            if acquired:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass
class PreparedRun:
    """One staged run waiting for confirmation."""

    token: str
    campus: str
    task_file: str
    card_png: str
    distance_km: float
    duration_s: float
    pace_min_per_km: float
    cadence: int
    stride_m: float
    point_count: int
    qualified: bool
    seed: int
    created_at: float
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    meta: dict[str, Any] = field(default_factory=dict)
    # Set once execution starts. ``worker`` lets a caller tell its own background thread
    # apart from an abandoned one, which is what makes "already running" answerable
    # instead of guessed.
    started_at: float | None = None
    worker: Any = None

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    @property
    def expired(self) -> bool:
        return self.age_seconds > self.ttl_seconds

    def summary(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "campus": self.campus,
            "task_file": self.task_file,
            "card_png": self.card_png,
            "card_exists": bool(self.card_png) and Path(self.card_png).is_file(),
            # 确认弹窗要按 run id 取卡（/api/console/runs/<id>/card.png）。prepare_run 把
            # 生成出来的 run id 放在 meta 里；老令牌没有这个键时，退回从卡片路径的父目录名取。
            # 少了这个键，前端会拼出 /runs//card.png —— 两步确认里唯一让人「看清跑道」的
            # 东西就变成一张裂图。
            "card_run_id": ((self.meta or {}).get("run_id")
                            or (Path(self.card_png).parent.name
                                if self.card_png and Path(self.card_png).name == "card.png"
                                else None)),
            "distance_km": self.distance_km,
            "duration_s": self.duration_s,
            "duration_min": round(self.duration_s / 60.0, 2),
            "pace_min_per_km": self.pace_min_per_km,
            "cadence": self.cadence,
            "stride_m": self.stride_m,
            "point_count": self.point_count,
            "qualified": self.qualified,
            "seed": self.seed,
            "age_seconds": round(self.age_seconds, 1),
            "expires_in_seconds": round(max(0.0, self.ttl_seconds - self.age_seconds), 1),
            "requires_confirmation": True,
            # 供前端在确认前展示：这场跑步的人脸要求与场地核对状态（meta 由
            # prepare_run 写入；旧令牌没有这些键时为 None，前端按未知处理）。
            "face": (self.meta or {}).get("face"),
            "stadium": (self.meta or {}).get("stadium"),
            "started": self.started_at is not None,
            "elapsed_seconds": (round(time.time() - self.started_at, 1)
                                if self.started_at else None),
        }


class RunRegistry:
    """Single-use, expiring store of prepared runs, mirrored to disk.

    The on-disk mirror is not a nicety. Pending runs are created and confirmed by
    *different* entry points -- a robot plugin owns one bridge, the standalone MCP
    server owns another, and the web console a third -- and each of those is a separate
    process with its own memory. Without a shared file, a token minted by one is
    invisible to the others ("no pending run for that token"), and reloading the plugin
    silently discards every token it had issued. Both were observed in practice.
    """

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 path: str | Path | None = None) -> None:
        self.ttl_seconds = ttl_seconds
        self.path = Path(path) if path else None
        self._runs: dict[str, PreparedRun] = {}
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------ persistence

    @contextmanager
    def _file_lock(self):
        """Hold a mandatory lock for the authoritative shared registry."""
        if self.path is None:
            yield
            return
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with file_mutex(lock_path):
            yield

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            entries = document.get("runs", document) if isinstance(document, dict) else document
        except OSError as exc:
            raise RegistryError("cannot read shared token registry") from exc
        except (ValueError, TypeError):
            # Corrupt input provides no redeemable tokens.
            return
        if not isinstance(entries, dict):
            return
        for token, payload in entries.items():
            if not isinstance(payload, dict):
                continue
            try:
                run = PreparedRun(**payload)
            except TypeError:
                continue
            run.worker = None            # a thread handle does not survive a process
            self._runs[str(token)] = run

    def _save_locked(self) -> None:
        """Write the mirror atomically; a reader must never see a partial file."""
        if self.path is None:
            return
        payload = {"version": 1, "runs": {token: asdict(run) for token, run in self._runs.items()}}
        for entry in payload["runs"].values():
            entry.pop("worker", None)
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=self.path.parent, delete=False,
                                             prefix=self.path.name + ".", suffix=".tmp") as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            self._refresh_locked()
            raise RegistryError("cannot persist token claim; execution refused") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _drop_dead_files_locked(self) -> None:
        """Forget entries whose staged table disappeared while we were not running."""
        for token, run in list(self._runs.items()):
            if not Path(run.task_file).is_file() and not run.expired:
                self._runs.pop(token, None)

    # ------------------------------------------------------------------ api

    def add(self, **kwargs: Any) -> PreparedRun:
        token = secrets.token_hex(16)
        kwargs.setdefault("created_at", time.time())
        run = PreparedRun(token=token, ttl_seconds=self.ttl_seconds, **kwargs)
        with self._lock:
            with self._file_lock():
                # Re-read *inside* the lock and before saving. Without this the whole
                # in-memory dict is written back, and a process whose snapshot predates a
                # token minted by another process silently deletes it -- observed live: the
                # bot minted a token, the standalone MCP server then prepared its own run,
                # and the bot's token was gone from the file 25 seconds after being issued.
                self._refresh_locked()
                self._purge_locked()
                self._runs[token] = run
                self._save_locked()
        return run

    def get(self, token: str) -> PreparedRun | None:
        with self._lock:
            with self._file_lock():
                self._refresh_locked()
                self._purge_locked()
                return self._runs.get(str(token or "").strip())

    def pop(self, token: str) -> PreparedRun | None:
        """Take the run out of the registry: a token is single-use."""
        with self._lock:
            with self._file_lock():
                self._refresh_locked()
                self._purge_locked()
                run = self._runs.pop(str(token or "").strip(), None)
                if run is not None:
                    self._save_locked()
            return run

    def pending(self) -> list[PreparedRun]:
        with self._lock:
            with self._file_lock():
                self._refresh_locked()
                self._purge_locked()
                return list(self._runs.values())

    def _refresh_locked(self) -> None:
        """Reload from disk, which is the authoritative view.

        No merging: ``add`` persists synchronously, so every token this process minted is
        already in the file, and anything missing from the file was consumed or expired by
        *someone*. Merging in-memory state back in resurrected a single-use token that
        another process had already run -- with two processes that is a double-spend, so
        the file wins outright.
        """
        if self.path is None:
            return
        self._runs = {}
        self._load()
        # A staged table deleted while we were not looking makes its token useless; drop it
        # here so get/pop/pending all agree instead of only the listing doing so.
        self._drop_dead_files_locked()

    def _purge_locked(self) -> None:
        expired = [t for t, r in self._runs.items() if r.expired]
        for token in expired:
            self._runs.pop(token, None)
        self._drop_dead_files_locked()
        if expired:
            self._save_locked()
