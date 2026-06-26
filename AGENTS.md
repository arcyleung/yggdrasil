# Agent notes (Yggdrasil)

## Agent skills

### Issue tracker

GitHub Issues on `arcyleung/yggdrasil` (use `gh`). External PRs are **not** a triage request surface by default. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical roles: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/`. See `docs/agents/domain.md`.

### Matt Pocock skills (installed under `~/.grok/skills/mattpocock/`)

For **architecture / scaling** work prefer:

| Skill | Use for |
|-------|---------|
| `improve-codebase-architecture` | Deepening opportunities → HTML report (SQLite→Postgres, replica-safe writes, search fan-out) |
| `codebase-design` | Deep-module vocabulary, seams, design-it-twice |
| `domain-modeling` | Keep `CONTEXT.md` + ADRs current as we scale |
| `decision-mapping` | Multi-session scaling decisions (queue, replicas, tenancy) |
| `grill-me` / `grill-with-docs` | Align before big infra changes |
| `to-prd` / `to-issues` | Turn scaling plan into agent-ready issues |
| `prototype` | Throwaway perf / queue prototypes |
| `diagnosing-bugs` | Throughput regressions, lock contention |
| `tdd` + `implement` | Vertical slices at store/search seams |
| `handoff` | Pass multi-replica work to a fresh agent |
| `triage` | Label GitHub issues for AFK agents |

Clone source: https://github.com/mattpocock/skills.git (see `~/.grok/skills/mattpocock/SOURCE.txt`).
