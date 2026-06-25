"""Map proxy-log API credentials to human owner names without persisting secrets.

Mongo ``request_headers`` often store already-truncated ``Authorization`` values
(e.g. ``Bearer s…0I7u`` len~18), but some local environments can map full keys
to real owners via ``KEY_NAME_MAP`` / ``user_mapping.yaml``. Full keys are used
only in memory to derive stable fingerprints; persisted files contain only
fingerprints and owner names.

Also supports stable ``user_id`` prefixes from proxy_log docs when headers are absent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

# Header names checked for credential material (values must already be truncated/redacted upstream)
_AUTH_HEADER_KEYS = (
    "authorization",
    "Authorization",
    "x-api-key",
    "X-Api-Key",
    "X-API-KEY",
    "api-key",
    "Api-Key",
)

# PoC default roster when auto-assigning distinct fingerprints (first seen → name)
DEFAULT_OWNER_ROSTER = (
    "alice",
    "bob",
    "carol",
    "dave",
    "erin",
    "frank",
    "grace",
    "heidi",
    "ivan",
    "judy",
    "mallory",
    "niaj",
    "olivia",
    "peggy",
    "quentin",
    "rupert",
    "sybil",
    "trent",
    "uma",
    "victor",
    "wendy",
)


def _norm_header_map(headers: Mapping[str, Any] | None) -> dict[str, str]:
    if not headers or not isinstance(headers, Mapping):
        return {}
    out: dict[str, str] = {}
    for k, v in headers.items():
        if v is None:
            continue
        out[str(k)] = str(v).strip()
    return out


def extract_auth_raw(headers: Mapping[str, Any] | None) -> str | None:
    """Return first auth-like header value if present (may already be truncated in Mongo)."""
    h = _norm_header_map(headers)
    for key in _AUTH_HEADER_KEYS:
        if key in h and h[key]:
            return h[key]
        # case-insensitive fallback
    lower = {k.lower(): v for k, v in h.items()}
    for key in ("authorization", "x-api-key", "api-key"):
        if key in lower and lower[key]:
            return lower[key]
    return None


def fingerprint_auth_value(raw: str | None) -> str | None:
    """Stable non-secret fingerprint from truncated/redacted auth material.

    Uses the full string as stored (often short/truncated). Adds a short sha256
    prefix so collisions on identical truncated suffixes are still addressable.
    Never stores or returns the full secret—only a label suitable for mapping.
    """
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    # Strip Bearer prefix for display token; keep in hash input for stability
    token_part = re.sub(r"(?i)^bearer\s+", "", s).strip()
    if not token_part:
        return None
    digest = hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]
    # Display uses only first/last chars of what was stored (safe if already truncated)
    if len(token_part) <= 8:
        vis = token_part
    else:
        vis = f"{token_part[:3]}…{token_part[-4:]}"
    return f"auth:{digest}:{vis}"


def _strip_auth_scheme(raw: str) -> str:
    return re.sub(r"(?i)^bearer\s+", "", str(raw).strip()).strip()


def _parse_simple_key_owner_yaml(text: str) -> dict[str, str]:
    """Parse the repo's simple ``user_mapping: {api_key: owner}`` YAML shape.

    This intentionally supports only the minimal checked-in format so the importer
    does not need a PyYAML dependency just to build owner fingerprints.
    """
    out: dict[str, str] = {}
    in_mapping = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw_line.startswith((" ", "\t")):
            key = stripped.split(":", 1)[0].strip()
            in_mapping = key == "user_mapping"
            if key != "user_mapping" and ":" in stripped:
                map_key, map_owner = stripped.split(":", 1)
                map_key = map_key.strip().strip("'\"")
                map_owner = map_owner.strip().strip("'\"")
                if map_key and map_owner:
                    out[map_key] = map_owner
            continue
        if not in_mapping or ":" not in stripped:
            continue
        map_key, map_owner = stripped.split(":", 1)
        map_key = map_key.strip().strip("'\"")
        map_owner = map_owner.strip().strip("'\"")
        if map_key and map_owner:
            out[map_key] = map_owner
    return out


def _parse_key_owner_mapping_text(text: str) -> dict[str, str]:
    """Parse full API key → owner mappings from JSON, YAML, or ``key=name`` CSV."""
    raw = text.strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        data = json.loads(raw)
        if isinstance(data, dict):
            if "user_mapping" in data and isinstance(data["user_mapping"], dict):
                data = data["user_mapping"]
            return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
        return {}
    yaml_map = _parse_simple_key_owner_yaml(raw)
    if yaml_map:
        return yaml_map
    out: dict[str, str] = {}
    for part in re.split(r"[,;\n]", raw):
        if not part.strip():
            continue
        sep = "=" if "=" in part else ":"
        if sep not in part:
            continue
        key, owner = part.split(sep, 1)
        key = key.strip().strip("'\"")
        owner = owner.strip().strip("'\"")
        if key and owner:
            out[key] = owner
    return out


def load_key_name_map(
    raw_or_path: str | Path | None = None,
    *,
    base_dir: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Load full API key → real owner names from ``KEY_NAME_MAP`` or a file.

    ``raw_or_path`` may be:
    - a path such as ``user_mapping.yaml``;
    - inline JSON: ``{"sk-...": "alice"}``;
    - inline CSV: ``sk-...=alice,sk-...=bob``.

    The returned map contains full keys in memory only. Persisted owner maps use
    fingerprints from :func:`owner_map_from_key_name_map`, never full keys.
    """
    raw_value = str(raw_or_path).strip() if raw_or_path is not None else ""
    if not raw_value:
        raw_value = (env or os.environ).get("KEY_NAME_MAP", "").strip()
    if not raw_value:
        return {}

    candidates: list[Path] = []
    p = Path(raw_value)
    if p.is_absolute():
        candidates.append(p)
    else:
        if base_dir is not None:
            candidates.append(Path(base_dir) / p)
        candidates.append(p)
    for candidate in candidates:
        if candidate.is_file():
            return _parse_key_owner_mapping_text(candidate.read_text(encoding="utf-8"))

    return _parse_key_owner_mapping_text(raw_value)


def owner_map_from_key_name_map(key_name_map: Mapping[str, str]) -> dict[str, str]:
    """Convert full API key → owner into safe auth-fingerprint → owner entries."""
    out: dict[str, str] = {}
    for raw_key, owner in key_name_map.items():
        key = _strip_auth_scheme(str(raw_key))
        name = str(owner).strip()
        if not key or not name:
            continue
        # Mongo may store either the bare key or an Authorization value.
        for variant in (key, f"Bearer {key}"):
            fp = fingerprint_auth_value(variant)
            if fp:
                out[fp] = name
    return out


def owner_label_map_from_key_name_map(key_name_map: Mapping[str, str]) -> dict[str, str]:
    """Return visible auth label → owner for uniquely identifiable redacted keys.

    Mongo proxy logs may contain redacted auth material such as ``sk-…0I7u``.
    Its fingerprint digest cannot match a fingerprint of the full key, but the
    visible label can still identify an owner when that label is unique in the
    full key map. Ambiguous labels are intentionally omitted.
    """
    labels: dict[str, str] = {}
    ambiguous: set[str] = set()
    for raw_key, owner in key_name_map.items():
        key = _strip_auth_scheme(str(raw_key))
        name = str(owner).strip()
        fp = fingerprint_auth_value(key)
        if not fp or not name:
            continue
        label = fp.split(":", 2)[-1]
        previous = labels.get(label)
        if previous is None:
            labels[label] = name
        elif previous != name:
            ambiguous.add(label)
    for label in ambiguous:
        labels.pop(label, None)
    return labels


def auth_label_from_fingerprint(fingerprint: str | None) -> str | None:
    """Extract the non-secret visible label from an ``auth:<digest>:<label>`` key."""
    if not fingerprint:
        return None
    parts = str(fingerprint).split(":", 2)
    if len(parts) == 3 and parts[0] == "auth":
        return parts[2]
    return None


def fingerprint_user_id(user_id: str | None) -> str | None:
    """Fallback identity from proxy user_id (hash prefix before _account_/_session_)."""
    if not user_id or not str(user_id).strip():
        return None
    uid = str(user_id).strip()
    # Typical: user_<hex>_account__session_<uuid>
    m = re.match(r"^(user_[0-9a-fA-F]{16,})", uid)
    core = m.group(1) if m else uid.split("_account")[0][:48]
    digest = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:12]
    return f"uid:{digest}:{core[:20]}…"


def identity_from_mongo_doc(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Build safe identity fields from a live Mongo proxy_log doc (headers only for fingerprint)."""
    headers = doc.get("request_headers") if isinstance(doc.get("request_headers"), dict) else {}
    auth_raw = extract_auth_raw(headers)
    auth_fp = fingerprint_auth_value(auth_raw)
    user_id = doc.get("user_id")
    if isinstance(user_id, str):
        uid_fp = fingerprint_user_id(user_id)
    else:
        uid_fp = None
    # Prefer auth fingerprint; fall back to user_id
    primary = auth_fp or uid_fp
    return {
        "api_key_fingerprint": auth_fp,
        "user_id_fingerprint": uid_fp,
        "identity_key": primary,
        "session_id": doc.get("session_id"),
        "request_id": str(doc.get("_id", {}).get("$oid", doc.get("_id", "")))
        if isinstance(doc.get("_id"), dict)
        else str(doc.get("_id", "")),
    }


def load_owner_map(path: Path | str | None) -> dict[str, str]:
    """Load fingerprint → owner name. Supports JSON object or list of {fingerprint, owner}."""
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        # allow {"owners": {...}} or flat map
        if "owners" in data and isinstance(data["owners"], dict):
            return {str(k): str(v) for k, v in data["owners"].items()}
        if "fingerprint_to_owner" in data and isinstance(data["fingerprint_to_owner"], dict):
            return {str(k): str(v) for k, v in data["fingerprint_to_owner"].items()}
        # flat: skip non-string values (meta keys)
        return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and not k.startswith("_")}
    if isinstance(data, list):
        out: dict[str, str] = {}
        for row in data:
            if isinstance(row, dict) and row.get("fingerprint") and row.get("owner"):
                out[str(row["fingerprint"])] = str(row["owner"])
        return out
    return {}


def assign_owners_for_fingerprints(
    fingerprints: list[str],
    *,
    existing_map: Mapping[str, str] | None = None,
    roster: tuple[str, ...] = DEFAULT_OWNER_ROSTER,
    unknown_prefix: str = "user",
    assign_unknown: bool = True,
) -> dict[str, str]:
    """Map each distinct fingerprint to an owner name (merge existing, then roster order)."""
    out: dict[str, str] = dict(existing_map or {})
    used_names = set(out.values())
    roster_i = 0
    for fp in fingerprints:
        if not fp or fp in out:
            continue
        if not assign_unknown:
            continue
        # assign next unused roster name
        while roster_i < len(roster) and roster[roster_i] in used_names:
            roster_i += 1
        if roster_i < len(roster):
            name = roster[roster_i]
            roster_i += 1
        else:
            # overflow: stable pseudo-name from hash
            name = f"{unknown_prefix}_{hashlib.sha256(fp.encode()).hexdigest()[:6]}"
        out[fp] = name
        used_names.add(name)
    return out


def owner_bundle_for_identity(
    identity_key: str | None,
    owner_map: Mapping[str, str],
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """external_refs fragment: owner, agent_id, team, fingerprints."""
    owner = owner_map.get(identity_key or "") if identity_key else None
    if not owner and identity_key:
        owner = "unknown"
    agent_id = f"{owner}-mongo-agent" if owner and owner != "unknown" else "unknown-mongo-agent"
    refs: dict[str, Any] = {
        "owner": owner or "unknown",
        "agent_id": agent_id,
        "team": "org-proxy-logs",
        "project": "mongo-import",
        "identity_key": identity_key,
        "api_key_fingerprint": (extra or {}).get("api_key_fingerprint"),
        "user_id_fingerprint": (extra or {}).get("user_id_fingerprint"),
        "experience_grade": True,
    }
    if extra:
        for k in ("session_id", "workspace"):
            if extra.get(k):
                refs[k] = extra[k]
    return refs


def save_owner_map(path: Path | str, owner_map: Mapping[str, str], *, meta: dict[str, Any] | None = None) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": "fingerprint -> owner. Fingerprints are non-secret truncated/hashed ids; full API keys are never written.",
        "owners": dict(owner_map),
        **(meta or {}),
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
