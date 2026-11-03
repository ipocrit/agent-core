# agent-core

可复用的企业 Agent 底座：工具调用、知识检索、人工确认、审计、评测等通用能力放在 `core/`，行业差异放在 `domains/` 下的行业包里。新增一个场景只需要加一个行业包，不改底座代码。

所有业务数据都是脚本生成的模拟数据。

## 当前进度

阶段 0：已把 MES 卡站诊断（只读 MCP 服务 + LangGraph Agent）原样迁入 `domains/mes/`，`core/` 还是空目录，下一阶段从 MES 代码里拆出通用部分。

## 目录

```text
core/          通用底座（规划中）：llm / tools / knowledge / workflow / eval / observe
domains/mes/   MES 行业包
  mcp_server/  只读查询 MCP 服务：三个工具、四道安全防线、审计日志
  agent/       卡站诊断 Agent：查询 → 诊断 → 校验 → 人工确认 → 模拟执行 → 记录
api/           FastAPI 服务（规划中）
ui/            演示页（规划中）
reports/       评测报告（规划中）
```

## 运行

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync                    # 安装依赖
uv run pytest              # 运行全部测试（首次会自动生成演示数据库）
```

运行卡站诊断 Agent（不设 `DEEPSEEK_API_KEY` 时用规则模式诊断）：

```bash
cd domains/mes/agent
uv run agent.py start LOT-A001                          # 跑到"等人确认"为止
uv run agent.py resume <线程ID> --approve --operator 张三  # 批准后继续
```

各模块的细节见 `domains/mes/mcp_server/README.md` 和 `domains/mes/agent/README.md`。
