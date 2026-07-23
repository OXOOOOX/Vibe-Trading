# P4B2-F 提高输出限额后的真实试运行记录（2026-07-19）

机器记录：[P4B2_LIVE_GENERATION_ATTEMPT_2026-07-19.json](P4B2_LIVE_GENERATION_ATTEMPT_2026-07-19.json)

## 结论

本次已按授权把单成分输出上限从 600 提高到 1,000 tokens、全批输出上限从 1,800 提高到 3,000 tokens；输入上限仍为单成分 6,000、全批 18,000，最多 3 次模型调用，自动修复为 0，标的严格限定为 `588870.SH` 的 `688256.SH`、`688041.SH`、`688981.SH`。

Preflight 通过并建立真实 Plan，但第一只 `688256.SH` 的请求在生成前被 OpenAI Codex 内部端点以 HTTP 400 拒绝，原因是该端点不支持 `max_output_tokens`。系统立即停止，未自动重试；后两只 Job 已取消。因此 P4B2-F 真实生成与统一知识发布仍未完成。

## 授权、备份与 Plan

- 生产备份：`C:\Users\23479\.vibe-trading\cache\backups\research_cache.pre-p4b2.20260718T160339Z.sqlite3`
- 大小：24,182,784 bytes
- SHA-256：`49d6c6a5d767016553833cf73278da1ca8988cf421edbf5bf4eb03b4068234d2`
- 备份和当前生产库 `integrity_check`：`ok`
- Plan：`p4b2plan_71d5eddbdeaf767a9e93e702`
- 截止时间：`2026-07-18T16:05:13.736852+00:00`
- Plan 状态：`dry_run=false`、`authorized=true`
- 预算预演：3 次调用，输入 17,207，输出 3,000
- 单只输入/输出预算：`688256.SH` 5,752/1,000；`688041.SH` 5,748/1,000；`688981.SH` 5,707/1,000

## 实际执行结果

| 成分 | Job | 状态 | 请求计数 | 实际输入 | 实际输出 |
|---|---|---:|---:|---:|---:|
| 688256.SH | `p4b2job_bf8d2ea3881ef18adee031f2` | failed | 1 | 0 | 0 |
| 688041.SH | `p4b2job_74d57253732feb60321e4851` | cancelled | 0 | 0 | 0 |
| 688981.SH | `p4b2job_2531b147354ce82bdf62930b` | cancelled | 0 | 0 | 0 |

首个请求失败信息：

```text
OpenAI Codex HTTP 400: {"detail":"Unsupported parameter: max_output_tokens"}
```

预算台账 `p4b2budget_8a17e3f3ae91f35a6eb8d0af` 已结算为 1 次请求、0 输入 token、0 输出 token。Publish Result 为 0，`component_research` Report 为 0；没有生成 Claim，也没有 P4B1 reusable/partial_reusable 回流。

## 提供方硬上限边界

`openai-codex/gpt-5.6-terra` 使用的 ChatGPT Codex 内部端点不接受公开 Responses API 的 `max_output_tokens` 字段。本次 400 发生在生成前。适配器已改为不再向该端点发送不支持的字段，并保留调用后实际用量校验；但这只能形成提示词约束和事后拒收，不能形成服务端生成前的严格输出硬上限。

因此，在不新增授权的情况下，不再继续请求。若仍要求严格服务端输出上限，需要改用明确支持输出上限且已配置凭据的提供方，并重新授权新的精确三次调用；若继续使用 Codex，则必须明确接受“服务端硬上限不可用、仅提示词与调用后计量门控”的较弱边界。

## 范围审计与修正

模型请求没有扩大标的：只尝试了 `688256.SH`，另两只取消，`688008.SH` 和 `688012.SH` 没有模型调用、报告或生成知识写入。

但在首次请求前，旧执行路径为重建 P4B1 Resolution 而读取了该 ETF 的完整五条 P4A Binding，因此新 Resolution `componentresolution_34f7237a6eb3903043ea93a2` 确定性写入了五只成分的 `missing` 账务，其中包含未授权模型标的 `688008.SH`、`688012.SH`。这是零模型的 P4B1 状态写入，不是研究生成，但超出了本次 Plan 应触及的理想账务范围。

执行路径现已按 Plan `authorization_scope` 过滤重建输入，并新增回归测试，后续不会因三成分 Plan 再重建另外两条 Binding。不删除已存在的审计历史。

## 验证与安全状态

- P4B2 相关测试：43 passed，0 failed。
- `git diff --check`：通过。
- 自动修复/自动重试：0。
- 提供方切换：0。
- 持久生成开关：`ETF_COMPONENT_RESEARCH_GENERATION_ENABLED=0`。
- 持久 live-run 开关：`ETF_COMPONENT_RESEARCH_LIVE_RUN_ENABLED=0`。
- 生产库 `integrity_check`：`ok`。

P4B2-F 状态为 `attempted_but_halted_before_generation`，不得写成真实试运行完成。
