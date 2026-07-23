"""Versioned official mainland-market settlement and tax rules."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool


_RULES: dict[str, dict[str, Any]] = {
    "sell_proceeds_reuse": {
        "rule_id": "sse.sell_proceeds_reuse.v1",
        "effective_from": "current",
        "statement": (
            "Exchange sale proceeds may be used immediately for eligible on-exchange purchases; "
            "cash withdrawal availability is a separate settlement question."
        ),
        "source_title": "上海证券交易所投资者教育：卖出股票后资金使用",
        "official_url": "https://edu.sse.com.cn/etfgame/gametopic/c/4940402.shtml",
    },
    "dividend_tax_fifo": {
        "rule_id": "tax.dividend_fifo.2012.v1",
        "effective_from": "2013-01-01",
        "statement": (
            "Dividend-tax holding periods are determined at transfer and securities are matched "
            "using first-in-first-out. A same-day repurchase does not erase the completed sale."
        ),
        "source_title": "财税〔2012〕85号",
        "official_url": "https://gtm-cn-uqm3dbhc20k.guangdong.chinatax.gov.cn/gdsw/zjfg/2012-11/21/content_f6994554327347b38e8cf788a9d73ff9.shtml",
    },
    "dividend_tax_withholding": {
        "rule_id": "tax.dividend_withholding.2015.v1",
        "effective_from": "2015-09-08",
        "statement": "Differentiated dividend tax is calculated and withheld when the shares are transferred.",
        "source_title": "财税〔2015〕101号",
        "official_url": "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5203902/content.html",
    },
    "stamp_duty": {
        "rule_id": "tax.securities_stamp_duty.2023.v1",
        "effective_from": "2023-08-28",
        "statement": "Securities transaction stamp duty is levied at half of the previous rate (0.05% on taxable sales).",
        "rate": 0.0005,
        "source_title": "财政部 税务总局关于减半征收证券交易印花税的公告",
        "official_url": "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5211343/content.html",
    },
    "broker_commission": {
        "rule_id": "broker.commission.user_specific.v1",
        "effective_from": "current",
        "statement": "Broker commission and minimum charges are account-specific and must come from the broker statement or user input.",
        "official_url": None,
        "exact_value_policy": "unavailable_without_broker_evidence",
    },
}


class MarketRulesTool(BaseTool):
    name = "get_market_rules"
    description = (
        "Return versioned official rules for A-share sale-proceeds reuse, dividend-tax FIFO, "
        "withholding, stamp duty and broker-commission evidence. Use this instead of memory."
    )
    repeatable = True
    is_readonly = True
    parameters = {
        "type": "object",
        "properties": {
            "topics": {
                "type": "array",
                "items": {"type": "string", "enum": list(_RULES)},
                "description": "Omit to return every maintained rule.",
            }
        },
        "required": [],
    }

    def execute(self, **kwargs: Any) -> str:
        topics = [str(item) for item in (kwargs.get("topics") or list(_RULES))]
        unknown = [topic for topic in topics if topic not in _RULES]
        if unknown:
            return json.dumps(
                {"status": "error", "error": f"unsupported rule topics: {', '.join(unknown)}"},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "status": "ok",
                "schema_version": 1,
                "jurisdiction": "CN_mainland_exchange",
                "rules": {topic: _RULES[topic] for topic in topics},
                "trade_execution": "forbidden",
            },
            ensure_ascii=False,
        )
