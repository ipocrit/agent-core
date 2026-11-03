# MES 生产数据只读查询 MCP Server

给 Agent 用的 MES 数据查询服务：Agent 可以查表结构、执行 SELECT，但改不了任何数据；每次调用都写审计日志。数据全部是虚构的演示数据。

## 文件

| 文件 | 作用 |
|---|---|
| `seed_demo_db.py` | 生成演示数据库 `data/mes_demo.db`（产品、站点、工单、Lot、过站记录、Hold 记录） |
| `server.py` | MCP 服务：三个工具 + 四道安全防线 + 审计日志 |
| `tests/test_server.py` | 11 个测试：功能、危险语句拦截、超时、审计、MCP 协议端到端 |
| `logs/audit.jsonl` | 审计日志（运行后生成） |

## 工具

| 工具 | 作用 |
|---|---|
| `list_tables` | 列出所有表和行数 |
| `describe_table(table)` | 查看某张表的字段 |
| `run_select(sql, limit=50)` | 执行一条只读查询，最多返回 200 行 |

## 四道安全防线

1. **只读连接**：`mode=ro` 打开数据库 + `PRAGMA query_only = ON`
2. **语句检查**：只接受单条 `SELECT` / `WITH`
3. **授权回调**：SQLite 每个动作都要过 `set_authorizer`，只放行读表、查询、函数
4. **结果限制**：最多 200 行，超过 3 秒自动中断

## 使用

```bash
uv run seed_demo_db.py                      # 生成演示数据
uv run python -m unittest discover -s tests -v   # 跑测试
claude                                      # 在本目录打开 Claude Code，会提示是否启用 mes-readonly
```

在 Claude Code 里可以直接问：“现在有哪些 Lot 处于 Hold？卡在哪个站、什么原因？”
