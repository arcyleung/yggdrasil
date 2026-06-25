"""Artifact refs and team identity helpers for experience memory."""
from __future__ import annotations

from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field


class ArtifactRef(BaseModel):
    """A concrete deliverable or reference produced/used during agent work.

    Stored on trajectories (and optionally on step payloads) so lab forensics
    can locate files/URLs without relying on vector search alone.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = "other"  # md | code | url | log | data | other
    path_or_url: str
    label: str | None = None
    step_seq: int | None = None
    workspace: str | None = None
    note: str | None = None


def normalize_artifacts(raw: Iterable[ArtifactRef | dict[str, Any]] | None) -> list[ArtifactRef]:
    if not raw:
        return []
    out: list[ArtifactRef] = []
    for item in raw:
        if isinstance(item, ArtifactRef):
            out.append(item)
        else:
            out.append(ArtifactRef.model_validate(item))
    return out


def merge_artifacts(
    existing: list[ArtifactRef] | None,
    incoming: list[ArtifactRef] | list[dict[str, Any]] | None,
    *,
    replace: bool = False,
) -> list[ArtifactRef]:
    if replace and incoming is not None:
        return normalize_artifacts(incoming)
    base = list(existing or [])
    for a in normalize_artifacts(incoming):
        key = (a.kind, a.path_or_url, a.step_seq)
        if any((b.kind, b.path_or_url, b.step_seq) == key for b in base):
            continue
        base.append(a)
    return base


def artifacts_from_step_payload(payload: dict[str, Any] | None, *, step_seq: int | None = None) -> list[ArtifactRef]:
    """Extract artifact refs from a step payload if agents used the skill contract."""
    if not payload:
        return []
    out: list[ArtifactRef] = []
    arts = payload.get("artifacts")
    if isinstance(arts, list):
        for item in arts:
            if isinstance(item, dict):
                d = dict(item)
                if step_seq is not None and d.get("step_seq") is None:
                    d["step_seq"] = step_seq
                out.append(ArtifactRef.model_validate(d))
    # Single-ref shortcuts only when no explicit artifacts list was provided
    if not arts:
        for key, default_kind in (("path", "code"), ("file", "code"), ("url", "url"), ("md_path", "md")):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                pu = val.strip()
                if key == "url" or pu.startswith("http://") or pu.startswith("https://"):
                    kind = "url"
                elif pu.endswith(".md"):
                    kind = "md"
                elif pu.endswith(".log"):
                    kind = "log"
                elif key == "md_path":
                    kind = "md"
                else:
                    kind = default_kind
                out.append(
                    ArtifactRef(
                        kind=kind,
                        path_or_url=pu,
                        step_seq=step_seq,
                        label=payload.get("label") if isinstance(payload.get("label"), str) else None,
                        workspace=payload.get("workspace") if isinstance(payload.get("workspace"), str) else None,
                    )
                )
    return out


def team_identity_from_refs(external_refs: dict[str, Any] | None) -> dict[str, str | None]:
    """Pull owner/agent/team/workspace from external_refs (skill contract)."""
    refs = external_refs or {}
    return {
        "owner": _str_or_none(refs.get("owner") or refs.get("user") or refs.get("engineer")),
        "agent_id": _str_or_none(refs.get("agent_id") or refs.get("agent")),
        "team": _str_or_none(refs.get("team") or refs.get("lab")),
        "workspace": _str_or_none(refs.get("workspace") or refs.get("cwd") or refs.get("worktree")),
        "project": _str_or_none(refs.get("project")),
    }


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def artifact_payload_fields(artifacts: list[ArtifactRef] | None) -> dict[str, Any]:
    """Compact fields for Qdrant payload / search hit projection."""
    arts = artifacts or []
    paths: list[str] = []
    urls: list[str] = []
    kinds: list[str] = []
    for a in arts:
        kinds.append(a.kind)
        pu = a.path_or_url
        if pu.startswith("http://") or pu.startswith("https://"):
            urls.append(pu)
        else:
            paths.append(pu)
    return {
        "artifact_count": len(arts),
        "has_artifacts": len(arts) > 0,
        "artifact_paths": paths[:32],
        "artifact_urls": urls[:16],
        "artifact_kinds": sorted(set(kinds))[:16],
        "artifacts_preview": [
            {"kind": a.kind, "path_or_url": a.path_or_url, "label": a.label, "step_seq": a.step_seq}
            for a in arts[:12]
        ],
    }
