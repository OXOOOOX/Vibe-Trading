import {
  Activity,
  AlertTriangle,
  ExternalLink,
  Landmark,
  Loader2,
  RefreshCw,
} from "lucide-react";

import type { ETFProductField, ETFProductProfile } from "@/lib/api";


const FIELD_LABELS: Record<string, string> = {
  fund_full_name: "基金全称",
  manager: "管理人",
  custodian: "托管人",
  exchange: "上市交易所",
  contract_effective_date: "合同生效日",
  listing_date: "上市日",
  tracked_index_code: "跟踪指数",
  version: "规则版本",
  target_component_count: "成分数量",
  single_constituent_weight_cap: "单一权重上限",
  top_five_weight_cap: "前五大权重上限",
  review_frequency: "调样频率",
  management_fee_rate: "管理费率",
  custody_fee_rate: "托管费率",
  unit_nav: "单位净值",
  fund_units: "基金份额",
  published_net_assets: "定期报告资产净值",
  exchange_market_value: "场内市值",
  iopv: "IOPV",
  premium_discount_rate: "折溢价率",
};


export function ETFProductOverview({
  profile,
  refreshing,
  onRefresh,
}: {
  profile?: ETFProductProfile | null;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  const identity = profile?.identity || {};
  const methodology = profile?.index_methodology || {};
  const metrics = profile?.product_metrics || {};
  const peer = profile?.peer_group;
  const peerIndex = peerIndexLabel(peer);
  const share = profile?.share_history;
  const sourceRules = profile?.source_policy?.rules || [];
  const hasWarning = Boolean(
    profile?.stale
    || profile?.quality_status !== "passed"
    || profile?.conflicts.length
    || profile?.cache_reused_sections?.length
    || peer?.warnings.length,
  );

  return (
    <section aria-label="ETF 产品与指数" className="rounded-lg border bg-card p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <Landmark className="h-4 w-4 text-primary" />
            <h3 className="font-semibold">ETF 产品与指数</h3>
            {profile ? <StatusBadge profile={profile} /> : null}
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            产品身份、指数规则、费率和份额变化绑定同一不可变快照；打开页面只读取缓存。
          </p>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={refreshing}
          className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50"
        >
          {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          {profile ? "刷新 ETF 资料" : "获取 ETF 资料"}
        </button>
      </div>

      {!profile ? (
        <div className="mt-4 rounded-md border border-dashed p-5 text-sm text-muted-foreground">
          暂无 ETF 产品快照。点击获取后才会联网刷新产品、指数、行情与同指数产品组。
        </div>
      ) : (
        <>
          <div className="mt-4 grid gap-4 xl:grid-cols-2">
            <FieldGroup
              title="产品身份"
              fields={[
                ["fund_full_name", identity.fund_full_name],
                ["manager", identity.manager],
                ["custodian", identity.custodian],
                ["exchange", identity.exchange],
                ["contract_effective_date", identity.contract_effective_date],
                ["listing_date", identity.listing_date],
                ["tracked_index_code", combineIndex(identity.tracked_index_code, identity.tracked_index_name)],
              ]}
            />
            <FieldGroup
              title="指数规则"
              fields={[
                ["version", methodology.version],
                ["target_component_count", methodology.target_component_count],
                ["single_constituent_weight_cap", methodology.single_constituent_weight_cap],
                ["top_five_weight_cap", methodology.top_five_weight_cap],
                ["review_frequency", methodology.review_frequency],
              ]}
            />
          </div>

          <div className="mt-5">
            <h4 className="text-sm font-semibold">费率与时点数据</h4>
            <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
              {[
                "management_fee_rate", "custody_fee_rate", "unit_nav", "fund_units",
                "published_net_assets", "exchange_market_value", "iopv", "premium_discount_rate",
              ].map((key) => <MetricCard key={key} fieldKey={key} field={metrics[key]} />)}
            </div>
          </div>

          {share || peer ? (
            <div className="mt-5 rounded-md border bg-background/50 p-4">
              <div className="flex flex-wrap items-center gap-2">
                <Activity className="h-4 w-4 text-primary" />
                <h4 className="text-sm font-semibold">同指数 ETF 流量分析</h4>
                {peerIndex ? (
                  <span className="text-xs text-muted-foreground">{peerIndex}</span>
                ) : null}
              </div>
              <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                <FlowStat label="本基金份额" value={formatLarge(share?.current_units, "份")} />
                <FlowStat label="单日份额变化" value={formatSigned(share?.delta_1d, "份")} />
                <FlowStat label="同指数组估算净流量" value={formatMoney(peer?.estimated_net_flow_1d)} />
                <FlowStat
                  label="同指数组覆盖"
                  value={`${peer?.member_count || 0} 只 · ${formatPct(peer?.unit_change_coverage_ratio)}`}
                />
              </div>
              <p className="mt-3 text-xs text-muted-foreground">
                净流量采用“份额变化×可用估算价格”代理估算：优先使用交易所市场价格，深市缺少市价时使用
                交易所公布净值并逐项标注；它不等同于成交额或基金公司确认资金流，份额数本身也不跨产品直接相加。
              </p>
              {peer?.members?.length ? (
                <div className="mt-3 overflow-x-auto">
                  <table className="w-full min-w-[820px] text-left text-xs">
                    <thead className="text-muted-foreground">
                      <tr className="border-b">
                        <th className="py-2 pr-3 font-medium">同指数产品</th>
                        <th className="py-2 pr-3 text-right font-medium">最新份额</th>
                        <th className="py-2 pr-3 text-right font-medium">单日变化</th>
                        <th className="py-2 pr-3 text-right font-medium">估算净流量</th>
                        <th className="py-2 pr-3 font-medium">估算口径</th>
                        <th className="py-2 font-medium">映射状态</th>
                      </tr>
                    </thead>
                    <tbody>
                      {peer.members.map((member) => (
                        <tr key={member.symbol} className="border-b last:border-0">
                          <td className="py-2 pr-3">
                            <div className="font-medium">{member.name || member.symbol}</div>
                            <div className="font-mono text-muted-foreground">{member.symbol}</div>
                          </td>
                          <td className="py-2 pr-3 text-right">{formatLarge(member.current_units, "份")}</td>
                          <td className={`py-2 pr-3 text-right ${flowTone(member.delta_1d)}`}>
                            {formatSigned(member.delta_1d, "份")}
                          </td>
                          <td className={`py-2 pr-3 text-right ${flowTone(member.estimated_net_flow_1d)}`}>
                            {formatMoney(member.estimated_net_flow_1d)}
                          </td>
                          <td className="py-2 pr-3 text-muted-foreground">
                            {flowBasisLabel(member.estimation_price_type)}
                          </td>
                          <td className="py-2 text-muted-foreground">
                            {mappingStatusLabel(member.mapping_status)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : null}
            </div>
          ) : null}

          {hasWarning ? (
            <div className="mt-4 space-y-1 rounded-md border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-amber-800 dark:text-amber-200">
              {profile.stale ? <Warning text="本次刷新失败，当前显示的是上一份缓存快照。" /> : null}
              {profile.missing_hard_fields.length ? <Warning text={`发布硬字段暂缺：${profile.missing_hard_fields.join("、")}`} /> : null}
              {profile.missing_optional_fields.length ? <Warning text={`可选字段暂缺：${profile.missing_optional_fields.join("、")}`} /> : null}
              {profile.conflicts.length ? <Warning text="不同来源的口径或时点存在差异，正式报告会保留差异及处理记录。" /> : null}
              {profile.cache_reused_sections?.length ? (
                <Warning text={`本轮部分官方来源获取失败，已保留上一不可变快照：${profile.cache_reused_sections.map((item) => item.section).join("、")}`} />
              ) : null}
              {(peer?.warnings || []).map((warning) => <Warning key={warning} text={warning} />)}
            </div>
          ) : null}

          {sourceRules.length ? (
            <div className="mt-5 rounded-md border bg-background/50 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h4 className="text-sm font-semibold">来源与获取规则</h4>
                <span className="text-xs text-muted-foreground">
                  {profile.source_policy?.registry_version} · {sourceRules.filter((rule) => rule.status === "completed").length}/{sourceRules.length} 成功
                </span>
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                每条规则声明适用范围、解析器、刷新频率与失败处理；报告保存本轮实际执行结果，而不是只保存网页链接。
              </p>
              <div className="mt-3 grid gap-2 md:grid-cols-2">
                {sourceRules.map((rule, index) => (
                  <div key={`${rule.phase}:${rule.rule_id}:${index}`} className="rounded border bg-card p-3 text-xs">
                    <div className="flex items-start justify-between gap-2">
                      <div>
                        <div className="font-medium">{rule.label}</div>
                        <div className="mt-0.5 text-muted-foreground">{rule.publisher} · {sourcePhaseLabel(rule.phase)}</div>
                      </div>
                      <RuleStatus status={rule.status} />
                    </div>
                    <div className="mt-2 text-muted-foreground">
                      {freshnessLabel(rule.freshness_days, rule.refresh_trigger)} · {rule.parser_id}
                    </div>
                    {rule.required_for_publish ? <div className="mt-1 text-amber-700 dark:text-amber-300">正式发布硬来源</div> : null}
                    {rule.error ? <div className="mt-1 text-red-700 dark:text-red-300">{rule.error}</div> : null}
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 border-t pt-3 text-xs text-muted-foreground">
            <span>资料截至 {formatDateTime(profile.data_as_of)}</span>
            <span>快照 {profile.profile_snapshot_id.slice(-10)}</span>
            {profile.sources.filter((source) => source.url).map((source) => (
              <a
                key={source.source_id}
                href={source.url || undefined}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 hover:text-foreground"
              >
                {source.title}<ExternalLink className="h-3 w-3" />
              </a>
            ))}
          </div>
        </>
      )}
    </section>
  );
}


function RuleStatus({ status }: { status: string }) {
  if (status === "completed") {
    return <span className="rounded bg-emerald-500/10 px-2 py-0.5 text-emerald-700 dark:text-emerald-300">成功</span>;
  }
  if (status === "completed_with_gaps") {
    return <span className="rounded bg-amber-500/10 px-2 py-0.5 text-amber-700 dark:text-amber-300">部分成功</span>;
  }
  return <span className="rounded bg-red-500/10 px-2 py-0.5 text-red-700 dark:text-red-300">失败</span>;
}


function sourcePhaseLabel(phase: string) {
  return phase === "share_flow" ? "份额与同指数组" : "产品与指数资料";
}


function peerIndexLabel(peer?: ETFProductProfile["peer_group"]): string {
  if (!peer) return "";
  return [peer.tracked_index_name, peer.tracked_index_code].filter(Boolean).join(" · ");
}


function flowBasisLabel(value?: string | null): string {
  if (value === "exchange_market_price") return "市场价×份额变化";
  if (value === "exchange_published_nav_proxy") return "公布净值×份额变化";
  return "暂缺估算价格";
}


function mappingStatusLabel(value: string): string {
  if (value === "official_index_code") return "交易所指数代码";
  if (value === "official_index_name") return "交易所指数名称";
  if (value === "subject_fallback") return "标的本身·待核验";
  return "名称映射·待交叉核验";
}


function freshnessLabel(days: number, trigger: string) {
  if (days <= 0) return "每次显式刷新或生成报告时获取";
  if (trigger === "when_stale_or_explicit_refresh") return `缓存 ${days} 天，过期或显式刷新时获取`;
  return `缓存 ${days} 天，显式刷新或生成报告时校验`;
}


function StatusBadge({ profile }: { profile: ETFProductProfile }) {
  if (profile.stale) return <span className="rounded bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-700">过期缓存</span>;
  if (profile.conflicts.length) return <span className="rounded bg-red-500/10 px-2 py-0.5 text-[11px] text-red-700">冲突</span>;
  if (profile.quality_status !== "passed") return <span className="rounded bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-700">部分缺失</span>;
  return <span className="rounded bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-700">已核验</span>;
}


function FieldGroup({ title, fields }: { title: string; fields: Array<[string, ETFProductField | undefined]> }) {
  return (
    <div className="rounded-md border bg-background/50 p-4">
      <h4 className="text-sm font-semibold">{title}</h4>
      <dl className="mt-3 grid gap-x-4 gap-y-3 sm:grid-cols-2">
        {fields.map(([key, field]) => (
          <div key={key} className={key === "fund_full_name" ? "sm:col-span-2" : ""}>
            <dt className="text-xs text-muted-foreground">{FIELD_LABELS[key] || key}</dt>
            <dd className="mt-0.5 text-sm font-medium">{formatField(field)}</dd>
            {field?.data_as_of ? <div className="mt-0.5 text-[11px] text-muted-foreground">截至 {field.data_as_of}</div> : null}
          </div>
        ))}
      </dl>
    </div>
  );
}


function MetricCard({ fieldKey, field }: { fieldKey: string; field?: ETFProductField }) {
  const unavailable = !field || field.status !== "available" || field.value === null;
  return (
    <div className="rounded-md border bg-background/60 p-3" title={field?.note || field?.semantics}>
      <div className="text-xs text-muted-foreground">{FIELD_LABELS[fieldKey] || fieldKey}</div>
      <div className={`mt-1 text-lg font-semibold ${unavailable ? "text-muted-foreground" : ""}`}>
        {unavailable ? statusLabel(field?.status) : formatField(field)}
      </div>
      <div className="mt-1 text-[11px] text-muted-foreground">
        {field?.note || (field?.data_as_of ? `截至 ${field.data_as_of}` : "暂无可追溯时点")}
      </div>
    </div>
  );
}


function FlowStat({ label, value }: { label: string; value: string }) {
  return <div className="rounded bg-muted/40 p-3"><div className="text-xs text-muted-foreground">{label}</div><div className="mt-1 font-semibold">{value}</div></div>;
}


function Warning({ text }: { text: string }) {
  return <div className="flex items-start gap-2"><AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /><span>{text}</span></div>;
}


function combineIndex(code?: ETFProductField, name?: ETFProductField): ETFProductField | undefined {
  if (!code) return name;
  return { ...code, value: [name?.value, code.value].filter(Boolean).join(" · ") };
}


function formatField(field?: ETFProductField): string {
  if (!field || field.status !== "available" || field.value === null) return statusLabel(field?.status);
  if (typeof field.value === "number") {
    if (field.unit === "ratio") return `${(field.value * 100).toFixed(2)}%`;
    if (field.unit === "CNY") return formatLarge(field.value, "元");
    if (field.unit === "CNY_per_fund_unit") return `${field.value.toFixed(4)} 元/份`;
    if (field.unit === "fund_units") return formatLarge(field.value, "份");
    return field.value.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
  }
  if (Array.isArray(field.value)) return field.value.join("、");
  return String(field.value);
}


function statusLabel(status?: string): string {
  if (status === "conflict") return "冲突";
  if (status === "stale") return "过期";
  return "暂缺";
}


function formatLarge(value: number | null | undefined, unit: string): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "暂缺";
  if (Math.abs(value) >= 100_000_000) return `${(value / 100_000_000).toFixed(2)} 亿${unit}`;
  if (Math.abs(value) >= 10_000) return `${(value / 10_000).toFixed(2)} 万${unit}`;
  return `${value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ${unit}`;
}


function formatSigned(value: number | null | undefined, unit: string): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "暂缺";
  const sign = value > 0 ? "+" : "";
  return `${sign}${formatLarge(value, unit)}`;
}


function formatMoney(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "暂缺";
  const sign = value > 0 ? "+" : "";
  return `${sign}${formatLarge(value, "元")}`;
}


function formatPct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "暂缺";
  return `${(value * 100).toFixed(1)}% 可比`;
}


function flowTone(value: number | null | undefined): string {
  if (!value) return "text-muted-foreground";
  return value > 0 ? "text-emerald-600" : "text-red-600";
}


function formatDateTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("zh-CN", { hour12: false });
}
