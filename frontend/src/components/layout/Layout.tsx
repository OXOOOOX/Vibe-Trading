import { useTranslation } from "react-i18next";
import { useEffect, useState } from "react";
import { Link, Outlet, useLocation, useSearchParams } from "react-router-dom";
import { Activity, BarChart3, Bot, BriefcaseBusiness, FileText, Languages, Moon, Sun, Plus, Trash2, Pencil, MessageSquare, ChevronsLeft, ChevronsRight, Settings, Layers, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useDarkMode } from "@/hooks/useDarkMode";
import { api, type SessionItem } from "@/lib/api";
import { useAgentStore } from "@/stores/agent";
import { ConnectionBanner } from "@/components/layout/ConnectionBanner";

// Bump on each release; one place keeps the footer in sync with package.json.
const APP_VERSION = "v0.1.10";

export function Layout() {
  const { t, i18n: i18nHook } = useTranslation();
  const isChinese = i18nHook.resolvedLanguage?.startsWith("zh") ?? i18nHook.language.startsWith("zh");

  const switchLanguage = async () => {
    const nextLanguage = isChinese ? "en" : "zh-CN";
    await i18nHook.changeLanguage(nextLanguage);
    document.documentElement.lang = nextLanguage;
    // Several legacy screens call the i18n singleton outside React hooks.
    // Reload after persisting the choice so those modules initialize in the new language.
    window.location.reload();
  };

  const NAV = [
    { to: "/", icon: BarChart3, label: t('layout.home') },
    { to: "/agent", icon: Bot, label: t('layout.agent') },
    { to: "/runtime", icon: Activity, label: t('layout.runtime') },
    { to: "/portfolio", icon: BriefcaseBusiness, label: isChinese ? "组合/持仓" : "Portfolio" },
    { to: "/reports", icon: FileText, label: t('layout.reports') },
    { to: "/alpha-zoo", icon: Layers, label: t('layout.alphaZoo') },
    { to: "/settings", icon: Settings, label: t('layout.settings') },
    { to: "/correlation", icon: BarChart3, label: t('layout.correlation') },
  ];
  const { pathname } = useLocation();
  const [searchParams] = useSearchParams();
  const { dark, toggle } = useDarkMode();
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const sseStatus = useAgentStore(s => s.sseStatus);
  const sseRetryAttempt = useAgentStore(s => s.sseRetryAttempt);
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("qa-sidebar") === "collapsed");

  const activeSessionId = searchParams.get("session");
  const streamingSessionId = useAgentStore(s => s.streamingSessionId);

  useEffect(() => {
    localStorage.setItem("qa-sidebar", collapsed ? "collapsed" : "expanded");
  }, [collapsed]);

  const loadSessions = () => {
    api.listSessions()
      .then((list) => setSessions(Array.isArray(list) ? list : []))
      .catch(() => {})
      .finally(() => setSessionsLoading(false));
  };

  // Load sessions on mount. Also refresh when navigating TO /agent or when
  // the active session changes (covers new session creation from Agent).
  const isAgentPage = pathname.startsWith("/agent");
  useEffect(() => { loadSessions(); }, [isAgentPage, activeSessionId]);

  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const deleteSession = async (sid: string) => {
    try {
      await api.deleteSession(sid);
      setSessions((prev) => prev.filter((s) => s.session_id !== sid));
    } catch { /* ignore */ }
    setDeleteTarget(null);
  };

  const renameSession = async (sid: string) => {
    if (!renameValue.trim()) { setRenameTarget(null); return; }
    try {
      await api.renameSession(sid, renameValue.trim());
      setSessions((prev) => prev.map((s) => s.session_id === sid ? { ...s, title: renameValue.trim() } : s));
    } catch { /* ignore */ }
    setRenameTarget(null);
  };

  return (
    <div className="flex h-screen bg-background">
      {/* Sidebar */}
      <aside className={cn(
        "flex w-12 shrink-0 flex-col border-r bg-card transition-all duration-200",
        collapsed ? "md:w-12" : "md:w-64"
      )}>
        {/* Brand */}
        <div className={cn("flex justify-center border-b p-2", !collapsed && "md:block md:p-4")}>
          <Link to="/" className={cn("flex items-center justify-center text-base font-bold tracking-tight", !collapsed && "md:justify-start md:gap-2")} title="Vibe-Trading">
            <BarChart3 className="h-5 w-5 text-primary shrink-0" />
            {!collapsed ? <span className="hidden md:inline">Vibe-Trading</span> : null}
          </Link>
        </div>

        {/* Nav */}
        <nav className={cn("space-y-0.5 p-1", !collapsed && "md:p-2")}>
          {NAV.map(({ to, icon: Icon, label }) => {
            const text = label;
            return (
              <Link
                key={to}
                to={to}
                className={cn(
                  "flex items-center rounded-md text-sm transition-colors",
                  "justify-center p-2",
                  !collapsed && "md:justify-start md:gap-3 md:px-3 md:py-2",
                  (to === "/" ? pathname === "/" : pathname.startsWith(to))
                    ? "bg-primary/10 text-primary font-medium"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground"
                )}
                title={text}
              >
                <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                {!collapsed ? <span className="hidden md:inline">{text}</span> : null}
              </Link>
            );
          })}
        </nav>

        {/* Sessions — hidden when collapsed */}
        {!collapsed && (
          <div className="mt-2 hidden flex-1 flex-col overflow-auto border-t md:flex">
            <div className="flex items-center justify-between px-4 py-2">
              <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                <MessageSquare className="h-3.5 w-3.5" />
                {t('layout.sessions')}
              </span>
              <Link
                to="/agent"
                className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
                title={t('layout.newChat')}
              >
                <Plus className="h-3.5 w-3.5" />
              </Link>
            </div>

            <div className="px-2 pb-2 space-y-0.5 overflow-auto flex-1">
              {sessionsLoading ? (
                <div className="space-y-1.5 px-2 py-1">
                  {[1, 2, 3].map((i) => (
                    <div key={i} className="h-7 rounded-md bg-muted/50 animate-pulse" />
                  ))}
                </div>
              ) : sessions.length === 0 ? (
                <p className="px-3 py-2 text-xs text-muted-foreground/60">{t('layout.noSessions')}</p>
              ) : null}
              {sessions.map((s) => {
                const isActive = s.session_id === activeSessionId;
                const isDeleting = deleteTarget === s.session_id;
                const isRenaming = renameTarget === s.session_id;
                return (
                  <div key={s.session_id} className="group relative flex items-center">
                    {isRenaming ? (
                      <input
                        autoFocus
                        value={renameValue}
                        onChange={(e) => setRenameValue(e.target.value)}
                        onKeyDown={(e) => { if (e.key === "Enter") renameSession(s.session_id); if (e.key === "Escape") setRenameTarget(null); }}
                        onBlur={() => renameSession(s.session_id)}
                        className="flex-1 min-w-0 pl-3 pr-2 py-1 rounded-md text-xs border border-primary bg-background outline-none"
                      />
                    ) : (
                      <Link
                        to={`/agent?session=${s.session_id}`}
                        className={cn(
                          "flex-1 min-w-0 pl-3 pr-14 py-1.5 rounded-md text-xs transition-colors truncate block border-l-2",
                          isActive
                            ? "border-l-primary bg-primary/10 text-primary font-medium"
                            : "border-l-transparent text-muted-foreground hover:bg-muted hover:text-foreground"
                        )}
                        title={s.title || s.session_id}
                      >
                        <span className="flex items-center gap-1.5">
                          {streamingSessionId === s.session_id ? (
                            <Loader2 className="h-3 w-3 shrink-0 animate-spin text-primary" />
                          ) : (
                            <span className={cn(
                              "h-1.5 w-1.5 rounded-full shrink-0",
                              isActive ? "bg-primary/70" : "bg-muted-foreground/40"
                            )} />
                          )}
                          {s.title || s.session_id.slice(0, 16)}
                        </span>
                      </Link>
                    )}
                    {!isRenaming && isDeleting ? (
                      <div className="absolute right-0.5 flex items-center gap-0.5">
                        <button onClick={() => deleteSession(s.session_id)} className="p-1 text-danger hover:bg-danger/10 rounded text-[10px] font-medium">{t('layout.confirm')}</button>
                        <button onClick={() => setDeleteTarget(null)} className="p-1 text-muted-foreground hover:bg-muted rounded text-[10px]">{t('layout.cancel')}</button>
                      </div>
                    ) : !isRenaming ? (
                      <div className="absolute right-1 opacity-0 group-hover:opacity-100 flex items-center gap-0.5 transition-opacity">
                        <button
                          onClick={(e) => { e.preventDefault(); e.stopPropagation(); setRenameTarget(s.session_id); setRenameValue(s.title || ""); }}
                          className="p-1 text-muted-foreground hover:text-foreground rounded"
                          title={t('layout.rename')}
                        >
                          <Pencil className="h-3 w-3" />
                        </button>
                        <button
                          onClick={(e) => { e.preventDefault(); e.stopPropagation(); setDeleteTarget(s.session_id); }}
                          className="p-1 text-muted-foreground hover:text-danger rounded"
                          title={t('layout.delete')}
                        >
                          <Trash2 className="h-3 w-3" />
                        </button>
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Spacer when collapsed */}
        {collapsed && <div className="flex-1" />}
        {!collapsed ? <div className="flex-1 md:hidden" /> : null}

        <div className="flex flex-col items-center gap-1 border-t p-1 md:hidden">
          <button onClick={toggle} className="p-1.5 text-muted-foreground transition-colors hover:text-foreground" title={dark ? t('layout.light') : t('layout.dark')}>
            {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
          </button>
          <button onClick={switchLanguage} className="p-1.5 text-muted-foreground transition-colors hover:text-foreground" title={isChinese ? "English" : "中文"}>
            <Languages className="h-3.5 w-3.5" />
          </button>
        </div>

        {/* Footer */}
        <div className={cn("hidden border-t", collapsed ? "md:flex md:flex-col md:items-center md:gap-1 md:p-1" : "md:block md:space-y-2 md:p-3")}>
          {collapsed ? (
            <>
              <button onClick={toggle} className="p-1.5 text-muted-foreground hover:text-foreground rounded transition-colors" title={dark ? t('layout.light') : t('layout.dark')}>
                {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
              </button>
              <button onClick={() => setCollapsed(false)} className="p-1.5 text-muted-foreground hover:text-foreground rounded transition-colors" title={t('layout.expand')}>
                <ChevronsRight className="h-3.5 w-3.5" />
              </button>
            </>
          ) : (
            <>
              <div className="flex items-center justify-between">
                <button
                  onClick={toggle}
                  className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
                >
                  {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
                  {dark ? t('layout.light') : t('layout.dark')}
                </button>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => setCollapsed(true)}
                    className="p-1 text-muted-foreground hover:text-foreground rounded transition-colors"
                    title={t('layout.collapse')}
                  >
                    <ChevronsLeft className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
              <div className="flex items-center justify-between">
                <button
                  onClick={switchLanguage}
                  className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
                >
                  <Languages className="h-3.5 w-3.5" />
                  {isChinese ? "English" : "中文"}
                </button>
                <p className="text-xs text-muted-foreground/60">{APP_VERSION}</p>
              </div>
            </>
          )}
        </div>
      </aside>

      {/* Main */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <ConnectionBanner status={sseStatus} retryAttempt={sseRetryAttempt} />
        <main className="flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
