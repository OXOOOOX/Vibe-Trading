export interface DeepReportSessionIdentity {
  securityName: string;
  symbol: string;
}

export function initialSessionTitle(
  prompt: string,
  deepReportIdentity?: DeepReportSessionIdentity,
): string {
  if (deepReportIdentity) {
    return `${deepReportIdentity.securityName}（${deepReportIdentity.symbol}）穿透式深度研究`;
  }
  return prompt.slice(0, 50);
}
