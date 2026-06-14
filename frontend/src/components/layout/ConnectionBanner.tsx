import { WifiOff, RefreshCw } from "lucide-react";
import type { SSEStatus } from "@/hooks/useSSE";
import { useI18n } from "@/lib/i18n";

interface Props {
  status: SSEStatus;
  retryAttempt?: number;
}

export function ConnectionBanner({ status, retryAttempt }: Props) {
  const { language } = useI18n();
  if (status === "connected" || status === "disconnected") return null;

  return (
    <div className="flex items-center gap-2 px-4 py-2 text-xs bg-warning/15 text-warning border-b border-warning/30">
      {status === "reconnecting" ? (
        <>
          <RefreshCw className="h-3.5 w-3.5 animate-spin" />
          <span>{language === "zh-CN" ? `连接已断开，正在进行第 ${retryAttempt || 1} 次重连…` : `Connection lost, reconnecting (attempt ${retryAttempt || 1})…`}</span>
        </>
      ) : (
        <>
          <WifiOff className="h-3.5 w-3.5" />
          <span>{language === "zh-CN" ? "连接已断开" : "Connection lost"}</span>
        </>
      )}
    </div>
  );
}
