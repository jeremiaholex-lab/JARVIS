"""§6.6 pipeline: gap -> proposed skill -> sandbox tests -> approval gate.

Run:  python -m jarvis.skills.pipeline "capability gap description"
      (options: --no-llm uses the static demo proposal; --auto-approve for tests)

Done-test #2 walkthrough: the agent forges a brand-new skill for a
capability it lacked, it passes its sandbox tests, it asks the user to
approve, and after approval it is invokable in the next session
(installed into the OpenJarvis skills dir, which the framework's
SkillManager.discover() scans at startup).
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from .approve import SkillGovernance
from .propose import Proposal, ProposalError, propose_skill
from .sandbox_test import run_skill_tests

# Module logger
_logger = logging.getLogger(__name__)

_DEMO_PROPOSAL = Proposal(
    name="word-count-lines",
    manifest=None,  # filled by validate below
    skill_md=(
        "---\n"
        'name: word-count-lines\n'
        'description: Count words and lines in a text file and report both.\n'
        "user_invocable: true\n"
        "---\n"
        "Use this skill when asked to summarize the size of a text file.\n"
        "Run the counting script against the target path and report the numbers.\n"
    ),
    test_script=(
        "import sys, pathlib\n"
        "workdir = pathlib.Path(sys.argv[1])\n"
        "sample = workdir / 'sample.txt'\n"
        "sample.write_text('one two three\\nfour five\\n')\n"
        "text = sample.read_text()\n"
        "words = len(text.split())\n"
        "lines = len(text.splitlines())\n"
        "assert words == 5, words\n"
        "assert lines == 2, lines\n"
        "print('OK word-count-lines:', words, 'words,', lines, 'lines')\n"
    ),
    summary="Counts words/lines in a text file given a path. Touches: read-only file access only.",
    source_gap="demo: no way to report word/line counts of a file",
)


def _engines():
    """Local brain plus cloud escalation, either of which may be absent."""
    from jarvis.security.launcher import _build_engines

    return _build_engines()


def build_proposal(gap: str, *, no_llm: bool) -> Proposal:
    if no_llm:
        proposal = _DEMO_PROPOSAL
    else:
        local, cloud = _engines()
        if local is None and cloud is None:
            raise ProposalError(
                "no inference engine available (Ollama down and no cloud key) — "
                "use --no-llm for the built-in demo proposal"
            )
        proposal = propose_skill(gap, local_engine=local, cloud_engine=cloud)
    from .propose import validate_proposal

    proposal.manifest = validate_proposal(proposal.skill_md)
    return proposal


def run_pipeline(gap: str, *, no_llm: bool = False, auto_approve: bool = False) -> int:
    _logger.info("capability gap: %s", gap)
    try:
        proposal = build_proposal(gap, no_llm=no_llm)
    except ProposalError as exc:
        # A rejected proposal is a decision, not a crash: report it and stop.
        _logger.error("no skill proposed: %s", exc)
        return 1
    _logger.info("agent proposed skill '%s': %s", proposal.name, proposal.summary)

    _logger.info("running the skill's tests in the sandbox...")
    test = run_skill_tests(proposal.test_script)
    _logger.info("sandbox result: passed=%s isolation=%s rc=%s %s",
                 test.passed, test.isolation, test.returncode, test.note)
    if test.stdout.strip():
        _logger.info("test output: %s", test.stdout.strip()[:400])

    gov = SkillGovernance()
    approved = gov.request_approval(proposal, test, input_fn=(lambda _p: "approve" if auto_approve else input(_p)))
    if approved:
        _logger.info("done-test: next session, SkillManager.discover() loads %s — invocable now.", proposal.name)
    return 0 if approved else 1


def main(argv: Optional[list[str]] = None) -> int:
    from jarvis.logging import setup_from_env

    setup_from_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("gap", nargs="?", default="no way to count words and lines in a file")
    ap.add_argument("--no-llm", action="store_true", help="use the static demo proposal")
    ap.add_argument("--auto-approve", action="store_true", help="test mode: approve without typing")
    args = ap.parse_args(argv)
    return run_pipeline(args.gap, no_llm=args.no_llm, auto_approve=args.auto_approve)


if __name__ == "__main__":
    sys.exit(main())
