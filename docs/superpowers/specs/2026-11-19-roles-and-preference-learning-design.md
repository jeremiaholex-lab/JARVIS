# Design: Role-scoped sub-agents and real preference learning

Date: 2026-11-19
Status: implemented

## Why

Two gaps from the roadmap, both of which turned out to be wiring rather than
construction:

1. **Preference learning.** `UserModel` already merges extracted facts after
   every turn and injects them into the system prompt. But the *explicit*
   path was broken: `Memory.maybe_store_fact()` writes "remember that …"
   statements into a separate `facts` table that nothing reads back into a
   prompt. The user hears "Noted: …" and the fact is gone. There was also no
   way for the model itself to pin something the user asked it to remember.

2. **Multi-agent delegation.** `decompose_goal` → `TaskStore.create_plan` →
   `DurableTaskRunner` already decompose, persist and execute a DAG of
   sub-tasks. Every sub-task, however, receives the same blanket set of all
   READ-tier tools. There was no notion of *which kind of agent* should run a
   step, so "research this, then write code, then review it" was three
   identical generalists.

## Goals

- An explicit "remember this" statement survives forever and always reaches
  the prompt, even when the fact table is full.
- The model can pin a preference on its own initiative during a turn.
- A plan step can name a role; the role decides which tools that sub-task may
  use.
- Sub-agents cannot enqueue more tasks (no recursion), and cannot write.

## Non-goals

- No new runtime, no process-per-agent, no parallelism. Sub-tasks keep
  executing sequentially in topological order through the existing runner.
- No new provider or network surface. Roles only *remove* tools.
- No change to the confirmation flow for project writes and file edits.

## Approaches considered

**A. Role as a prompt-only hint.** Add "act as a researcher" to the sub-task
prompt. Cheapest, but the tool list stays identical, so a "researcher" can
still call `sandbox_execute`. The delegation is cosmetic.

**B. Role as a tool-subset filter, stored on the task (chosen).** A role is a
named tuple of tool names. The planner emits a role per step; it is persisted
on the task row and used to filter the tool list at execution time. The
constraint is enforced by the runtime, not by the prompt, so it holds even if
the model ignores its instructions.

**C. Separate planner/executor agents with their own loops.** Real
multi-agent, but it duplicates the runner, the store and the confirmation
path, and buys nothing while sub-tasks run sequentially anyway.

B is chosen: it is the only option where the role actually restricts what
runs, and it reuses the existing durable queue unchanged.

## Design

### Preference learning

`UserModel` gains a `pinned` column (default 0) and a `learn()` entry point:

```
learn(text, call_engine=None) -> list[Fact]
```

1. If the text is an explicit statement ("remember that …"), store it with
   `source="explicit"` and `pinned=1`, no model call.
2. Otherwise, when `call_engine` is supplied, extract candidate facts and
   `merge()` them as before.

`active_facts()` orders by `pinned DESC, updated_at DESC`, so an explicit
preference is never evicted by the 14-fact prompt budget. Re-stating a pinned
fact keeps it pinned.

The orb's ack ("Noted: …") and the post-turn extraction both go through
`UserModel`; `Memory.maybe_store_fact()` stops being the write path.

A new READ-adjacent tool, `jarvis_remember`, lets the model pin a fact
mid-turn. It is tiered WRITE (it does mutate state) but listed in
`_LOCAL_WRITE_TOOLS`, which `execute()` permits because the write is local,
reversible, and user-initiated. Nothing leaves the machine.

### Role-scoped sub-agents

`jarvis/agent/roles.py` holds:

- `AgentRole(name, description, tools, system_addendum)`
- `ROLES`: `researcher`, `coder`, `reviewer`, `general`
- `tools_for_role(role, available)` → the subset of `available` the role may use
- `normalise_role(name)` → a known role name, falling back to `general`

`PlanStep.role` defaults to `"general"`. The planner prompt asks for a role
per step and validation coerces unknown names to `general` rather than
rejecting the plan — a hallucinated role should degrade, not fail.

`AgentTask.role` is persisted (`role TEXT NOT NULL DEFAULT ''`, with a
migration for existing databases). `create_plan` carries `step.role` onto each
sub-task row.

`Brain._process_background_task` filters its tool list by
`tools_for_role(task.role, read_tools)` and appends the role's
`system_addendum` to the sub-task prompt.

Safety properties enforced in `roles.py`:

- `jarvis_task_enqueue` and `jarvis_plan_and_enqueue` are in **no** role. A
  sub-agent cannot spawn more work.
- Every role is a subset of READ-tier tools, so the existing "no unattended
  writes" rule is unchanged.

## What is left to the builder

- Adding a role is a single entry in `ROLES`; no schema or planner change.
- Role-specific prompts are one string each; they are deliberately short
  because the tool subset already does most of the steering.
