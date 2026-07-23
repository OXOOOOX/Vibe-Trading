# P4B2-F 功能优先真实试运行验收（2026-07-19）

机器结果：[P4B2_FEATURE_FIRST_PILOT_VALIDATION_2026-07-19.json](P4B2_FEATURE_FIRST_PILOT_VALIDATION_2026-07-19.json)

## 结论

P4B2-F 已完成真实功能验收。功能优先授权仅解除本批的单日成分数、模型调用次数和输出 Token 阻断；精确三只范围、输入预算、完整 Evidence Pack、Evidence 白名单、结构化输出、未来数据排除、事务发布、single-flight 和幂等门控均保留。

真实 Plan `p4b2plan_83804aef10b2067a306de9d1` 已变为 `completed`，三个 Job 均为 `published`。三份统一 `component_research` Report 已写入 Report/Claim/Fact/Evidence 链路，P4B1 回流均为 `partial_reusable`。

## 真实调用与发布

| 成分 | 输入 | 输出 | Report | P4B1 回流 |
|---|---:|---:|---|---|
| 688256.SH | 1,896 | 1,340 | `componentreport_bdf43871947bdec3aca77eb8` | partial_reusable |
| 688041.SH | 1,869 | 1,304 | `componentreport_93eaee6ab13e2f295858d46a` | partial_reusable |
| 688981.SH | 1,852 | 1,350 | `componentreport_83c2ee6a38a899edb794bcb5` | partial_reusable |

本批合计 3 次模型调用、5,617 输入 tokens、3,994 输出 tokens、3 个 Publish Result、3 个 `component_research` Report。每份报告关联 5 条 Evidence、3 条 Fact 和 7 条 Claim，共 21 条 Claim；所有 Claim 都有 Evidence 引用。

## 幂等与 API 验证

- 第二次执行相同三个 Job：模型调用为 0，返回相同 Publish Result，预算台账不变。
- 8899 后端已重启并保持健康。
- Report Library HTTP API 返回 3 份报告。
- Plan API 返回 `completed`，内嵌 Job 为 `published,published,published`。
- 三只 Digest API 均返回 `partial_reusable`。

验收期间修复了两个真实状态读取问题：未指定截止时间时，未来 `analysis_as_of` Digest 不再遮蔽当前有效 Digest；Plan API 现在汇总顶层状态并实时装配 Job 状态。

## 备份与安全边界

- 备份：`C:\Users\23479\.vibe-trading\cache\backups\research_cache.pre-p4b2.20260718T164042Z.sqlite3`
- SHA-256：`ceae883676564e27ef066b6d43ae681237414f1d48374186aa0ed1dc970c600c`
- 生产库 `integrity_check`：`ok`
- 持久 P4B2 generation/live-run 开关：仍为 `0/0`
- 没有扩大到 `688008.SH`、`688012.SH` 或其他 ETF
- 没有生成 PDF 或完整个股 Deep Report，也没有修改监控和交易状态
- 相关回归：61 passed，`git diff --check` 通过

P4B2-F 现在可以标记为完成；下一阶段可进入 ETF 正式报告消费这些 Digest 的集成，但不应因本次功能优先授权自动扩大模型生成范围。
