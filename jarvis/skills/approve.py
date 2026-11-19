"""Human approval gate + catalog promotion (§6.6 step 3).

Only after sandbox tests PASS does a proposal reach the user's approval.
Approval is an explicit, recorded, one-word 'approve'; on approval the
skill lands in OpenJarvis's skills dir (<home>/skills/<name>/SKILL.md),
so the framework discovers it on the NEXT session's skill scan — exactly
the "invokable next session" requirement.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from openjarvis.core.paths import get_config_dir

from .propose import Proposal
from .sandbox_test import SandboxTestResult

APPROVALS = "jarvis-skill-approvals.json"


@dataclass(slots=True)
class ApprovalRecord:
    name: str
    status: str  # "proposed" | "sandbox_passed" | "approved" | "rejected"
    summary: str
    gap: str
    tests: SandboxTestResult
    decided_at: float = 0.0
    skill_dir: str = ""


class SkillGovernance:
    """Persists approval state and promotes approved skills to the catalog."""

    def __init__(self, home: Path | None = None) -> None:
        home = home or Path(get_config_dir())
        self._home = home
        self._dir = home / "skill-governance"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / APPROVALS

    # -- state ----------------------------------------------------------
    def _load(self) -> dict[str, dict]:
        if self._file.exists():
            try:
                return json.loads(self._file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def _save(self, records: dict[str, dict]) -> None:
        self._file.write_text(json.dumps(records, indent=2), encoding="utf-8")

    def record(self, proposal: Proposal, test: SandboxTestResult, status: str) -> ApprovalRecord:
        records = self._load()
        entry = {
            "name": proposal.name,
            "status": status,
            "summary": proposal.summary,
            "gap": proposal.source_gap,
            "tests": asdict(test),
            "isolation": getattr(test, "isolation", "fallback"),
            "decided_at": time.time(),
            "skill_md_sha": _sha(proposal.skill_md),
            "skill_dir": "",
        }
        records[proposal.name] = entry
        self._save(records)
        return ApprovalRecord(
            name=proposal.name,
            status=status,
            summary=proposal.summary,
            gap=proposal.source_gap,
            tests=test,
            decided_at=entry["decided_at"],
            skill_dir=entry["skill_dir"],
        )

    def approved_skills(self) -> list[dict]:
        return [e for e in self._load().values() if e.get("status") == "approved"]

    def _record_skill_dir(self, name: str, skill_dir: str) -> None:
        records = self._load()
        if name in records:
            records[name]["skill_dir"] = skill_dir
            self._save(records)

    # -- the gate itself -------------------------------------------------
    def request_approval(
        self,
        proposal: Proposal,
        test: SandboxTestResult,
        *,
        input_fn: Callable[[str], str] = input,
        print_fn=print,
    ) -> bool:
        """§6.6 step 3: the user sees what the skill does and what it can
        touch, and approves or rejects. Refused unless sandbox tests passed."""
        if not test.passed:
            print_fn("  [skills] tests did not pass — no approval prompt shown.")
            self.record(proposal, test, "rejected")
            return False
        print_fn("=" * 64)
        print_fn(f"  New skill proposal: {proposal.name}")
        print_fn(f"  Capability gap addressed: {proposal.source_gap}")
        print_fn(f"  What it does / can touch: {proposal.summary}")
        print_fn(f"  Sandbox tests: PASSED (rc={test.returncode}; {test.note or 'clean'})")
        if test.isolation != "docker":
            print_fn("  ⚠ Isolation: RESTRICTED PROCESS, not a container. The test ran"
                     " as this user and could have read any file this user can read."
                     " Install Docker on WSL2 to get the design's §6.6 boundary.")
        else:
            print_fn("  Isolation: Docker container (no network, memory-capped, no host mounts)")
        print_fn("  Skill source:")
        for line in proposal.skill_md.splitlines()[:25]:
            print_fn(f"    | {line}")
        answer = input_fn("  Approve and install this skill? [approve/reject] ").strip().lower()
        approved = answer in ("approve", "a", "y", "yes")
        self.record(proposal, test, "approved" if approved else "rejected")
        if approved:
            dest = self.promote(proposal)
            self._record_skill_dir(proposal.name, str(dest))
            print_fn(f"  [skills] approved — installed at {dest}")
        else:
            print_fn("  [skills] rejected — not installed.")
        return approved

    # -- promotion -------------------------------------------------------
    def promote(self, proposal: Proposal) -> Path:
        """Write SKILL.md into the OpenJarvis skills dir so next session's
        SkillManager.discover() picks it up. JARVIS-side; no framework edits.

        Uses this instance's own home: writing to the global config dir while
        the ledger records a different home leaves the ledger pointing at a
        directory the skill was never installed into.
        """
        dest = self._home / "skills" / proposal.name
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "SKILL.md").write_text(proposal.skill_md, encoding="utf-8")
        return dest


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = ["SkillGovernance", "ApprovalRecord"]
