# Design: Cron schedule layer over the durable task queue

Date: 2026-11-20
Status: approved for implementation (builder-check addenda folded in)

## Why

Roadmap item #5. Tasks today are user-initiated or watcher-initiated; nothing
fires at a time. A user should be able to say "every Monday 9am, summarize
last week's emails" once, and JARVIS re-enqueues that work on its own,
durable across restarts.

## Goals

- `jarvis_task_schedule` tool: create / list / pause / resume / remove a
  recurring schedule, addressed by cron string (5 fields, local time).
- Schedules persist in SQLite; a missed slot fires late once on startup
  (catch-up), then resynchronizes.
- A fired schedule enqueues ONE read-only task through the existing
  `TaskStore` — the durable queue and its runner do all execution.
- No new process, thread, or provider surface.

## Non-goals (v1)

- One-shot reminders ("in 20 minutes") — a different row shape, later.
- Dynamic interval re-planning; timezone/DST transitions (local time only).
- Per-schedule execution results surfaced back into the schedule row (the
  task id is recorded; status is read via `jarvis_task_status`).

## Approaches

**A. Cron-string schedules (chosen).** `schedule("0 9 * * 1", ...)`.
Matches the "cron layer" wording of the roadmap; converting user intent to a
cron string is reliable for an LLM. Cost: a restricted 5-field parser.

**B. Structured spec** (`kind="weekly", day=1, time="09:00"`). Zero parsing
ambiguity and easy to display, but a closed grammar that needs new fields
for anything beyond its shapes, and is not what was asked for.

**C. One-shot tasks with `run_after` timestamps, re-enqueued by a sweeper.**
Simplest possible store, but "weekly at 9am" becomes a chain of one-shots;
each missed slot is a catch-up problem anyway, and the user sees a moving
target instead of one durable schedule. Rejected.

## Data model

New table in the **same DB file** as `agent_tasks`
(`.openjarvis/agent_tasks.db`, opened with the same
`isolation_level="IMMEDIATE"` discipline as `TaskStore`):

```sql
CREATE TABLE IF NOT EXISTS agent_schedules (
    id TEXT PRIMARY KEY,
    cron TEXT NOT NULL,             -- validated 5-field expression
    instruction TEXT NOT NULL,      -- the task goal, <= 20,000 chars
    enabled INTEGER NOT NULL DEFAULT 1,
    last_fired_at REAL NOT NULL,    -- timestamp the slot was claimed (0 = never)
    next_run_at REAL NOT NULL,      -- next slot >= now; 0 until first check
    last_task_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
```

Limits: max 50 schedules per host, cron expression <= 120 chars.

## Cron grammar (restricted, hand-rolled)

5 fields: `minute hour day-of-month month day-of-week`. Local machine time.

- Allowed per field: `*`, a single number, a comma list, and `a-b/step`
  ranges. `*/n` sugar. Ranges must have `start <= end` with no wraparound.
- day-of-week: `0-7` with both 0 and 7 = Sunday (normalized to 0).
- **No** month/day names, `L`, `W`, `#`, or question marks.

Month/day-of-week interaction follows classic cron: when *both* are
restricted (not `*`), a time matches if EITHER field matches. The tool's
documentation string says so, so the model (and the user) are not surprised.

Next-slot computation: from a reference time, walk forward in one-minute
steps until a match, capped at 366*24*60 checks. At most a handful of
schedules exist and this runs at most once per 2s loop, so brute force is
fine; the cap guards against expressions that cannot fire (e.g. Feb 30).

## Catch-up semantics

The pump (`ScheduleStore.check(now) -> list[AgentTask]`) runs inside Brain's
existing 2-second task worker loop, after `run_once()`:

- `next_run_at == 0` (created but never checked): if the first slot is in the
  past, fire now (late) and set `next_run_at` to the *first future* slot
  strictly after now.
- `next_run_at < now`: fire now, then advance `next_run_at` to the first
  future slot strictly after the *due* slot (i.e. the missed slot itself is
  not backfilled — one late fire per missed slot).
- Paused schedules are skipped without touching `next_run_at`; resuming
  recomputes it, so a long-paused schedule fires late-once on resume.

**Idempotency:** the enqueued task carries
`idempotency_key = f"schedule:{schedule_id}:{due_slot_timestamp}"`. On a
re-pump of the same slot, `TaskStore.create` dedupes by *returning the
existing row* (task_runner.py:188-194) rather than raising — so a crash
between enqueue and `next_run_at` commit yields the original task, not a
duplicate. A `sqlite3.IntegrityError` guard around the enqueue remains as
defense in depth. The store advances `next_run_at` only after a successful
enqueue. The task goal is prefixed so it is self-describing:
`[schedule <id>] <instruction>`.

## Tools and tiering

One new tool, `jarvis_task_schedule`, exposed in `build_tools()` through
`_check_tier` like `jarvis_plan_and_enqueue`:

```
action=create   cron, instruction            -> {id, next_run_at, note}
action=list     -> [{id, cron, instruction, enabled, next_run_at, last_fired_at, last_task_id}]
action=pause    schedule_id
action=resume   schedule_id
action=remove   schedule_id
```

`tiers.py` gains `jarvis_task_schedule` in the READ set with the same
rationale as `jarvis_plan_and_enqueue`: it only writes to local SQLite and
enqueues read-only queue rows. A schedule row is configuration, not a host
mutation.

Host wiring: `Brain` builds `self.schedules = ScheduleStore(self.tasks.db_path)`
and calls `self.schedules.check(time.time())` each loop in `_task_worker`,
wrapped in the same try/except-continue as the runner. The `check` method
takes a `now` argument so tests drive time explicitly.

## Test

`test_schedules.py` (new, alongside `test_task_runner.py`), plain-pytest
style matching the repo:

- parser: each field shape, list/step/range, Sunday 0/7, rejection of names,
  `L`/`W`/`#`, out-of-range values, >5 fields.
- next-slot: daily, weekly Monday-9, `*/15` minutes, dom/dow OR interaction,
  impossible expression (Feb 30) returns 0.
- store: create/list/pause/resume/remove, 50-schedule cap.
- catch-up: fake now past two slots -> exactly one late fire, key dedupes a
  re-check after "crash" (second check returns nothing new).
- end-to-end over a real `TaskStore` in tmp_path: fired task is claimable by
  `claim_next` and carries the right idempotency key.

## Builder's remaining choices (marked, not asked)

- Module file: `jarvis/agent/schedules.py`, mirroring `task_runner.py`
  structure (dataclass + store class, sqlite3, no external deps).
- `_task_worker` gets the check call; no new thread.

## Builder-check addenda (settled 2026-11-20)

All four are builder calls recorded here so they stay visible:

- **Human-readable next run.** The tool layer returns `next_run_at` as a
  local datetime string (plus the raw epoch under `next_run_at_ts`), not a
  bare epoch — the model-facing response must answer "when will this first
  run?"
- **Impossible expressions are rejected at create.** `create` runs the
  366-day next-slot scan; an expression with no slot (e.g. `0 9 30 2 *`)
  is refused with the reason. As defense in depth, `check()` no-ops any
  enabled row whose recompute yields 0 instead of entering the
  fire-late branch (the 0 sentinel otherwise means "never checked").
- **Prompt discovery.** One `TOOL_GUIDANCE` sentence in
  `jarvis/agent/turn.py` naming `jarvis_task_schedule` for recurring work,
  mirroring the lines for its siblings — otherwise the tool is reachable
  but never suggested for "do X every Monday".
- **Concurrency.** One connection per store; `IMMEDIATE` isolation plus a
  10s busy timeout serializes the schedule-pump, runner, and
  watcher-worker writers on `agent_tasks`. Unit tests use isolated
  tmp_path databases, matching the repo's existing style.
