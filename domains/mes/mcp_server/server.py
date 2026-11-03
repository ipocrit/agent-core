"""MES 生产数据只读查询 MCP Server。

给 Agent 提供三个工具：list_tables、describe_table、run_select。
安全上有四道防线，任何一道拦住就拒绝执行：
  1. 只读连接：用 SQLite 的 mode=ro 打开数据库，并开启 query_only；
  2. 语句检查：只接受单条 SELECT / WITH 语句；
  3. 授权回调：SQLite 每做一个动作都会问一次，只放行“读表、查询、调用函数”；
  4. 结果限制：最多返回 MAX_ROWS 行，查询超时自动中断。
每次调用（无论成功、拒绝还是出错）都会写一行审计日志到 logs/audit.jsonl。

启动：uv run server.py（stdio 方式，由 Claude Code 等 MCP 客户端拉起）
"""

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("MES_DB_PATH", BASE_DIR / "data" / "mes_demo.db"))
AUDIT_LOG = Path(os.environ.get("MES_AUDIT_LOG", BASE_DIR / "logs" / "audit.jsonl"))
MAX_ROWS = 200          # 单次查询最多返回的行数
DEFAULT_LIMIT = 50      # 调用方不指定时返回的行数
TIMEOUT_SECONDS = 3.0   # 单次查询最长执行时间

# SQLite 授权回调里允许的动作：读列、SELECT、调用函数、递归 CTE
_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}

server = MCPServer(
    name="mes-readonly",
    instructions=(
        "MES 生产数据只读查询服务（演示数据）。先用 list_tables 和 describe_table 了解表结构，"
        "再用 run_select 执行 SELECT 查询。只能读，不能改；所有调用都会记录审计日志。"
    ),
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


# ---------- 审计日志 ----------

def audit(tool: str, args: dict[str, Any], status: str, started: float, **extra: Any) -> None:
    """追加一行 JSON 审计记录。status 取 ok / rejected / error。"""
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": tool,
        "args": args,
        "status": status,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        **extra,
    }
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------- 数据库连接（防线 1、3、4） ----------

def _authorizer(action: int, *_args: Any) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def open_readonly() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise ToolError(f"数据库不存在：{DB_PATH}，请先运行 uv run seed_demo_db.py")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_authorizer)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    # 每执行 1000 条虚拟机指令检查一次是否超时，返回非 0 即中断查询
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    return conn


# ---------- 语句检查（防线 2） ----------

_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)


def check_select(sql: str) -> str | None:
    """不合规时返回拒绝理由，合规返回 None。"""
    stripped = _COMMENTS.sub(" ", sql).strip().rstrip(";").strip()
    if not stripped:
        return "SQL 为空"
    if not sqlite3.complete_statement(stripped + ";"):
        return "SQL 不完整"
    # 去掉末尾分号后还有分号，说明是多条语句
    if ";" in re.sub(r"'(?:[^']|'')*'", "''", stripped):
        return "只允许执行单条语句"
    first = stripped.split(None, 1)[0].upper()
    if first not in {"SELECT", "WITH"}:
        return f"只允许 SELECT 查询，收到的是 {first}"
    return None


# ---------- 工具 ----------

@server.tool(annotations=READ_ONLY)
def list_tables() -> list[dict[str, Any]]:
    """列出数据库中所有业务表及其行数。"""
    started = time.monotonic()
    with open_readonly() as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        result = [
            {"table": n, "rows": conn.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0]}
            for n in names
        ]
    audit("list_tables", {}, "ok", started, rows=len(result))
    return result


@server.tool(annotations=READ_ONLY)
def describe_table(table: str) -> list[dict[str, Any]]:
    """查看某张表的字段名、类型、是否主键。"""
    started = time.monotonic()
    with open_readonly() as conn:
        known = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )}
        if table not in known:
            reason = f"表不存在：{table}"
            audit("describe_table", {"table": table}, "rejected", started, reason=reason)
            raise ToolError(reason)
        conn.set_authorizer(None)  # 表名已校验，PRAGMA table_info 是只读的
        cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    result = [{"column": c[1], "type": c[2], "primary_key": bool(c[5])} for c in cols]
    audit("describe_table", {"table": table}, "ok", started, rows=len(result))
    return result


@server.tool(annotations=READ_ONLY)
def run_select(sql: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """执行一条只读 SELECT 查询。limit 为最多返回行数（上限 200），超出部分会被截断并标注。"""
    started = time.monotonic()
    args = {"sql": sql, "limit": limit}
    reason = check_select(sql)
    if reason:
        audit("run_select", args, "rejected", started, reason=reason)
        raise ToolError(f"已拒绝：{reason}")
    limit = max(1, min(int(limit), MAX_ROWS))
    try:
        with open_readonly() as conn:
            cur = conn.execute(sql)
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(limit + 1)
    except sqlite3.DatabaseError as exc:
        msg = str(exc)
        if "not authorized" in msg or "readonly" in msg:
            audit("run_select", args, "rejected", started, reason=msg)
            raise ToolError(f"已拒绝：该语句涉及非只读操作（{msg}）") from exc
        if "interrupted" in msg:
            audit("run_select", args, "error", started, reason="查询超时")
            raise ToolError(f"查询超过 {TIMEOUT_SECONDS} 秒，已中断") from exc
        audit("run_select", args, "error", started, reason=msg)
        raise ToolError(f"SQL 执行出错：{msg}") from exc
    truncated = len(rows) > limit
    rows = rows[:limit]
    audit("run_select", args, "ok", started, rows=len(rows), truncated=truncated)
    return {
        "columns": columns,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }


if __name__ == "__main__":
    server.run()
