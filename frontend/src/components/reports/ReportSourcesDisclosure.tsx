import { useState } from "react";
import { ChevronDown, ChevronUp, ExternalLink, FileCheck2, Loader2 } from "lucide-react";
import { api, type ReportSourceLink } from "@/lib/api";

export function ReportSourcesDisclosure({ reportId }: { reportId: string }) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [sources, setSources] = useState<ReportSourceLink[]>([]);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (!next || loaded || loading) return;
    setLoading(true);
    setError(null);
    try {
      const result = await api.getReportLibrarySources(reportId, 200);
      setSources(result.sources);
      setLoaded(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "报告资料加载失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="mt-3 rounded-md border bg-background/60">
      <button
        type="button"
        onClick={() => void toggle()}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-xs font-medium hover:bg-muted/60"
      >
        <span className="inline-flex items-center gap-2"><FileCheck2 className="h-3.5 w-3.5" /> 本报告使用的资料与证据</span>
        {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : open ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
      </button>
      {open ? (
        <div className="border-t px-3 py-2">
          {error ? <div className="text-xs text-destructive">{error}</div> : null}
          {!loading && !error && sources.length === 0 ? (
            <div className="text-xs text-muted-foreground">该报告没有引用可追溯的外部资料。</div>
          ) : null}
          <div className="divide-y">
            {sources.map((source) => (
              <article key={`${source.document_ref}:${source.relation_type}`} className="py-2 first:pt-0 last:pb-0">
                <div className="flex items-start gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
                      <span className="rounded border px-1.5 py-0.5 text-primary">
                        {source.relation_type === "cited" ? "报告引用" : "结论支撑"}
                      </span>
                      <span className="text-muted-foreground">{source.publisher}</span>
                      {source.section_ids.length ? <span className="text-muted-foreground">章节 {source.section_ids.join("、")}</span> : null}
                    </div>
                    <div className="mt-1 text-xs font-medium">{source.title}</div>
                    <div className="mt-1 text-[10px] text-muted-foreground">
                      Evidence {source.evidence_ids.length} · Fact {source.fact_ids.length} · Claim {source.claim_ids.length}
                    </div>
                  </div>
                  {source.source_url ? (
                    <a href={source.source_url} target="_blank" rel="noreferrer" aria-label={`打开来源：${source.title}`}>
                      <ExternalLink className="h-3.5 w-3.5 text-muted-foreground hover:text-primary" />
                    </a>
                  ) : null}
                </div>
              </article>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
