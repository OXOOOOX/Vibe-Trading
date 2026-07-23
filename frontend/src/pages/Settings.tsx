import i18n from "@/i18n";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import {
  BookOpen,
  ChevronDown,
  Copy,
  Database,
  KeyRound,
  Loader2,
  MessageSquareMore,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  Server,
  SlidersHorizontal,
  Square,
  Terminal,
} from "lucide-react";
import { toast } from "sonner";
import {
  api,
  isAuthRequiredError,
  type ChannelRuntimeStatus,
  type CodexCliStatus,
  type DataSourceSettings,
  type FeishuDeliverySettings,
  type LLMModelOption,
  type LLMProviderOption,
  type LLMSettings,
  type MonitorDeliveryBindingAttempt,
  type ResearchSettings,
} from "@/lib/api";
import { getApiAuthKey, setApiAuthKey } from "@/lib/apiAuth";

interface LLMFormState {
  provider: string;
  model_name: string;
  base_url: string;
  temperature: number;
  timeout_seconds: number;
  max_retries: number;
  reasoning_effort: string;
}

const fieldClass =
  "w-full rounded-md border bg-background px-3 py-2 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/20 disabled:cursor-not-allowed disabled:opacity-60";
const labelClass = "text-sm font-medium";
const hintClass = "text-xs text-muted-foreground";
const CUSTOM_MODEL_VALUE = "__custom_model__";

function toCodexCliModelSlug(value: string): string {
  return value.replace(/^openai[-_]codex\//, "");
}

function toForm(settings: LLMSettings): LLMFormState {
  return {
    provider: settings.provider,
    model_name: settings.model_name,
    base_url: settings.base_url,
    temperature: settings.temperature,
    timeout_seconds: settings.timeout_seconds,
    max_retries: settings.max_retries,
    reasoning_effort: settings.reasoning_effort || "",
  };
}

function mergeWithCurrentModel(
  discoveredOptions: LLMModelOption[],
  fallbackOptions: LLMModelOption[],
  currentModel: string | undefined,
): LLMModelOption[] {
  const discoveredIds = new Set(discoveredOptions.map((option) => option.id));
  const merged: LLMModelOption[] = [...discoveredOptions];
  for (const option of fallbackOptions) {
    if (!discoveredIds.has(option.id)) {
      merged.push(option);
      discoveredIds.add(option.id);
    }
  }

  if (!currentModel || merged.some((option) => option.id === currentModel)) {
    return merged;
  }

  return [
    {
      id: currentModel,
      label: currentModel,
      description: "Use the exact model id currently configured for this provider.",
      reasoning_efforts: [],
    },
    ...merged,
  ];
}

export function Settings() {

  const [settings, setSettings] = useState<LLMSettings | null>(null);
  const [dataSettings, setDataSettings] = useState<DataSourceSettings | null>(null);
  const [researchSettings, setResearchSettings] = useState<ResearchSettings | null>(null);
  const [codexStatus, setCodexStatus] = useState<CodexCliStatus | null>(null);
  const [codexLoadError, setCodexLoadError] = useState<string | null>(null);
  const [channelStatus, setChannelStatus] = useState<ChannelRuntimeStatus | null>(null);
  const [channelLoadError, setChannelLoadError] = useState<string | null>(null);
  const [feishuDelivery, setFeishuDelivery] = useState<FeishuDeliverySettings | null>(null);
  const [feishuDeliveryError, setFeishuDeliveryError] = useState<string | null>(null);
  const [feishuDeliverySaving, setFeishuDeliverySaving] = useState(false);
  const [feishuBinding, setFeishuBinding] = useState<MonitorDeliveryBindingAttempt | null>(null);
  const [feishuBindingLoading, setFeishuBindingLoading] = useState(false);
  const [form, setForm] = useState<LLMFormState | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [localApiKey, setLocalApiKeyState] = useState(() => getApiAuthKey());
  const [clearApiKey, setClearApiKey] = useState(false);
  const [tushareToken, setTushareToken] = useState("");
  const [clearTushareToken, setClearTushareToken] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dataSaving, setDataSaving] = useState(false);
  const [researchSaving, setResearchSaving] = useState<"main" | "auto" | "engine" | "cli_model" | null>(null);
  const [codexRefreshing, setCodexRefreshing] = useState(false);
  const [codexLoginOpening, setCodexLoginOpening] = useState(false);
  const [codexManagementExpanded, setCodexManagementExpanded] = useState(false);
  const [codexCliModel, setCodexCliModel] = useState("gpt-5.6-terra");
  const [codexCliReasoningEffort, setCodexCliReasoningEffort] = useState("medium");
  const [codexModelRefreshing, setCodexModelRefreshing] = useState(false);
  const [codexModelRefreshNote, setCodexModelRefreshNote] = useState<string | null>(null);
  const [channelRefreshing, setChannelRefreshing] = useState(false);
  const [channelAction, setChannelAction] = useState<"start" | "stop" | null>(null);
  const [settingsLoadError, setSettingsLoadError] = useState<string | null>(null);
  const [modelChoicesByProvider, setModelChoicesByProvider] = useState<Record<string, LLMModelOption[]>>({});
  const [modelRefreshing, setModelRefreshing] = useState(false);
  const [modelRefreshNote, setModelRefreshNote] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    Promise.all([
      api.getLLMSettings(),
      api.getDataSourceSettings(),
      api.getResearchSettings(),
      api.getCodexCliStatus()
        .then((status) => ({ status, error: null }))
        .catch((error: unknown) => ({
          status: null,
          error: error instanceof Error ? error.message : "Unknown error",
        })),
      api.getChannelStatus()
        .then((status) => ({ status, error: null }))
        .catch((error: unknown) => ({
          status: null,
          error: error instanceof Error ? error.message : "Unknown error",
        })),
      api.getFeishuDeliverySettings()
        .then((settings) => ({ settings, error: null }))
        .catch((error: unknown) => ({
          settings: null,
          error: error instanceof Error ? error.message : "Unknown error",
        })),
    ])
      .then(([llmData, dataSourceData, researchData, codexResult, channelResult, feishuResult]) => {
        if (!alive) return;
        setSettings(llmData);
        setForm(toForm(llmData));
        setDataSettings(dataSourceData);
        setResearchSettings(researchData);
        setCodexManagementExpanded(researchData.deep_research_engine === "codex_cli");
        setCodexCliModel(researchData.codex_cli_model);
        setCodexCliReasoningEffort(researchData.codex_cli_reasoning_effort);
        setCodexStatus(codexResult.status);
        setCodexLoadError(codexResult.error);
        setChannelStatus(channelResult.status);
        setChannelLoadError(channelResult.error);
        setFeishuDelivery(feishuResult.settings);
        setFeishuDeliveryError(feishuResult.error);
        setSettingsLoadError(null);
      })
      .catch((error) => {
        const message = error instanceof Error ? error.message : "Unknown error";
        setSettingsLoadError(message);
        if (isAuthRequiredError(error)) {
          toast.error(message);
        } else {
          toast.error(`Failed to load LLM settings: ${message}`);
          toast.error(`Failed to load data source settings: ${message}`);
        }
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => { alive = false; };
  }, []);

  const refreshChannelStatus = async () => {
    setChannelRefreshing(true);
    try {
      setChannelStatus(await api.getChannelStatus());
      setChannelLoadError(null);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown error";
      setChannelLoadError(message);
      toast.error(`${i18n.t("settings.channels.refreshFailed")}: ${message}`);
    } finally {
      setChannelRefreshing(false);
    }
  };

  const setChannelsRunning = async (action: "start" | "stop") => {
    setChannelAction(action);
    try {
      const updated = action === "start" ? await api.startChannels() : await api.stopChannels();
      setChannelStatus(updated);
      setChannelLoadError(null);
      toast.success(
        action === "start"
          ? i18n.t("settings.channels.started")
          : i18n.t("settings.channels.stoppedToast"),
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown error";
      toast.error(
        `${i18n.t(
          action === "start" ? "settings.channels.startFailed" : "settings.channels.stopFailed",
        )}: ${message}`,
      );
    } finally {
      setChannelAction(null);
    }
  };

  const providers = settings?.providers ?? [];
  const selectedProvider = useMemo<LLMProviderOption | undefined>(
    () => providers.find((provider) => provider.name === form?.provider),
    [form?.provider, providers],
  );
  const selectableModels = form
    ? mergeWithCurrentModel(
        modelChoicesByProvider[form.provider] ?? [],
        selectedProvider?.models ?? [],
        form.model_name,
      )
    : [];
  const selectedModelOption = selectableModels.find((model) => model.id === form?.model_name);
  const usingCustomModel = selectableModels.length > 0 && !selectedModelOption;
  const codexProvider = providers.find((provider) => provider.name === "openai-codex");
  const codexCliModelOptions = (
    modelChoicesByProvider["openai-codex"] ?? codexProvider?.models ?? []
  ).map((model) => ({ ...model, id: toCodexCliModelSlug(model.id) }));
  const selectedCodexCliModel = codexCliModelOptions.find((model) => model.id === codexCliModel);
  const codexCliReasoningOptions = selectedCodexCliModel?.reasoning_efforts?.length
    ? selectedCodexCliModel.reasoning_efforts
    : ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"];

  const applyProviderDefaults = (provider = selectedProvider) => {
    if (!provider || !form) return;
    setForm({
      ...form,
      model_name: provider.default_model,
      base_url: provider.default_base_url,
    });
  };

  const onProviderChange = (name: string) => {
    const provider = providers.find((item) => item.name === name);
    if (!provider || !form) return;
    setForm({
      ...form,
      provider: provider.name,
      model_name: provider.default_model,
      base_url: provider.default_base_url,
    });
    setApiKey("");
    setClearApiKey(false);
    setModelRefreshNote(null);
  };

  const saveDefaultFeishuTarget = async (targetId: string | null) => {
    setFeishuDeliverySaving(true);
    setFeishuDeliveryError(null);
    try {
      setFeishuDelivery(await api.updateFeishuDeliverySettings(targetId));
      toast.success(targetId ? "默认飞书发送目标已更新" : "已清除默认飞书发送目标");
    } catch (error) {
      const message = error instanceof Error ? error.message : "保存失败";
      setFeishuDeliveryError(message);
      toast.error(message);
    } finally {
      setFeishuDeliverySaving(false);
    }
  };

  const createFeishuBinding = async () => {
    setFeishuBindingLoading(true);
    setFeishuDeliveryError(null);
    try {
      setFeishuBinding(await api.createFeishuDeliveryBindingCode());
    } catch (error) {
      const message = error instanceof Error ? error.message : "绑定码创建失败";
      setFeishuDeliveryError(message);
      toast.error(message);
    } finally {
      setFeishuBindingLoading(false);
    }
  };

  const checkFeishuBinding = async () => {
    if (!feishuBinding) return;
    setFeishuBindingLoading(true);
    try {
      const result = await api.getFeishuDeliveryBindingCode(feishuBinding.binding_id);
      setFeishuBinding(result);
      if (result.status === "claimed") {
        setFeishuDelivery(await api.getFeishuDeliverySettings());
        toast.success("飞书发送目标绑定成功");
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "绑定状态检查失败");
    } finally {
      setFeishuBindingLoading(false);
    }
  };

  const revokeFeishuTarget = async (targetId: string) => {
    setFeishuDeliverySaving(true);
    try {
      await api.revokeFeishuDeliveryTarget(targetId);
      setFeishuDelivery(await api.getFeishuDeliverySettings());
      toast.success("飞书发送目标已停用");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "停用失败");
    } finally {
      setFeishuDeliverySaving(false);
    }
  };

  const refreshAvailableModels = async () => {
    if (!form || !selectedProvider?.model_discovery) return;
    const provider = form.provider;
    setModelRefreshing(true);
    try {
      const result = await api.getLLMModels(provider);
      setModelChoicesByProvider((current) => ({
        ...current,
        [provider]: result.models,
      }));
      setModelRefreshNote(
        result.warning
        || `${result.models.length} models refreshed from ${result.source === "remote" ? "your account" : "project defaults"}.`,
      );
      if (result.warning) {
        toast.warning(result.warning);
      } else {
        toast.success(`Found ${result.models.length} available models`);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown error";
      setModelRefreshNote(message);
      toast.error(`Unable to refresh models: ${message}`);
    } finally {
      setModelRefreshing(false);
    }
  };

  const refreshCodexCliModels = async () => {
    setCodexModelRefreshing(true);
    try {
      const result = await api.getLLMModels("openai-codex");
      setModelChoicesByProvider((current) => ({
        ...current,
        "openai-codex": result.models,
      }));
      const note = result.warning
        || i18n.t("settings.codexCli.modelsRefreshed", { count: result.models.length });
      setCodexModelRefreshNote(note);
      if (result.warning) toast.warning(result.warning);
      else toast.success(note);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown error";
      setCodexModelRefreshNote(message);
      toast.error(`${i18n.t("settings.codexCli.modelRefreshFailed")}: ${message}`);
    } finally {
      setCodexModelRefreshing(false);
    }
  };

  const saveCodexCliPreferences = async () => {
    if (!researchSettings || researchSaving !== null) return;
    setResearchSaving("cli_model");
    try {
      const updated = await api.updateResearchSettings({
        codex_cli_model: codexCliModel,
        codex_cli_reasoning_effort: codexCliReasoningEffort,
      });
      setResearchSettings(updated);
      setCodexCliModel(updated.codex_cli_model);
      setCodexCliReasoningEffort(updated.codex_cli_reasoning_effort);
      toast.success(i18n.t("settings.codexCli.modelSettingsSaved"));
    } catch (error) {
      toast.error(
        `${i18n.t("settings.codexCli.modelSettingsSaveFailed")}: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setResearchSaving(null);
    }
  };

  const submitLocalApiKey = (event: FormEvent) => {
    event.preventDefault();
    setApiAuthKey(localApiKey);
    toast.success("Local API key saved");
    window.location.reload();
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!form) return;
    setSaving(true);
    try {
      const updated = await api.updateLLMSettings({
        ...form,
        api_key: apiKey.trim() || undefined,
        clear_api_key: clearApiKey,
      });
      setSettings(updated);
      setForm(toForm(updated));
      setApiKey("");
      setClearApiKey(false);
      toast.success("LLM settings saved");
    } catch (error) {
      toast.error(`Failed to save LLM settings: ${error instanceof Error ? error.message : "Unknown error"}`);
    } finally {
      setSaving(false);
    }
  };

  const submitDataSources = async (event: FormEvent) => {
    event.preventDefault();
    setDataSaving(true);
    try {
      const updated = await api.updateDataSourceSettings({
        tushare_token: tushareToken.trim() || undefined,
        clear_tushare_token: clearTushareToken,
      });
      setDataSettings(updated);
      setTushareToken("");
      setClearTushareToken(false);
      toast.success("Data source settings saved");
    } catch (error) {
      toast.error(`Failed to save data source settings: ${error instanceof Error ? error.message : "Unknown error"}`);
    } finally {
      setDataSaving(false);
    }
  };

  const toggleDeepReport = async () => {
    if (!researchSettings || researchSaving !== null) return;
    const nextEnabled = !researchSettings.deep_report_enabled;
    setResearchSaving("main");
    try {
      const updated = await api.updateResearchSettings({ deep_report_enabled: nextEnabled });
      setResearchSettings(updated);
      toast.success(nextEnabled ? "穿透式单股深度研究已启用" : "穿透式单股深度研究已关闭");
    } catch (error) {
      toast.error(`研究能力设置保存失败：${error instanceof Error ? error.message : "Unknown error"}`);
    } finally {
      setResearchSaving(null);
    }
  };

  const toggleMonitorAutoDeepReport = async () => {
    if (!researchSettings || researchSaving !== null || !researchSettings.deep_report_enabled) return;
    const nextEnabled = !researchSettings.monitor_auto_deep_report_enabled;
    setResearchSaving("auto");
    try {
      const updated = await api.updateResearchSettings({
        monitor_auto_deep_report_enabled: nextEnabled,
      });
      setResearchSettings(updated);
      toast.success(nextEnabled ? "AI 自主监控自动生成已启用" : "AI 自主监控自动生成已关闭");
    } catch (error) {
      toast.error(`自动研究设置保存失败：${error instanceof Error ? error.message : "Unknown error"}`);
    } finally {
      setResearchSaving(null);
    }
  };

  const refreshCodexStatus = async () => {
    setCodexRefreshing(true);
    try {
      const updated = await api.getCodexCliStatus();
      setCodexStatus(updated);
      setCodexLoadError(null);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown error";
      setCodexLoadError(message);
      toast.error(`${i18n.t("settings.codexCli.refreshFailed")}: ${message}`);
    } finally {
      setCodexRefreshing(false);
    }
  };

  const openCodexLogin = async () => {
    setCodexLoginOpening(true);
    try {
      await api.openCodexCliLogin();
      toast.success(i18n.t("settings.codexCli.loginOpened"));
    } catch (error) {
      toast.error(
        `${i18n.t("settings.codexCli.loginFailed")}: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setCodexLoginOpening(false);
    }
  };

  const copyCodexCommand = async (command: string) => {
    try {
      await navigator.clipboard.writeText(command);
      toast.success(
        i18n.t(
          codexStatus?.command_shell === "powershell"
            ? "settings.codexCli.copiedPowerShell"
            : "settings.codexCli.copiedTerminal",
        ),
      );
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Unable to copy command");
    }
  };

  const selectDeepResearchEngine = async (
    engine: ResearchSettings["deep_research_engine"],
  ) => {
    if (!researchSettings || researchSaving !== null) return;
    if (engine === researchSettings.deep_research_engine) return;
    if (engine === "codex_cli" && !codexStatus?.ready) {
      toast.error(codexStatus?.message || i18n.t("settings.codexCli.notReady"));
      return;
    }
    setResearchSaving("engine");
    try {
      const updated = await api.updateResearchSettings({ deep_research_engine: engine });
      setResearchSettings(updated);
      setCodexManagementExpanded(updated.deep_research_engine === "codex_cli");
      toast.success(
        i18n.t(
          engine === "provider"
            ? "settings.codexCli.providerSelected"
            : "settings.codexCli.cliSelected",
        ),
      );
    } catch (error) {
      toast.error(
        `${i18n.t("settings.codexCli.saveFailed")}: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setResearchSaving(null);
    }
  };

  const localApiAccessSection = (
    <form onSubmit={submitLocalApiKey} className="rounded-lg border bg-card p-5 shadow-sm">
      <div className="mb-4 space-y-1">
        <div className="flex items-center gap-2">
          <KeyRound className="h-4 w-4 text-primary" />
          <h2 className="text-base font-semibold">{"Local API access"}</h2>
        </div>
        <p className="text-sm text-muted-foreground">{"For remote or private Web UI deployments, enter the server API key once in this browser. Localhost use can stay blank."}</p>
      </div>
      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]">
        <label className="grid gap-2">
          <span className={labelClass}>{"Server API key"}</span>
          <input
            type="password"
            value={localApiKey}
            onChange={(event) => setLocalApiKeyState(event.target.value)}
            className={fieldClass}
            placeholder={"Stored only in this browser. Leave blank to clear it."}
            autoComplete="current-password"
          />
        </label>
        <button
          type="submit"
          className="inline-flex items-center justify-center gap-2 self-end rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90"
        >
          <Save className="h-4 w-4" />
          {i18n.t("settings.save")}
        </button>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">{"Stored only in this browser. Leave blank to clear it."}</p>
    </form>
  );

  if (loading || !form || !settings || !dataSettings || !researchSettings) {
    return (
      <div className="mx-auto max-w-5xl space-y-6 p-6">
        <div className="space-y-2">
          <h1 className="text-2xl font-semibold tracking-tight">{"Settings"}</h1>
          <p className="max-w-3xl text-sm text-muted-foreground">{"Configure model credentials and market data source tokens for this local project."}</p>
        </div>
        {localApiAccessSection}
        <div className="flex min-h-32 items-center justify-center rounded-lg border bg-card p-5 text-sm text-muted-foreground">
          {settingsLoadError ? (
            <div className="text-center">
              <div className="font-medium text-foreground">{"Settings are unavailable"}</div>
              <div className="mt-1">{settingsLoadError}</div>
            </div>
          ) : (
            <>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              {"Loading..."}
            </>
          )}
        </div>
      </div>
    );
  }

  const keyStatus = settings.api_key_configured
    ? "Configured"
    : settings.api_key_required
      ? "Leave blank to keep the current key"
      : selectedProvider?.auth_type === "oauth" && selectedProvider.login_command
        ? `This provider uses OAuth. Run: ${selectedProvider.login_command}`
        : "This provider does not require an API key.";
  const apiKeyDisabled = !selectedProvider?.api_key_required || clearApiKey;
  const tushareStatus = dataSettings.tushare_token_configured
    ? "Configured"
    : "Leave blank to keep the current token";
  const channelRows = Object.entries(channelStatus?.channels ?? {}).sort(([a], [b]) => a.localeCompare(b));
  const channelEnabledCount = channelRows.filter(([, item]) => item.enabled).length;
  const channelLoadedCount = channelRows.filter(([, item]) => item.loaded).length;
  const channelUnavailableCount = channelRows.filter(([, item]) => item.available === false).length;
  const channelBusy = channelRefreshing || channelAction !== null;
  const codexUsesPowerShell = codexStatus?.command_shell === "powershell";

  return (
    <div className="mx-auto max-w-5xl space-y-6 p-6">
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">{"Settings"}</h1>
        <p className="max-w-3xl text-sm text-muted-foreground">{"Configure model credentials and market data source tokens for this local project."}</p>
      </div>

      {localApiAccessSection}

      <section className="rounded-lg border bg-card p-5 shadow-sm">
        <div className="flex flex-col gap-5 md:flex-row md:items-center md:justify-between">
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <BookOpen className="h-4 w-4 text-primary" />
              <h2 className="text-base font-semibold">研究能力</h2>
              <span className={`rounded-full px-2 py-0.5 text-xs ${researchSettings.equity_deep_research_enabled ? "bg-success/10 text-success" : "bg-muted text-muted-foreground"}`}>
                {researchSettings.equity_deep_research_enabled ? "已启用" : "已关闭"}
              </span>
            </div>
            <div>
              <div className="text-sm font-medium">穿透式单股深度研究</div>
              <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
                控制聊天页是否允许新建 equity_deep_research 报告。关闭后，已有报告仍可查看和下载，日报与回测流程不受影响。
              </p>
            </div>
            <p className="text-xs text-muted-foreground">
              设置写入 <span className="font-mono text-foreground/80">{researchSettings.env_path}</span>，对之后的新请求立即生效，无需重启服务。
            </p>
          </div>

          <div className="flex shrink-0 items-center gap-3 self-start md:self-center">
            <span className="text-sm font-medium text-muted-foreground">
              {researchSettings.deep_report_enabled ? "开启" : "关闭"}
            </span>
            <button
              type="button"
              role="switch"
              aria-label="启用穿透式单股深度研究"
              aria-checked={researchSettings.deep_report_enabled}
              disabled={researchSaving !== null}
              onClick={toggleDeepReport}
              className={`relative inline-flex h-7 w-12 items-center rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60 ${researchSettings.deep_report_enabled ? "bg-primary" : "bg-muted-foreground/30"}`}
            >
              <span className={`inline-flex h-5 w-5 items-center justify-center rounded-full bg-background shadow-sm transition-transform ${researchSettings.deep_report_enabled ? "translate-x-6" : "translate-x-1"}`}>
                {researchSaving === "main" ? <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" /> : null}
              </span>
            </button>
          </div>
        </div>

        <div className="mt-5 flex flex-col gap-4 border-t pt-4 md:flex-row md:items-center md:justify-between">
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium">AI 自主监控自动生成穿透式报告</span>
              <span className={`rounded-full px-2 py-0.5 text-xs ${researchSettings.effective_monitor_auto_deep_report_enabled ? "bg-violet-500/10 text-violet-700 dark:text-violet-300" : "bg-muted text-muted-foreground"}`}>
                {researchSettings.effective_monitor_auto_deep_report_enabled ? "已授权" : "未授权"}
              </span>
            </div>
            <p className="max-w-3xl text-sm text-muted-foreground">
              当自主监控发现原报告缺失、过期或证据不足时，允许它自动排队生成完整穿透式报告。同一股票每天最多创建一次，并优先复用当天已有报告。
            </p>
            {!researchSettings.deep_report_enabled ? (
              <p className="text-xs text-amber-700 dark:text-amber-300">请先开启上方的穿透式单股深度研究总开关。</p>
            ) : null}
          </div>

          <div className="flex shrink-0 items-center gap-3 self-start md:self-center">
            <span className="text-sm font-medium text-muted-foreground">
              {researchSettings.monitor_auto_deep_report_enabled ? "允许" : "禁止"}
            </span>
            <button
              type="button"
              role="switch"
              aria-label="允许 AI 自主监控自动生成穿透式报告"
              aria-checked={researchSettings.monitor_auto_deep_report_enabled}
              disabled={researchSaving !== null || !researchSettings.deep_report_enabled}
              onClick={toggleMonitorAutoDeepReport}
              className={`relative inline-flex h-7 w-12 items-center rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 ${researchSettings.monitor_auto_deep_report_enabled ? "bg-violet-600" : "bg-muted-foreground/30"}`}
            >
              <span className={`inline-flex h-5 w-5 items-center justify-center rounded-full bg-background shadow-sm transition-transform ${researchSettings.monitor_auto_deep_report_enabled ? "translate-x-6" : "translate-x-1"}`}>
                {researchSaving === "auto" ? <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" /> : null}
              </span>
            </button>
          </div>
        </div>

        <div className="mt-5 space-y-4 border-t pt-4">
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <SlidersHorizontal className="h-4 w-4 text-primary" />
              <span className="text-sm font-semibold">{i18n.t("settings.codexCli.title")}</span>
              <span className="rounded-full bg-primary/10 px-2 py-0.5 text-xs text-primary">
                {researchSettings.deep_research_engine === "provider"
                  ? i18n.t("settings.codexCli.providerTitle")
                  : i18n.t("settings.codexCli.cliTitle")}
              </span>
            </div>
            <p className="max-w-3xl text-sm text-muted-foreground">
              {i18n.t("settings.codexCli.description")}
            </p>
          </div>

          <div
            role="radiogroup"
            aria-label={i18n.t("settings.codexCli.title")}
            className="grid gap-3 md:grid-cols-2"
          >
            <button
              type="button"
              role="radio"
              aria-checked={researchSettings.deep_research_engine === "provider"}
              disabled={researchSaving !== null}
              onClick={() => selectDeepResearchEngine("provider")}
              className={`rounded-lg border p-4 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60 ${researchSettings.deep_research_engine === "provider" ? "border-primary bg-primary/5 shadow-sm" : "hover:bg-muted/50"}`}
            >
              <span className="flex items-start gap-3">
                <Server className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                <span className="min-w-0 space-y-1">
                  <span className="flex flex-wrap items-center gap-2 text-sm font-medium">
                    {i18n.t("settings.codexCli.providerTitle")}
                    <span className="rounded-full bg-success/10 px-2 py-0.5 text-[11px] text-success">
                      {i18n.t("settings.codexCli.recommended")}
                    </span>
                  </span>
                  <span className="block text-xs font-normal text-muted-foreground">
                    {i18n.t("settings.codexCli.providerDescription")}
                  </span>
                </span>
              </span>
            </button>

            <button
              type="button"
              role="radio"
              aria-checked={researchSettings.deep_research_engine === "codex_cli"}
              disabled={researchSaving !== null || (!codexStatus?.ready && researchSettings.deep_research_engine !== "codex_cli")}
              onClick={() => selectDeepResearchEngine("codex_cli")}
              className={`rounded-lg border p-4 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60 ${researchSettings.deep_research_engine === "codex_cli" ? "border-primary bg-primary/5 shadow-sm" : "hover:bg-muted/50"}`}
            >
              <span className="flex items-start gap-3">
                <Terminal className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                <span className="min-w-0 space-y-1">
                  <span className="flex flex-wrap items-center gap-2 text-sm font-medium">
                    {i18n.t("settings.codexCli.cliTitle")}
                    <span className={`rounded-full px-2 py-0.5 text-[11px] ${codexStatus?.ready ? "bg-blue-500/10 text-blue-700 dark:text-blue-300" : "bg-amber-500/10 text-amber-700 dark:text-amber-300"}`}>
                      {codexStatus?.ready
                        ? i18n.t("settings.codexCli.ready")
                        : i18n.t("settings.codexCli.notReady")}
                    </span>
                  </span>
                  <span className="block text-xs font-normal text-muted-foreground">
                    {i18n.t("settings.codexCli.cliDescription")}
                  </span>
                </span>
              </span>
            </button>
          </div>

          <div className="overflow-hidden rounded-lg border bg-muted/20">
            <div className="grid gap-4 p-4 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-start">
              <button
                type="button"
                aria-expanded={codexManagementExpanded}
                onClick={() => setCodexManagementExpanded((expanded) => !expanded)}
                className="flex min-w-0 items-start gap-3 rounded-md text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
              >
                  <span className="rounded-md border bg-background p-2 text-primary">
                    <Terminal className="h-4 w-4" />
                  </span>
                  <span className="min-w-0 flex-1 space-y-1">
                    <div className="text-sm font-medium">{i18n.t("settings.codexCli.cliManagementTitle")}</div>
                    <p className="max-w-3xl text-xs leading-5 text-muted-foreground">
                      {i18n.t("settings.codexCli.oneTimeHint")}
                    </p>
                  </span>
                  <ChevronDown className={`mt-1 h-4 w-4 shrink-0 text-muted-foreground transition-transform ${codexManagementExpanded ? "rotate-180" : ""}`} />
              </button>

              {codexManagementExpanded ? (
                <button
                  type="button"
                  onClick={refreshCodexStatus}
                  disabled={codexRefreshing || codexLoginOpening}
                  className="inline-flex items-center justify-center gap-2 rounded-md border bg-background px-3 py-2 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {codexRefreshing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                  {i18n.t("settings.codexCli.refresh")}
                </button>
              ) : null}
            </div>

            {codexManagementExpanded ? (
              <div className="space-y-4 border-t p-4">
                <p className="max-w-3xl rounded-md border border-dashed bg-background/60 px-3 py-2 text-xs leading-5 text-muted-foreground">
                  {i18n.t("settings.codexCli.isolationHint")}
                </p>
                <div className="flex items-start gap-3 rounded-md border bg-background/60 px-3 py-2.5">
                  <Terminal className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                  <div className="space-y-1">
                    <div className="text-xs font-medium text-foreground">{i18n.t("settings.codexCli.commandLocationTitle")}</div>
                    <p className="text-xs leading-5 text-muted-foreground">
                      {i18n.t(
                        codexUsesPowerShell
                          ? "settings.codexCli.commandLocationPowerShell"
                          : "settings.codexCli.commandLocationTerminal",
                      )}
                    </p>
                  </div>
                </div>

                {codexStatus ? (
                  <div className="grid gap-4 lg:grid-cols-2">
                    <section aria-labelledby="codex-version-card-title" className="flex min-w-0 flex-col rounded-lg border bg-background/60 p-4">
                      <div className="mb-4 flex items-center gap-2">
                        <Server className="h-4 w-4 text-primary" />
                        <h3 id="codex-version-card-title" className="text-sm font-medium">
                          {i18n.t("settings.codexCli.versionManagementTitle")}
                        </h3>
                      </div>
                      <div className="grid grid-cols-3 gap-3 text-xs">
                        <div className="space-y-1 rounded-md border bg-background px-3 py-2.5">
                          <div className="text-muted-foreground">{i18n.t("settings.codexCli.version")}</div>
                          <div className={`font-mono ${codexStatus.version_supported ? "text-foreground" : "text-amber-700 dark:text-amber-300"}`}>
                            {codexStatus.version || "—"}
                          </div>
                        </div>
                        <div className="space-y-1 rounded-md border bg-background px-3 py-2.5">
                          <div className="text-muted-foreground">{i18n.t("settings.codexCli.latestVersion")}</div>
                          <div className="font-mono text-foreground">{codexStatus.latest_version || "—"}</div>
                        </div>
                        <div className="space-y-1 rounded-md border bg-background px-3 py-2.5">
                          <div className="text-muted-foreground">{i18n.t("settings.codexCli.minimumVersion")}</div>
                          <div className="font-mono text-foreground">{codexStatus.minimum_version}</div>
                        </div>
                      </div>
                      <div className="mt-auto flex justify-end pt-4">
                        <button
                          type="button"
                          onClick={() => copyCodexCommand(codexStatus.install_command)}
                          className="inline-flex shrink-0 items-center justify-center gap-2 rounded-md border bg-background px-3 py-2 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground"
                        >
                          <Copy className="h-3.5 w-3.5" />
                          {i18n.t(codexUsesPowerShell ? "settings.codexCli.copyInstallPowerShell" : "settings.codexCli.copyInstallTerminal")}
                        </button>
                      </div>
                    </section>

                    <section aria-labelledby="codex-login-card-title" className="flex min-w-0 flex-col rounded-lg border bg-background/60 p-4">
                      <div className="mb-3 flex items-center justify-between gap-3">
                        <div className="flex items-center gap-2">
                          <KeyRound className="h-4 w-4 text-primary" />
                          <h3 id="codex-login-card-title" className="text-sm font-medium">
                            {i18n.t("settings.codexCli.loginManagementTitle")}
                          </h3>
                        </div>
                        <span className={`rounded-full px-2 py-0.5 text-[11px] ${codexStatus.auth_state === "authenticated" ? "bg-success/10 text-success" : "bg-amber-500/10 text-amber-700 dark:text-amber-300"}`}>
                          {codexStatus.auth_state === "authenticated"
                            ? i18n.t("settings.codexCli.authenticated")
                            : i18n.t("settings.codexCli.unauthenticated")}
                        </span>
                      </div>
                      <p className={`text-xs leading-5 ${codexLoadError || !codexStatus.ready ? "text-amber-700 dark:text-amber-300" : "text-muted-foreground"}`}>
                        {codexLoadError || codexStatus.message || i18n.t("settings.codexCli.notReady")}
                      </p>
                      {codexStatus.environment !== "native" ? (
                        <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
                          {i18n.t("settings.codexCli.remoteHint")}
                        </p>
                      ) : null}
                      <div className="mt-auto flex flex-wrap gap-2 pt-4">
                        {codexStatus.can_launch_login ? (
                          <button
                            type="button"
                            onClick={openCodexLogin}
                            disabled={codexRefreshing || codexLoginOpening}
                            className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
                          >
                            {codexLoginOpening ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Terminal className="h-3.5 w-3.5" />}
                            {i18n.t(codexUsesPowerShell ? "settings.codexCli.openLoginPowerShell" : "settings.codexCli.openLogin")}
                          </button>
                        ) : null}
                        <button
                          type="button"
                          onClick={() => copyCodexCommand(codexStatus.login_command)}
                          className="inline-flex items-center justify-center gap-2 rounded-md border bg-background px-3 py-2 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground"
                        >
                          <Copy className="h-3.5 w-3.5" />
                          {i18n.t(codexUsesPowerShell ? "settings.codexCli.copyLoginPowerShell" : "settings.codexCli.copyLoginTerminal")}
                        </button>
                      </div>
                    </section>
                  </div>
                ) : (
                  <p className="text-xs text-amber-700 dark:text-amber-300">
                    {codexLoadError || i18n.t("settings.codexCli.notReady")}
                  </p>
                )}

                <section aria-labelledby="codex-model-card-title" className="rounded-lg border bg-background/60 p-4">
                  <div className="mb-3 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                    <div className="flex items-start gap-3">
                      <SlidersHorizontal className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                      <div className="space-y-1">
                        <h3 id="codex-model-card-title" className="text-sm font-medium">{i18n.t("settings.codexCli.modelSettingsTitle")}</h3>
                        <p className="max-w-3xl text-xs leading-5 text-muted-foreground">
                          {i18n.t("settings.codexCli.modelSettingsDescription")}
                        </p>
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={refreshCodexCliModels}
                      disabled={codexModelRefreshing || researchSaving !== null}
                      aria-label={i18n.t("settings.codexCli.refreshModels")}
                      className="inline-flex items-center justify-center gap-2 rounded-md border bg-background px-3 py-2 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      {codexModelRefreshing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                      {i18n.t("settings.codexCli.refreshModels")}
                    </button>
                  </div>

                  <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(180px,0.45fr)_auto] md:items-start">
                    <label className="grid gap-2">
                      <span className="text-xs font-medium">{i18n.t("settings.codexCli.modelLabel")}</span>
                      <select
                        aria-label={i18n.t("settings.codexCli.modelLabel")}
                        value={codexCliModel}
                        onChange={(event) => {
                          const nextModel = event.target.value;
                          const option = codexCliModelOptions.find((item) => item.id === nextModel);
                          const efforts = option?.reasoning_efforts ?? [];
                          setCodexCliModel(nextModel);
                          if (efforts.length > 0 && !efforts.includes(codexCliReasoningEffort)) {
                            setCodexCliReasoningEffort(option?.default_reasoning_effort || efforts[0]);
                          }
                        }}
                        className={fieldClass}
                      >
                        {!selectedCodexCliModel ? <option value={codexCliModel}>{codexCliModel}</option> : null}
                        {codexCliModelOptions.map((model) => (
                          <option key={model.id} value={model.id}>{model.label}</option>
                        ))}
                      </select>
                      <span className={`${hintClass} line-clamp-1`} title={selectedCodexCliModel?.description || i18n.t("settings.codexCli.modelHint")}>
                        {selectedCodexCliModel?.description || i18n.t("settings.codexCli.modelHint")}
                      </span>
                    </label>

                    <label className="grid gap-2">
                      <span className="text-xs font-medium">{i18n.t("settings.codexCli.reasoningEffortLabel")}</span>
                      <select
                        aria-label={i18n.t("settings.codexCli.reasoningEffortLabel")}
                        value={codexCliReasoningEffort}
                        onChange={(event) => setCodexCliReasoningEffort(event.target.value)}
                        className={fieldClass}
                      >
                        {codexCliReasoningOptions.map((effort) => (
                          <option key={effort} value={effort}>{effort}</option>
                        ))}
                      </select>
                      <span className={hintClass}>{i18n.t("settings.codexCli.reasoningEffortHint")}</span>
                    </label>

                    <button
                      type="button"
                      onClick={saveCodexCliPreferences}
                      disabled={researchSaving !== null || !codexCliModel}
                      className="inline-flex h-10 items-center justify-center gap-2 rounded-md bg-primary px-4 text-xs font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60 md:mt-6"
                    >
                      {researchSaving === "cli_model" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                      {i18n.t("settings.codexCli.saveModelSettings")}
                    </button>
                  </div>
                  {codexModelRefreshNote ? (
                    <p className="mt-2 text-xs text-muted-foreground">{codexModelRefreshNote}</p>
                  ) : null}
                </section>
              </div>
            ) : null}
          </div>
        </div>
      </section>

      <section className="rounded-lg border bg-card p-5 shadow-sm">
        <div className="mb-5 flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <MessageSquareMore className="h-4 w-4 text-primary" />
              <h2 className="text-base font-semibold">{i18n.t("settings.channels.title")}</h2>
            </div>
            <p className="max-w-3xl text-sm text-muted-foreground">
              {i18n.t("settings.channels.description")}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={refreshChannelStatus}
              disabled={channelBusy}
              className="inline-flex items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
            >
              {channelRefreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              {i18n.t("settings.channels.refresh")}
            </button>
            <button
              type="button"
              onClick={() => setChannelsRunning("start")}
              disabled={channelBusy}
              className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {channelAction === "start" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              {i18n.t("settings.channels.start")}
            </button>
            <button
              type="button"
              onClick={() => setChannelsRunning("stop")}
              disabled={channelBusy}
              className="inline-flex items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
            >
              {channelAction === "stop" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}
              {i18n.t("settings.channels.stop")}
            </button>
          </div>
        </div>

        {channelLoadError && !channelStatus ? (
          <div className="rounded-md border border-warning/30 bg-warning/5 px-4 py-3 text-sm">
            <div className="font-medium text-foreground">{i18n.t("settings.channels.unavailable")}</div>
            <div className="mt-1 break-words text-xs text-muted-foreground">{channelLoadError}</div>
          </div>
        ) : (
          <>
            <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <div className="rounded-md border bg-muted/20 px-3 py-2">
                <div className="text-xs text-muted-foreground">{i18n.t("settings.channels.runtime")}</div>
                <div className="text-sm font-medium">
                  {channelStatus?.running ? i18n.t("settings.channels.running") : i18n.t("settings.channels.stopped")}
                </div>
              </div>
              <div className="rounded-md border bg-muted/20 px-3 py-2">
                <div className="text-xs text-muted-foreground">{i18n.t("settings.channels.enabled")}</div>
                <div className="text-sm font-medium">{channelEnabledCount}</div>
              </div>
              <div className="rounded-md border bg-muted/20 px-3 py-2">
                <div className="text-xs text-muted-foreground">{i18n.t("settings.channels.loaded")}</div>
                <div className="text-sm font-medium">{channelLoadedCount}</div>
              </div>
              <div className="rounded-md border bg-muted/20 px-3 py-2">
                <div className="text-xs text-muted-foreground">{i18n.t("settings.channels.unavailable")}</div>
                <div className="text-sm font-medium">{channelUnavailableCount}</div>
              </div>
            </div>

            <div className="overflow-x-auto rounded-md border">
              <table className="min-w-full text-sm">
                <thead className="bg-muted/40 text-xs text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">{i18n.t("settings.channels.channel")}</th>
                    <th className="px-3 py-2 text-left font-medium">{i18n.t("settings.channels.state")}</th>
                    <th className="px-3 py-2 text-left font-medium">{i18n.t("settings.channels.recovery")}</th>
                  </tr>
                </thead>
                <tbody>
                  {channelRows.map(([name, item]) => (
                    <tr key={name} className="border-t">
                      <td className="px-3 py-2 align-top">
                        <div className="font-medium">{item.display_name || name}</div>
                        <div className="text-xs text-muted-foreground">{name}</div>
                      </td>
                      <td className="px-3 py-2 align-top">
                        <div className="flex flex-wrap gap-1.5">
                          <span className={`rounded-full px-2 py-0.5 text-xs ${item.enabled ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"}`}>
                            {item.enabled ? i18n.t("settings.channels.enabled") : i18n.t("settings.channels.disabled")}
                          </span>
                          <span className={`rounded-full px-2 py-0.5 text-xs ${item.loaded ? "bg-success/10 text-success" : "bg-muted text-muted-foreground"}`}>
                            {item.loaded ? i18n.t("settings.channels.loaded") : i18n.t("settings.channels.notLoaded")}
                          </span>
                          <span className={`rounded-full px-2 py-0.5 text-xs ${item.running ? "bg-success/10 text-success" : "bg-muted text-muted-foreground"}`}>
                            {item.running ? i18n.t("settings.channels.running") : i18n.t("settings.channels.stopped")}
                          </span>
                        </div>
                      </td>
                      <td className="max-w-md px-3 py-2 align-top text-xs text-muted-foreground">
                        {item.install_hint || item.error || i18n.t("settings.channels.noRecovery")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}

        <section className="mt-5 border-t pt-5" aria-labelledby="feishu-delivery-title">
          <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
            <div>
              <h3 id="feishu-delivery-title" className="text-sm font-semibold">飞书发送目标</h3>
              <p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
                Report 一键发送和新建 AI 监控默认继承这里的唯一默认目标；已有监控保留自己的独立目标。
              </p>
            </div>
            <button
              type="button"
              onClick={createFeishuBinding}
              disabled={feishuBindingLoading}
              className="inline-flex items-center justify-center gap-2 rounded-md border px-3 py-2 text-xs font-medium hover:bg-muted disabled:opacity-50"
            >
              {feishuBindingLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <MessageSquareMore className="h-3.5 w-3.5" />}
              绑定新的私聊或群聊
            </button>
          </div>

          {feishuDeliveryError ? (
            <div role="alert" className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-700 dark:text-amber-300">{feishuDeliveryError}</div>
          ) : null}

          {feishuBinding && feishuBinding.status === "pending" ? (
            <div className="mt-3 rounded-md border border-blue-500/25 bg-blue-500/5 p-3 text-sm">
              <div className="font-medium">在目标飞书会话发送以下绑定命令</div>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <code className="min-w-0 flex-1 break-all rounded bg-background px-2.5 py-2 text-xs">{feishuBinding.command || `/绑定监控 ${feishuBinding.code}`}</code>
                <button type="button" onClick={() => void navigator.clipboard.writeText(feishuBinding.command || `/绑定监控 ${feishuBinding.code}`)} className="inline-flex items-center gap-1.5 rounded border bg-background px-2.5 py-2 text-xs hover:bg-muted"><Copy className="h-3.5 w-3.5" />复制</button>
                <button type="button" onClick={checkFeishuBinding} disabled={feishuBindingLoading} className="inline-flex items-center gap-1.5 rounded border bg-background px-2.5 py-2 text-xs hover:bg-muted disabled:opacity-50"><RefreshCw className="h-3.5 w-3.5" />检查绑定状态</button>
              </div>
            </div>
          ) : null}

          {feishuDelivery?.requires_selection ? (
            <div className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-700 dark:text-amber-300">
              当前有多个激活目标但尚未设置默认值。Report 将禁止猜测发送目标，请在下方选择一个默认目标。
            </div>
          ) : null}

          <div className="mt-3 grid gap-2">
            {(feishuDelivery?.targets || []).map((target) => {
              const active = target.status === "active";
              return (
                <article key={target.target_id} className="flex flex-col gap-3 rounded-md border bg-muted/10 p-3 sm:flex-row sm:items-center">
                  <label className="flex min-w-0 flex-1 cursor-pointer items-center gap-3">
                    <input
                      type="radio"
                      name="default-feishu-target"
                      value={target.target_id}
                      checked={feishuDelivery?.default_target_id === target.target_id}
                      disabled={!active || feishuDeliverySaving}
                      onChange={() => void saveDefaultFeishuTarget(target.target_id)}
                      className="h-4 w-4 accent-primary"
                    />
                    <span className="min-w-0">
                      <span className="block text-sm font-medium">飞书 · {target.chat_type === "group" ? "群聊" : "私聊"} · …{target.chat_id.slice(-6)}</span>
                      <span className="mt-0.5 block text-xs text-muted-foreground">{active ? (feishuDelivery?.effective_target_id === target.target_id ? "当前有效发送目标" : "已绑定") : "已停用"}</span>
                    </span>
                  </label>
                  {active ? <button type="button" onClick={() => void revokeFeishuTarget(target.target_id)} disabled={feishuDeliverySaving} className="rounded border px-2.5 py-1.5 text-xs text-muted-foreground hover:bg-muted disabled:opacity-50">停用</button> : null}
                </article>
              );
            })}
            {feishuDelivery && feishuDelivery.targets.length === 0 ? (
              <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">尚未绑定飞书发送目标。绑定后，单一目标会自动成为有效发送目标。</div>
            ) : null}
          </div>
        </section>
      </section>

      <div className="space-y-2">
        <h2 className="text-lg font-semibold tracking-tight">{"LLM Settings"}</h2>
        <p className="max-w-3xl text-sm text-muted-foreground">{"Choose the model used by the agent and save it to the project-local agent/.env file."}</p>
      </div>

      <form onSubmit={submit} className="grid gap-6 lg:grid-cols-[minmax(0,1.4fr)_minmax(320px,0.8fr)]">
        <section className="rounded-lg border bg-card p-5 shadow-sm">
          <div className="mb-5 flex items-center gap-2">
            <Server className="h-4 w-4 text-primary" />
            <h2 className="text-base font-semibold">{"Connection"}</h2>
          </div>

          <div className="grid gap-4">
            <label className="grid gap-2">
              <span className={labelClass}>{i18n.t("settings.provider")}</span>
              <select
                value={form.provider}
                onChange={(event) => onProviderChange(event.target.value)}
                className={fieldClass}
              >
                {providers.map((provider) => (
                  <option key={provider.name} value={provider.name}>{provider.label}</option>
                ))}
              </select>
              <span className={hintClass}>{"Changing providers updates the recommended model and endpoint."}</span>
            </label>

            <div className="grid gap-2">
              <span className={labelClass}>{"Model"}</span>
              <div className="flex gap-2">
                {selectableModels.length > 0 ? (
                  <select
                    aria-label="Model"
                    value={selectedModelOption ? form.model_name : CUSTOM_MODEL_VALUE}
                    onChange={(event) => {
                      const value = event.target.value;
                      setForm({
                        ...form,
                        model_name: value === CUSTOM_MODEL_VALUE ? "" : value,
                      });
                    }}
                    className={fieldClass}
                  >
                    {selectableModels.map((model) => (
                      <option key={model.id} value={model.id}>{model.label}</option>
                    ))}
                    <option value={CUSTOM_MODEL_VALUE}>Custom model ID…</option>
                  </select>
                ) : (
                  <input
                    aria-label="Model"
                    value={form.model_name}
                    onChange={(event) => setForm({ ...form, model_name: event.target.value })}
                    className={fieldClass}
                    required
                  />
                )}
                {selectedProvider?.model_discovery ? (
                  <button
                    type="button"
                    onClick={refreshAvailableModels}
                    disabled={modelRefreshing}
                    className="inline-flex shrink-0 items-center gap-2 rounded-md border px-3 py-2 text-sm text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
                    title="Ask the provider which models this account can use"
                    aria-label="Refresh available models"
                  >
                    {modelRefreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                    <span className="hidden sm:inline">Refresh</span>
                  </button>
                ) : null}
                <button
                  type="button"
                  onClick={() => applyProviderDefaults()}
                  className="inline-flex shrink-0 items-center gap-2 rounded-md border px-3 py-2 text-sm text-muted-foreground transition hover:bg-muted hover:text-foreground"
                  title={"Use provider defaults"}
                >
                  <RotateCcw className="h-4 w-4" />
                  <span className="hidden sm:inline">{"Use provider defaults"}</span>
                </button>
              </div>
              {usingCustomModel ? (
                <input
                  aria-label="Custom model ID"
                  value={form.model_name}
                  onChange={(event) => setForm({ ...form, model_name: event.target.value })}
                  className={fieldClass}
                  placeholder="Enter the exact model ID"
                  required
                />
              ) : null}
              <span className={hintClass}>
                {selectedModelOption?.description || "Use the exact model id required by your provider."}
              </span>
              {modelRefreshNote ? <span className={hintClass}>{modelRefreshNote}</span> : null}
            </div>

            <label className="grid gap-2">
              <span className={labelClass}>{i18n.t("settings.baseUrl")}</span>
              <input
                value={form.base_url}
                onChange={(event) => setForm({ ...form, base_url: event.target.value })}
                className={fieldClass}
                placeholder={selectedProvider?.default_base_url}
                disabled={selectedProvider?.auth_type === "oauth"}
              />
            </label>

            <label className="grid gap-2">
              <span className={labelClass}>
                {selectedProvider?.auth_type === "oauth" ? "OAuth" : "API key"}
              </span>
              <div className="relative">
                <KeyRound className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                <input
                  type="password"
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  className={`${fieldClass} pl-9`}
                  placeholder={keyStatus}
                  autoComplete="current-password"
                  disabled={apiKeyDisabled}
                />
              </div>
              <div className="flex items-center justify-between gap-3">
                <span className={hintClass}>{keyStatus}</span>
                {selectedProvider?.api_key_required ? (
                  <label className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
                    <input
                      type="checkbox"
                      checked={clearApiKey}
                      onChange={(event) => {
                        setClearApiKey(event.target.checked);
                        if (event.target.checked) setApiKey("");
                      }}
                      className="h-3.5 w-3.5 accent-primary"
                    />
                    {"Clear saved API key"}
                  </label>
                ) : null}
              </div>
            </label>
          </div>
        </section>

        <section className="rounded-lg border bg-card p-5 shadow-sm">
          <div className="mb-5 flex items-center gap-2">
            <SlidersHorizontal className="h-4 w-4 text-primary" />
            <h2 className="text-base font-semibold">{"Generation"}</h2>
          </div>

          <div className="grid gap-4">
            <label className="grid gap-2">
              <span className={labelClass}>{i18n.t("settings.temperature")}</span>
              <input
                type="number"
                min={0}
                max={2}
                step={0.1}
                value={form.temperature}
                onChange={(event) => setForm({ ...form, temperature: Number(event.target.value) })}
                className={fieldClass}
              />
            </label>

            <label className="grid gap-2">
              <span className={labelClass}>{i18n.t("settings.timeoutSeconds")}</span>
              <input
                type="number"
                min={1}
                max={3600}
                step={1}
                value={form.timeout_seconds}
                onChange={(event) => setForm({ ...form, timeout_seconds: Number(event.target.value) })}
                className={fieldClass}
              />
            </label>

            <label className="grid gap-2">
              <span className={labelClass}>{"Max retries"}</span>
              <input
                type="number"
                min={0}
                max={20}
                step={1}
                value={form.max_retries}
                onChange={(event) => setForm({ ...form, max_retries: Number(event.target.value) })}
                className={fieldClass}
              />
            </label>

            <label className="grid gap-2">
              <span className={labelClass}>{i18n.t("settings.reasoningEffort")}</span>
              <select
                value={form.reasoning_effort}
                onChange={(event) => setForm({ ...form, reasoning_effort: event.target.value })}
                className={fieldClass}
              >
                <option value="">{"Off"}</option>
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
                <option value="max">max</option>
              </select>
              <span className={hintClass}>{"How hard the model thinks before answering. Higher is more thorough but slower; leave Off for fastest replies."}</span>
            </label>

            <div className="rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
              <span className="font-medium text-foreground">{i18n.t("settings.saved")}: </span>
              <span className="break-all font-mono">{settings.env_path}</span>
            </div>

            <button
              type="submit"
              disabled={saving}
              className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-70"
            >
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              {saving ? i18n.t("settings.saving") : i18n.t("settings.save")}
            </button>
          </div>
        </section>
      </form>

      <form onSubmit={submitDataSources} className="rounded-lg border bg-card p-5 shadow-sm">
        <div className="mb-5 space-y-1">
          <div className="flex items-center gap-2">
            <Database className="h-4 w-4 text-primary" />
            <h2 className="text-base font-semibold">{"Data Source Settings"}</h2>
          </div>
          <p className="text-sm text-muted-foreground">{"Configure optional market data credentials used by backtests and research agents."}</p>
        </div>

        <div className="grid gap-5 lg:grid-cols-[minmax(0,1.1fr)_minmax(280px,0.9fr)]">
          <div className="grid gap-4">
            <label className="grid gap-2">
              <span className={labelClass}>{"Tushare token"}</span>
              <div className="relative">
                <KeyRound className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                <input
                  type="password"
                  value={tushareToken}
                  onChange={(event) => setTushareToken(event.target.value)}
                  className={`${fieldClass} pl-9`}
                  placeholder={tushareStatus}
                  autoComplete="current-password"
                  disabled={clearTushareToken}
                />
              </div>
              <div className="flex items-center justify-between gap-3">
                <span className={hintClass}>{"Used for China A-share, futures, fund, and macro data. If unset, the project falls back to AKShare where available."}</span>
                <label className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={clearTushareToken}
                    onChange={(event) => {
                      setClearTushareToken(event.target.checked);
                      if (event.target.checked) setTushareToken("");
                    }}
                    className="h-3.5 w-3.5 accent-primary"
                  />
                  {"Clear saved Tushare token"}
                </label>
              </div>
            </label>

            <div className="rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
              <span className="font-medium text-foreground">{i18n.t("settings.saved")}: </span>
              <span className="break-all font-mono">{dataSettings.env_path}</span>
            </div>

            <button
              type="submit"
              disabled={dataSaving}
              className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-70"
            >
              {dataSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              {dataSaving ? i18n.t("settings.saving") : "Save data source settings"}
            </button>
          </div>

          <div className="rounded-md border bg-muted/20 p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <span className="text-sm font-medium">{"BaoStock"}</span>
              <span className={`rounded-full px-2 py-0.5 text-xs ${dataSettings.baostock_supported ? "bg-success/10 text-success" : "bg-warning/10 text-warning"}`}>
                {dataSettings.baostock_supported ? "Loader available" : "No project loader"}
              </span>
            </div>
            <div className="space-y-2 text-sm text-muted-foreground">
              <p>{dataSettings.baostock_message}</p>
              <p>
                {dataSettings.baostock_installed
                  ? "Python package installed"
                  : "Python package not installed"}
              </p>
            </div>
          </div>
        </div>
      </form>
    </div>
  );
}
