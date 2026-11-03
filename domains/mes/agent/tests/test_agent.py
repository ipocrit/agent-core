"""卡站处理 Agent 测试（离线规则模式，不需要 API Key）。

运行：uv run python -m unittest discover -s tests -v
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_tmp = Path(tempfile.mkdtemp())
os.environ.pop("DEEPSEEK_API_KEY", None)  # 强制规则模式，测试结果可重复
os.environ["AGENT_CHECKPOINT_DB"] = str(_tmp / "checkpoints.db")
os.environ["AGENT_LOG_DIR"] = str(_tmp / "logs")
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

MES_DB = agent.MCP_DIR / "data" / "mes_demo.db"


def run(thread: str, payload):
    """每次调用都新建连接和图，模拟“进程退出后重新启动”。"""
    with SqliteSaver.from_conn_string(os.environ["AGENT_CHECKPOINT_DB"]) as saver:
        graph = agent.build_graph(saver)
        graph.invoke(payload, {"configurable": {"thread_id": thread}})
        return graph.get_state({"configurable": {"thread_id": thread}})


def log_lines(name: str) -> list[dict]:
    p = _tmp / "logs" / name
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


class FlowTests(unittest.TestCase):
    def test_qtime_lot_pauses_then_resumes_after_restart(self):
        snap = run("t-qtime", {"lot_id": "LOT-A001"})
        self.assertTrue(snap.interrupts, "应该停在人工确认")
        self.assertEqual(snap.values["diagnosis"]["action"], "QUALITY_REVIEW")
        before = len(log_lines("simulated_actions.jsonl"))

        snap = run("t-qtime", Command(resume={"approved": True, "operator": "测试员"}))
        self.assertFalse(snap.interrupts)
        self.assertTrue(snap.values["result"].startswith("已模拟执行"))
        self.assertEqual(len(log_lines("simulated_actions.jsonl")), before + 1)
        # 恢复后不会重新查询：查询只做过一次
        self.assertEqual(sum(t.startswith("查询") for t in snap.values["trace"]), 1)

    def test_reject_does_not_execute(self):
        run("t-reject", {"lot_id": "LOT-B001"})
        before = len(log_lines("simulated_actions.jsonl"))
        snap = run("t-reject", Command(resume={"approved": False, "operator": "测试员", "comment": "先修设备"}))
        self.assertEqual(snap.values["result"], "已驳回，未执行")
        self.assertEqual(len(log_lines("simulated_actions.jsonl")), before)

    def test_equipment_hold(self):
        snap = run("t-eqp", {"lot_id": "LOT-B001"})
        self.assertEqual(snap.values["diagnosis"]["action"], "WAIT_EQP")

    def test_unknown_lot_goes_to_human_without_approval(self):
        snap = run("t-unknown", {"lot_id": "LOT-Z999"})
        self.assertFalse(snap.interrupts, "查不到的 Lot 不应进入审批")
        self.assertIn("转人工", snap.values["result"])

    def test_injection_like_lot_id_rejected(self):
        snap = run("t-inject", {"lot_id": "X'; DROP TABLE lots; --"})
        self.assertFalse(snap.interrupts)
        self.assertIn("格式不合法", snap.values["result"])

    def test_every_run_is_recorded(self):
        n = len(log_lines("agent_runs.jsonl"))
        run("t-record", {"lot_id": "LOT-Z998"})
        self.assertEqual(len(log_lines("agent_runs.jsonl")), n + 1)


class ValidateTests(unittest.TestCase):
    FACTS = {"lot": {"lot_id": "LOT-A001"}, "holds": [{"reason_code": "QTIME_OVER"}]}

    def test_qtime_cannot_release_directly(self):
        out = agent.validate({"facts": self.FACTS,
                              "diagnosis": {"action": "RELEASE_HOLD", "evidence": ["x"]}})
        self.assertFalse(out["check"]["ok"])
        self.assertTrue(any("品质判定" in p for p in out["check"]["problems"]))

    def test_unknown_action_rejected(self):
        out = agent.validate({"facts": self.FACTS, "diagnosis": {"action": "DELETE_LOT", "evidence": ["x"]}})
        self.assertFalse(out["check"]["ok"])

    def test_missing_evidence_rejected(self):
        out = agent.validate({"facts": self.FACTS, "diagnosis": {"action": "QUALITY_REVIEW", "evidence": []}})
        self.assertFalse(out["check"]["ok"])


class SafetyTests(unittest.TestCase):
    def test_mes_database_unchanged(self):
        before = hashlib.sha256(MES_DB.read_bytes()).hexdigest()
        snap = run("t-safe", {"lot_id": "LOT-A001"})
        run("t-safe", Command(resume={"approved": True, "operator": "测试员"}))
        self.assertEqual(hashlib.sha256(MES_DB.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
