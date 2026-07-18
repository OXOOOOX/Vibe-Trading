import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { toast } from "sonner";

import {
  MONITOR_EFFECTS_RECENT_IDS_KEY,
  MONITOR_EFFECTS_STORAGE_KEY,
  PortfolioMonitorEffectsProvider,
  usePortfolioMonitorEffects,
} from "../PortfolioMonitorEffectsProvider";
import { api, type MonitorEvent, type PortfolioMonitoringStatus } from "@/lib/api";

type Listener = (event: Event) => void;

class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly listeners = new Map<string, Set<Listener>>();
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: Listener) {
    const listeners = this.listeners.get(type) ?? new Set<Listener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: Listener) {
    this.listeners.get(type)?.delete(listener);
  }

  close() {
    this.closed = true;
  }

  emit(type: string, data: unknown, id = "") {
    const event = new MessageEvent(type, { data: JSON.stringify(data), lastEventId: id });
    this.listeners.get(type)?.forEach((listener) => listener(event));
  }
}

class MockBroadcastChannel {
  static channels: MockBroadcastChannel[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;

  constructor(readonly name: string) {
    MockBroadcastChannel.channels.push(this);
  }

  postMessage(data: unknown) {
    for (const channel of MockBroadcastChannel.channels) {
      if (channel !== this && channel.name === this.name) channel.onmessage?.(new MessageEvent("message", { data }));
    }
  }

  close() {
    MockBroadcastChannel.channels = MockBroadcastChannel.channels.filter((channel) => channel !== this);
  }
}

const play = vi.fn<() => Promise<void>>();
const pause = vi.fn();

class MockAudio {
  currentTime = 0;
  preload = "";
  volume = 1;
  play = play;
  pause = pause;

  constructor(readonly src: string) {}
}

function Probe({ name = "probe" }: { name?: string }) {
  const effects = usePortfolioMonitorEffects();
  return (
    <div data-testid={name}>
      <output data-testid={`${name}-enabled`}>{String(effects.enabled)}</output>
      <output data-testid={`${name}-volume`}>{effects.volume}</output>
      <output data-testid={`${name}-live`}>{effects.liveEvents.length}</output>
      <output data-testid={`${name}-reset`}>{effects.resetVersion}</output>
      <output data-testid={`${name}-status`}>{effects.playbackStatus}</output>
      <button type="button" onClick={() => void effects.enableAndTest()}>enable</button>
      <button type="button" onClick={() => effects.setVolume(0.25)}>set volume</button>
    </div>
  );
}

function monitorEvent(
  eventId: string,
  firstSeenAt = new Date().toISOString(),
  direction: "above" | "below" = "above",
): MonitorEvent {
  return {
    event_id: eventId,
    profile_id: "profile-1",
    symbol: `${eventId}.SH`,
    plan_version: 3,
    kind: "market_rule_trigger",
    status: "confirmed",
    severity: "warning",
    title: "关键点位突破",
    summary: "连续两根闭合 K 线确认。",
    facts: {
      client_rule_id: "breakout",
      direction,
      threshold: 2.2,
      confirmation_count: 2,
      alert_cue: "ymca_v1",
      delivery_mode: "deliver",
    },
    first_seen_at: firstSeenAt,
  };
}

const originalLocks = Object.getOwnPropertyDescriptor(navigator, "locks");

beforeEach(() => {
  localStorage.clear();
  MockEventSource.instances = [];
  MockBroadcastChannel.channels = [];
  play.mockReset().mockResolvedValue(undefined);
  pause.mockReset();
  vi.restoreAllMocks();
  vi.stubGlobal("EventSource", MockEventSource);
  vi.stubGlobal("BroadcastChannel", MockBroadcastChannel);
  vi.stubGlobal("Audio", MockAudio);
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:ymca") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  Object.defineProperty(navigator, "locks", {
    configurable: true,
    value: { request: vi.fn(async (_name, _options, callback) => callback({ name: _name })) },
  });
  vi.spyOn(api, "getPortfolioMonitorYmcaAudio").mockResolvedValue(new Blob(["audio"]));
  vi.spyOn(api, "portfolioMonitorEventsSseUrl").mockReturnValue("/portfolio/monitor-events/stream?api_key=test");
  vi.spyOn(api, "getPortfolioMonitoringStatus").mockResolvedValue({
    effects: {
      ymca_v1: {
        audio_ready: true,
        up_sticker_ready: true,
        down_sticker_ready: true,
        sticker_ready: true,
        available: true,
      },
    },
  } as PortfolioMonitoringStatus);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  if (originalLocks) Object.defineProperty(navigator, "locks", originalLocks);
  else delete (navigator as Navigator & { locks?: unknown }).locks;
});

it("defaults sound to off at 60% and fetches audio only after Enable & Test", async () => {
  render(<PortfolioMonitorEffectsProvider><Probe /></PortfolioMonitorEffectsProvider>);

  expect(screen.getByTestId("probe-enabled")).toHaveTextContent("false");
  expect(screen.getByTestId("probe-volume")).toHaveTextContent("0.6");
  expect(api.getPortfolioMonitorYmcaAudio).not.toHaveBeenCalled();
  expect(MockEventSource.instances[0].url).toContain("api_key=test");

  fireEvent.click(screen.getByRole("button", { name: "enable" }));
  await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
  expect(api.getPortfolioMonitorYmcaAudio).toHaveBeenCalledTimes(1);
  expect(JSON.parse(localStorage.getItem(MONITOR_EFFECTS_STORAGE_KEY) || "{}")).toEqual({
    version: 1,
    enabled: true,
    volume: 0.6,
  });
});

it("recovers from corrupt preferences and persists volume with the versioned schema", () => {
  localStorage.setItem(MONITOR_EFFECTS_STORAGE_KEY, "{not-json");
  render(<PortfolioMonitorEffectsProvider><Probe /></PortfolioMonitorEffectsProvider>);

  expect(screen.getByTestId("probe-enabled")).toHaveTextContent("false");
  expect(screen.getByTestId("probe-volume")).toHaveTextContent("0.6");
  fireEvent.click(screen.getByRole("button", { name: "set volume" }));
  expect(JSON.parse(localStorage.getItem(MONITOR_EFFECTS_STORAGE_KEY) || "{}")).toEqual({
    version: 1,
    enabled: false,
    volume: 0.25,
  });
});

it("coalesces a qualifying event into a visual toast while sound is disabled", async () => {
  vi.useFakeTimers();
  const toastSuccess = vi.spyOn(toast, "success");
  render(<PortfolioMonitorEffectsProvider><Probe /></PortfolioMonitorEffectsProvider>);
  const event = monitorEvent("silent-cue");

  await act(async () => {
    MockEventSource.instances[0].emit("portfolio.monitor.confirmed", { event }, event.event_id);
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(1_000);
  });

  expect(toastSuccess).toHaveBeenCalledWith("silent-cue.SH 已向上突破 YMCA 关键点位。");
  expect(api.getPortfolioMonitoringStatus).not.toHaveBeenCalled();
  expect(api.getPortfolioMonitorYmcaAudio).not.toHaveBeenCalled();
  expect(play).not.toHaveBeenCalled();
});

it("keeps stale reconnect events in the live list without replaying them", async () => {
  localStorage.setItem(MONITOR_EFFECTS_STORAGE_KEY, JSON.stringify({ version: 1, enabled: true, volume: 0.6 }));
  render(<PortfolioMonitorEffectsProvider><Probe /></PortfolioMonitorEffectsProvider>);
  const stale = monitorEvent("stale", new Date(Date.now() - 121_000).toISOString());

  act(() => MockEventSource.instances[0].emit("portfolio.monitor.confirmed", { event: stale }, stale.event_id));

  expect(screen.getByTestId("probe-live")).toHaveTextContent("1");
  expect(play).not.toHaveBeenCalled();
  expect(api.getPortfolioMonitorYmcaAudio).not.toHaveBeenCalled();
});

it("lists shadow and cue-free events without playing either one", () => {
  localStorage.setItem(MONITOR_EFFECTS_STORAGE_KEY, JSON.stringify({ version: 1, enabled: true, volume: 0.6 }));
  render(<PortfolioMonitorEffectsProvider><Probe /></PortfolioMonitorEffectsProvider>);
  const shadow = monitorEvent("shadow");
  shadow.facts.delivery_mode = "shadow";
  const cueFree = monitorEvent("cue-free");
  cueFree.facts.alert_cue = "none";

  act(() => {
    MockEventSource.instances[0].emit("portfolio.monitor.confirmed", { event: shadow }, shadow.event_id);
    MockEventSource.instances[0].emit("portfolio.monitor.confirmed", { event: cueFree }, cueFree.event_id);
  });

  expect(screen.getByTestId("probe-live")).toHaveTextContent("2");
  expect(play).not.toHaveBeenCalled();
  expect(api.getPortfolioMonitorYmcaAudio).not.toHaveBeenCalled();
});

it("lists a malformed cue event without playing it when the direction is not a price crossing", () => {
  localStorage.setItem(MONITOR_EFFECTS_STORAGE_KEY, JSON.stringify({ version: 1, enabled: true, volume: 0.6 }));
  render(<PortfolioMonitorEffectsProvider><Probe /></PortfolioMonitorEffectsProvider>);
  const malformed = monitorEvent("malformed-direction");
  malformed.facts.direction = "enter";

  act(() => {
    MockEventSource.instances[0].emit("portfolio.monitor.confirmed", { event: malformed }, malformed.event_id);
  });

  expect(screen.getByTestId("probe-live")).toHaveTextContent("1");
  expect(play).not.toHaveBeenCalled();
  expect(api.getPortfolioMonitorYmcaAudio).not.toHaveBeenCalled();
});

it("increments the reset signal without treating a reset frame as a monitor event", () => {
  render(<PortfolioMonitorEffectsProvider><Probe /></PortfolioMonitorEffectsProvider>);

  act(() => MockEventSource.instances[0].emit("portfolio.monitor.reset", {
    reason: "cursor_not_found",
    cursor: "latest",
  }));

  expect(screen.getByTestId("probe-reset")).toHaveTextContent("1");
  expect(screen.getByTestId("probe-live")).toHaveTextContent("0");
  expect(play).not.toHaveBeenCalled();
});

it("uses Web Locks and shared recent IDs to dedupe tabs, then coalesces one-second bursts", async () => {
  vi.useFakeTimers();
  const toastSuccess = vi.spyOn(toast, "success");
  localStorage.setItem(MONITOR_EFFECTS_STORAGE_KEY, JSON.stringify({ version: 1, enabled: true, volume: 0.6 }));
  render(
    <>
      <PortfolioMonitorEffectsProvider><Probe name="one" /></PortfolioMonitorEffectsProvider>
      <PortfolioMonitorEffectsProvider><Probe name="two" /></PortfolioMonitorEffectsProvider>
    </>,
  );
  const first = monitorEvent("event-one");
  const second = monitorEvent("event-two", new Date().toISOString(), "below");

  await act(async () => {
    for (const source of MockEventSource.instances) {
      source.emit("portfolio.monitor.confirmed", { event: first }, first.event_id);
      source.emit("portfolio.monitor.confirmed", { event: second }, second.event_id);
      source.emit("portfolio.monitor.confirmed", { event: first }, first.event_id);
    }
    await Promise.resolve();
    await Promise.resolve();
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_000);
  });

  expect(play).toHaveBeenCalledTimes(1);
  expect(toastSuccess).toHaveBeenCalledWith(
    "2 个 YMCA 关键点位同时触发上涨/下跌，提醒音已播放：event-one.SH、event-two.SH",
  );
  expect(api.getPortfolioMonitorYmcaAudio).toHaveBeenCalledTimes(1);
  expect((navigator.locks.request as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(0);
  expect(JSON.parse(localStorage.getItem(MONITOR_EFFECTS_RECENT_IDS_KEY) || "{}").ids).toEqual([
    "event-one",
    "event-two",
  ]);
});

it("plays the same audio for a downward event even when its Feishu sticker is unavailable", async () => {
  vi.useFakeTimers();
  const toastSuccess = vi.spyOn(toast, "success");
  localStorage.setItem(MONITOR_EFFECTS_STORAGE_KEY, JSON.stringify({ version: 1, enabled: true, volume: 0.6 }));
  vi.mocked(api.getPortfolioMonitoringStatus).mockResolvedValueOnce({
    effects: {
      ymca_v1: {
        audio_ready: true,
        up_sticker_ready: true,
        down_sticker_ready: false,
        sticker_ready: false,
        available: false,
      },
    },
  } as PortfolioMonitoringStatus);
  render(<PortfolioMonitorEffectsProvider><Probe /></PortfolioMonitorEffectsProvider>);
  const event = monitorEvent("down-cue", new Date().toISOString(), "below");

  await act(async () => {
    MockEventSource.instances[0].emit("portfolio.monitor.confirmed", { event }, event.event_id);
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(1_000);
  });

  expect(play).toHaveBeenCalledTimes(1);
  expect(api.getPortfolioMonitorYmcaAudio).toHaveBeenCalledTimes(1);
  expect(toastSuccess).toHaveBeenCalledWith(
    "down-cue.SH 已向下跌破 YMCA 关键点位，提醒音已播放。",
  );
});

it("does not duplicate playback when StrictMode remounts the global provider", async () => {
  vi.useFakeTimers();
  localStorage.setItem(MONITOR_EFFECTS_STORAGE_KEY, JSON.stringify({ version: 1, enabled: true, volume: 0.6 }));
  render(
    <StrictMode>
      <PortfolioMonitorEffectsProvider><Probe /></PortfolioMonitorEffectsProvider>
    </StrictMode>,
  );
  const source = MockEventSource.instances.at(-1)!;
  const event = monitorEvent("strict-event");

  await act(async () => {
    source.emit("portfolio.monitor.confirmed", { event }, event.event_id);
    source.emit("portfolio.monitor.confirmed", { event }, event.event_id);
    await Promise.resolve();
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_000);
  });

  expect(MockEventSource.instances.length).toBeGreaterThanOrEqual(2);
  expect(play).toHaveBeenCalledTimes(1);
});

it("drops the current batch with a visual unavailable state when the fresh audio GET fails", async () => {
  vi.useFakeTimers();
  localStorage.setItem(MONITOR_EFFECTS_STORAGE_KEY, JSON.stringify({ version: 1, enabled: true, volume: 0.6 }));
  vi.mocked(api.getPortfolioMonitorYmcaAudio).mockRejectedValueOnce(new Error("audio removed"));
  render(<PortfolioMonitorEffectsProvider><Probe /></PortfolioMonitorEffectsProvider>);
  const event = monitorEvent("missing-audio");

  await act(async () => {
    MockEventSource.instances[0].emit("portfolio.monitor.confirmed", { event }, event.event_id);
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(1_000);
  });

  expect(api.getPortfolioMonitoringStatus).toHaveBeenCalledTimes(1);
  expect(api.getPortfolioMonitorYmcaAudio).toHaveBeenCalledTimes(1);
  expect(play).not.toHaveBeenCalled();
  expect(screen.getByTestId("probe-status")).toHaveTextContent("unavailable");
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(play).not.toHaveBeenCalled();
});

it("shows a recovery action when play is rejected and does not enable future playback", async () => {
  play.mockRejectedValueOnce(new DOMException("user gesture required", "NotAllowedError"));
  render(<PortfolioMonitorEffectsProvider><Probe /></PortfolioMonitorEffectsProvider>);

  fireEvent.click(screen.getByRole("button", { name: "enable" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("YMCA 提醒音被浏览器阻止");
  expect(screen.getByRole("button", { name: "重新启用并试听" })).toBeInTheDocument();
  expect(screen.getByTestId("probe-enabled")).toHaveTextContent("false");
  expect(localStorage.getItem(MONITOR_EFFECTS_STORAGE_KEY)).toBeNull();
});
