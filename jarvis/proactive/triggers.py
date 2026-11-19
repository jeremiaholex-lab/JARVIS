"""Proactive triggers (design §6.9).

The design names three non-schedule triggers and I implemented exactly those,
plus the deadline rule that already existed:

  inbox backlog      — a threshold of unanswered mail worth triaging
  meetings coming up — a meeting inside the next hour the owner may not have
                       prepared for
  agent failures     — the agent's own runs failing repeatedly (distinct from
                       the sentry, which *patches*; this only tells you)
  deadlines          — open/overdue deadlines inside the monitoring window

Each trigger returns Signals and is deliberately side-effect free: no tool
calls, no writes outside the alert store. §6.9: "the agent can raise an issue,
but acting on it still follows the permission tiers."
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Sequence

from .alerts import Signal

_logger = logging.getLogger(__name__)

_INBOX_COOLDOWN_S = 3 * 3600.0
_MEETING_COOLDOWN_S = 24 * 3600.0
_FAILURE_COOLDOWN_S = 2 * 3600.0

_ACTIONABLE_SENDERS = re.compile(
    r"(noreply|no-reply|notification|marketing|newsletter|support@|security@|"
    r"billing@|receipt|invoice)",
    re.I,
)


@dataclass(slots=True)
class TriggerContext:
    """Everything the triggers may read. All of it optional and read-only."""

    project_store: object = None
    google: object = None
    traces_db: Optional[Path] = None
    now: Optional[Callable[[], datetime]] = None

    def __post_init__(self) -> None:
        if self.now is None:
            self.now = lambda: datetime.now().astimezone()


def deadlines_trigger(ctx: TriggerContext) -> list[Signal]:
    store = ctx.project_store
    if store is None:
        return []
    now = ctx.now()
    due = store.get_proactive_deadlines(now)
    signals: list[Signal] = []
    for item in due:
        due_raw = item["due_date"]
        try:
            due_dt = datetime.fromisoformat(due_raw)
        except ValueError:
            continue
        overdue = due_dt < now
        if overdue:
            hours = (now - due_dt).total_seconds() / 3600
            severity = "urgent" if hours > 24 else "notice"
            text = (f"{item['title']} for {item['project_name']} is overdue by "
                    f"{int(hours)}h")
        else:
            hours_left = (due_dt - now).total_seconds() / 3600
            severity = "urgent" if hours_left < 6 else "notice"
            text = (f"{item['title']} for {item['project_name']} is due in "
                    f"{int(hours_left)}h ({due_raw[:16]})")
        signals.append(Signal(
            key=f"deadline:{item['id']}:{'overdue' if overdue else 'due'}",
            source="deadlines", severity=severity, text=text,
            suggested_action=f"open the {item['project_name']} project and update the deadline",
            cooldown_s=12 * 3600.0,
        ))
    return signals


def inbox_trigger(ctx: TriggerContext, threshold: int = 12) -> list[Signal]:
    if ctx.google is None:
        return []
    try:
        data = ctx.google.fetch_daily()
    except Exception as exc:
        _logger.debug("inbox trigger skipped: %s", exc)
        return []
    emails = data.get("email") or []
    if len(emails) < threshold:
        return []
    # Count only mail that looks like it came from a person, so a promo blast
    # day does not produce a false "you are buried" alert.
    humans = [e for e in emails if not _ACTIONABLE_SENDERS.search(e)]
    if len(humans) < max(3, threshold // 3):
        return []
    return [Signal(
        key=f"inbox:backlog:{time.strftime('%Y%m%d%H')}",
        source="inbox", severity="notice",
        text=f"{len(emails)} messages in the last day, {len(humans)} from people",
        suggested_action="ask JARVIS to triage the inbox",
        cooldown_s=_INBOX_COOLDOWN_S,
    )]


_MEETING_LINE = re.compile(r"(?P<when>\d{4}-\d{2}-\d{2}[T ](?P<time>\d{2}:\d{2}))")


def meetings_trigger(ctx: TriggerContext, within_minutes: int = 60) -> list[Signal]:
    if ctx.google is None:
        return []
    try:
        data = ctx.google.fetch_daily()
    except Exception as exc:
        _logger.debug("meeting trigger skipped: %s", exc)
        return []
    now = ctx.now()
    signals: list[Signal] = []
    for entry in data.get("calendar") or []:
        match = _MEETING_LINE.search(entry or "")
        if not match:
            continue
        try:
            start = datetime.fromisoformat(match.group("when"))
        except ValueError:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=now.tzinfo)
        minutes = (start - now).total_seconds() / 60
        if 0 < minutes <= within_minutes:
            title = entry.split(":", 1)[-1].strip() or "a meeting"
            signals.append(Signal(
                key=f"meeting:{start:%Y%m%d%H%M}:{title[:32]}",
                source="calendar", severity="notice",
                text=f"{title} starts in {int(minutes)} min",
                suggested_action="ask JARVIS for a call prep brief",
                cooldown_s=_MEETING_COOLDOWN_S,
            ))
    return signals


def agent_failure_trigger(ctx: TriggerContext, window_minutes: int = 30, at_least: int = 2) -> list[Signal]:
    """Repeated failures of JARVIS's own work, reported — not patched.

    The sentry (§6.7) handles fixing skill code; this exists so the owner
    learns that something is going wrong, including failures the sentry is
    not allowed to touch.
    """
    db = ctx.traces_db
    if db is None or not Path(db).is_file():
        return []
    cutoff = time.time() - window_minutes * 60
    try:
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT agent, COUNT(*) AS n FROM traces WHERE started_at >= ? "
            "AND (outcome IN ('failed','error') OR result LIKE '%Traceback%' "
            "OR result LIKE '%error:%') GROUP BY agent", (cutoff,),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        _logger.debug("failure trigger skipped: %s", exc)
        return []
    signals: list[Signal] = []
    for agent, count in rows:
        if count < at_least:
            continue
        signals.append(Signal(
            key=f"failures:{agent}:{time.strftime('%Y%m%d%H', time.localtime(cutoff))}",
            source="agent-health", severity="urgent" if count >= 5 else "notice",
            text=f"{count} failed run(s) by {agent or 'unknown agent'} in the last {window_minutes} min",
            suggested_action="run: python phase4.py sentry  (or ask JARVIS what broke)",
            cooldown_s=_FAILURE_COOLDOWN_S,
        ))
    return signals


TRIGGERS: dict[str, Callable[[TriggerContext], list[Signal]]] = {
    "deadlines": deadlines_trigger,
    "inbox": inbox_trigger,
    "meetings": meetings_trigger,
    "agent-health": agent_failure_trigger,
}


def collect(ctx: TriggerContext, only: Sequence[str] = ()) -> list[Signal]:
    """Run every trigger, isolating failures so one bad source cannot mute the rest."""
    out: list[Signal] = []
    for name, fn in TRIGGERS.items():
        if only and name not in only:
            continue
        try:
            out.extend(fn(ctx) or [])
        except Exception as exc:
            _logger.warning("trigger %s failed: %s: %s", name, exc.__class__.__name__, exc)
    return out


__all__ = [
    "TriggerContext", "TRIGGERS", "collect", "deadlines_trigger", "inbox_trigger",
    "meetings_trigger", "agent_failure_trigger",
]
