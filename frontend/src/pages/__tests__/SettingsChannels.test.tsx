import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Settings } from "../Settings";

const apiMock = vi.hoisted(() => ({
  getLLMSettings: vi.fn(),
  getLLMModels: vi.fn(),
  getDataSourceSettings: vi.fn(),
  getResearchSettings: vi.fn(),
  getCodexCliStatus: vi.fn(),
  openCodexCliLogin: vi.fn(),
  getChannelStatus: vi.fn(),
  startChannels: vi.fn(),
  stopChannels: vi.fn(),
  getFeishuDeliverySettings: vi.fn(),
  updateFeishuDeliverySettings: vi.fn(),
  createFeishuDeliveryBindingCode: vi.fn(),
  getFeishuDeliveryBindingCode: vi.fn(),
  revokeFeishuDeliveryTarget: vi.fn(),
  updateLLMSettings: vi.fn(),
  updateDataSourceSettings: vi.fn(),
  updateResearchSettings: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: apiMock,
    isAuthRequiredError: vi.fn(() => false),
  };
});

vi.mock("@/lib/apiAuth", () => ({
  getApiAuthKey: vi.fn(() => ""),
  setApiAuthKey: vi.fn(),
}));

function llmSettings() {
  return {
    provider: "openrouter",
    model_name: "deepseek/deepseek-v3.2",
    base_url: "https://openrouter.ai/api/v1",
    api_key_env: "OPENROUTER_API_KEY",
    api_key_configured: false,
    api_key_required: true,
    temperature: 0.1,
    timeout_seconds: 120,
    max_retries: 2,
    reasoning_effort: "",
    sse_timeout_seconds: 300,
    env_path: "agent/.env",
    providers: [
      {
        name: "openrouter",
        label: "OpenRouter",
        api_key_env: "OPENROUTER_API_KEY",
        base_url_env: "OPENROUTER_BASE_URL",
        default_model: "deepseek/deepseek-v3.2",
        default_base_url: "https://openrouter.ai/api/v1",
        api_key_required: true,
        auth_type: "api_key",
      },
    ],
  };
}

function dataSourceSettings() {
  return {
    tushare_token_configured: false,
    baostock_supported: true,
    baostock_installed: true,
    baostock_message: "BaoStock available",
    env_path: "agent/.env",
  };
}

function codexLlmSettings() {
  return {
    ...llmSettings(),
    provider: "openai-codex",
    model_name: "openai-codex/gpt-5.6-terra",
    base_url: "https://chatgpt.com/backend-api/codex/responses",
    api_key_env: null,
    api_key_configured: true,
    api_key_required: false,
    providers: [
      {
        name: "openai-codex",
        label: "OpenAI Codex (Legacy direct OAuth Provider)",
        api_key_env: null,
        base_url_env: "OPENAI_CODEX_BASE_URL",
        default_model: "openai-codex/gpt-5.6-terra",
        default_base_url: "https://chatgpt.com/backend-api/codex/responses",
        api_key_required: false,
        auth_type: "oauth",
        model_discovery: "codex_oauth" as const,
        models: [
          {
            id: "openai-codex/gpt-5.6-sol",
            label: "GPT-5.6 Sol",
            description: "Frontier",
            default_reasoning_effort: "high",
            reasoning_efforts: ["medium", "high", "xhigh", "max", "ultra"],
          },
          {
            id: "openai-codex/gpt-5.6-terra",
            label: "GPT-5.6 Terra",
            description: "Balanced",
            default_reasoning_effort: "medium",
            reasoning_efforts: ["low", "medium", "high", "max"],
          },
          {
            id: "openai-codex/gpt-5.6-luna",
            label: "GPT-5.6 Luna",
            description: "Fast",
            default_reasoning_effort: "medium",
            reasoning_efforts: ["low", "medium", "high"],
          },
        ],
      },
    ],
  };
}

function researchSettings(enabled = true, auto = false, codex = false) {
  return {
    deep_report_enabled: enabled,
    equity_deep_research_enabled: enabled,
    etf_deep_research_enabled: enabled,
    monitor_auto_deep_report_enabled: auto,
    effective_monitor_auto_deep_report_enabled: enabled && auto,
    deep_research_engine: codex ? "codex_cli" as const : "provider" as const,
    codex_cli_enabled: codex,
    codex_cli_ready: true,
    effective_codex_cli_enabled: enabled && codex,
    codex_cli_model: "gpt-5.6-terra",
    codex_cli_reasoning_effort: "medium",
    enabled_profiles: ["equity_deep_research", "etf_deep_research"],
    available_profiles: ["equity_deep_research", "etf_deep_research"],
    env_path: "agent/.env",
  };
}

function codexStatus(overrides = {}) {
  return {
    installed: true,
    version: "0.144.5",
    latest_version: "0.145.0",
    minimum_version: "0.144.5",
    version_supported: true,
    auth_state: "authenticated",
    ready: true,
    environment: "native",
    command_shell: "powershell",
    can_launch_login: true,
    login_command: "codex login",
    install_command: "npm install -g @openai/codex@latest",
    message: "Codex CLI is installed and authenticated.",
    ...overrides,
  };
}

function channelStatus(overrides = {}) {
  return {
    running: false,
    inbound_queue: 0,
    outbound_queue: 0,
    session_count: 0,
    channels: {
      websocket: {
        name: "websocket",
        display_name: "WebSocket",
        configured: true,
        enabled: true,
        available: true,
        loaded: true,
        running: false,
        error: "",
        install_hint: "",
      },
      telegram: {
        name: "telegram",
        display_name: "Telegram",
        configured: true,
        enabled: false,
        available: false,
        loaded: false,
        running: false,
        error: "ModuleNotFoundError",
        install_hint: "pip install 'vibe-trading-ai[telegram]'",
      },
    },
    ...overrides,
  };
}

describe("Settings IM channels panel", () => {
  beforeEach(() => {
    apiMock.getLLMSettings.mockResolvedValue(llmSettings());
    apiMock.getLLMModels.mockResolvedValue({
      provider: "openai-codex",
      source: "remote",
      models: [],
    });
    apiMock.getDataSourceSettings.mockResolvedValue(dataSourceSettings());
    apiMock.getResearchSettings.mockResolvedValue(researchSettings());
    apiMock.getCodexCliStatus.mockResolvedValue(codexStatus());
    apiMock.openCodexCliLogin.mockResolvedValue({
      launched: true,
      manual_required: false,
      command: "codex login",
      message: "opened",
    });
    apiMock.getChannelStatus.mockResolvedValue(channelStatus());
    apiMock.getFeishuDeliverySettings.mockResolvedValue({
      targets: [],
      default_target_id: null,
      effective_target_id: null,
      requires_selection: false,
    });
    apiMock.startChannels.mockResolvedValue(channelStatus({ running: true }));
    apiMock.stopChannels.mockResolvedValue(channelStatus());
    apiMock.updateResearchSettings.mockResolvedValue(researchSettings(false));
  });

  it("renders channel runtime status and refreshes it", async () => {
    render(<Settings />);

    expect(await screen.findByText("IM Channels")).toBeInTheDocument();
    expect(screen.getByText("websocket")).toBeInTheDocument();
    expect(screen.getByText("telegram")).toBeInTheDocument();
    expect(screen.getByText("pip install 'vibe-trading-ai[telegram]'")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(apiMock.getChannelStatus).toHaveBeenCalledTimes(2));
  });

  it("starts channels from the settings control surface", async () => {
    render(<Settings />);
    await screen.findByText("IM Channels");

    fireEvent.click(screen.getByRole("button", { name: "Start channels" }));

    await waitFor(() => expect(apiMock.startChannels).toHaveBeenCalledTimes(1));
  });

  it("requires and persists one global default when multiple Feishu targets are active", async () => {
    const targets = [
      { target_id: "target-group", channel: "feishu", chat_id: "oc_group123456", chat_type: "group", session_key: "feishu:group", status: "active", created_at: "2026-07-19T10:00:00Z" },
      { target_id: "target-p2p", channel: "feishu", chat_id: "ou_user654321", chat_type: "p2p", session_key: "feishu:p2p", status: "active", created_at: "2026-07-19T10:01:00Z" },
    ];
    apiMock.getFeishuDeliverySettings.mockResolvedValueOnce({
      targets,
      default_target_id: null,
      effective_target_id: null,
      requires_selection: true,
    });
    apiMock.updateFeishuDeliverySettings.mockResolvedValue({
      targets,
      default_target_id: "target-group",
      effective_target_id: "target-group",
      requires_selection: false,
    });

    render(<Settings />);

    expect(await screen.findByText("飞书发送目标")).toBeInTheDocument();
    expect(screen.getByText(/多个激活目标但尚未设置默认值/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: /飞书 · 群聊 · …123456/ }));

    await waitFor(() => {
      expect(apiMock.updateFeishuDeliverySettings).toHaveBeenCalledWith("target-group");
    });
  });

  it("keeps core settings usable when channel status is unavailable", async () => {
    apiMock.getChannelStatus.mockRejectedValueOnce(new Error("channel runtime unavailable"));

    render(<Settings />);

    expect(await screen.findByText("IM Channels")).toBeInTheDocument();
    expect(screen.getByText("channel runtime unavailable")).toBeInTheDocument();
    expect(screen.getByText("Connection")).toBeInTheDocument();
    expect(screen.queryByText("Settings are unavailable")).not.toBeInTheDocument();
  });

  it("persists the Deep Report feature switch from the research settings card", async () => {
    render(<Settings />);

    const toggle = await screen.findByRole("switch", { name: "启用穿透式单股深度研究" });
    expect(toggle).toHaveAttribute("aria-checked", "true");

    fireEvent.click(toggle);

    await waitFor(() => {
      expect(apiMock.updateResearchSettings).toHaveBeenCalledWith({ deep_report_enabled: false });
      expect(toggle).toHaveAttribute("aria-checked", "false");
    });
  });

  it("persists explicit consent for autonomous monitoring to generate Deep Reports", async () => {
    apiMock.updateResearchSettings.mockResolvedValueOnce(researchSettings(true, true));
    render(<Settings />);

    const toggle = await screen.findByRole("switch", {
      name: "允许 AI 自主监控自动生成穿透式报告",
    });
    expect(toggle).toHaveAttribute("aria-checked", "false");

    fireEvent.click(toggle);

    await waitFor(() => {
      expect(apiMock.updateResearchSettings).toHaveBeenCalledWith({
        monitor_auto_deep_report_enabled: true,
      });
      expect(toggle).toHaveAttribute("aria-checked", "true");
    });
    expect(screen.getByText("已授权")).toBeInTheDocument();
  });

  it("selects the isolated Codex CLI executor only after readiness is confirmed", async () => {
    apiMock.updateResearchSettings.mockResolvedValueOnce(researchSettings(true, false, true));
    render(<Settings />);

    const provider = await screen.findByRole("radio", { name: /Current Provider/ });
    const codexCli = screen.getByRole("radio", { name: /Isolated Codex CLI/ });
    expect(provider).toHaveAttribute("aria-checked", "true");
    expect(codexCli).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("button", { name: /Isolated CLI management/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/Paste it into Windows PowerShell/)).not.toBeInTheDocument();

    fireEvent.click(codexCli);

    await waitFor(() => {
      expect(apiMock.updateResearchSettings).toHaveBeenCalledWith({
        deep_research_engine: "codex_cli",
      });
      expect(codexCli).toHaveAttribute("aria-checked", "true");
      expect(screen.getByRole("button", { name: /Isolated CLI management/ })).toHaveAttribute("aria-expanded", "true");
    });
    expect(screen.getByText(/Paste it into Windows PowerShell/)).toBeInTheDocument();
  });

  it("folds isolated CLI management again when Provider is selected", async () => {
    apiMock.getResearchSettings.mockResolvedValueOnce(researchSettings(true, false, true));
    apiMock.updateResearchSettings.mockResolvedValueOnce(researchSettings());
    render(<Settings />);

    const management = await screen.findByRole("button", { name: /Isolated CLI management/ });
    expect(management).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(screen.getByRole("radio", { name: /Current Provider/ }));

    await waitFor(() => {
      expect(apiMock.updateResearchSettings).toHaveBeenCalledWith({
        deep_research_engine: "provider",
      });
      expect(management).toHaveAttribute("aria-expanded", "false");
    });
    expect(screen.queryByText(/Paste it into Windows PowerShell/)).not.toBeInTheDocument();
  });

  it("keeps Provider available when the optional Codex CLI is not ready", async () => {
    apiMock.getCodexCliStatus.mockResolvedValueOnce(codexStatus({
      ready: false,
      auth_state: "unauthenticated",
    }));
    render(<Settings />);

    const provider = await screen.findByRole("radio", { name: /Current Provider/ });
    const codexCli = screen.getByRole("radio", { name: /Isolated Codex CLI/ });

    expect(provider).toBeEnabled();
    expect(provider).toHaveAttribute("aria-checked", "true");
    expect(codexCli).toBeDisabled();
  });

  it("opens the fixed local Codex login flow", async () => {
    apiMock.getResearchSettings.mockResolvedValueOnce(researchSettings(true, false, true));
    render(<Settings />);
    const button = await screen.findByRole("button", { name: "Open PowerShell login" });

    fireEvent.click(button);

    await waitFor(() => expect(apiMock.openCodexCliLogin).toHaveBeenCalledTimes(1));
  });

  it("shows the server-side device login command for remote settings", async () => {
    apiMock.getResearchSettings.mockResolvedValueOnce(researchSettings(true, false, true));
    apiMock.getCodexCliStatus.mockResolvedValueOnce(codexStatus({
      ready: false,
      auth_state: "unauthenticated",
      environment: "remote",
      command_shell: "terminal",
      can_launch_login: false,
      login_command: "codex login --device-auth",
    }));
    render(<Settings />);

    expect(await screen.findByText(/remote browser cannot open that host's terminal/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open Codex login" })).not.toBeInTheDocument();
  });

  it("explains one-off execution and where copied Windows commands must run", async () => {
    apiMock.getResearchSettings.mockResolvedValueOnce(researchSettings(true, false, true));
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(<Settings />);

    expect(await screen.findByText("Isolated CLI management")).toBeInTheDocument();
    expect(screen.getByText(/fresh, isolated, one-off Codex CLI task/)).toBeInTheDocument();
    expect(screen.getByText(/Paste it into Windows PowerShell/)).toBeInTheDocument();
    const copyLogin = screen.getByRole("button", { name: "Copy PowerShell login command" });
    expect(copyLogin).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy PowerShell install/upgrade command" })).toBeInTheDocument();

    fireEvent.click(copyLogin);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("codex login"));
  });

  it("groups version, login, and model controls into three management cards", async () => {
    apiMock.getResearchSettings.mockResolvedValueOnce(researchSettings(true, false, true));
    apiMock.getLLMSettings.mockResolvedValueOnce(codexLlmSettings());
    render(<Settings />);

    const versionCard = await screen.findByRole("region", { name: "Version and upgrades" });
    const loginCard = screen.getByRole("region", { name: "Login and credentials" });
    const modelCard = screen.getByRole("region", { name: "Isolated CLI model settings" });

    expect(within(versionCard).getByText("0.145.0")).toBeInTheDocument();
    expect(within(versionCard).getByRole("button", { name: "Copy PowerShell install/upgrade command" })).toBeInTheDocument();
    expect(within(versionCard).queryByRole("button", { name: "Copy PowerShell login command" })).not.toBeInTheDocument();
    expect(within(loginCard).getByText("Authenticated")).toBeInTheDocument();
    expect(within(loginCard).getByRole("button", { name: "Copy PowerShell login command" })).toBeInTheDocument();
    expect(within(modelCard).getByRole("combobox", { name: "Isolated CLI model" })).toBeInTheDocument();
    expect(within(modelCard).getByRole("combobox", { name: "Isolated CLI reasoning effort" })).toBeInTheDocument();
  });

  it("selects configured Codex models and refreshes account availability", async () => {
    apiMock.getResearchSettings.mockResolvedValueOnce(researchSettings(true, false, true));
    apiMock.getLLMSettings.mockResolvedValueOnce(codexLlmSettings());
    apiMock.getLLMModels.mockResolvedValueOnce({
      provider: "openai-codex",
      source: "remote",
      refreshed_at: "2026-07-18T22:00:00+08:00",
      models: [
        {
          id: "openai-codex/gpt-5.6-sol",
          label: "GPT-5.6-Sol",
          description: "Frontier",
        },
        {
          id: "openai-codex/gpt-5.6-terra",
          label: "GPT-5.6-Terra",
          description: "Balanced",
        },
        {
          id: "openai-codex/gpt-5.5",
          label: "GPT-5.5",
          description: "Previous frontier",
        },
      ],
    });
    render(<Settings />);

    const model = await screen.findByRole("combobox", { name: "Model" });
    expect(model).toHaveValue("openai-codex/gpt-5.6-terra");
    expect(within(model).getByRole("option", { name: "GPT-5.6 Sol" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Isolated CLI model" })).toHaveValue("gpt-5.6-terra");
    expect(screen.getByText("Isolated CLI model settings")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Refresh available models" }));

    await waitFor(() => {
      expect(apiMock.getLLMModels).toHaveBeenCalledWith("openai-codex");
      expect(within(model).getByRole("option", { name: "GPT-5.5" })).toBeInTheDocument();
    });
    expect(model).toHaveValue("openai-codex/gpt-5.6-terra");
  });

  it("saves an independent model and reasoning effort for isolated research", async () => {
    apiMock.getResearchSettings.mockResolvedValueOnce(researchSettings(true, false, true));
    apiMock.getLLMSettings.mockResolvedValueOnce(codexLlmSettings());
    apiMock.updateResearchSettings.mockResolvedValueOnce({
      ...researchSettings(),
      codex_cli_model: "gpt-5.6-sol",
      codex_cli_reasoning_effort: "high",
    });
    render(<Settings />);

    const cliModel = await screen.findByRole("combobox", { name: "Isolated CLI model" });
    const cliReasoning = screen.getByRole("combobox", { name: "Isolated CLI reasoning effort" });
    fireEvent.change(cliModel, { target: { value: "gpt-5.6-sol" } });
    fireEvent.change(cliReasoning, { target: { value: "high" } });
    fireEvent.click(screen.getByRole("button", { name: "Save CLI model settings" }));

    await waitFor(() => {
      expect(apiMock.updateResearchSettings).toHaveBeenCalledWith({
        codex_cli_model: "gpt-5.6-sol",
        codex_cli_reasoning_effort: "high",
      });
    });
  });

  it("refreshes the isolated model selector through the Codex Provider catalog", async () => {
    apiMock.getResearchSettings.mockResolvedValueOnce(researchSettings(true, false, true));
    apiMock.getLLMSettings.mockResolvedValueOnce(codexLlmSettings());
    apiMock.getLLMModels.mockResolvedValueOnce({
      provider: "openai-codex",
      source: "remote",
      models: [{
        id: "openai-codex/gpt-5.6-sol",
        label: "GPT-5.6 Sol refreshed",
        description: "Frontier",
        default_reasoning_effort: "high",
        reasoning_efforts: ["high", "xhigh", "max", "ultra"],
      }],
    });
    render(<Settings />);

    fireEvent.click(await screen.findByRole("button", { name: "Refresh isolated CLI models" }));

    await waitFor(() => expect(apiMock.getLLMModels).toHaveBeenCalledWith("openai-codex"));
    const cliModel = screen.getByRole("combobox", { name: "Isolated CLI model" });
    expect(within(cliModel).getByRole("option", { name: "GPT-5.6 Sol refreshed" })).toBeInTheDocument();
  });
});
