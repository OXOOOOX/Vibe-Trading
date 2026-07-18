import type { EquityResolutionOption } from "@/lib/api";

interface DeepReportEquityPickerProps {
  candidates: EquityResolutionOption[];
  disabled?: boolean;
  onConfirm: (candidate: EquityResolutionOption) => void;
}

export function DeepReportEquityPicker({
  candidates,
  disabled = false,
  onConfirm,
}: DeepReportEquityPickerProps) {
  if (candidates.length === 0) return null;

  return (
    <section
      aria-label="上市公司候选"
      className="rounded-xl border border-cyan-500/25 bg-cyan-500/5 p-3"
    >
      <div className="mb-2 text-sm font-medium text-foreground">请选择正确的上市公司</div>
      <p className="mb-3 text-xs text-muted-foreground">
        已按名称和简称进行模糊搜索。确认后将使用对应股票代码开始穿透式深度研究。
      </p>
      <div className="grid gap-2 sm:grid-cols-2">
        {candidates.map((candidate) => (
          <button
            key={candidate.symbol}
            type="button"
            disabled={disabled}
            onClick={() => onConfirm(candidate)}
            aria-label={`确认并研究 ${candidate.security_name}（${candidate.symbol}）`}
            className="flex items-center justify-between gap-3 rounded-lg border bg-background px-3 py-2 text-left transition-colors hover:border-cyan-500/50 hover:bg-cyan-500/5 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <span className="min-w-0">
              <span className="block truncate text-sm font-medium text-foreground">
                {candidate.security_name}
              </span>
              <span className="block font-mono text-xs text-muted-foreground">
                {candidate.symbol}
              </span>
            </span>
            <span className="shrink-0 text-xs font-medium text-cyan-700 dark:text-cyan-300">
              确认并开始
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}
