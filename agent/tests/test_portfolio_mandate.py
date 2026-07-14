from __future__ import annotations

from pathlib import Path

import pytest

from src.portfolio.mandate import (
    MandateValidationError,
    default_mandate,
    ensure_assignments,
    save_mandate,
    suggest_classifications,
    update_assignment,
)


def test_mandate_rejects_invalid_amount_band(tmp_path: Path) -> None:
    mandate = default_mandate()
    mandate["sleeves"][0].update(
        {"configured": True, "target_amount": 10_000, "min_amount": 12_000}
    )

    with pytest.raises(MandateValidationError, match="cannot exceed"):
        save_mandate(mandate, path=tmp_path / "mandate.json")


def test_agent_assigns_new_holding_and_user_has_final_lock(tmp_path: Path) -> None:
    path = tmp_path / "mandate.json"
    mandate = ensure_assignments(
        [{"symbol": "600036.SH", "name": "招商银行"}], path=path
    )

    assigned = mandate["assignments"]["600036.SH"]
    assert assigned["active_sleeve_id"] == "defensive"
    assert assigned["assigned_by"] == "agent"
    assert assigned["user_locked"] is False
    assert len(assigned["classification_evidence"]["dimensions"]) == 7

    updated = update_assignment("600036", "offensive", path=path)
    assert updated["assignments"]["600036.SH"]["active_sleeve_id"] == "offensive"
    assert updated["assignments"]["600036.SH"]["assigned_by"] == "user"
    assert updated["assignments"]["600036.SH"]["user_locked"] is True
    assert updated["version"] > mandate["version"]

    suggested = suggest_classifications(
        [{"symbol": "600036.SH", "name": "招商银行"}], path=path
    )
    assert suggested["assignments"]["600036.SH"]["active_sleeve_id"] == "offensive"
    assert suggested["assignments"]["600036.SH"]["suggested_sleeve_id"] == "defensive"


def test_agent_reclassification_requires_two_consecutive_confirmations(tmp_path: Path) -> None:
    path = tmp_path / "mandate.json"
    initial = ensure_assignments(
        [{"symbol": "600036.SH", "name": "未知公司"}], path=path
    )
    assert initial["assignments"]["600036.SH"]["active_sleeve_id"] == "offensive"

    first = suggest_classifications(
        [{"symbol": "600036.SH", "name": "招商银行"}], path=path
    )
    assert first["assignments"]["600036.SH"]["active_sleeve_id"] == "offensive"
    assert first["assignments"]["600036.SH"]["suggestion_run_count"] == 1
    assert first["version"] == initial["version"]
    assert first["suggestion_revision"] > initial["suggestion_revision"]

    second = suggest_classifications(
        [{"symbol": "600036.SH", "name": "招商银行"}], path=path
    )
    assert second["assignments"]["600036.SH"]["active_sleeve_id"] == "defensive"
    assert second["version"] > first["version"]


def test_mandate_rejects_cycles_and_parent_assignments(tmp_path: Path) -> None:
    mandate = default_mandate()
    mandate["sleeves"] = [
        {**mandate["sleeves"][0], "id": "parent", "parent_id": "child"},
        {**mandate["sleeves"][1], "id": "child", "parent_id": "parent"},
    ]
    with pytest.raises(MandateValidationError, match="cycle"):
        save_mandate(mandate, path=tmp_path / "cycle.json")

    mandate = default_mandate()
    mandate["sleeves"].append(
        {
            **mandate["sleeves"][0],
            "id": "offensive_growth",
            "name": "进攻成长",
            "parent_id": "offensive",
            "sort_order": 11,
        }
    )
    mandate["assignments"] = {
        "600036.SH": {"active_sleeve_id": "offensive"}
    }
    with pytest.raises(MandateValidationError, match="leaf sleeve"):
        save_mandate(mandate, path=tmp_path / "parent.json")
