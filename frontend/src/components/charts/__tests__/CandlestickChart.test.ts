import { getCandlestickValues } from "../CandlestickChart";

describe("getCandlestickValues", () => {
  it("uses the raw candlestick data instead of ECharts' encoded value dimensions", () => {
    expect(getCandlestickValues({
      data: [2.172, 2.174, 2.172, 2.174],
      value: [221, 2.172, 2.174, 2.172, 2.174],
    })).toEqual([2.172, 2.174, 2.172, 2.174]);
  });

  it("falls back to the final four encoded values", () => {
    expect(getCandlestickValues({ value: [221, 2.172, 2.174, 2.172, 2.174] }))
      .toEqual([2.172, 2.174, 2.172, 2.174]);
  });

  it("accepts the category index in data before the OHLC values", () => {
    expect(getCandlestickValues({
      data: [221, 2.172, 2.174, 2.172, 2.174],
      value: [221, 2.172, 2.174, 2.172, 2.174],
    })).toEqual([2.172, 2.174, 2.172, 2.174]);
  });

  it("unwraps object-form ECharts data values", () => {
    expect(getCandlestickValues({ data: { value: [221, 2.172, 2.174, 2.172, 2.174] } }))
      .toEqual([2.172, 2.174, 2.172, 2.174]);
  });
});
