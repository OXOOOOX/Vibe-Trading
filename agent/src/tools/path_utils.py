"""Path safety helpers used by file-access tools.

Three helpers, three threat models:

* `safe_path(p, workdir)` — tool-controlled sandbox. Resolves `p` under
  `workdir` and rejects any escape. Used by `read_file` / `write_file` /
  `edit_file` where the LLM must stay inside the current run dir.

* `safe_user_path(p)` — user-supplied broker files. Accepts files only under
  explicit import roots, not the whole home directory or project tree.

* `safe_document_path(p)` — document-reader inputs. Uses the same import-root
  boundary as `safe_user_path()`.

All helpers raise ``ValueError`` on rejection — callers already expect this.
"""

from __future__ import annotations

import os
from pathlib import Path

_ALLOWED_FILE_ROOTS_ENV = "VIBE_TRADING_ALLOWED_FILE_ROOTS"
_ALLOWED_RUN_ROOTS_ENV = "VIBE_TRADING_ALLOWED_RUN_ROOTS"
_OBSIDIAN_VAULT_ROOTS_ENV = "VIBE_TRADING_OBSIDIAN_VAULT_ROOTS"


def _rejects_unc(p: str) -> None:
    """Raise ValueError if `p` starts with a UNC share prefix."""
    if p.startswith("\\\\") or p.startswith("//"):
        raise ValueError(f"UNC paths are not allowed: {p!r}")


def safe_path(p: str, workdir: Path) -> Path:
    """Resolve `p` under `workdir` and ensure it stays inside.

    Args:
        p: User-supplied path (relative or absolute).
        workdir: Workspace root. `p` must resolve to a location inside.

    Returns:
        Absolute resolved path inside `workdir`.

    Raises:
        ValueError: If `p` uses a UNC share, or its resolved form escapes
            `workdir`. Callers surface this back to the LLM as a tool error.
    """
    _rejects_unc(p)
    base = Path(workdir).resolve()
    resolved = (base / p).resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Path {p!r} escapes the workspace root") from exc
    return resolved


def _agent_root() -> Path:
    """Return the agent package root."""
    return Path(__file__).resolve().parents[2]


def _configured_file_roots() -> list[Path]:
    """Return file roots configured through the environment."""
    raw = os.getenv(_ALLOWED_FILE_ROOTS_ENV, "")
    roots: list[Path] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        _rejects_unc(item)
        roots.append(Path(item).expanduser().resolve())
    return roots


def _default_file_roots() -> list[Path]:
    """Return default roots for uploaded/imported user files."""
    cwd = Path.cwd().resolve()
    home = Path.home().resolve()
    agent_root = _agent_root()
    return [
        agent_root / "uploads",
        agent_root / "runs",
        cwd / "uploads",
        cwd / "data",
        home / ".vibe-trading" / "uploads",
        home / ".vibe-trading" / "imports",
    ]


def _default_run_roots() -> list[Path]:
    """Return default roots for generated backtest/tool run directories."""
    from src.swarm.store import swarm_runs_root

    cwd = Path.cwd().resolve()
    home = Path.home().resolve()
    agent_root = _agent_root()
    return [
        agent_root / "runs",
        swarm_runs_root(),
        cwd / "runs",
        home / ".vibe-trading" / "shadow_runs",
        home / ".vibe-trading" / "runs",
    ]


def _allowed_file_roots() -> list[Path]:
    """Return all roots allowed for document and broker-file reads."""
    roots: list[Path] = []
    for root in [*_default_file_roots(), *_configured_file_roots()]:
        resolved = root.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _allowed_run_roots() -> list[Path]:
    """Return all roots allowed for run_dir-based tools."""
    raw = os.getenv(_ALLOWED_RUN_ROOTS_ENV, "")
    configured: list[Path] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        _rejects_unc(item)
        configured.append(Path(item).expanduser().resolve())

    roots: list[Path] = []
    for root in [*_default_run_roots(), *configured]:
        resolved = root.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _configured_obsidian_vault_roots() -> list[Path]:
    """Return Obsidian vault roots configured through the environment."""
    raw = os.getenv(_OBSIDIAN_VAULT_ROOTS_ENV, "")
    roots: list[Path] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        _rejects_unc(item)
        resolved = Path(item).expanduser().resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def safe_obsidian_note_path(note_path: str, *, vault_root: str | None = None) -> Path:
    """Resolve an Obsidian note path under an explicitly configured vault root.

    Args:
        note_path: Relative note path inside the vault. Must end in ``.md``.
        vault_root: Optional configured vault root to use when multiple roots
            are configured. If omitted, exactly one configured root is required.

    Returns:
        Absolute resolved path inside the selected vault root.

    Raises:
        ValueError: If no vault root is configured, the note is not Markdown,
            the path is absolute/UNC, or it escapes the selected vault root.
    """
    _rejects_unc(note_path)
    note = Path(note_path)
    if note.is_absolute():
        raise ValueError("Obsidian note path must be relative to the vault root")
    if not note_path.strip() or any(part in {"", ".", ".."} for part in note.parts):
        raise ValueError("Obsidian note path must not contain empty, dot, or parent segments")
    if note.suffix.lower() != ".md":
        raise ValueError("Obsidian note path must end with .md")

    roots = _configured_obsidian_vault_roots()
    if not roots:
        raise ValueError(
            "No Obsidian vault root configured. Set "
            f"{_OBSIDIAN_VAULT_ROOTS_ENV} in agent/.env, for example "
            f"{_OBSIDIAN_VAULT_ROOTS_ENV}=C:\\Users\\you\\Documents\\Obsidian."
        )

    if vault_root:
        _rejects_unc(vault_root)
        selected = Path(vault_root).expanduser().resolve()
        if selected not in roots:
            raise ValueError(
                "Requested Obsidian vault root is not configured in "
                f"{_OBSIDIAN_VAULT_ROOTS_ENV}"
            )
    elif len(roots) == 1:
        selected = roots[0]
    else:
        raise ValueError(
            "Multiple Obsidian vault roots configured; pass vault_root explicitly"
        )

    resolved = (selected / note).resolve()
    if not resolved.is_relative_to(selected):
        raise ValueError("Obsidian note path escapes the vault root")
    return resolved


def _import_candidate(p: str) -> Path:
    """Return the filesystem candidate for an import path.

    Browser uploads are exposed as ``uploads/<name>`` so the UI never needs
    a local absolute path. Resolve that handle back to the agent upload root
    before enforcing the allowlist.
    """
    candidate = Path(p).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()

    parts = candidate.parts
    if parts and parts[0] == "uploads":
        return (_agent_root() / candidate).resolve()
    if len(parts) >= 2 and parts[0] == "agent" and parts[1] == "uploads":
        return (_agent_root() / Path(*parts[1:])).resolve()
    return (Path.cwd() / candidate).resolve()


def _safe_import_path(p: str, *, purpose: str) -> Path:
    """Validate a user-supplied path against explicit import roots.

    Args:
        p: User-supplied path. `~` expansion is supported.
        purpose: Human-readable purpose for error messages.

    Returns:
        Absolute resolved path inside an allowed import root.

    Raises:
        ValueError: If `p` is a UNC share or resolves outside all allowed
            import roots.
    """
    _rejects_unc(p)
    resolved = _import_candidate(p)

    for root in _allowed_file_roots():
        if resolved.is_relative_to(root):
            return resolved

    raise ValueError(
        f"Path {p!r} is outside allowed {purpose} roots. "
        f"Set {_ALLOWED_FILE_ROOTS_ENV} to add an import directory."
    )


def safe_user_path(p: str) -> Path:
    """Validate a user-supplied broker/export file path.

    Args:
        p: User-supplied path. `~` expansion is supported.

    Returns:
        Absolute resolved path inside an allowed import root.

    Raises:
        ValueError: If `p` is a UNC share or resolves outside all allowed
            import roots.
    """
    return _safe_import_path(p, purpose="user-file")


def safe_document_path(p: str) -> Path:
    """Validate a document-reader file path.

    Args:
        p: User-supplied document path. `~` expansion is supported.

    Returns:
        Absolute resolved path inside an allowed import root.

    Raises:
        ValueError: If `p` is a UNC share or resolves outside all allowed
            import roots.
    """
    return _safe_import_path(p, purpose="document")


def safe_run_dir(p: str) -> Path:
    """Validate a run directory used by generated-code tools.

    Args:
        p: User/LLM-supplied run directory. `~` expansion is supported.

    Returns:
        Absolute resolved path inside an allowed run root.

    Raises:
        ValueError: If `p` is a UNC share or resolves outside all allowed run
            roots.
    """
    _rejects_unc(p)
    resolved = Path(p).expanduser().resolve()

    for root in _allowed_run_roots():
        if resolved.is_relative_to(root):
            return resolved

    raise ValueError(
        f"run_dir {p!r} is outside allowed run roots. "
        f"Set {_ALLOWED_RUN_ROOTS_ENV} to add a run directory."
    )


def safe_run_id(run_id: str) -> Path:
    """Resolve a bare run id to an existing allowed run directory.

    Args:
        run_id: Bare run directory name, not a path.

    Returns:
        Existing run directory under one of the allowed run roots.

    Raises:
        ValueError: If the run id is empty, path-shaped, or not found.
    """
    _rejects_unc(run_id)
    candidate = Path(run_id)
    if (
        not run_id.strip()
        or candidate.is_absolute()
        or len(candidate.parts) != 1
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ValueError(f"run_id {run_id!r} must be a bare run directory name")

    for root in _allowed_run_roots():
        resolved = (root / candidate.name).resolve()
        if resolved.is_relative_to(root) and resolved.is_dir():
            return resolved

    raise ValueError(f"run_id {run_id!r} was not found under allowed run roots")
