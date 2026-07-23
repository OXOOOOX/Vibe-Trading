# P4B2 Codex 客户端软限制试运行记录（2026-07-19）

机器结果：[P4B2_CODEX_SOFT_LIMIT_ATTEMPT_2026-07-19.json](P4B2_CODEX_SOFT_LIMIT_ATTEMPT_2026-07-19.json)

## 结论

本次确认继续使用 `openai-codex/gpt-5.6-terra`，请求体没有发送 `max_output_tokens`。新备份和零模型 Preflight 均通过，真实 Plan `p4b2plan_cce84f5968e47a27c6265c92` 仅包含 `688256.SH`、`688041.SH`、`688981.SH`。

前两只真实生成分别使用 1,893/1,119 和 1,867/1,323 输入/输出 tokens。两份响应都超过单只 1,000 输出软限制，因此按“超限不发布”规则拒收。累计输出达到 2,442 后，第三只的 1,000 预算预留会超过全批 3,000，故在提供方调用前被阻止并取消。

## 实际结果

| 成分 | 状态 | 模型生成调用 | 输入 tokens | 输出 tokens | 发布 |
|---|---:|---:|---:|---:|---:|
| 688256.SH | failed | 1 | 1,893 | 1,119 | 0 |
| 688041.SH | failed | 1 | 1,867 | 1,323 | 0 |
| 688981.SH | cancelled | 0 | 0 | 0 | 0 |

批次合计：2 次真实生成调用、3,760 输入、2,442 输出、0 Publish Result、0 `component_research` Report。没有自动重试或修复。任务范围 P4B1 Resolution `componentresolution_29c86a5d3a7c3c878fb7a68e` 只含精确三只，状态仍全部为 `missing`。

## 安全与验证

- 新备份：`C:\Users\23479\.vibe-trading\cache\backups\research_cache.pre-p4b2.20260718T162848Z.sqlite3`
- 备份 SHA-256：`d77fb895bfa15ee53505da0f5d5d6510b49f3aab47f9aee739a9d423e582d6ae`
- 生产库 `integrity_check`：`ok`
- 持久生成与 live-run 开关：均为 `0`
- 专项测试：44 passed

## 下一门槛

被拒收的原始响应没有进入知识库，也没有保留为可发布草稿，因此无法在不重新调用模型的情况下补发。若要在同一自然日重新完整覆盖三只，需要新的精确授权同时处理两项变化：把客户端接受上限提高到至少单只 1,600、全批 4,800，并把单日模型调用硬上限从 5 提高到至少 6。仍应保持 0 自动修复、超限不发布和精确三只范围。
