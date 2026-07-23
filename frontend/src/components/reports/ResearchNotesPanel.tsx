import { useEffect, useId, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Loader2,
  MessageSquareText,
  RefreshCw,
  ShieldAlert,
  XCircle,
} from "lucide-react";

import { api, type ResearchNote } from "@/lib/api";
import { cn } from "@/lib/utils";

const STATUS = {
  unverified: { label: "待确认", icon: ShieldAlert, style: "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300" },
  confirmed: { label: "已确认", icon: CheckCircle2, style: "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" },
  contradicted: { label: "存在冲突", icon: XCircle, style: "border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-300" },
  superseded: { label: "已被替代", icon: CheckCircle2, style: "border-border bg-muted text-muted-foreground" },
} as const;

type NoteStatus = ResearchNote["derived_status"];
type Filter = "all" | NoteStatus;

const FILTERS: Array<{ value: Filter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "unverified", label: "待确认" },
  { value: "confirmed", label: "已确认" },
  { value: "superseded", label: "已被替代" },
  { value: "contradicted", label: "存在冲突" },
];

export function ResearchNotesPanel({
  subjectKey,
  totalHint = 0,
  confirmedHint = 0,
}: {
  subjectKey: string;
  totalHint?: number;
  confirmedHint?: number;
}) {
  const contentId = `research-notes-${useId().replace(/:/g, "")}`;
  const [expanded, setExpanded] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [notes, setNotes] = useState<ResearchNote[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({
    unverified: 0,
    confirmed: confirmedHint,
    contradicted: 0,
    superseded: 0,
  });
  const [totalCount, setTotalCount] = useState(totalHint);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openNotes, setOpenNotes] = useState<Set<string>>(new Set());

  useEffect(() => {
    setExpanded(false);
    setFilter("all");
    setNotes([]);
    setCounts({ unverified: 0, confirmed: confirmedHint, contradicted: 0, superseded: 0 });
    setTotalCount(totalHint);
    setNextCursor(null);
    setError(null);
    setOpenNotes(new Set());
  }, [subjectKey, totalHint, confirmedHint]);

  async function load(cursor?: string, selectedFilter = filter) {
    setLoading(true);
    setError(null);
    try {
      const result = await api.getReportLibraryResearchNotes(subjectKey, {
        status: selectedFilter === "all" ? undefined : selectedFilter,
        limit: 10,
        cursor,
      });
      setNotes((current) => cursor ? [...current, ...result.notes] : result.notes);
      setCounts(result.counts || {});
      setTotalCount(result.total_count);
      setNextCursor(result.next_cursor || null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "研究笔记加载失败");
      if (!cursor) setNotes([]);
    } finally {
      setLoading(false);
    }
  }

  function toggleExpanded() {
    const next = !expanded;
    setExpanded(next);
    if (next && notes.length === 0 && !loading) void load();
  }

  function selectFilter(next: Filter) {
    setFilter(next);
    setNotes([]);
    setNextCursor(null);
    void load(undefined, next);
  }

  const displayedTotal = expanded ? totalCount : totalHint;
  const displayedConfirmed = expanded ? (counts.confirmed || 0) : confirmedHint;

  return (
    <section className="overflow-hidden rounded-lg border bg-card" aria-labelledby={`${contentId}-title`}>
      <button
        type="button"
        onClick={toggleExpanded}
        aria-expanded={expanded}
        aria-controls={contentId}
        className="flex w-full items-center gap-3 p-4 text-left transition hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-inset"
      >
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
          <MessageSquareText className="h-4 w-4" />
        </span>
        <span className="min-w-0 flex-1">
          <span id={`${contentId}-title`} className="font-semibold">
            研究笔记 · {displayedTotal} 条 · {displayedConfirmed} 条已被正式报告确认
          </span>
          <span className="mt-1 block text-xs text-muted-foreground">展开后按最新时间查看，笔记状态由正式报告证据更新。</span>
        </span>
        <ChevronDown className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform", expanded && "rotate-180")} />
      </button>

      {expanded ? (
        <div id={contentId} className="border-t p-4">
          <div className="flex flex-wrap gap-2" aria-label="研究笔记状态筛选">
            {FILTERS.map((item) => (
              <button
                key={item.value}
                type="button"
                aria-pressed={filter === item.value}
                onClick={() => selectFilter(item.value)}
                className={cn(
                  "rounded-md border px-2.5 py-1.5 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
                  filter === item.value ? "border-primary bg-primary/10 text-primary" : "hover:bg-muted",
                )}
              >
                {item.label}{item.value === "all" ? ` ${Object.values(counts).reduce((sum, value) => sum + value, 0)}` : ` ${counts[item.value] || 0}`}
              </button>
            ))}
          </div>

          {error ? (
            <div role="alert" className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-sm">
              <span className="flex items-center gap-2 text-amber-700 dark:text-amber-300"><AlertTriangle className="h-4 w-4" />{error}</span>
              <button type="button" onClick={() => void load()} className="inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs hover:bg-muted">
                <RefreshCw className="h-3.5 w-3.5" /> 就地重试
              </button>
            </div>
          ) : null}

          {notes.length ? (
            <div className="mt-4 grid gap-2">
              {notes.map((note) => {
                const status = STATUS[note.derived_status] || STATUS.unverified;
                const Icon = status.icon;
                const open = openNotes.has(note.note_claim_id);
                return (
                  <article key={note.note_claim_id} className="rounded-md border bg-background/70 p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={cn("inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px]", status.style)}>
                        <Icon className="h-3 w-3" /> {status.label}
                      </span>
                      <span className="text-[11px] text-muted-foreground">
                        {note.role === "user" ? "用户记录" : "AI 研究记录"} · {formatTime(note.created_at)}
                      </span>
                    </div>
                    <p className={cn("mt-2 whitespace-pre-wrap text-xs leading-5", !open && "line-clamp-3")}>{note.text}</p>
                    {note.text.length > 140 ? (
                      <button
                        type="button"
                        onClick={() => setOpenNotes((current) => {
                          const next = new Set(current);
                          if (open) next.delete(note.note_claim_id); else next.add(note.note_claim_id);
                          return next;
                        })}
                        className="mt-1 text-xs text-primary hover:underline"
                      >
                        {open ? "收起全文" : "展开全文"}
                      </button>
                    ) : null}
                    {note.resolutions.length ? (
                      <details className="mt-2 text-[11px] text-muted-foreground">
                        <summary className="cursor-pointer select-none">关联详情</summary>
                        <div className="mt-1 break-all">正式报告：{note.resolutions.map((item) => item.report_id).join("、")}</div>
                      </details>
                    ) : null}
                  </article>
                );
              })}
            </div>
          ) : !loading && !error ? (
            <div className="mt-4 rounded-md border border-dashed p-4 text-sm text-muted-foreground">
              {filter === "all" ? "暂无研究笔记。" : "当前筛选条件下没有研究笔记。"}
            </div>
          ) : null}

          {loading ? <div className="mt-4 flex items-center justify-center gap-2 py-3 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />加载研究笔记…</div> : null}
          {nextCursor && !loading ? (
            <button type="button" onClick={() => void load(nextCursor)} className="mt-3 w-full rounded-md border py-2 text-sm hover:bg-muted">
              加载更多研究笔记
            </button>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function formatTime(value: string): string {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}
