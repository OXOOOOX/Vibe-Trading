import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SubjectSourceBundle } from "@/components/reports/SubjectSourceBundle";
import type { ReportSourceBundle, ReportSourceDocument, SourceKind } from "@/lib/api";


function sourceDocument(kind: SourceKind, index: number): ReportSourceDocument {
  return {
    document_id: `${kind}-${index}`,
    kind,
    title: `${kind} 资料 ${index}`,
    publisher: "测试来源",
    published_at: `2026-07-${String(20 - index).padStart(2, "0")}T09:00:00+08:00`,
    retrieved_at: "2026-07-19T10:00:00+08:00",
    source_url: null,
    verification_status: kind === "news" ? "live_retrieved" : "official_primary",
    metrics: [],
  };
}

function sourceBundle(): ReportSourceBundle {
  const financialDocuments = Array.from({ length: 5 }, (_, index) => (
    sourceDocument("official_filing", index + 1)
  ));
  const newsDocuments = Array.from({ length: 6 }, (_, index) => (
    sourceDocument("news", index + 1)
  ));

  return {
    symbol: "600519.SH",
    generated_at: "2026-07-19T10:00:00+08:00",
    traceable_count: financialDocuments.length + newsDocuments.length,
    excluded_count: 0,
    verification_counts: {
      official_primary: financialDocuments.length,
      live_retrieved: newsDocuments.length,
      source_recorded: 0,
      historical_context: 0,
    },
    verification_contract: {
      official_primary: "官方原文",
      live_retrieved: "实时抓取",
      source_recorded: "来源记录",
      historical_context: "历史缓存",
    },
    domains: [
      {
        kind: "official_filing",
        label: "官方财报",
        description: "交易所披露原文",
        document_count: financialDocuments.length,
        documents: financialDocuments,
      },
      {
        kind: "news",
        label: "相关新闻",
        description: "按发布时间倒序",
        document_count: newsDocuments.length,
        documents: newsDocuments,
      },
    ],
  };
}

describe("SubjectSourceBundle", () => {
  it("shows persistent per-year discovery, download, parsing and validation progress", () => {
    render(
      <SubjectSourceBundle
        bundle={sourceBundle()}
        annualReportsBackfilling
        annualReportBackfillJob={{
          schema_version: 1,
          job_id: "annual_backfill_fixture1234",
          symbol: "600519.SH",
          years: [2025, 2024],
          force: false,
          status: "running",
          stage: "downloading",
          message: "正在下载并读取 2025 年报全文",
          progress_pct: 63,
          year_progress: [
            {
              year: 2025,
              status: "running",
              current_stage: "downloading",
              message: "正在下载并读取 2025 年报全文",
              phases: {
                discovery: "completed",
                download: "running",
                parsing: "pending",
                validation: "pending",
              },
            },
            {
              year: 2024,
              status: "reused",
              current_stage: "reused",
              message: "2024 年报已归档并可复用",
              phases: {
                discovery: "reused",
                download: "reused",
                parsing: "reused",
                validation: "reused",
              },
            },
          ],
          created_at: "2026-07-19T10:00:00Z",
          updated_at: "2026-07-19T10:01:00Z",
        }}
      />,
    );

    const task = screen.getByRole("region", { name: "历史年报补齐任务" });
    expect(task).toHaveTextContent("后台执行中 · 63%");
    expect(task).toHaveTextContent("2025");
    expect(task).toHaveTextContent("处理中");
    expect(task).toHaveTextContent("2024");
    expect(task).toHaveTextContent("已复用");
    expect(within(task).getByRole("progressbar", { name: "历史年报补齐总进度" })).toHaveValue(63);
    expect(within(task).getAllByText("发现").length).toBeGreaterThan(0);
    expect(within(task).getAllByText("下载").length).toBeGreaterThan(0);
    expect(within(task).getAllByText("解析").length).toBeGreaterThan(0);
    expect(within(task).getAllByText("校验").length).toBeGreaterThan(0);
  });

  it("starts a bounded historical annual-report backfill from the subject dossier", () => {
    const onBackfill = vi.fn();
    render(
      <SubjectSourceBundle
        bundle={sourceBundle()}
        onBackfillAnnualReports={onBackfill}
        annualReportCoverage={{
          symbol: "600519.SH",
          requested_years: [2025, 2024],
          covered_years: [2025],
          archived_years: [2025],
          analysis_ready_years: [2025],
          needs_review_years: [],
          unusable_years: [2024],
          missing_years: [2024],
          coverage_ratio: 0.5,
          analysis_ready_ratio: 0.5,
          documents_by_year: {},
        }}
      />,
    );

    fireEvent.change(screen.getByLabelText("历史年报范围"), { target: { value: "10" } });
    fireEvent.click(screen.getByRole("button", { name: "补齐历史年报" }));

    expect(onBackfill).toHaveBeenCalledWith(10);
    const officialSection = screen.getByText("官方财报").closest("article");
    expect(officialSection).not.toBeNull();
    if (!officialSection) return;
    expect(within(officialSection).getByText("年报覆盖 1/2 年")).toBeInTheDocument();
    expect(within(officialSection).getByText("缺 2024")).toBeInTheDocument();
    expect(screen.queryByText(/可直接用于确定性分析/)).not.toBeInTheDocument();
    expect(screen.queryByText(/按内容哈希直接复用/)).not.toBeInTheDocument();
  });

  it("hides a completed backfill task while keeping compact coverage in official disclosure", () => {
    render(
      <SubjectSourceBundle
        bundle={sourceBundle()}
        annualReportBackfillJob={{
          schema_version: 1,
          job_id: "annual_backfill_completed",
          symbol: "600519.SH",
          years: [2025, 2024],
          force: false,
          status: "completed",
          stage: "completed",
          message: "历史年报补齐完成",
          progress_pct: 100,
          year_progress: [],
          created_at: "2026-07-19T10:00:00Z",
          updated_at: "2026-07-19T10:02:00Z",
        }}
        annualReportCoverage={{
          symbol: "600519.SH",
          requested_years: [2025, 2024],
          covered_years: [2025, 2024],
          archived_years: [2025, 2024],
          analysis_ready_years: [2025, 2024],
          needs_review_years: [],
          unusable_years: [],
          missing_years: [],
          coverage_ratio: 1,
          analysis_ready_ratio: 1,
          documents_by_year: {},
        }}
      />,
    );

    expect(screen.queryByRole("region", { name: "历史年报补齐任务" })).not.toBeInTheDocument();
    const officialSection = screen.getByText("官方财报").closest("article");
    expect(officialSection).not.toBeNull();
    if (!officialSection) return;
    expect(within(officialSection).getByText("年报覆盖 2/2 年")).toBeInTheDocument();
  });

  it("links a broker report title to its traceable provider detail page", () => {
    const document = sourceDocument("broker_research", 1);
    document.title = "Broker report";
    document.source_url = "https://data.eastmoney.com/report/zw_stock.jshtml?encodeUrl=example";
    document.evidence_level = "B";
    document.association_scope = "key_constituent";
    document.related_symbol = "688256.SH";
    document.analyst = ["张三", "李四"];
    const bundle = sourceBundle();
    bundle.domains = [{
      kind: "broker_research",
      label: "Broker research",
      description: "Traceable metadata",
      document_count: 1,
      documents: [document],
    }];

    render(<SubjectSourceBundle bundle={bundle} />);

    expect(screen.getByText("Broker report").closest("a")).toHaveAttribute(
      "href",
      document.source_url,
    );
    expect(screen.getByText("B 级券商观点")).toBeInTheDocument();
    expect(screen.getByText("成分股延伸 · 688256.SH")).toBeInTheDocument();
    expect(screen.getByText(/分析师 张三、李四/)).toBeInTheDocument();
    expect(screen.getByText(/不等于官方事实/)).toBeInTheDocument();
  });

  it("previews three financial documents and five news items, then expands each domain independently", () => {
    render(<SubjectSourceBundle bundle={sourceBundle()} />);

    const financialSection = screen.getByText("官方财报").closest("article");
    const newsSection = screen.getByText("相关新闻").closest("article");
    expect(financialSection).not.toBeNull();
    expect(newsSection).not.toBeNull();
    if (!financialSection || !newsSection) return;

    expect(within(financialSection).getAllByText(/official_filing 资料/)).toHaveLength(3);
    expect(within(financialSection).queryByText("official_filing 资料 4")).not.toBeInTheDocument();
    expect(within(newsSection).getAllByText(/news 资料/)).toHaveLength(5);
    expect(within(newsSection).queryByText("news 资料 6")).not.toBeInTheDocument();

    const financialToggle = within(financialSection).getByRole("button", { name: "展开全部 5 条" });
    expect(financialToggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(financialToggle);

    expect(within(financialSection).getAllByText(/official_filing 资料/)).toHaveLength(5);
    expect(within(newsSection).getAllByText(/news 资料/)).toHaveLength(5);
    const financialCollapse = within(financialSection).getByRole("button", { name: "收起，保留最近 3 条" });
    expect(financialCollapse).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(within(newsSection).getByRole("button", { name: "展开全部 6 条" }));
    expect(within(newsSection).getAllByText(/news 资料/)).toHaveLength(6);
    expect(within(newsSection).getByRole("button", { name: "收起，保留最近 5 条" })).toBeInTheDocument();

    fireEvent.click(financialCollapse);
    expect(within(financialSection).getAllByText(/official_filing 资料/)).toHaveLength(3);
  });

  it("sorts every source domain by published time from newest to oldest without mutating the bundle", () => {
    const bundle = sourceBundle();
    const originalOrder = [
      bundle.domains[0].documents[4],
      bundle.domains[0].documents[1],
      bundle.domains[0].documents[0],
      bundle.domains[0].documents[3],
      bundle.domains[0].documents[2],
    ];
    bundle.domains[0].documents = originalOrder;

    render(<SubjectSourceBundle bundle={bundle} />);

    const officialSection = screen.getByText("官方财报").closest("article");
    expect(officialSection).not.toBeNull();
    if (!officialSection) return;
    expect(within(officialSection).getAllByText(/official_filing 资料/).map((node) => node.textContent)).toEqual([
      "official_filing 资料 1",
      "official_filing 资料 2",
      "official_filing 资料 3",
    ]);
    expect(bundle.domains[0].documents).toEqual(originalOrder);
  });

  it("does not render a fold control when a filtered domain fits in its preview", () => {
    render(<SubjectSourceBundle bundle={sourceBundle()} />);

    fireEvent.click(screen.getByRole("button", { name: "已实时抓取" }));

    const financialSection = screen.getByText("官方财报").closest("article");
    const newsSection = screen.getByText("相关新闻").closest("article");
    expect(financialSection).not.toBeNull();
    expect(newsSection).not.toBeNull();
    if (!financialSection || !newsSection) return;

    expect(within(financialSection).queryByRole("button", { name: /展开全部/ })).not.toBeInTheDocument();
    expect(within(newsSection).getByRole("button", { name: "展开全部 6 条" })).toBeInTheDocument();
  });

  it("hides overview domains and recalculates counts for the supplementary evidence view", () => {
    const bundle = sourceBundle();
    const etfDomains = ([
      ["fund_product", "ETF 产品资料"],
      ["index_methodology", "指数编制方案"],
      ["index_constituents", "成分与权重"],
      ["fund_share_scale", "ETF 份额"],
      ["market_data", "行情快照"],
    ] as const).map(([kind, label]) => ({
      kind,
      label,
      description: `${label}说明`,
      document_count: 1,
      documents: [sourceDocument(kind, 1)],
    }));
    bundle.domains.push(...etfDomains);
    bundle.traceable_count += etfDomains.length;
    bundle.verification_counts.official_primary += etfDomains.length;

    const { rerender } = render(
      <SubjectSourceBundle bundle={bundle} showOverviewSources={false} />,
    );

    expect(screen.getByText("官方财报")).toBeInTheDocument();
    expect(screen.getByText("相关新闻")).toBeInTheDocument();
    expect(screen.queryByText("ETF 产品资料")).not.toBeInTheDocument();
    expect(screen.queryByText("指数编制方案")).not.toBeInTheDocument();
    expect(screen.queryByText("成分与权重")).not.toBeInTheDocument();
    expect(screen.queryByText("ETF 份额")).not.toBeInTheDocument();
    expect(screen.queryByText("行情快照")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "标的资料与证据" })).toHaveTextContent("可追溯资料 11 条");

    rerender(<SubjectSourceBundle bundle={bundle} showOverviewSources />);

    expect(screen.getByText("ETF 产品资料")).toBeInTheDocument();
    expect(screen.getByText("指数编制方案")).toBeInTheDocument();
    expect(screen.getByText("成分与权重")).toBeInTheDocument();
    expect(screen.getByText("ETF 份额")).toBeInTheDocument();
    expect(screen.getByText("行情快照")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "标的资料与证据" })).toHaveTextContent("可追溯资料 16 条");
  });
});
