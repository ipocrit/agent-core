"""卡站处理 Agent（LangGraph 演示）。

流程：查询 → 诊断 → 校验 → 人工确认 → 模拟执行 → 记录
  - 查询：通过 MCP 调用 ../mcp_server 的 run_select，取 Lot、过站记录、Hold 记录；
  - 诊断：有 DEEPSEEK_API_KEY 时调用 DeepSeek，否则用规则诊断（离线演示和测试用）；
  - 校验：按业务规则检查建议是否合规，不合规直接转人工，不进入执行；
  - 人工确认：interrupt() 暂停流程，状态存进 SQLite，进程可以退出；
  - 执行：只写模拟执行日志，不改任何生产数据；
  - 记录：每次运行的结论写入 logs/agent_runs.jsonl。

用法：
  uv run agent.py start LOT-A001              # 跑到“等人确认”为止，打印诊断
  uv run agent.py resume <线程ID> --approve --operator 张三
  uv run agent.py resume <线程ID> --reject  --operator 张三 --comment "先等品质结论"
  uv run agent.py status <线程ID>
"""

import argparse
import asyncio
import json
import operator
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

BASE_DIR = Path(__file__).parent
MCP_DIR = Path(os.environ.get("MES_MCP_DIR", BASE_DIR.parent / "mcp_server"))
CHECKPOINT_DB = Path(os.environ.get("AGENT_CHECKPOINT_DB", BASE_DIR / "data" / "checkpoints.db"))
LOG_DIR = Path(os.environ.get("AGENT_LOG_DIR", BASE_DIR / "logs"))

# 诊断允许给出的处置建议（白名单）
ALLOWED_ACTIONS = {
    "QUALITY_REVIEW": "提交品质判定，按结论放行或返工",
    "WAIT_EQP": "等待设备恢复后继续作业",
    "REROUTE_EQP": "改派同工序的其他设备",
    "RELEASE_HOLD": "解除 Hold，继续流转",
    "ESCALATE": "信息不足，转人工排查",
}


class State(TypedDict, total=False):
    lot_id: str
    facts: dict[str, Any]
    diagnosis: dict[str, Any]
    check: dict[str, Any]
    decision: dict[str, Any]
    result: str
    trace: Annotated[list[str], operator.add]  # 每个节点追加一行，列表自动合并


# ---------- 查询：通过 MCP 调用只读查询服务 ----------

LOT_ID_PATTERN = re.compile(r"^[A-Z0-9-]{1,32}$")


async def _mcp_queries(sqls: dict[str, str]) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command="uv",
        args=["--directory", str(MCP_DIR), "run", "server.py"],
        env={**os.environ},
    )
    out: dict[str, Any] = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for key, sql in sqls.items():
                res = await session.call_tool("run_select", {"sql": sql, "limit": 50})
                if res.is_error:
                    raise RuntimeError(f"MCP 查询失败：{res.content[0].text}")
                data = res.structured_content or json.loads(res.content[0].text)
                out[key] = [dict(zip(data["columns"], row)) for row in data["rows"]]
    return out


def query(state: State) -> dict[str, Any]:
    lot_id = state["lot_id"].strip().upper()
    if not LOT_ID_PATTERN.match(lot_id):  # Lot 号要拼进 SQL，先校验格式，防注入
        return {"facts": {"error": f"Lot 号格式不合法：{lot_id}"}, "trace": ["查询：Lot 号格式不合法"]}
    sqls = {
        "lot": (
            "SELECT l.lot_id, l.status, l.qty, l.current_station, s.station_name, s.qtime_limit_min, "
            "l.updated_at, w.wo_id, w.product_id FROM lots l "
            "JOIN work_orders w ON w.wo_id = l.wo_id "
            f"LEFT JOIN stations s ON s.station_id = l.current_station WHERE l.lot_id = '{lot_id}'"
        ),
        "history": (
            "SELECT station_id, event, event_time, note FROM lot_history "
            f"WHERE lot_id = '{lot_id}' ORDER BY event_time"
        ),
        "holds": (
            "SELECT station_id, reason_code, reason, hold_time, released FROM holds "
            f"WHERE lot_id = '{lot_id}' AND released = 0"
        ),
    }
    facts = asyncio.run(_mcp_queries(sqls))
    facts["lot"] = facts["lot"][0] if facts["lot"] else None
    return {
        "lot_id": lot_id,
        "facts": facts,
        "trace": [f"查询：通过 MCP 取到过站记录 {len(facts['history'])} 条、未解除 Hold {len(facts['holds'])} 条"],
    }


# ---------- 诊断：DeepSeek 或规则 ----------

SYSTEM_PROMPT = """你是面板厂 MES 的卡站诊断助手。根据给出的 Lot 信息、过站记录和 Hold 记录，判断卡站原因并给出一个处置建议。
只能从以下处置建议中选择一个 action：
{actions}
输出 JSON：{{"cause": "一句话原因", "evidence": ["引用的具体记录，至少一条"], "action": "上面列表中的代码", "reason": "为什么选这个处置"}}
只依据给出的数据，不要猜测数据里没有的信息；信息不足时 action 选 ESCALATE。"""


def _diagnose_by_llm(facts: dict[str, Any]) -> dict[str, Any]:
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
    actions = "\n".join(f"- {k}：{v}" for k, v in ALLOWED_ACTIONS.items())
    resp = client.chat.completions.create(
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT.format(actions=actions)},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    result = json.loads(resp.choices[0].message.content)
    result["mode"] = "llm"
    return result


def _diagnose_by_rules(facts: dict[str, Any]) -> dict[str, Any]:
    lot, holds = facts.get("lot"), facts.get("holds", [])
    if not lot:
        return {"cause": "查不到该 Lot", "evidence": [], "action": "ESCALATE", "reason": "数据缺失", "mode": "rule"}
    if holds:
        h = holds[0]
        evidence = [f"holds: {h['reason_code']} @ {h['station_id']} {h['hold_time']}：{h['reason']}"]
        if h["reason_code"] == "QTIME_OVER":
            return {"cause": "Q-time 超时被 Hold", "evidence": evidence, "action": "QUALITY_REVIEW",
                    "reason": "超时品可能影响良率，需品质判定后再决定放行或返工", "mode": "rule"}
        if h["reason_code"] == "EQP_DOWN":
            return {"cause": "设备异常被 Hold", "evidence": evidence, "action": "WAIT_EQP",
                    "reason": "设备恢复前无法作业，可视情况改派其他设备", "mode": "rule"}
    return {"cause": "未找到明确原因", "evidence": [], "action": "ESCALATE", "reason": "需人工排查", "mode": "rule"}


def diagnose(state: State) -> dict[str, Any]:
    facts = state["facts"]
    if facts.get("error"):
        d = {"cause": facts["error"], "evidence": [], "action": "ESCALATE", "reason": "输入有误", "mode": "rule"}
    elif os.environ.get("DEEPSEEK_API_KEY"):
        d = _diagnose_by_llm(facts)
    else:
        d = _diagnose_by_rules(facts)
    return {"diagnosis": d, "trace": [f"诊断（{d['mode']}）：{d['cause']} → 建议 {d['action']}"]}


# ---------- 校验：业务规则兜底 ----------

def validate(state: State) -> dict[str, Any]:
    d, facts = state["diagnosis"], state["facts"]
    problems = []
    if facts.get("error"):
        problems.append(facts["error"])
    if d.get("action") not in ALLOWED_ACTIONS:
        problems.append(f"建议 {d.get('action')} 不在允许列表内")
    if not facts.get("lot"):
        problems.append("查不到 Lot，不能执行任何操作")
    if d.get("action") != "ESCALATE" and not d.get("evidence"):
        problems.append("诊断没有给出证据")
    reason_codes = {h["reason_code"] for h in facts.get("holds", [])}
    if "QTIME_OVER" in reason_codes and d.get("action") == "RELEASE_HOLD":
        problems.append("Q-time 超时品不能直接解除 Hold，必须先经品质判定")
    if d.get("action") == "ESCALATE":
        problems.append("诊断建议转人工")
    ok = not problems
    return {"check": {"ok": ok, "problems": problems},
            "trace": ["校验：通过" if ok else "校验：不通过 → " + "；".join(problems)]}


def route_after_validate(state: State) -> str:
    return "approval" if state["check"]["ok"] else "record"


# ---------- 人工确认：interrupt 暂停 ----------

def approval(state: State) -> dict[str, Any]:
    decision = interrupt({
        "lot_id": state["lot_id"],
        "diagnosis": state["diagnosis"],
        "action_text": ALLOWED_ACTIONS[state["diagnosis"]["action"]],
        "question": "是否执行该处置建议？",
    })
    who = decision.get("operator", "未署名")
    return {"decision": decision,
            "trace": [f"人工确认：{who} {'批准' if decision.get('approved') else '驳回'}"
                      + (f"（{decision['comment']}）" if decision.get("comment") else "")]}


# ---------- 执行与记录 ----------

def _append_jsonl(name: str, record: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / name).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def execute(state: State) -> dict[str, Any]:
    action = state["diagnosis"]["action"]
    if not state["decision"].get("approved"):
        return {"result": "已驳回，未执行", "trace": ["执行：已驳回，跳过"]}
    _append_jsonl("simulated_actions.jsonl", {
        "time": _now(), "lot_id": state["lot_id"], "action": action,
        "operator": state["decision"].get("operator"), "note": "模拟执行，未修改生产数据",
    })
    return {"result": f"已模拟执行：{ALLOWED_ACTIONS[action]}", "trace": [f"执行：模拟执行 {action}"]}


def record(state: State) -> dict[str, Any]:
    result = state.get("result") or "未执行，转人工处理：" + "；".join(state["check"]["problems"])
    _append_jsonl("agent_runs.jsonl", {
        "time": _now(), "lot_id": state.get("lot_id"), "diagnosis": state.get("diagnosis"),
        "check": state.get("check"), "decision": state.get("decision"), "result": result,
    })
    return {"result": result, "trace": ["记录：已写入运行日志"]}


# ---------- 组装图 ----------

def build_graph(checkpointer):
    g = StateGraph(State)
    g.add_node("query", query)
    g.add_node("diagnose", diagnose)
    g.add_node("validate", validate)
    g.add_node("approval", approval)
    g.add_node("execute", execute)
    g.add_node("record", record)
    g.add_edge(START, "query")
    g.add_edge("query", "diagnose")
    g.add_edge("diagnose", "validate")
    g.add_conditional_edges("validate", route_after_validate, ["approval", "record"])
    g.add_edge("approval", "execute")
    g.add_edge("execute", "record")
    g.add_edge("record", END)
    return g.compile(checkpointer=checkpointer)


# ---------- 命令行 ----------

def _show(graph, thread_id: str) -> None:
    snap = graph.get_state({"configurable": {"thread_id": thread_id}})
    v = snap.values
    print(f"线程：{thread_id}")
    for line in v.get("trace", []):
        print("  ·", line)
    if snap.interrupts:
        payload = snap.interrupts[0].value
        d = payload["diagnosis"]
        print(f"\n【等待人工确认】Lot {payload['lot_id']}")
        print(f"  原因：{d['cause']}")
        print(f"  证据：{'；'.join(d['evidence'])}")
        print(f"  建议：{d['action']}（{payload['action_text']}）")
        print(f"  理由：{d['reason']}")
        print(f"\n批准：uv run agent.py resume {thread_id} --approve --operator 你的名字")
        print(f"驳回：uv run agent.py resume {thread_id} --reject --operator 你的名字 --comment 原因")
    elif v.get("result"):
        print(f"\n结论：{v['result']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="卡站处理 Agent（LangGraph 演示）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start"); s.add_argument("lot_id"); s.add_argument("--thread")
    r = sub.add_parser("resume"); r.add_argument("thread")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true"); g.add_argument("--reject", action="store_true")
    r.add_argument("--operator", default="未署名"); r.add_argument("--comment", default="")
    st = sub.add_parser("status"); st.add_argument("thread")
    args = ap.parse_args()

    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
        graph = build_graph(saver)
        if args.cmd == "start":
            thread = args.thread or f"{args.lot_id}-{uuid.uuid4().hex[:6]}"
            graph.invoke({"lot_id": args.lot_id}, {"configurable": {"thread_id": thread}})
            _show(graph, thread)
        elif args.cmd == "resume":
            decision = {"approved": args.approve, "operator": args.operator, "comment": args.comment}
            graph.invoke(Command(resume=decision), {"configurable": {"thread_id": args.thread}})
            _show(graph, args.thread)
        else:
            _show(graph, args.thread)


if __name__ == "__main__":
    main()
