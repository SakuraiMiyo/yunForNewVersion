from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mcp_bridge import YunLocalBridge


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LocalBridgeTests(unittest.TestCase):
    def test_status_does_not_expose_token(self) -> None:
        bridge = YunLocalBridge(PROJECT_ROOT)
        result = bridge.status()
        self.assertTrue(result["ok"])
        raw_token = bridge._config().get("User", "token", fallback="")
        if raw_token:
            self.assertNotIn(raw_token, json.dumps(result, ensure_ascii=False))
        self.assertFalse(result["network_submission_available"])

    def test_allowlisted_task_listing_and_analysis(self) -> None:
        # 发布版说明：条数随本地 tasks_else 的实际内容断言（原始开发机上恰为 7 份），
        # 空目录（发布版默认状态）跳过。
        exports = sorted((PROJECT_ROOT / "tasks_else").glob("tasklist_*.json"))
        if not exports:
            self.skipTest("tasks_else 为空（发布版默认状态）：跳过清单断言")
        expected = len(exports)
        bridge = YunLocalBridge(PROJECT_ROOT)
        listed = bridge.list_tasks("else")
        self.assertTrue(listed["ok"])
        self.assertEqual(listed["count"], expected)
        analyzed = bridge.analyze_tasks("else", 3.0, 30.0)
        self.assertTrue(analyzed["ok"])
        # 记录总数与目录一致；达标条数随数据本身（用户新下载的历史可能含
        # 不合规记录），只约束在 [0, 总数] 区间而不是硬等于。
        report = analyzed["report"]
        self.assertLessEqual(report["stated_rule_pass_count"], expected)
        self.assertGreaterEqual(report["stated_rule_pass_count"], 0)
        self.assertFalse(bridge.list_tasks("../")["ok"])

    def test_local_generation_and_path_guard(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temporary:
            bridge = YunLocalBridge(PROJECT_ROOT, simulation_root=temporary)
            generated = bridge.generate_track(0.2, 6.0, 2, 123)
            self.assertTrue(generated["ok"])
            self.assertTrue(generated["synthetic"])
            self.assertTrue(Path(generated["output_dir"]).is_dir())
            detail = bridge.simulation_detail(generated["run_id"])
            self.assertTrue(detail["ok"])
            self.assertFalse(bridge.simulation_detail("../config.ini")["ok"])


if __name__ == "__main__":
    unittest.main()

