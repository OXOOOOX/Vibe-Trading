import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { AlertTriangle, Volume2 } from "lucide-react";
import { toast } from "sonner";

import {
  api,
  type MonitorEffectAvailability,
  type MonitorEvent,
} from "@/lib/api";

const PREFERENCES_VERSION = 1 as const;
const MAX_RECENT_EVENT_IDS = 100;
const MAX_PLAYBACK_AGE_MS = 2 * 60 * 1_000;
const CLOCK_SKEW_ALLOWANCE_MS = 30_000;
const COALESCE_WINDOW_MS = 1_000;
const CHANNEL_NAME = "vibe-trading:portfolio-monitor-effects:v1";
const PLAYBACK_LOCK_PREFIX = "vibe-trading:portfolio-monitor-effect:";

export const MONITOR_EFFECTS_STORAGE_KEY = "vibe-trading:portfolio-monitor-effects:v1";
export const MONITOR_EFFECTS_RECENT_IDS_KEY = "vibe-trading:portfolio-monitor-effect-ids:v1";

interface MonitorEffectPreferences {
  version: typeof PREFERENCES_VERSION;
  enabled: boolean;
  volume: number;
}

type PlaybackStatus = "disabled" | "loading" | "ready" | "blocked" | "unavailable" | "error";
type StreamStatus = "connecting" | "connected" | "reconnecting" | "disconnected";
type YmcaAvailability = MonitorEffectAvailability;

interface PortfolioMonitorEffectsValue {
  enabled: boolean;
  volume: number;
  playbackStatus: PlaybackStatus;
  streamStatus: StreamStatus;
  playbackError: string | null;
  availability: YmcaAvailability | null;
  liveEvents: MonitorEvent[];
  resetVersion: number;
  enableAndTest: () => Promise<boolean>;
  testSound: () => Promise<boolean>;
  disableSound: () => void;
  setVolume: (volume: number) => void;
  syncAvailability: (availability: YmcaAvailability | null | undefined) => void;
}

const DEFAULT_PREFERENCES: MonitorEffectPreferences = {
  version: PREFERENCES_VERSION,
  enabled: false,
  volume: 0.6,
};

const EMPTY_CONTEXT: PortfolioMonitorEffectsValue = {
  enabled: false,
  volume: DEFAULT_PREFERENCES.volume,
  playbackStatus: "disabled",
  streamStatus: "disconnected",
  playbackError: null,
  availability: null,
  liveEvents: [],
  resetVersion: 0,
  enableAndTest: async () => false,
  testSound: async () => false,
  disableSound: () => undefined,
  setVolume: () => undefined,
  syncAvailability: () => undefined,
};

const PortfolioMonitorEffectsContext = createContext<PortfolioMonitorEffectsValue>(EMPTY_CONTEXT);

function clampVolume(value: unknown): number {
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? Math.min(1, Math.max(0, number)) : DEFAULT_PREFERENCES.volume;
}

function readPreferences(): MonitorEffectPreferences {
  try {
    const raw = window.localStorage.getItem(MONITOR_EFFECTS_STORAGE_KEY);
    if (!raw) return DEFAULT_PREFERENCES;
    const value = JSON.parse(raw) as Partial<MonitorEffectPreferences>;
    if (value.version !== PREFERENCES_VERSION || typeof value.enabled !== "boolean") {
      return DEFAULT_PREFERENCES;
    }
    return {
      version: PREFERENCES_VERSION,
      enabled: value.enabled,
      volume: clampVolume(value.volume),
    };
  } catch {
    return DEFAULT_PREFERENCES;
  }
}

function writePreferences(value: MonitorEffectPreferences): void {
  try {
    window.localStorage.setItem(MONITOR_EFFECTS_STORAGE_KEY, JSON.stringify(value));
  } catch {
    // Storage is an optimization; playback still works in this tab.
  }
}

function readRecentEventIds(): string[] {
  try {
    const raw = window.localStorage.getItem(MONITOR_EFFECTS_RECENT_IDS_KEY);
    if (!raw) return [];
    const value = JSON.parse(raw) as { version?: number; ids?: unknown };
    if (value.version !== PREFERENCES_VERSION || !Array.isArray(value.ids)) return [];
    return value.ids.filter((item): item is string => typeof item === "string").slice(-MAX_RECENT_EVENT_IDS);
  } catch {
    return [];
  }
}

function writeRecentEventIds(ids: string[]): void {
  try {
    window.localStorage.setItem(MONITOR_EFFECTS_RECENT_IDS_KEY, JSON.stringify({
      version: PREFERENCES_VERSION,
      ids: ids.slice(-MAX_RECENT_EVENT_IDS),
    }));
  } catch {
    // In-memory deduplication remains active when storage is unavailable.
  }
}

function isMonitorEvent(value: unknown): value is MonitorEvent {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<MonitorEvent>;
  return typeof candidate.event_id === "string"
    && typeof candidate.symbol === "string"
    && typeof candidate.first_seen_at === "string"
    && Boolean(candidate.facts && typeof candidate.facts === "object");
}

function parseMonitorEvent(raw: MessageEvent): MonitorEvent | null {
  try {
    const payload = JSON.parse(String(raw.data)) as { event?: unknown } | unknown;
    const nested = payload && typeof payload === "object" && "event" in payload
      ? (payload as { event?: unknown }).event
      : payload;
    if (!isMonitorEvent(nested)) return null;
    if (!nested.event_id && raw.lastEventId) return { ...nested, event_id: raw.lastEventId };
    return nested;
  } catch {
    return null;
  }
}

function isPlaybackEligible(event: MonitorEvent, nowMs: number): boolean {
  const firstSeenMs = Date.parse(event.first_seen_at);
  if (!Number.isFinite(firstSeenMs)) return false;
  const ageMs = nowMs - firstSeenMs;
  const deliverMode = event.facts.delivery_mode === "deliver"
    || Boolean(event.deliveries?.some((delivery) => delivery.delivery_mode === "deliver"));
  return event.status === "confirmed"
    && event.facts.alert_cue === "ymca_v1"
    && (event.facts.direction === "above" || event.facts.direction === "below")
    && deliverMode
    && ageMs >= -CLOCK_SKEW_ALLOWANCE_MS
    && ageMs <= MAX_PLAYBACK_AGE_MS;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "无法播放 YMCA 提醒音";
}

function eventDirection(event: MonitorEvent): "up" | "down" | "unknown" {
  if (event.facts.direction === "above") return "up";
  if (event.facts.direction === "below") return "down";
  return "unknown";
}

function eventToastMessage(events: MonitorEvent[], played: boolean): string {
  const symbols = [...new Set(events.map((event) => event.symbol))];
  const directions = new Set(events.map(eventDirection));
  const playedSuffix = played ? "，提醒音已播放" : "";
  if (events.length === 1) {
    const action = directions.has("up")
      ? "已向上突破"
      : directions.has("down") ? "已向下跌破" : "已触发";
    return `${symbols[0]} ${action} YMCA 关键点位${playedSuffix}。`;
  }
  const action = directions.size === 1 && directions.has("up")
    ? "同时向上突破"
    : directions.size === 1 && directions.has("down")
      ? "同时向下跌破"
      : "同时触发上涨/下跌";
  return `${events.length} 个 YMCA 关键点位${action}${playedSuffix}：${symbols.join("、")}`;
}

type LockManagerLike = {
  request<T>(
    name: string,
    options: { ifAvailable: true },
    callback: (lock: unknown | null) => T | Promise<T>,
  ): Promise<T>;
};

export function PortfolioMonitorEffectsProvider({ children }: { children: ReactNode }) {
  const [preferences, setPreferences] = useState<MonitorEffectPreferences>(readPreferences);
  const [playbackStatus, setPlaybackStatus] = useState<PlaybackStatus>(
    preferences.enabled ? "ready" : "disabled",
  );
  const [streamStatus, setStreamStatus] = useState<StreamStatus>("connecting");
  const [playbackError, setPlaybackError] = useState<string | null>(null);
  const [availability, setAvailability] = useState<YmcaAvailability | null>(null);
  const [liveEvents, setLiveEvents] = useState<MonitorEvent[]>([]);
  const [resetVersion, setResetVersion] = useState(0);

  const preferencesRef = useRef(preferences);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const audioUrlRef = useRef<string | null>(null);
  const audioPromiseRef = useRef<Promise<HTMLAudioElement> | null>(null);
  const broadcastRef = useRef<BroadcastChannel | null>(null);
  const receivedIdsRef = useRef(new Set<string>());
  const playbackIdsRef = useRef(new Set<string>());
  const playbackIdsInitializedRef = useRef(false);
  const pendingEventsRef = useRef<MonitorEvent[]>([]);
  const coalesceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const receiveEventRef = useRef<(event: MonitorEvent) => void>(() => undefined);

  if (!playbackIdsInitializedRef.current) {
    playbackIdsRef.current = new Set(readRecentEventIds());
    playbackIdsInitializedRef.current = true;
  }

  useEffect(() => {
    preferencesRef.current = preferences;
  }, [preferences]);

  const publishPreferences = useCallback((next: MonitorEffectPreferences) => {
    preferencesRef.current = next;
    setPreferences(next);
    writePreferences(next);
    broadcastRef.current?.postMessage({ type: "preferences", value: next });
  }, []);

  const disposeAudio = useCallback(() => {
    audioRef.current?.pause();
    audioRef.current = null;
    if (audioUrlRef.current) URL.revokeObjectURL(audioUrlRef.current);
    audioUrlRef.current = null;
  }, []);

  const ensureAudio = useCallback(async (forceRefresh = false): Promise<HTMLAudioElement> => {
    if (forceRefresh) {
      if (audioPromiseRef.current) {
        try { await audioPromiseRef.current; } catch { /* fetch below will provide the current result */ }
      }
      disposeAudio();
    }
    if (audioRef.current) return audioRef.current;
    if (audioPromiseRef.current) return audioPromiseRef.current;
    setPlaybackStatus("loading");
    setPlaybackError(null);
    const promise = api.getPortfolioMonitorYmcaAudio().then((blob) => {
      if (!blob.size) throw new Error("YMCA 音频素材为空");
      const objectUrl = URL.createObjectURL(blob);
      const audio = new Audio(objectUrl);
      audio.preload = "auto";
      audio.volume = preferencesRef.current.volume;
      audioRef.current = audio;
      audioUrlRef.current = objectUrl;
      return audio;
    }).finally(() => {
      audioPromiseRef.current = null;
    });
    audioPromiseRef.current = promise;
    return promise;
  }, [disposeAudio]);

  const markPlaybackFailure = useCallback((error: unknown) => {
    const message = errorMessage(error);
    const blocked = error instanceof DOMException && error.name === "NotAllowedError";
    setPlaybackStatus(blocked ? "blocked" : "unavailable");
    setPlaybackError(message);
    toast.error(blocked
      ? "浏览器阻止了 YMCA 自动播放，请重新启用并试听。"
      : `YMCA 提醒音不可用：${message}`);
  }, []);

  const playOnce = useCallback(async (forceRefresh = false): Promise<boolean> => {
    try {
      const audio = await ensureAudio(forceRefresh);
      audio.volume = preferencesRef.current.volume;
      audio.currentTime = 0;
      await audio.play();
      setPlaybackStatus("ready");
      setPlaybackError(null);
      return true;
    } catch (error) {
      markPlaybackFailure(error);
      return false;
    }
  }, [ensureAudio, markPlaybackFailure]);

  const enableAndTest = useCallback(async (): Promise<boolean> => {
    const played = await playOnce();
    if (played) {
      publishPreferences({ ...preferencesRef.current, enabled: true });
      toast.success("YMCA 突破提醒音已启用。");
    }
    return played;
  }, [playOnce, publishPreferences]);

  const testSound = useCallback(async (): Promise<boolean> => {
    const played = await playOnce();
    if (played) toast.success("YMCA 提醒音试听成功。");
    return played;
  }, [playOnce]);

  const disableSound = useCallback(() => {
    audioRef.current?.pause();
    publishPreferences({ ...preferencesRef.current, enabled: false });
    setPlaybackStatus("disabled");
    setPlaybackError(null);
  }, [publishPreferences]);

  const setVolume = useCallback((value: number) => {
    const volume = clampVolume(value);
    if (audioRef.current) audioRef.current.volume = volume;
    publishPreferences({ ...preferencesRef.current, volume });
  }, [publishPreferences]);

  const syncAvailability = useCallback((next: YmcaAvailability | null | undefined) => {
    if (next?.audio_ready === false) {
      disposeAudio();
      if (preferencesRef.current.enabled) setPlaybackStatus("unavailable");
    }
    setAvailability((current) => {
      const normalized = next ?? null;
      if (current?.audio_ready === normalized?.audio_ready
        && current?.sticker_ready === normalized?.sticker_ready
        && current?.up_sticker_ready === normalized?.up_sticker_ready
        && current?.down_sticker_ready === normalized?.down_sticker_ready
        && current?.available === normalized?.available) return current;
      return normalized;
    });
  }, [disposeAudio]);

  const claimPlayback = useCallback(async (eventId: string): Promise<boolean> => {
    const claim = () => {
      const stored = readRecentEventIds();
      for (const id of stored) playbackIdsRef.current.add(id);
      if (playbackIdsRef.current.has(eventId)) return false;
      playbackIdsRef.current.add(eventId);
      const ids = [...playbackIdsRef.current].slice(-MAX_RECENT_EVENT_IDS);
      playbackIdsRef.current = new Set(ids);
      writeRecentEventIds(ids);
      broadcastRef.current?.postMessage({ type: "seen", eventId });
      return true;
    };
    const locks = (navigator as Navigator & { locks?: LockManagerLike }).locks;
    if (!locks?.request) return claim();
    try {
      return await locks.request(
        `${PLAYBACK_LOCK_PREFIX}${eventId}`,
        { ifAvailable: true },
        (lock) => lock ? claim() : false,
      );
    } catch {
      return claim();
    }
  }, []);

  const flushPendingEvents = useCallback(async () => {
    coalesceTimerRef.current = null;
    const events = pendingEventsRef.current.splice(0);
    if (!events.length) return;
    let played = false;
    if (preferencesRef.current.enabled) {
      try {
        const status = await api.getPortfolioMonitoringStatus();
        const currentAvailability = status.effects?.ymca_v1;
        syncAvailability(currentAvailability);
        if (!currentAvailability?.audio_ready) {
          throw new Error("服务器上的 YMCA 音频素材未就绪");
        }
        played = await playOnce(true);
      } catch (error) {
        markPlaybackFailure(error);
      }
    }
    toast.success(eventToastMessage(events, played));
  }, [markPlaybackFailure, playOnce, syncAvailability]);

  receiveEventRef.current = (event) => {
    if (!receivedIdsRef.current.has(event.event_id)) {
      receivedIdsRef.current.add(event.event_id);
      if (receivedIdsRef.current.size > MAX_RECENT_EVENT_IDS) {
        const oldest = receivedIdsRef.current.values().next().value;
        if (oldest) receivedIdsRef.current.delete(oldest);
      }
      setLiveEvents((current) => [event, ...current].slice(0, MAX_RECENT_EVENT_IDS));
    }
    if (!isPlaybackEligible(event, Date.now())) return;
    void claimPlayback(event.event_id).then((claimed) => {
      if (!claimed) return;
      pendingEventsRef.current.push(event);
      if (!coalesceTimerRef.current) {
        coalesceTimerRef.current = setTimeout(() => void flushPendingEvents(), COALESCE_WINDOW_MS);
      }
    });
  };

  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const channel = new BroadcastChannel(CHANNEL_NAME);
    broadcastRef.current = channel;
    channel.onmessage = (message: MessageEvent) => {
      const payload = message.data as { type?: string; eventId?: unknown; value?: unknown };
      if (payload.type === "seen" && typeof payload.eventId === "string") {
        playbackIdsRef.current.add(payload.eventId);
        if (playbackIdsRef.current.size > MAX_RECENT_EVENT_IDS) {
          const oldest = playbackIdsRef.current.values().next().value;
          if (oldest) playbackIdsRef.current.delete(oldest);
        }
      }
      if (payload.type === "preferences" && payload.value && typeof payload.value === "object") {
        const next = payload.value as Partial<MonitorEffectPreferences>;
        if (next.version === PREFERENCES_VERSION && typeof next.enabled === "boolean") {
          const normalized = { version: PREFERENCES_VERSION, enabled: next.enabled, volume: clampVolume(next.volume) };
          preferencesRef.current = normalized;
          setPreferences(normalized);
          if (!normalized.enabled) setPlaybackStatus("disabled");
        }
      }
    };
    return () => {
      channel.close();
      if (broadcastRef.current === channel) broadcastRef.current = null;
    };
  }, []);

  useEffect(() => {
    const handleStorage = (event: StorageEvent) => {
      if (event.key === MONITOR_EFFECTS_STORAGE_KEY) {
        const next = readPreferences();
        preferencesRef.current = next;
        setPreferences(next);
        if (!next.enabled) setPlaybackStatus("disabled");
      } else if (event.key === MONITOR_EFFECTS_RECENT_IDS_KEY) {
        for (const id of readRecentEventIds()) playbackIdsRef.current.add(id);
      }
    };
    window.addEventListener("storage", handleStorage);
    return () => window.removeEventListener("storage", handleStorage);
  }, []);

  useEffect(() => {
    setStreamStatus("connecting");
    const source = new EventSource(api.portfolioMonitorEventsSseUrl());
    const handleConfirmed = (raw: Event) => {
      const event = parseMonitorEvent(raw as MessageEvent);
      if (event) receiveEventRef.current(event);
    };
    const handleReset = () => setResetVersion((value) => value + 1);
    source.addEventListener("portfolio.monitor.confirmed", handleConfirmed);
    source.addEventListener("portfolio.monitor.reset", handleReset);
    source.onopen = () => setStreamStatus("connected");
    source.onerror = () => setStreamStatus("reconnecting");
    return () => {
      source.removeEventListener("portfolio.monitor.confirmed", handleConfirmed);
      source.removeEventListener("portfolio.monitor.reset", handleReset);
      source.close();
    };
  }, []);

  useEffect(() => () => {
    if (coalesceTimerRef.current) clearTimeout(coalesceTimerRef.current);
    disposeAudio();
  }, [disposeAudio]);

  const contextValue = useMemo<PortfolioMonitorEffectsValue>(() => ({
    enabled: preferences.enabled,
    volume: preferences.volume,
    playbackStatus,
    streamStatus,
    playbackError,
    availability,
    liveEvents,
    resetVersion,
    enableAndTest,
    testSound,
    disableSound,
    setVolume,
    syncAvailability,
  }), [
    availability,
    disableSound,
    enableAndTest,
    liveEvents,
    playbackError,
    playbackStatus,
    preferences.enabled,
    preferences.volume,
    resetVersion,
    setVolume,
    streamStatus,
    syncAvailability,
    testSound,
  ]);

  return (
    <PortfolioMonitorEffectsContext.Provider value={contextValue}>
      {children}
      {playbackStatus === "blocked" ? (
        <div
          role="alert"
          className="fixed bottom-4 right-4 z-[80] flex max-w-sm items-start gap-3 rounded-md border border-amber-500/50 bg-background p-4 text-sm shadow-xl"
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden="true" />
          <div>
            <div className="font-medium">YMCA 提醒音被浏览器阻止</div>
            <p className="mt-1 text-xs text-muted-foreground">本次不补播；点击后重新授权未来的突破提醒。</p>
            <button
              type="button"
              onClick={() => void enableAndTest()}
              className="mt-3 inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium hover:bg-muted"
            >
              <Volume2 className="h-3.5 w-3.5" aria-hidden="true" />重新启用并试听
            </button>
          </div>
        </div>
      ) : null}
    </PortfolioMonitorEffectsContext.Provider>
  );
}

export function usePortfolioMonitorEffects(): PortfolioMonitorEffectsValue {
  return useContext(PortfolioMonitorEffectsContext);
}
