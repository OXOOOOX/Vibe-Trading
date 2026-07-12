"""Obsidian publishing tool: write Markdown notes to configured vault roots."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.tools.path_utils import safe_obsidian_note_path
from src.tools.redaction import redact_internal_paths


class PublishObsidianNoteTool(BaseTool):
    """Publish a Markdown note into an explicitly configured Obsidian vault."""

    name = "publish_obsidian_note"
    description = (
        "Publish a Markdown note directly into a configured Obsidian vault. "
        "Use this instead of write_file when the user asks to publish/save/export "
        "Markdown to Obsidian. Requires VIBE_TRADING_OBSIDIAN_VAULT_ROOTS and "
        "only accepts vault-relative .md paths, never absolute paths."
    )
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative Markdown path inside the vault, e.g. QQQ/Invest/daily.md",
            },
            "content": {"type": "string", "description": "Markdown note content"},
            "overwrite": {
                "type": "boolean",
                "description": "Whether to replace an existing note. Defaults to false.",
            },
            "vault_root": {
                "type": "string",
                "description": "Optional configured vault root when multiple roots are configured.",
            },
        },
        "required": ["path", "content"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        note_path = kwargs["path"]
        content = kwargs["content"]
        overwrite = bool(kwargs.get("overwrite", False))
        vault_root = kwargs.get("vault_root")

        try:
            resolved = safe_obsidian_note_path(note_path, vault_root=vault_root)
        except ValueError as exc:
            return json.dumps(
                {
                    "status": "error",
                    "error": str(exc),
                },
                ensure_ascii=False,
            )

        try:
            if resolved.exists() and not overwrite:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "Obsidian note already exists; pass overwrite=true to replace it",
                        "path": str(resolved),
                    },
                    ensure_ascii=False,
                )
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
            return json.dumps(
                {
                    "status": "ok",
                    "path": str(resolved),
                    "bytes_written": len(content.encode("utf-8")),
                    "overwritten": overwrite,
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {
                    "status": "error",
                    "error": redact_internal_paths(str(exc)),
                },
                ensure_ascii=False,
            )
