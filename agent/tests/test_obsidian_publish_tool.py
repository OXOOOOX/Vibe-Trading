"""Tests for the Obsidian publishing tool."""

from __future__ import annotations

import json
from pathlib import Path

from src.tools.obsidian_publish_tool import PublishObsidianNoteTool


def _body(raw: str) -> dict:
    return json.loads(raw)


def test_publish_obsidian_note_writes_to_configured_vault(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_OBSIDIAN_VAULT_ROOTS", str(tmp_path))

    body = _body(
        PublishObsidianNoteTool().execute(
            path="QQQ/Invest/daily.md",
            content="# Daily\n",
        )
    )

    assert body["status"] == "ok"
    assert (tmp_path / "QQQ" / "Invest" / "daily.md").read_text(encoding="utf-8") == "# Daily\n"


def test_publish_obsidian_note_refuses_overwrite_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_OBSIDIAN_VAULT_ROOTS", str(tmp_path))
    target = tmp_path / "QQQ" / "Invest" / "daily.md"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")

    body = _body(
        PublishObsidianNoteTool().execute(
            path="QQQ/Invest/daily.md",
            content="new",
        )
    )

    assert body["status"] == "error"
    assert target.read_text(encoding="utf-8") == "old"


def test_publish_obsidian_note_overwrites_when_requested(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_OBSIDIAN_VAULT_ROOTS", str(tmp_path))
    target = tmp_path / "QQQ" / "Invest" / "daily.md"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")

    body = _body(
        PublishObsidianNoteTool().execute(
            path="QQQ/Invest/daily.md",
            content="new",
            overwrite=True,
        )
    )

    assert body["status"] == "ok"
    assert target.read_text(encoding="utf-8") == "new"


def test_publish_obsidian_note_rejects_escape(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_OBSIDIAN_VAULT_ROOTS", str(tmp_path))

    body = _body(
        PublishObsidianNoteTool().execute(
            path="../outside.md",
            content="nope",
        )
    )

    assert body["status"] == "error"
    assert not (tmp_path.parent / "outside.md").exists()
