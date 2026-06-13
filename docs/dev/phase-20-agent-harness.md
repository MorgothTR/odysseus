# Phase 20 — Agent Harness (sub-agent orchestration)

Status: **design** · Branch: `phase-20-agent-harness` · Author: Murat + Claude · 2026-06-13

## Goal

Let an Odysseus agent spawn **sub-agents** — each with its own fresh context,
its own bounded tool loop, and its own task — run them (often in parallel), and
get back only their *results*, so the parent's context stays lean while children
burn context doing the work.

Two milestones, shippable independently:

- **Phase A — Agentic swarm.** Upgrade `code_review_swarm` reviewers from
  one-shot calls against a fixed snapshot into **mini agent loops** with
  read-only tools (`grep` / `read_file` / `glob` / `ls`) confined to the review
  root. Each reviewer pulls the files *it* wants instead of whatever fit in the
  snapshot. High value, low risk, no new UI, reuses everything.
- **Phase B — General `spawn_agent` tool.** A first-class tool the chat agent
  can call to delegate arbitrary tasks to session-backed sub-agents, with the
  bg_jobs-style registry, follow-up routing, depth/concurrency/budget guards.

## Why this is mostly wiring, not new machinery

Odysseus already owns the hard parts. The table maps each capability a harness
needs to the existing component that provides it:

| Harness capability | Already exists in Odysseus |
|---|---|
| Drive an agent loop headlessly, collect final text | `task_scheduler._run_agent_loop` ([task_scheduler.py:1565](../../src/task_scheduler.py)) — drives `stream_agent_loop`, accumulates `delta` events into `full_text`, captures tool results, grace-summarizes on round exhaustion |
| Summary-only return (no child transcript in parent) | same — the parent only ever sees `full_text`, never the child's message list |
| Fresh per-child context | `stream_agent_loop(messages=[system, user])` ([agent_loop.py:1548](../../src/agent_loop.py)) takes a fresh message list per call |
| Tool gating / restricted toolset | `disabled_tools` + `relevant_tools` params already on `stream_agent_loop`; `NON_ADMIN_BLOCKED_TOOLS` / plan-mode allowlist in `tool_security.py` |
| Workspace confinement | `workspace=` param on `stream_agent_loop` (same allowed-folder policy as file tools) |
| Concurrency-capped parallel fan-out | `asyncio.Semaphore(MAX_PARALLEL_REVIEWERS)` pattern in `code_review_swarm.py` |
| Running-job registry: list / stop / status | `bg_jobs.py` — module store + lifecycle, already battle-tested |
| Result routing when a detached job finishes | `bg_jobs` follow-up monitor (the `#!bg` re-invocation path) |
| Round budget per child | `max_rounds=` param on `stream_agent_loop` |

The only genuinely new code is: a reusable headless driver (extracted/generalized
from `_run_agent_loop`), a sub-agent registry, the depth guard, and the
`spawn_agent` tool surface + registration.

## Reference studied: hermes-agent `delegate_tool.py`

(MIT, on disk at `C:\Users\darth\AppData\Local\hermes\hermes-agent`. 2957 lines,
production-grade.) Decisions we adopt, and the one we explicitly reject:

**Adopt:**
1. **Depth guard = one integer.** `_delegate_depth` on the agent object; default
   `MAX_DEPTH = 1` (flat — a sub-agent cannot spawn sub-sub-agents). Child gets
   `parent_depth + 1`; reject the `spawn_agent` call when `depth >= MAX_DEPTH`.
2. **`leaf` vs `orchestrator` roles.** Only orchestrators may re-delegate, and a
   child is force-demoted to leaf when depth would exceed the cap. Makes
   recursion opt-in. (Phase B can ship leaf-only first; add orchestrator later.)
3. **Child toolset ⊆ parent toolset.** A child can never gain a capability the
   parent lacked — intersect requested tools with the parent's enabled set.
4. **Blocked-for-children set.** Children may not `spawn_agent` (unless
   orchestrator), write memory, message the user, or open interactive prompts.
5. **Config-authoritative budgets.** Ignore a model-supplied `max_rounds` /
   `agent_count`; the configured ceiling always wins. Stops a confused parent
   from handing children a runaway budget.
6. **Module-level registry + lock**, with `list_active` and `interrupt(id)`.
   This is the same shape as `bg_jobs` — reuse it rather than add a parallel one.

**Reject (important gotcha):**
- hermes **removed hard wall-clock timeouts** because they kept killing
  legitimate slow work (deep reviews, slow reasoning models). Our own reviewers
  ran **105 s** on 2026-06-13. Use **staleness detection**, not a fixed timeout:
  only kill a child that stops making progress (no new round / tool call for a
  generous window), never one that's merely slow. For Phase A/B v1 we can lean
  on the existing `max_rounds` cap + the stream inactivity timeout already in
  `stream_agent_loop`, and defer a dedicated heartbeat monitor.

**Also reject (architecture):** hermes uses threads + a `ThreadPoolExecutor`
because it bolts onto a synchronous TUI agent. Odysseus is async end-to-end —
use `asyncio.gather` + a semaphore (as the swarm already does). Cleaner than the
reference on this axis.

## Bonus find: hermes `mixture_of_agents_tool.py` == our swarm

Their MoA tool is structurally identical to `code_review_swarm` (parallel
reference models → aggregator) and hit the **same thinking-model empty-content
bug** we fixed on 2026-06-13. They solve it with retry-on-empty +
`extract_content_or_reasoning` + `MIN_SUCCESSFUL_REFERENCES = 1` (proceed with
however many succeeded). We solve it with `think=false` (more efficient — never
generate the reasoning).

**Steal their graceful degradation regardless of Phase A/B:** the swarm should
synthesize from whatever reviewers returned substantive output instead of
emitting a broken report when some come back empty. Small, independent hardening
— track as a pre-req fix below.

---

## Phase A — Agentic swarm reviewers

### Current shape (`code_review_swarm.py`)
`_collect_snapshot` samples files into one big text blob; each `review_role`
makes a single `_call_llm` with that blob; `asyncio.gather` fans them out under
`Semaphore(MAX_PARALLEL_REVIEWERS)`; one synthesis call merges them.

### Target shape
Replace the single `_call_llm(snapshot)` per reviewer with a **bounded mini
agent loop** that gets:
- the review goal + role focus as its system/user prompt,
- a small **starter snapshot** (file tree + a few high-value files) so it knows
  where to look — much smaller than today's full snapshot,
- **read-only tools** (`read_file`, `grep`, `glob`, `ls`) confined to the
  review root via `workspace=root`,
- a tight round cap (e.g. `max_rounds = 6`),
- `think=false` already in place for the reviewer calls.

Each reviewer now *explores*: greps for the patterns its role cares about, reads
the files it finds suspicious, and returns findings grounded in code it actually
opened — not whatever fit in a 150 K snapshot. Synthesis stays as-is.

### Implementation sketch
- New helper `_run_reviewer_agent(role, goal, root, candidates) -> str` that
  calls the generalized headless driver (see Phase B) with the read-only toolset
  and `workspace=root`.
- Keep `_collect_snapshot` but shrink its role to "starter map" (tree + top N
  high-value files), not the whole review surface.
- Gate behind an arg (`"agentic": true` or a `mode`) for one release so we can
  A/B the output quality and token cost against the current snapshot swarm
  before making it the default.

### Cost note
Each reviewer now makes several calls instead of one. With kimi on Ollama Cloud
(cheap, per Murat) acceptable, but keep the round cap tight and `think=false` on.

---

## Phase B — `spawn_agent` tool

### Tool surface (`spawn_agent`)
```
{"tasks": [{"goal": "...", "context": "...", "tools": ["read_file","grep"]}],
 "model": "<optional override>", "max_rounds": 8}
```
- `tasks`: one or more sub-agent jobs (array → parallel fan-out under the
  concurrency cap). Single-object form allowed for the common case.
- `goal` / `context`: the child has **no** parent history — context must be
  explicit (file paths, constraints, what "done" looks like).
- `tools`: requested toolset, intersected with the parent's enabled set and the
  child blocklist. Default: read-only nav set.
- `model`: optional per-child model; defaults to the parent's / Utility model.
- Returns JSON: `{"results": [{"task_index", "status", "summary", "rounds",
  "tools_used"}], "duration_s"}` — **summaries only.**

### The reusable driver
Generalize `task_scheduler._run_agent_loop` into a standalone
`src/subagents.py::run_subagent(goal, context, *, root, toolset, model,
max_rounds, depth, owner, session_id) -> SubagentResult`:
- builds `[system, user]` messages from goal+context,
- resolves headers/fallbacks (lift the existing block),
- drives `stream_agent_loop` accumulating `full_text` + tool trace,
- grace-summarizes on round exhaustion (already in the scheduler version),
- stamps the child agent with `_delegate_depth = depth` so a nested
  `spawn_agent` can read it and refuse past `MAX_DEPTH`.

`spawn_agent` then = parse/validate args → depth check → per-task
`create_session` (child session, marked `_spawned_from=<parent>` so it's hidden
from pickers) → `asyncio.gather` over `run_subagent` under
`Semaphore(MAX_CONCURRENT_CHILDREN)` → collect summaries.

### Registry (reuse bg_jobs shape)
A child is registered on start (`id`, `parent_id`, `depth`, `goal`, `model`,
`status`, `started_at`) and unregistered on finish. `list_active_subagents()`
and `interrupt_subagent(id)` mirror `bg_jobs.list_for_session` / `stop`. A child
session IS a session, so the existing sidebar can show its transcript for free.

### Registration hooks (the nine touch-points, per phase-17/18 pattern)
`TOOL_TAGS`, `TOOL_SECTIONS` (with an explicit "delegate, don't role-play the
children" line — kimi WILL try to simulate them), `FUNCTION_TOOL_SCHEMAS`,
`BUILTIN_TOOL_DESCRIPTIONS`, `_KEYWORD_HINTS`, `tool_parsing` aliases,
`tool_security` (admin-gated + plan-mode mutator), `tool_execution` dispatch,
tests.

### Defaults (config-authoritative)
- `MAX_DEPTH = 1` (flat)
- `MAX_CONCURRENT_CHILDREN = 3`
- `DEFAULT_CHILD_ROUNDS = 8`
- child default toolset = `{read_file, grep, glob, ls}` (read-only)
- child blocklist = `{spawn_agent (leaf), manage_memory, ask_user, send_email,
  manage_*}`

---

## Sequencing

1. **Pre-req (independent):** swarm graceful degradation — synthesize from
   non-empty reviewers; never emit a broken report. Ships in the next build.
2. **Phase A:** agentic reviewers behind a `mode`/`agentic` flag. A/B vs.
   snapshot swarm. Promote to default if quality wins.
3. **Phase B v1:** `spawn_agent`, leaf-only, read-only default toolset, parallel
   fan-out, registry, the nine hooks, tests.
4. **Phase B v2:** orchestrator role (opt-in re-delegation), write-capable
   children behind admin gating, staleness/heartbeat monitor, a UI panel for
   the live sub-agent tree.

## Open decisions (resolve before cutting Phase B)
- Do children get **write** tools at all in v1, or strictly read-only until v2?
  (Lean read-only — safer, and covers the review/research use cases.)
- Surface child progress to the user live (stream child `delta`s up as
  `tool_progress`-style events) or only the final summaries? (Lean: summaries v1,
  live tree v2.)
- Token-budget ceiling per spawn call, or rely on round caps + Ollama being
  cheap? (Lean: round caps for v1; revisit if cost surprises.)
