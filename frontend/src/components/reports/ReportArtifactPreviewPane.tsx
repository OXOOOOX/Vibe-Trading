import {
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  AlertCircle,
  Download,
  FileText,
  Loader2,
  RefreshCw,
  Send,
  X,
} from "lucide-react";

import { api, type FeishuDeliverySettings } from "@/lib/api";
import { cn } from "@/lib/utils";
import { ReportMarkdownContent } from "@/components/reports/ReportMarkdownContent";

export type ReportArtifactSource = "report_library" | "deep_report" | "run";

export interface ReportPreviewArtifact {
  artifactId: string;
  label: string;
  filename: string;
  mediaType: string;
  previewUrl: string;
  downloadUrl: string;
}

export interface ReportArtifactPreviewTarget {
  source: ReportArtifactSource;
  reportId: string;
  title: string;
  subtitle?: string;
  initialArtifactId?: string;
  artifacts: ReportPreviewArtifact[];
}

interface Props {
  target: ReportArtifactPreviewTarget;
  onClose: () => void;
}

interface SplitLayoutProps {
  target: ReportArtifactPreviewTarget | null;
  onClose: () => void;
  children: ReactNode;
}

const DEFAULT_LEFT_PERCENT = 54;
const MIN_LEFT_PERCENT = 34;
const MAX_LEFT_PERCENT = 68;
const MIN_PANE_WIDTH_PX = 360;
const SPLIT_STORAGE_KEY = "vibe-report-preview-split-v1";

function splitBounds(width: number): { min: number; max: number } {
  if (!Number.isFinite(width) || width <= 0) {
    return { min: MIN_LEFT_PERCENT, max: MAX_LEFT_PERCENT };
  }
  const paneMinimum = (MIN_PANE_WIDTH_PX / width) * 100;
  const min = Math.max(MIN_LEFT_PERCENT, paneMinimum);
  const max = Math.min(MAX_LEFT_PERCENT, 100 - paneMinimum);
  return min <= max ? { min, max } : { min: 50, max: 50 };
}

function clampLeftPercent(value: number, width = 0): number {
  const { min, max } = splitBounds(width);
  return Math.min(max, Math.max(min, value));
}

function readStoredLeftPercent(): number {
  if (typeof window === "undefined") return DEFAULT_LEFT_PERCENT;
  try {
    const raw = window.localStorage.getItem(SPLIT_STORAGE_KEY);
    if (raw === null) return DEFAULT_LEFT_PERCENT;
    const stored = Number(raw);
    return Number.isFinite(stored)
      ? clampLeftPercent(stored)
      : DEFAULT_LEFT_PERCENT;
  } catch {
    return DEFAULT_LEFT_PERCENT;
  }
}

export function ReportPreviewSplitLayout({ target, onClose, children }: SplitLayoutProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const resizingRef = useRef(false);
  const [leftPercent, setLeftPercent] = useState(readStoredLeftPercent);
  const [resizing, setResizing] = useState(false);
  const previewPercent = 100 - leftPercent;

  useEffect(() => {
    try {
      window.localStorage.setItem(SPLIT_STORAGE_KEY, String(leftPercent));
    } catch {
      // The split still works when storage is unavailable.
    }
  }, [leftPercent]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      const width = entry?.contentRect.width || container.getBoundingClientRect().width;
      setLeftPercent((value) => clampLeftPercent(value, width));
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!resizing) return;
    const previousCursor = document.body.style.cursor;
    const previousSelection = document.body.style.userSelect;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    return () => {
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousSelection;
    };
  }, [resizing]);

  function updateFromPointer(clientX: number) {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) return;
    setLeftPercent(clampLeftPercent(((clientX - rect.left) / rect.width) * 100, rect.width));
  }

  function handlePointerDown(event: PointerEvent<HTMLDivElement>) {
    resizingRef.current = true;
    setResizing(true);
    event.currentTarget.setPointerCapture?.(event.pointerId);
    updateFromPointer(event.clientX);
  }

  function handlePointerMove(event: PointerEvent<HTMLDivElement>) {
    if (!resizingRef.current) return;
    updateFromPointer(event.clientX);
  }

  function handlePointerEnd(event: PointerEvent<HTMLDivElement>) {
    resizingRef.current = false;
    setResizing(false);
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const width = containerRef.current?.getBoundingClientRect().width || 0;
    let next: number | null = null;
    if (event.key === "ArrowLeft") next = leftPercent - 2;
    if (event.key === "ArrowRight") next = leftPercent + 2;
    if (event.key === "Home") next = splitBounds(width).min;
    if (event.key === "End") next = splitBounds(width).max;
    if (next === null) return;
    event.preventDefault();
    setLeftPercent(clampLeftPercent(next, width));
  }

  return (
    <div
      ref={containerRef}
      data-testid="report-preview-split"
      className="relative flex h-full min-h-0 w-full overflow-hidden"
    >
      <div className="min-w-0 flex-1 overflow-hidden">{children}</div>
      {target ? (
        <>
          <div
            role="separator"
            aria-label="调整报告列表与预览宽度"
            aria-orientation="vertical"
            aria-valuemin={MIN_LEFT_PERCENT}
            aria-valuemax={MAX_LEFT_PERCENT}
            aria-valuenow={Math.round(leftPercent)}
            aria-valuetext={`报告列表 ${Math.round(leftPercent)}%，预览 ${Math.round(previewPercent)}%`}
            tabIndex={0}
            title="拖动调整宽度，双击恢复默认比例"
            onDoubleClick={() => setLeftPercent(clampLeftPercent(DEFAULT_LEFT_PERCENT, containerRef.current?.getBoundingClientRect().width || 0))}
            onKeyDown={handleKeyDown}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={handlePointerEnd}
            onPointerCancel={handlePointerEnd}
            className={cn(
              "group relative z-20 hidden w-2 shrink-0 touch-none cursor-col-resize items-center justify-center bg-background outline-none lg:flex",
              "focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary",
              resizing && "bg-primary/10",
            )}
          >
            <span className={cn(
              "h-full w-px bg-border transition-colors group-hover:bg-primary/70",
              resizing && "w-0.5 bg-primary",
            )} />
          </div>
          <div
            data-testid="report-preview-region"
            className="contents lg:block lg:h-full lg:min-w-0 lg:shrink-0"
            style={{ width: `${previewPercent}%` }}
          >
            <ReportArtifactPreviewPane target={target} onClose={onClose} />
          </div>
        </>
      ) : null}
    </div>
  );
}

function isPdf(artifact: ReportPreviewArtifact): boolean {
  return artifact.mediaType.toLowerCase().includes("pdf")
    || artifact.filename.toLowerCase().endsWith(".pdf");
}

function isJson(artifact: ReportPreviewArtifact): boolean {
  return artifact.mediaType.toLowerCase().includes("json")
    || artifact.filename.toLowerCase().endsWith(".json");
}

export function ReportArtifactPreviewPane({ target, onClose }: Props) {
  const asideRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  const initialArtifactId = target.initialArtifactId || target.artifacts[0]?.artifactId || "";
  const [selectedId, setSelectedId] = useState(initialArtifactId);
  const [markdown, setMarkdown] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [sending, setSending] = useState(false);
  const [sendStatus, setSendStatus] = useState<string | null>(null);
  const [deliverySettings, setDeliverySettings] = useState<FeishuDeliverySettings | null>(null);
  const [deliveryLoading, setDeliveryLoading] = useState(true);
  const [deliveryError, setDeliveryError] = useState<string | null>(null);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const mobile = window.matchMedia("(max-width: 1023px)").matches;
    if (mobile) window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (!mobile) return;
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(asideRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]),a[href],select:not([disabled]),[tabindex]:not([tabindex="-1"])',
      ) || []);
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      if (mobile) restoreFocusRef.current?.focus();
    };
  }, [target.reportId]);

  useEffect(() => {
    let active = true;
    setDeliveryLoading(true);
    setDeliveryError(null);
    void api.getFeishuDeliverySettings()
      .then((settings) => {
        if (active) setDeliverySettings(settings);
      })
      .catch((reason: unknown) => {
        if (active) {
          setDeliverySettings(null);
          setDeliveryError(reason instanceof Error ? reason.message : "飞书目标加载失败");
        }
      })
      .finally(() => {
        if (active) setDeliveryLoading(false);
      });
    return () => { active = false; };
  }, [target.reportId]);

  useEffect(() => {
    setSelectedId(target.initialArtifactId || target.artifacts[0]?.artifactId || "");
    setSendStatus(null);
  }, [target.initialArtifactId, target.reportId, target.source]);

  const selected = useMemo(
    () => target.artifacts.find((artifact) => artifact.artifactId === selectedId)
      || target.artifacts[0],
    [selectedId, target.artifacts],
  );

  useEffect(() => {
    let active = true;
    setMarkdown(null);
    setError(null);
    setSendStatus(null);
    if (!selected || isPdf(selected)) {
      setLoading(false);
      return () => { active = false; };
    }

    setLoading(true);
    const contentPromise = target.source === "run"
      ? api.getRunReportPreview(target.reportId).then((value) => value.content)
      : fetch(selected.previewUrl).then(async (response) => {
        if (!response.ok) throw new Error(`报告预览加载失败（HTTP ${response.status}）`);
        return response.text();
      });

    void contentPromise
      .then((content) => {
        if (!active) return;
        if (selected && isJson(selected)) {
          try {
            setMarkdown(`\`\`\`json\n${JSON.stringify(JSON.parse(content), null, 2)}\n\`\`\``);
          } catch {
            setMarkdown(`\`\`\`json\n${content}\n\`\`\``);
          }
        } else {
          setMarkdown(content);
        }
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "报告预览加载失败");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [reloadToken, selected, target.reportId, target.source]);

  async function sendToFeishu() {
    if (!selected || sending) return;
    setSending(true);
    setSendStatus(null);
    try {
      const result = await api.sendReportArtifactToFeishu({
        source: target.source,
        report_id: target.reportId,
        artifact_id: selected.artifactId,
      });
      setSendStatus(`已发送到${result.target_name || "飞书默认目标"}：${result.filename}`);
    } catch (reason) {
      setSendStatus(reason instanceof Error ? reason.message : "发送到飞书失败");
    } finally {
      setSending(false);
    }
  }

  const effectiveTarget = deliverySettings?.targets.find(
    (item) => item.target_id === deliverySettings.effective_target_id,
  );
  const targetLabel = effectiveTarget
    ? `飞书 · ${effectiveTarget.chat_type === "group" ? "群聊" : "私聊"} · …${effectiveTarget.chat_id.slice(-6)}`
    : null;
  const deliveryUnavailable = !deliveryLoading && (!deliverySettings?.effective_target_id || Boolean(deliveryError));

  return (
    <>
      <button
        type="button"
        aria-label="关闭报告预览"
        onClick={onClose}
        className="fixed inset-0 z-40 bg-background/75 backdrop-blur-sm lg:hidden"
      />
      <aside
        ref={asideRef}
        role="dialog"
        aria-modal="true"
        aria-label="报告附件预览"
        className="fixed inset-y-0 right-0 z-50 flex w-[min(96vw,52rem)] flex-col border-l bg-background shadow-2xl lg:static lg:z-auto lg:h-full lg:w-full lg:min-w-0 lg:shadow-none"
      >
        <header className="border-b px-4 py-3">
          <div className="flex items-start gap-3">
            <div className="mt-0.5 rounded-lg bg-cyan-500/10 p-2 text-cyan-700 dark:text-cyan-300">
              <FileText className="h-4 w-4" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="text-[11px] font-medium text-cyan-700 dark:text-cyan-300">报告预览</div>
              <h2 className="mt-0.5 truncate text-sm font-semibold" title={target.title}>{target.title}</h2>
              <div className="mt-1 truncate text-[10px] text-muted-foreground" title={target.subtitle || selected?.filename}>
                {target.subtitle || selected?.filename}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              {selected ? (
                <a
                  href={selected.downloadUrl}
                  download={selected.filename}
                  aria-label={`下载 ${selected.filename}`}
                  title="下载当前文件"
                  className="rounded-md p-2 text-muted-foreground transition hover:bg-muted hover:text-foreground"
                >
                  <Download className="h-4 w-4" />
                </a>
              ) : null}
              <button
                type="button"
                onClick={() => void sendToFeishu()}
                disabled={!selected || sending || deliveryLoading || deliveryUnavailable}
                aria-label="一键发送到飞书"
                title={targetLabel ? `发送到：${targetLabel}` : "请先设置飞书发送目标"}
                className="rounded-md p-2 text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:opacity-50"
              >
                {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
              </button>
              <button
                ref={closeButtonRef}
                type="button"
                onClick={onClose}
                aria-label="关闭报告预览"
                title="关闭预览"
                className="rounded-md p-2 text-muted-foreground transition hover:bg-muted hover:text-foreground"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
            {deliveryLoading ? <span className="inline-flex items-center gap-1"><Loader2 className="h-3 w-3 animate-spin" />正在确认飞书发送目标</span> : null}
            {targetLabel ? <span>发送到：{targetLabel}</span> : null}
            {deliveryUnavailable ? <><span className="text-amber-700 dark:text-amber-300">{deliverySettings?.requires_selection ? "多个目标尚未设置默认值" : deliveryError || "尚未绑定飞书发送目标"}</span><a href="/settings" className="font-medium text-primary hover:underline">前往全局设置</a></> : null}
          </div>
          {target.artifacts.length > 1 ? (
            <nav aria-label="报告文件格式" className="mt-3 flex flex-wrap gap-1.5">
              {target.artifacts.map((artifact) => (
                <button
                  key={artifact.artifactId}
                  type="button"
                  aria-pressed={artifact.artifactId === selected?.artifactId}
                  onClick={() => setSelectedId(artifact.artifactId)}
                  className={cn(
                    "rounded-md border px-2.5 py-1.5 text-xs font-medium transition",
                    artifact.artifactId === selected?.artifactId
                      ? "border-primary bg-primary/10 text-primary"
                      : "hover:bg-muted",
                  )}
                >
                  {artifact.label}
                </button>
              ))}
            </nav>
          ) : null}
          {sendStatus ? (
            <div role="status" className="mt-2 rounded-md bg-muted/60 px-2.5 py-1.5 text-xs text-muted-foreground">
              {sendStatus}
            </div>
          ) : null}
        </header>

        <div className="min-h-0 flex-1 overflow-hidden bg-muted/10">
          {!selected ? (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">没有可预览的 Markdown 或 PDF</div>
          ) : isPdf(selected) ? (
            <iframe
              key={selected.previewUrl}
              src={selected.previewUrl}
              title={`${target.title} PDF 预览`}
              className="h-full min-h-[32rem] w-full bg-white"
            />
          ) : (
            <div className="h-full overflow-auto px-5 py-5">
              {loading ? (
                <div className="flex min-h-48 items-center justify-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin text-primary" /> 正在加载 Markdown…
                </div>
              ) : null}
              {error ? (
                <div role="alert" className="mx-auto mt-10 max-w-md rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm">
                  <div className="flex items-start gap-2 text-destructive">
                    <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                    <span>{error}</span>
                  </div>
                  <button
                    type="button"
                    onClick={() => setReloadToken((value) => value + 1)}
                    className="mt-3 inline-flex items-center gap-1.5 rounded-md border bg-background px-2.5 py-1.5 text-xs font-medium hover:bg-muted"
                  >
                    <RefreshCw className="h-3 w-3" /> 重新加载
                  </button>
                </div>
              ) : null}
              {markdown !== null && !loading ? (
                <ReportMarkdownContent content={markdown} />
              ) : null}
            </div>
          )}
        </div>
      </aside>
    </>
  );
}
