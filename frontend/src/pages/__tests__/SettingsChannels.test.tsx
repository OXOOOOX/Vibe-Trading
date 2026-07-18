import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Settings } from "../Settings";

const apiMock = vi.hoisted(() => ({
  getLLMSettings: vi.fn(),
  getDataSourceSettings: vi.fn(),
  getResearchSettings: vi.fn(),
  getChannelStatus: vi.fn(),
  startChannels: vi.fn(),
  stopChannels: vi.fn(),
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

function researchSettings(enabled = true, auto = false) {
  return {
    deep_report_enabled: enabled,
    equity_deep_research_enabled: enabled,
    monitor_auto_deep_report_enabled: auto,
    effective_monitor_auto_deep_report_enabled: enabled && auto,
    enabled_profiles: ["equity_deep_research"],
    available_profiles: ["equity_deep_research"],
    env_path: "agent/.env",
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
    apiMock.getDataSourceSettings.mockResolvedValue(dataSourceSettings());
    apiMock.getResearchSettings.mockResolvedValue(researchSettings());
    apiMock.getChannelStatus.mockResolvedValue(channelStatus());
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
});
