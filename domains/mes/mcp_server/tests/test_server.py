"""MCP Server 测试：功能、四道安全防线、审计日志、MCP 协议层。

运行：uv run python -m unittest -v
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "mes_demo.db"
if not DB.exists():
    subprocess.run([sys.executable, str(ROOT / "seed_demo_db.py")], check=True)

_tmp = tempfile.mkdtemp()
os.environ["MES_AUDIT_LOG"] = str(Path(_tmp) / "audit.jsonl")
sys.path.insert(0, str(ROOT))

import server  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


def db_hash() -> str:
    return hashlib.sha256(DB.read_bytes()).hexdigest()


def audit_lines() -> list[dict]:
    path = Path(os.environ["MES_AUDIT_LOG"])
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


class FunctionTests(unittest.TestCase):
    def test_list_tables(self):
        tables = {t["table"]: t["rows"] for t in server.list_tables()}
        self.assertEqual(set(tables), {"products", "stations", "work_orders", "lots", "lot_history", "holds"})
        self.assertEqual(tables["lots"], 5)

    def test_describe_table(self):
        cols = [c["column"] for c in server.describe_table("lots")]
        self.assertIn("current_station", cols)

    def test_describe_unknown_table_rejected(self):
        with self.assertRaises(ToolError):
            server.describe_table('lots"; DROP TABLE lots; --')

    def test_select_hold_lots(self):
        r = server.run_select(
            "SELECT l.lot_id, h.reason_code FROM lots l JOIN holds h ON h.lot_id = l.lot_id "
            "WHERE l.status = 'HOLD' ORDER BY l.lot_id"
        )
        self.assertEqual(r["rows"], [["LOT-A001", "QTIME_OVER"], ["LOT-B001", "EQP_DOWN"]])
        self.assertFalse(r["truncated"])

    def test_limit_truncates(self):
        r = server.run_select("SELECT * FROM lot_history", limit=3)
        self.assertEqual(r["row_count"], 3)
        self.assertTrue(r["truncated"])


class SecurityTests(unittest.TestCase):
    """每条危险语句都必须被拒绝，且数据库文件不变。"""

    DANGEROUS = [
        "DELETE FROM lots",
        "UPDATE lots SET status = 'DONE'",
        "DROP TABLE lots",
        "SELECT 1; DROP TABLE lots",
        "INSERT INTO products VALUES ('X', 'x')",
        "PRAGMA writable_schema = 1",
        "ATTACH DATABASE '/tmp/evil.db' AS evil",
        # 以 WITH 开头能骗过第 2 道防线，必须由后面的防线拦住
        "WITH x AS (SELECT 1) INSERT INTO products VALUES ('Y', 'y')",
        "WITH x AS (SELECT 1) DELETE FROM lots",
    ]

    def test_dangerous_statements_rejected(self):
        before = db_hash()
        for sql in self.DANGEROUS:
            with self.subTest(sql=sql):
                with self.assertRaises(ToolError):
                    server.run_select(sql)
        self.assertEqual(db_hash(), before, "数据库文件被改动了")

    def test_with_insert_blocked_by_later_defense(self):
        self.assertIsNone(server.check_select("WITH x AS (SELECT 1) DELETE FROM lots"))  # 第 2 道放过了
        with self.assertRaises(ToolError):  # 但最终仍被拒绝
            server.run_select("WITH x AS (SELECT 1) DELETE FROM lots")

    def test_semicolon_inside_string_is_allowed(self):
        r = server.run_select("SELECT 'a;b' AS v")
        self.assertEqual(r["rows"], [["a;b"]])

    def test_timeout(self):
        old = server.TIMEOUT_SECONDS
        server.TIMEOUT_SECONDS = 0.5
        try:
            with self.assertRaises(ToolError) as ctx:
                server.run_select(
                    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
                )
            self.assertIn("中断", str(ctx.exception))
        finally:
            server.TIMEOUT_SECONDS = old


class AuditTests(unittest.TestCase):
    def test_every_call_is_audited(self):
        n = len(audit_lines())
        server.run_select("SELECT 1")
        with self.assertRaises(ToolError):
            server.run_select("DELETE FROM lots")
        new = audit_lines()[n:]
        self.assertEqual([r["status"] for r in new], ["ok", "rejected"])
        self.assertEqual(new[1]["args"]["sql"], "DELETE FROM lots")
        self.assertIn("reason", new[1])


class ProtocolTests(unittest.TestCase):
    """真正启动 server.py 子进程，通过 MCP stdio 客户端调用，等同于 Agent 实际的使用方式。"""

    def _session_run(self, fn):
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=[str(ROOT / "server.py")],
            env={**os.environ},
        )

        async def main():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await fn(session)

        return asyncio.run(main())

    def test_end_to_end(self):
        async def scenario(session):
            tools = (await session.list_tools()).tools
            ok = await session.call_tool("run_select", {
                "sql": "SELECT lot_id FROM lots WHERE status = 'HOLD' ORDER BY lot_id"})
            bad = await session.call_tool("run_select", {"sql": "DROP TABLE lots"})
            return tools, ok, bad

        tools, ok, bad = self._session_run(scenario)
        self.assertEqual({t.name for t in tools}, {"list_tables", "describe_table", "run_select"})
        for t in tools:
            self.assertTrue(t.annotations.read_only_hint, t.name)
        self.assertFalse(ok.is_error)
        self.assertIn("LOT-A001", ok.content[0].text)
        self.assertTrue(bad.is_error)
        self.assertIn("已拒绝", bad.content[0].text)


if __name__ == "__main__":
    unittest.main()
