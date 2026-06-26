"""Headless Codex exec runner for the control-plane chat UI (JSONL → SSE events)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any


def _default_codex_bin() -> str:
    return os.environ.get("YGG_CODEX_BIN") or shutil.which("codex") or "codex"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _skill_path() -> Path:
    return _repo_root() / "skills" / "yggdrasil-trajectory-memory" / "SKILL.md"


def write_session_skill(
    *,
    dest_dir: Path,
    owner: str,
    tenant_id: str,
    public_base_url: str,
    mcp_url: str,
    bearer_token: str,
) -> Path:
    """Materialize a personalized skill (MCP embedded) for this chat turn."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    skill_src = _skill_path()
    base = skill_src.read_text(encoding="utf-8") if skill_src.is_file() else ""
    header = f"""---
name: yggdrasil-trajectory-memory
description: Lab experience memory for owner {owner} (tenant {tenant_id}). Prefer search_strategies before uncertain work.
---

# Yggdrasil (session-bound)

Owner **`{owner}`**, tenant **`{tenant_id}`**.

## Wire MCP (already available via Codex MCP config for this run)

- Public base: `{public_base_url}`
- MCP URL: `{mcp_url}`
- Bearer: use env `YGG_MCP_TOKEN` / MCP header (injected by the control plane; do not echo secrets)

Search with `search_mode=lab` when exploring org memory. Surface **owner** on hits.
Segment long work; write trajectories with honest outcomes.

---

"""
    out = dest_dir / "SKILL.md"
    out.write_text(header + "\n" + base, encoding="utf-8")
    return out


def build_codex_argv(
    prompt: str,
    *,
    cwd: Path,
    skill_dir: Path,
    codex_bin: str | None = None,
    model: str | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    bin_path = codex_bin or _default_codex_bin()
    argv = [
        bin_path,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--ephemeral",
        "--color",
        "never",
        "-C",
        str(cwd),
        # Prefer read-only sandbox for web-driven chat unless overridden
        "-s",
        os.environ.get("YGG_CODEX_SANDBOX", "read-only"),
        # Point Codex at skills dir (project-relative may vary; pass as config if supported)
        "-c",
        f'project_doc_fallback_filenames=["SKILL.md"]',
    ]
    # Instruct agent to load skill text via AGENTS-style: we also prepend skill path in prompt
    if model:
        argv.extend(["-m", model])
    if extra_args:
        argv.extend(extra_args)
    # Full auto for headless: bypass approvals when explicitly enabled (default on for UI)
    if os.environ.get("YGG_CODEX_FULL_AUTO", "1") not in ("0", "false", "no"):
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    # Prompt includes skill reminder; skill file lives in cwd/skills or skill_dir
    skill_hint = (
        f"You have the Yggdrasil trajectory-memory skill enabled "
        f"(see {skill_dir / 'SKILL.md'}). Use Yggdrasil MCP tools when relevant "
        f"(search_strategies before uncertain/high-overhead work; surface owners).\n\n"
        f"User request:\n{prompt}"
    )
    argv.append(skill_hint)
    return argv


def _classify_json_line(obj: dict[str, Any]) -> tuple[str, str]:
    """Map Codex JSONL event to (sse_event_name, text)."""
    # Best-effort across Codex versions
    typ = str(obj.get("type") or obj.get("event") or obj.get("kind") or "")
    msg = obj.get("message") or obj.get("text") or obj.get("delta") or obj.get("content")
    if isinstance(msg, dict):
        msg = msg.get("text") or msg.get("content") or json.dumps(msg)
    if msg is None and "item" in obj:
        item = obj["item"]
        if isinstance(item, dict):
            msg = item.get("text") or item.get("content") or item.get("summary")
    if msg is None:
        # agent message / reasoning fields
        for key in ("agent_message", "last_agent_message", "reasoning", "output"):
            if key in obj and obj[key]:
                msg = obj[key]
                break
    text = msg if isinstance(msg, str) else (json.dumps(obj, ensure_ascii=False) if obj else "")
    tl = typ.lower()
    if "error" in tl or obj.get("error"):
        return "error", text or str(obj.get("error") or typ)
    if "message" in tl or "agent_message" in tl or "assistant" in tl:
        return "message", text
    if "reasoning" in tl or "thought" in tl:
        return "reasoning", text
    if "tool" in tl or "command" in tl or "exec" in tl:
        return "tool", text or typ
    if "result" in tl or "completed" in tl or "done" in tl:
        return "done", text or typ
    if text and typ:
        return "event", f"[{typ}] {text}" if text else typ
    if text:
        return "log", text
    return "event", json.dumps(obj, ensure_ascii=False)[:2000]


async def stream_codex_exec(
    prompt: str,
    *,
    owner: str,
    tenant_id: str,
    public_base_url: str,
    mcp_url: str,
    bearer_token: str,
    cwd: Path | None = None,
    timeout_sec: float | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield dicts: {event, data} suitable for SSE."""
    work = Path(cwd or os.environ.get("YGG_CODEX_CWD") or _repo_root())
    timeout = timeout_sec
    if timeout is None:
        timeout = float(os.environ.get("YGG_CODEX_TIMEOUT_SEC", "600"))

    with tempfile.TemporaryDirectory(prefix="ygg-codex-skill-") as tmp:
        skill_dir = Path(tmp) / "yggdrasil-trajectory-memory"
        write_session_skill(
            dest_dir=skill_dir,
            owner=owner,
            tenant_id=tenant_id,
            public_base_url=public_base_url,
            mcp_url=mcp_url,
            bearer_token=bearer_token,
        )
        # Also drop skill under work/skills if writable for discovery
        try:
            proj_skill = work / ".ygg_ui_skill" / "yggdrasil-trajectory-memory"
            write_session_skill(
                dest_dir=proj_skill,
                owner=owner,
                tenant_id=tenant_id,
                public_base_url=public_base_url,
                mcp_url=mcp_url,
                bearer_token=bearer_token,
            )
        except OSError:
            proj_skill = skill_dir

        argv = build_codex_argv(prompt, cwd=work, skill_dir=proj_skill)
        env = os.environ.copy()
        env["YGG_MCP_TOKEN"] = bearer_token
        # MCP stdio config via env for agents that honor it
        env.setdefault("OPENAI_API_KEY", env.get("OPENAI_API_KEY", ""))

        yield {
            "event": "status",
            "data": json.dumps(
                {
                    "phase": "starting",
                    "cwd": str(work),
                    "owner": owner,
                    "cmd": " ".join(argv[:6]) + " …",
                }
            ),
        }

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(work),
                env=env,
                limit=8 * 1024 * 1024,
            )
        except FileNotFoundError as exc:
            yield {"event": "error", "data": json.dumps({"message": f"codex not found: {exc}"})}
            yield {"event": "done", "data": json.dumps({"ok": False})}
            return
        except Exception as exc:
            yield {"event": "error", "data": json.dumps({"message": str(exc)})}
            yield {"event": "done", "data": json.dumps({"ok": False})}
            return

        assert proc.stdout is not None
        assert proc.stderr is not None

        async def _pump_stderr() -> None:
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    yield_queue.put_nowait({"event": "log", "data": json.dumps({"stream": "stderr", "text": text})})

        yield_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def stderr_task() -> None:
            assert proc.stderr is not None
            try:
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        await yield_queue.put(
                            {"event": "log", "data": json.dumps({"stream": "stderr", "text": text})}
                        )
            finally:
                await yield_queue.put(None)

        t_stderr = asyncio.create_task(stderr_task())
        stderr_done = False

        try:
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    yield {
                        "event": "error",
                        "data": json.dumps({"message": f"codex timed out after {timeout}s"}),
                    }
                    break
                # drain stderr queue opportunistically
                while True:
                    try:
                        item = yield_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if item is None:
                        stderr_done = True
                    else:
                        yield item

                if not line:
                    break
                raw = line.decode("utf-8", errors="replace").rstrip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        ev, text = _classify_json_line(obj)
                        payload = {"text": text, "raw_type": obj.get("type") or obj.get("event")}
                        yield {"event": ev, "data": json.dumps(payload, ensure_ascii=False)}
                    else:
                        yield {"event": "log", "data": json.dumps({"text": raw})}
                except json.JSONDecodeError:
                    yield {"event": "log", "data": json.dumps({"text": raw})}

            rc = await proc.wait()
            # finish stderr
            if not stderr_done:
                try:
                    await asyncio.wait_for(t_stderr, timeout=2)
                except asyncio.TimeoutError:
                    t_stderr.cancel()
                while True:
                    try:
                        item = yield_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if item is not None:
                        yield item

            yield {
                "event": "done",
                "data": json.dumps({"ok": rc == 0, "exit_code": rc}),
            }
        except asyncio.CancelledError:
            proc.kill()
            raise
        except Exception as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            yield {"event": "error", "data": json.dumps({"message": str(exc)})}
            yield {"event": "done", "data": json.dumps({"ok": False})}
