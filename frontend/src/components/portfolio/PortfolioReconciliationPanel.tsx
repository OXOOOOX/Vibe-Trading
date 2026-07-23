import { useState, type FormEvent } from "react";
import { AlertTriangle, CheckCircle2, GitCompareArrows, Loader2 } from "lucide-react";
import { toast } from "sonner";

import {
  api,
  type PortfolioReconciliation,
} from "@/lib/api";


interface PortfolioReconciliationPanelProps {
  currentRevision: number;
  onCommitted: () => void | Promise<void>;
}


function optionalNumber(value: string): number | undefined {
  if (!value.trim()) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}


export default function PortfolioReconciliationPanel({
  currentRevision,
  onCommitted,
}: PortfolioReconciliationPanelProps) {
  const [rawText, setRawText] = useState("");
  const [cash, setCash] = useState("");
  const [brokerPnl, setBrokerPnl] = useState("");
  const [sourceLabel, setSourceLabel] = useState("券商持仓快照");
  const [preview, setPreview] = useState<PortfolioReconciliation | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [committing, setCommitting] = useState(false);
  const [confirmed, setConfirmed] = useState(false);

  const buildPreview = async (event: FormEvent) => {
    event.preventDefault();
    if (!rawText.trim()) {
      toast.error("请先粘贴券商持仓表。");
      return;
    }
    setPreviewing(true);
    setConfirmed(false);
    try {
      const result = await api.previewPortfolioReconciliation({
        raw_text: rawText,
        cash: optionalNumber(cash),
        broker_reported_pnl: optionalNumber(brokerPnl),
        cash_currency: "CNY",
        source_label: sourceLabel.trim() || "broker_snapshot",
      });
      setPreview(result);
      toast.success("已生成对账预览，现有持仓尚未修改。");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "生成对账预览失败");
    } finally {
      setPreviewing(false);
    }
  };

  const commit = async () => {
    if (!preview || !confirmed) return;
    setCommitting(true);
    try {
      await api.commitPortfolioReconciliation(
        preview.reconciliation_id,
        preview.base_revision,
      );
      toast.success("券商对账结果已提交，账本版本已更新。");
      setPreview(null);
      setConfirmed(false);
      setRawText("");
      await onCommitted();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "提交对账失败");
    } finally {
      setCommitting(false);
    }
  };

  return (
    <section className="rounded-md border border-amber-500/30 bg-amber-500/[0.03] p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <GitCompareArrows className="h-4 w-4 text-amber-600" />
            券商持仓对账
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            当前账本版本 {currentRevision}。先生成差异预览，确认后才会切换权威持仓；系统不会猜测缺失成交或费用。
          </p>
        </div>
        <span className="rounded border px-2 py-1 text-[11px] text-muted-foreground">
          SQLite 权威账本 / JSON 兼容投影
        </span>
      </div>

      <form onSubmit={buildPreview} className="mt-4 grid gap-3 lg:grid-cols-[minmax(0,1fr)_260px]">
        <textarea
          value={rawText}
          onChange={(event) => setRawText(event.target.value)}
          rows={7}
          className="w-full resize-y rounded-md border bg-background px-3 py-2 text-sm"
          placeholder="粘贴券商持仓表：名称 代码 数量 成本价……"
          aria-label="券商持仓表"
        />
        <div className="grid content-start gap-2">
          <input
            value={sourceLabel}
            onChange={(event) => setSourceLabel(event.target.value)}
            className="rounded-md border bg-background px-3 py-2 text-sm"
            placeholder="来源标签"
            aria-label="券商来源标签"
          />
          <input
            value={cash}
            onChange={(event) => setCash(event.target.value)}
            type="number"
            step="any"
            min="0"
            className="rounded-md border bg-background px-3 py-2 text-sm"
            placeholder="券商现金（可选）"
            aria-label="券商现金"
          />
          <input
            value={brokerPnl}
            onChange={(event) => setBrokerPnl(event.target.value)}
            type="number"
            step="any"
            className="rounded-md border bg-background px-3 py-2 text-sm"
            placeholder="券商报告盈亏（可选）"
            aria-label="券商报告盈亏"
          />
          <button
            type="submit"
            disabled={previewing}
            className="inline-flex items-center justify-center gap-2 rounded-md bg-amber-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {previewing ? <Loader2 className="h-4 w-4 animate-spin" /> : <GitCompareArrows className="h-4 w-4" />}
            生成差异预览
          </button>
        </div>
      </form>

      {preview ? (
        <div className="mt-4 space-y-3 rounded-md border bg-background p-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
            <span>预览号：{preview.reconciliation_id}</span>
            <span>基于版本：{preview.base_revision}</span>
          </div>
          {preview.preview.holding_diffs.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-muted-foreground">
                  <tr><th className="py-1">标的</th><th>状态</th><th>数量</th><th>成本</th></tr>
                </thead>
                <tbody>
                  {preview.preview.holding_diffs.map((diff) => (
                    <tr key={diff.symbol} className="border-t">
                      <td className="py-1.5 font-mono">{diff.symbol}</td>
                      <td>{diff.status}</td>
                      <td>{String(diff.changes.quantity?.current ?? "—")} → {String(diff.changes.quantity?.broker ?? "—")}</td>
                      <td>{String(diff.changes.cost_price?.current ?? "—")} → {String(diff.changes.cost_price?.broker ?? "—")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="flex items-center gap-2 text-xs text-emerald-600">
              <CheckCircle2 className="h-4 w-4" /> 持仓数量和成本未发现差异。
            </p>
          )}
          {preview.preview.suspicious_events.length ? (
            <p className="flex items-start gap-2 rounded border border-amber-500/30 bg-amber-500/10 p-2 text-xs">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
              发现 {preview.preview.suspicious_events.length} 条取消尝试候选。系统仅标记待核对，不会自动删除或反推缺失买入。
            </p>
          ) : null}
          {preview.preview.broker_reported_pnl != null ? (
            <p className="text-xs text-muted-foreground">
              券商报告盈亏：{preview.preview.broker_reported_pnl}；
              未解释差额：{preview.preview.unexplained_pnl ?? "无法由现有流水精确计算"}。
            </p>
          ) : null}
          <label className="flex items-start gap-2 text-xs">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
              className="mt-0.5"
            />
            我已核对券商快照，确认以预览中的数量、券商调整成本和现金替换当前持仓事实。
          </label>
          <button
            type="button"
            disabled={!confirmed || committing}
            onClick={() => void commit()}
            className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            {committing ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
            确认并提交对账
          </button>
        </div>
      ) : null}
    </section>
  );
}
