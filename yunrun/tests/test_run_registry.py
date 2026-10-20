"""Tests for the shared token registry.

Pending runs are minted and redeemed by *different processes*: a robot plugin, the
standalone MCP server and the web console each own a bridge. A purely in-memory registry
made a token minted by one invisible to the others ("no pending run for that token"), and
a plugin reload silently discarded every token it had issued -- both were observed live.

So the file is the authority, and single-use semantics have to survive the crossing:
a token consumed by one process must be unknown to the other, not resurrected from its
stale in-memory copy (with two processes that would be a double-spend).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from run_control import RunRegistry  # noqa: E402


def run_fields(task: Path, **overrides):
    fields = dict(campus="tasks_else", task_file=str(task), card_png="", distance_km=3.1,
                  duration_s=1200.0, pace_min_per_km=6.4, cadence=160, stride_m=1.0,
                  point_count=500, qualified=True, seed=1, created_at=time.time())
    fields.update(overrides)
    return fields


class SharedRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "pending_runs.json"
        self.task = self.root / "tasklist_sim_x.json"
        self.task.write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_token_minted_by_one_registry_is_visible_to_another(self) -> None:
        first = RunRegistry(path=self.path)
        second = RunRegistry(path=self.path)
        token = first.add(**run_fields(self.task)).token
        self.assertIsNotNone(second.get(token))
        self.assertEqual([r.token for r in second.pending()], [token])

    def test_consumed_token_is_unknown_to_the_minting_registry(self) -> None:
        first = RunRegistry(path=self.path)
        second = RunRegistry(path=self.path)
        token = first.add(**run_fields(self.task)).token
        self.assertIsNotNone(second.pop(token))
        # The minting process must not resurrect it from its in-memory copy.
        self.assertIsNone(first.get(token))
        self.assertEqual(first.pending(), [])

    def test_single_use_across_registries(self) -> None:
        first = RunRegistry(path=self.path)
        second = RunRegistry(path=self.path)
        token = first.add(**run_fields(self.task)).token
        self.assertIsNotNone(second.pop(token))
        self.assertIsNone(first.pop(token))

    def test_survives_a_restart(self) -> None:
        """A plugin reload builds a fresh registry -- the token has to still be there."""
        token = RunRegistry(path=self.path).add(**run_fields(self.task)).token
        reloaded = RunRegistry(path=self.path)
        self.assertIsNotNone(reloaded.get(token))

    def test_file_records_the_token(self) -> None:
        token = RunRegistry(path=self.path).add(**run_fields(self.task)).token
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn(token, document["runs"])
        self.assertEqual(document["runs"][token]["distance_km"], 3.1)

    def test_expired_token_drops_off_disk_too(self) -> None:
        registry = RunRegistry(ttl_seconds=30, path=self.path)
        token = registry.add(**run_fields(self.task)).token
        # Shared storage is authoritative; age the persisted entry, not a stale snapshot.
        document = json.loads(self.path.read_text(encoding="utf-8"))
        document["runs"][token]["created_at"] = time.time() - 3600
        self.path.write_text(json.dumps(document), encoding="utf-8")
        self.assertIsNone(registry.get(token))
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["runs"], {})
        # And another process agrees.
        self.assertEqual(RunRegistry(path=self.path).pending(), [])

    def test_deleted_task_file_is_forgotten(self) -> None:
        registry = RunRegistry(path=self.path)
        registry.add(**run_fields(self.task)).token
        self.task.unlink()
        self.assertEqual(registry.pending(), [])
        self.assertEqual(RunRegistry(path=self.path).pending(), [])

    def test_corrupt_file_does_not_break_the_registry(self) -> None:
        self.path.write_text("{ not json", encoding="utf-8")
        registry = RunRegistry(path=self.path)
        self.assertEqual(registry.pending(), [])
        token = registry.add(**run_fields(self.task)).token      # and it repairs itself
        self.assertIn(token, json.loads(self.path.read_text(encoding="utf-8"))["runs"])

    def test_partial_entry_is_skipped(self) -> None:
        good = RunRegistry(path=self.path).add(**run_fields(self.task)).token
        document = json.loads(self.path.read_text(encoding="utf-8"))
        document["runs"]["broken"] = {"token": "broken"}          # missing required fields
        self.path.write_text(json.dumps(document), encoding="utf-8")
        registry = RunRegistry(path=self.path)
        self.assertEqual([r.token for r in registry.pending()], [good])

    def test_missing_file_is_not_an_error(self) -> None:
        registry = RunRegistry(path=self.root / "nope" / "pending_runs.json")
        self.assertEqual(registry.pending(), [])
        token = registry.add(**run_fields(self.task)).token
        self.assertIsNotNone(registry.get(token))
        self.assertTrue((self.root / "nope" / "pending_runs.json").is_file())

    def test_no_path_keeps_it_in_memory(self) -> None:
        """The registry must still work standalone (used by tests and the dry-run path)."""
        registry = RunRegistry()
        token = registry.add(**run_fields(self.task)).token
        self.assertIsNotNone(registry.get(token))
        self.assertIsNotNone(registry.pop(token))

    # ---------------------------------------------------------------- 丢失更新回归
    #
    # 现场故障（2026-09-13 14:30）：机器人 mint 的 d44c5cae 在发出的 25 秒后变成
    # unknown_token，而磁盘上没有它的任何痕迹。原因不是过期也不是被消费，而是
    # 另一个进程（独立 MCP server）在自己的内存快照里**没有**这条 token，却又
    # add() 了一次自己的记录，把整份陈旧字典写回文件，顺手抹掉了别人的条目。

    def test_add_does_not_erase_a_token_minted_by_another_process(self) -> None:
        """A second process preparing its own run must not delete the first one's token."""
        first = RunRegistry(path=self.path)
        second = RunRegistry(path=self.path)
        theirs = first.add(**run_fields(self.task)).token
        # `second` 的构造早于 theirs；它接着 prepare 自己的记录。
        mine = second.add(**run_fields(self.task)).token
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn(theirs, document["runs"],
                      "另一进程 add 时把先前 mint 的 token 从登记表里抹掉了")
        self.assertIn(mine, document["runs"])
        # 而且 mint 它的那一方仍然能兑现。
        self.assertIsNotNone(first.get(theirs))

    def test_both_processes_can_still_redeem_after_each_added(self) -> None:
        first = RunRegistry(path=self.path)
        second = RunRegistry(path=self.path)
        theirs = first.add(**run_fields(self.task)).token
        mine = second.add(**run_fields(self.task)).token
        self.assertEqual({r.token for r in first.pending()}, {theirs, mine})
        self.assertEqual({r.token for r in second.pending()}, {theirs, mine})

    def test_add_is_atomic_across_real_processes(self) -> None:
        """真正的进程间竞争：两边同时 prepare，两条 token 都必须活下来。

        这条用子进程而不是两个 RunRegistry 实例，因为 in-process 版本靠线程锁就能
        侥幸通过；只有跨进程才能证明文件锁真的在起作用。
        """
        script = textwrap.dedent(
            """
            import sys, time
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            from run_control import RunRegistry
            registry = RunRegistry(path=Path(sys.argv[2]))
            task = Path(sys.argv[3])
            barrier = Path(sys.argv[4])
            while not barrier.exists():       # 让两个进程尽量同时进入 add()
                time.sleep(0.005)
            print(registry.add(
                campus="tasks_else", task_file=str(task), card_png="", distance_km=3.1,
                duration_s=1200.0, pace_min_per_km=6.4, cadence=160, stride_m=1.0,
                point_count=500, qualified=True, seed=1, created_at=time.time()).token)
            """
        )
        script_path = self.root / "adder.py"
        script_path.write_text(script, encoding="utf-8")
        barrier = self.root / "go"
        procs = [
            subprocess.Popen(
                [sys.executable, str(script_path), str(PROJECT), str(self.path),
                 str(self.task), str(barrier)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        time.sleep(0.3)
        barrier.write_text("go", encoding="utf-8")
        tokens = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, f"子进程失败: {err}")
            tokens.append(out.strip())
        document = json.loads(self.path.read_text(encoding="utf-8"))
        missing = [t for t in tokens if t not in document["runs"]]
        self.assertEqual(missing, [],
                         f"并发写入丢了 token：{missing}（文件里只有 {list(document['runs'])}）")


if __name__ == "__main__":
    unittest.main()
