# 卡站处理 Agent（LangGraph 演示）

以京东方 on-call 处理卡站的经验为原型：Agent 查数据、给诊断，**人确认后才执行**。全部使用演示数据，执行是模拟的，不改任何生产数据。

## 流程

```
查询（MCP） → 诊断（DeepSeek / 规则） → 校验（业务规则）
                                          ├─ 不通过 → 记录（转人工）
                                          └─ 通过 → 人工确认（interrupt 暂停）→ 模拟执行 → 记录
```

- **查询**：通过 MCP 调用 `../mcp_server` 的只读查询服务
- **诊断**：设置了 `DEEPSEEK_API_KEY` 就调用 DeepSeek（JSON 输出、白名单动作）；没设置就用规则诊断
- **校验**：动作必须在白名单内、必须有证据、Q-time 超时品不能直接解除 Hold
- **人工确认**：`interrupt()` 暂停，状态存进 `data/checkpoints.db`，进程可以退出，之后用 `resume` 接着跑
- **记录**：`logs/agent_runs.jsonl`（每次运行）、`logs/simulated_actions.jsonl`（批准后的模拟执行）

## 使用

```bash
uv run agent.py start LOT-A001                          # 跑到等人确认，打印诊断
uv run agent.py resume <线程ID> --approve --operator 张三
uv run agent.py resume <线程ID> --reject --operator 张三 --comment "先等品质结论"
uv run agent.py status <线程ID>
uv run python -m unittest discover -s tests -v          # 10 个测试
```

用 DeepSeek 诊断（在自己的终端里设置密钥，不要写进代码）：

```bash
export DEEPSEEK_API_KEY=你的密钥
uv run agent.py start LOT-A001
```
