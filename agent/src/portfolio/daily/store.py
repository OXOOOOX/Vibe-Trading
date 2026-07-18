"""Atomic persistence and artifact registry for DailyPortfolioRun."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config.paths import get_runtime_root


TERMINAL_STATUSES = {
    "completed",
    "completed_with_warnings",
    "failed",
    "cancelled",
    "interrupted",
}
INCOMPLETE_STATUSES = {"queued", "running", "cancelling", "waiting_data"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(path)


class DailyRunStore:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or get_runtime_root() / "portfolio" / "daily_runs"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        return self.base_dir / run_id

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        record = dict(record)
        record.setdefault("created_at", _now())
        record.setdefault("updated_at", record["created_at"])
        record.setdefault("artifacts", [])
        record.setdefault("workers", [])
        self.save(record)
        return record

    def save(self, record: dict[str, Any]) -> dict[str, Any]:
        updated = dict(record)
        updated["updated_at"] = _now()
        _atomic_json(self.run_dir(str(updated["run_id"])) / "run.json", updated)
        return updated

    def get(self, run_id: str) -> dict[str, Any] | None:
        path = self.run_dir(run_id) / "run.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        records = []
        for path in self.base_dir.glob("*/run.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict):
                records.append(record)
        records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return records[: max(1, limit)]

    def find_idempotent(self, key: str) -> dict[str, Any] | None:
        for record in self.list(limit=200):
            if record.get("idempotency_key") == key and record.get("status") != "failed":
                return record
        return None

    def next_revision(self, idempotency_key: str) -> int:
        revisions = [
            int(record.get("revision") or 1)
            for record in self.list(limit=10_000)
            if record.get("idempotency_key") == idempotency_key
        ]
        return max(revisions, default=0) + 1

    def mark_incomplete_interrupted(self) -> int:
        """Close records that cannot still have a live worker after restart."""

        changed = 0
        for record in self.list(limit=10_000):
            if record.get("status") not in INCOMPLETE_STATUSES:
                continue
            record.update(
                {
                    "status": "interrupted",
                    "stage": "interrupted",
                    "completed_at": _now(),
                    "error": "服务重启时任务仍未完成，可按冻结输入重试。",
                }
            )
            self.save(record)
            changed += 1
        return changed

    def write_json(self, run_id: str, relative_path: str, value: Any) -> Path:
        path = self.run_dir(run_id) / relative_path
        _atomic_json(path, value)
        return path

    def read_json(self, run_id: str, relative_path: str) -> Any | None:
        path = self.run_dir(run_id) / relative_path
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def write_artifact(
        self,
        run_id: str,
        *,
        kind: str,
        filename: str,
        payload: bytes,
        symbol: str | None = None,
        security_name: str | None = None,
        media_type: str = "application/pdf",
        revision: int = 1,
    ) -> dict[str, Any]:
        artifact_id = hashlib.sha256(
            f"{run_id}:{kind}:{symbol or ''}:{filename}".encode("utf-8")
        ).hexdigest()[:20]
        safe_name = Path(filename).name
        path = self.run_dir(run_id) / "artifacts" / f"{artifact_id}-{safe_name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)
        return {
            "artifact_id": artifact_id,
            "kind": kind,
            "symbol": symbol,
            "security_name": security_name,
            "filename": safe_name,
            "media_type": media_type,
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "path": str(path),
            "revision": max(1, int(revision)),
            "superseded": False,
            "expired": False,
            "created_at": _now(),
        }

    def supersede_artifacts(self, run_id: str, *, replacement_run_id: str) -> int:
        record = self.get(run_id)
        if not record:
            return 0
        artifacts = list(record.get("artifacts") or [])
        changed = 0
        for artifact in artifacts:
            if artifact.get("superseded"):
                continue
            artifact.update(
                {
                    "superseded": True,
                    "superseded_at": _now(),
                    "superseded_by_run_id": replacement_run_id,
                }
            )
            changed += 1
        if changed:
            record["artifacts"] = artifacts
            record["superseded_by_run_id"] = replacement_run_id
            self.save(record)
        return changed

    def resolve_artifact(self, run_id: str, artifact_id: str) -> tuple[dict[str, Any], Path] | None:
        record = self.get(run_id)
        if not record:
            return None
        artifact = next(
            (item for item in record.get("artifacts") or [] if item.get("artifact_id") == artifact_id),
            None,
        )
        if not artifact:
            return None
        if artifact.get("expired"):
            return None
        path = Path(str(artifact.get("path") or "")).resolve()
        root = self.run_dir(run_id).resolve()
        if root not in path.parents or not path.is_file():
            return None
        return artifact, path

    def enforce_retention(self, *, keep_days: int = 90, keep_latest: int = 120) -> int:
        """Expire old artifacts while retaining the one-year run metadata record."""

        records = self.list(limit=10_000)
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, keep_days))
        removed = 0
        for index, record in enumerate(records):
            if index < keep_latest:
                continue
            try:
                created = datetime.fromisoformat(str(record.get("created_at") or ""))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if created >= cutoff:
                continue
            run_dir = self.run_dir(str(record.get("run_id") or "")).resolve()
            if run_dir.parent != self.base_dir.resolve() or record.get("artifacts_expired"):
                continue
            artifacts = list(record.get("artifacts") or [])
            for artifact in artifacts:
                path = Path(str(artifact.get("path") or "")).resolve()
                if run_dir in path.parents:
                    path.unlink(missing_ok=True)
                artifact.update({"expired": True, "expired_at": _now()})
            for child in sorted(run_dir.rglob("*"), reverse=True):
                if child.name == "run.json":
                    continue
                if child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    try:
                        child.rmdir()
                    except OSError:
                        pass
            record.update(
                {
                    "artifacts": artifacts,
                    "artifacts_expired": True,
                    "artifacts_expired_at": _now(),
                }
            )
            self.save(record)
            removed += 1
        return removed
