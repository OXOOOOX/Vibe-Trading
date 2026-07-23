import { useId, useState, type ReactNode } from "react";
import { ChevronDown, Files } from "lucide-react";

import { cn } from "@/lib/utils";

interface Props {
  subjectName: string;
  subjectKey?: string | null;
  reportCount: number;
  latestLabel: string;
  badges?: string[];
  onExpandedChange?: (expanded: boolean) => void;
  children: ReactNode;
}

export function ReportSubjectGroup({
  subjectName,
  subjectKey,
  reportCount,
  latestLabel,
  badges = [],
  onExpandedChange,
  children,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const generatedId = useId().replace(/:/g, "");
  const contentId = `report-subject-${generatedId}`;
  const displayName = subjectKey && subjectKey !== subjectName
    ? `${subjectName}（${subjectKey}）`
    : subjectName;

  return (
    <section className="overflow-hidden rounded-lg border bg-card/70 transition hover:border-primary/30">
      <button
        type="button"
        onClick={() => setExpanded((current) => {
          const next = !current;
          onExpandedChange?.(next);
          return next;
        })}
        aria-expanded={expanded}
        aria-controls={contentId}
        aria-label={`${expanded ? "收起" : "展开"}${displayName}的 ${reportCount} 份报告`}
        className="flex w-full items-center gap-3 px-4 py-3.5 text-left transition hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-inset"
      >
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
          <Files className="h-4 w-4" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="truncate font-semibold">{subjectName}</span>
            {subjectKey && subjectKey !== subjectName ? (
              <span className="font-mono text-xs text-muted-foreground">{subjectKey}</span>
            ) : null}
          </span>
          <span className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <span>{latestLabel}</span>
            {badges.map((badge) => (
              <span key={badge} className="rounded bg-primary/5 px-1.5 py-0.5 text-primary">
                {badge}
              </span>
            ))}
          </span>
        </span>
        <span className="shrink-0 rounded border px-2.5 py-1 text-xs text-muted-foreground">
          {reportCount} 份
        </span>
        <ChevronDown
          className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform", expanded && "rotate-180")}
        />
      </button>

      {expanded ? (
        <div
          id={contentId}
          className="grid gap-3 border-t bg-background/45 p-3 [content-visibility:auto]"
        >
          {children}
        </div>
      ) : null}
    </section>
  );
}
