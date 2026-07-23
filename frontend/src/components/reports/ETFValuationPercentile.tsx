import { InstrumentHistoricalPercentile } from "@/components/reports/InstrumentHistoricalPercentile";
import type { ETFValuationPercentileSnapshot } from "@/lib/api";


/** Compatibility wrapper for report data produced by the former ETF-only API. */
export function ETFValuationPercentile({
  snapshot,
}: {
  snapshot?: ETFValuationPercentileSnapshot | null;
}) {
  return (
    <InstrumentHistoricalPercentile
      snapshot={snapshot}
      fallbackInstrumentType="etf"
    />
  );
}
