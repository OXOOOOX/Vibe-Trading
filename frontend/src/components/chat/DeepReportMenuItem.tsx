import { BookOpen, CheckCircle2 } from "lucide-react";
import { useId } from "react";

interface DeepReportMenuItemProps {
  onSelect: () => void;
}

const CAPABILITIES = [
  "上市公司：穿透三张财务报表、财务质量与市值隐含预期",
  "ETF：分析指数规则、暴露结构、流动性、量价与关键持仓",
  "保存 Evidence → Fact → Claim 证据链并输出 Markdown、PDF 与结构监控 JSON",
] as const;

export function DeepReportMenuItem({ onSelect }: DeepReportMenuItemProps) {
  const hintId = useId();

  return (
    <div className="group relative after:absolute after:inset-y-0 after:left-full after:w-3">
      <button
        type="button"
        onClick={onSelect}
        aria-describedby={hintId}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
      >
        <BookOpen className="h-4 w-4" />
        股票 / ETF 穿透式深度研究
      </button>

      <div
        id={hintId}
        role="tooltip"
        className="pointer-events-none invisible absolute bottom-full left-0 z-[60] mb-2 w-[min(20rem,calc(100vw-3rem))] translate-y-1 rounded-xl border border-border/80 bg-background p-4 text-foreground opacity-0 shadow-2xl ring-1 ring-black/10 backdrop-blur-xl transition duration-150 group-hover:visible group-hover:translate-y-0 group-hover:opacity-100 group-focus-within:visible group-focus-within:translate-y-0 group-focus-within:opacity-100 sm:bottom-1/2 sm:left-[calc(100%+0.75rem)] sm:mb-0 sm:translate-x-1 sm:translate-y-1/2 sm:group-hover:translate-x-0 sm:group-hover:translate-y-1/2 sm:group-focus-within:translate-x-0 sm:group-focus-within:translate-y-1/2"
      >
        <span
          aria-hidden="true"
          className="absolute -bottom-1.5 left-5 h-3 w-3 rotate-45 border-b border-r border-border/80 bg-background sm:-left-1.5 sm:bottom-1/2 sm:border-b-0 sm:border-l sm:border-t"
        />
        <div className="flex items-center gap-2">
          <span className="rounded-md bg-cyan-500/10 p-1.5 text-cyan-700 dark:text-cyan-300">
            <BookOpen className="h-4 w-4" />
          </span>
          <div>
            <div className="text-sm font-semibold">什么是穿透式深度研究？</div>
            <div className="mt-0.5 text-[11px] font-medium text-cyan-700 dark:text-cyan-300">
              上市公司或 ETF · 分类型结构 · 可校验报告
            </div>
          </div>
        </div>

        <p className="mt-3 text-xs leading-5 text-muted-foreground">
          系统先识别资产类型，再使用对应的公司或 ETF 研究门控；证据不足的模块会明确降级，不用模型补数字。
        </p>

        <ul className="mt-3 space-y-2">
          {CAPABILITIES.map((capability) => (
            <li key={capability} className="flex gap-2 text-xs leading-5">
              <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
              <span>{capability}</span>
            </li>
          ))}
        </ul>

        <div className="mt-3 rounded-lg border bg-muted/70 px-3 py-2 text-[11px] leading-5 text-muted-foreground">
          <span className="font-semibold text-foreground">没有替代原有深度研究：</span>
          “新建研究目标”仍用于开放式、多轮研究；这里用于生成标准化的单股穿透报告。
        </div>
      </div>
    </div>
  );
}
