import json
import sqlite3
import time

import pytest

from jarvis.skills.optimize import (
    Analysis, analyze, collect_evidence, optimize_skill, propose_improvement,
)

SKILL_MD = (
    "---\n"
    "name: word-count-lines\n"
    "description: Count words and lines in a text file.\n"
    "user_invocable: true\n"
    "---\n"
    "Use when asked for the size of a text file.\n"
)


def make_traces(db, rows):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE traces (id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT UNIQUE,"
        " query TEXT, agent TEXT, model TEXT, engine TEXT, result TEXT, outcome TEXT,"
        " feedback REAL, started_at REAL, ended_at REAL, total_tokens INTEGER,"
        " total_latency_seconds REAL, metadata TEXT, messages TEXT)"
    )
    for row in rows:
        conn.execute(
            "INSERT INTO traces (trace_id, query, agent, result, outcome, feedback,"
            " started_at, total_latency_seconds, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row["trace_id"], row.get("query", "run word-count-lines on notes.txt"),
                row.get("agent", "native_openhands"), row.get("result", "ok"),
                row.get("outcome", "success"), row.get("feedback"),
                row.get("started_at", time.time()), row.get("latency", 1.0),
                json.dumps(row.get("metadata", {})),
            ),
        )
    conn.commit()
    conn.close()


def test_only_traces_naming_the_skill_count_as_evidence(tmp_path):
    db = tmp_path / "traces.db"
    make_traces(db, [
        {"trace_id": "a", "query": "word-count-lines notes.txt", "result": "5 words"},
        {"trace_id": "b", "query": "some other skill entirely", "result": "unrelated"},
    ])
    evidence = collect_evidence("word-count-lines", traces_db=db)
    assert [e.trace_id for e in evidence] == ["a"]


def test_missing_trace_store_yields_no_evidence(tmp_path):
    assert collect_evidence("anything", traces_db=tmp_path / "nope.db") == []


def test_failure_outcome_and_error_text_are_both_detected(tmp_path):
    db = tmp_path / "traces.db"
    make_traces(db, [
        {"trace_id": "f1", "query": "word-count-lines x", "outcome": "failed",
         "result": "Traceback: FileNotFoundError"},
        {"trace_id": "f2", "query": "word-count-lines y", "outcome": "success",
         "result": "error: no such file or directory"},
        {"trace_id": "ok", "query": "word-count-lines z", "outcome": "success",
         "result": "12 words, 3 lines"},
        {"trace_id": "fb", "query": "word-count-lines w", "outcome": "success",
         "result": "meh", "feedback": 0.1},
    ])
    analysis = analyze("word-count-lines", collect_evidence("word-count-lines", traces_db=db))
    assert analysis.total == 4
    assert analysis.bad == 3
    assert analysis.has_signal
    assert any("traceback" in p for p in analysis.patterns)
    assert any("low user feedback" in p for p in analysis.patterns)


def test_clean_usage_produces_no_optimization_signal(tmp_path):
    db = tmp_path / "traces.db"
    make_traces(db, [
        {"trace_id": f"c{i}", "query": "word-count-lines notes.txt",
         "result": "9 words, 2 lines", "outcome": "success"} for i in range(5)
    ])
    analysis = analyze("word-count-lines", collect_evidence("word-count-lines", traces_db=db))
    assert not analysis.has_signal
    assert analysis.bad == 0


def test_slow_but_correct_runs_are_noted_without_being_called_failures(tmp_path):
    db = tmp_path / "traces.db"
    make_traces(db, [
        {"trace_id": "s1", "query": "word-count-lines big.txt", "result": "done",
         "outcome": "success", "latency": 61.0},
    ])
    analysis = analyze("word-count-lines", collect_evidence("word-count-lines", traces_db=db))
    assert analysis.bad == 0
    assert any("30s" in p for p in analysis.patterns)
    assert not analysis.has_signal, "slowness alone must not rewrite a working skill"


class Proposer:
    def __init__(self, payload=None, raises=False):
        self.payload = payload
        self.raises = raises

    def generate(self, msgs, **kwargs):
        if self.raises:
            raise RuntimeError("engine down")
        return {"content": json.dumps(self.payload)}


IMPROVED = {
    "skill_md": SKILL_MD.replace("Use when asked", "Check the file exists first. Use when asked"),
    "rationale": "traces show FileNotFoundError on missing paths",
    "test_script": (
        "import sys, pathlib\n"
        "d = pathlib.Path(sys.argv[1])\n"
        "(d / 'sample.txt').write_text('a b\\nc\\n')\n"
        "assert (d / 'sample.txt').read_text().splitlines() == ['a b', 'c']\n"
        "print('OK improved')\n"
    ),
}


def test_propose_prefers_local_engine():
    analysis = Analysis("word-count-lines", 4, 3, ["traceback appeared in 2 run(s)"], ["q"])
    payload = propose_improvement("word-count-lines", SKILL_MD, analysis,
                                 local_engine=Proposer(IMPROVED), cloud_engine=Proposer(IMPROVED))
    assert payload["skill_md"].startswith("---")
    assert payload["_engine"] == "qwen3:1.7b"


def test_propose_falls_back_to_cloud_when_local_gives_garbage():
    analysis = Analysis("word-count-lines", 4, 3, ["traceback appeared in 2 run(s)"], ["q"])
    payload = propose_improvement("word-count-lines", SKILL_MD, analysis,
                                 local_engine=Proposer({"nonsense": 1}),
                                 cloud_engine=Proposer(IMPROVED))
    assert payload["_engine"] == "agnes-3.0-flash"


def test_propose_returns_empty_when_every_engine_fails():
    analysis = Analysis("word-count-lines", 4, 3, ["traceback appeared in 2 run(s)"], ["q"])
    assert propose_improvement("word-count-lines", SKILL_MD, analysis,
                               local_engine=Proposer(raises=True),
                               cloud_engine=Proposer(raises=True)) == {}


@pytest.fixture
def live_skill(tmp_path):
    skill_dir = tmp_path / "skills" / "word-count-lines"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    db = tmp_path / "traces.db"
    make_traces(db, [
        {"trace_id": "f1", "query": "word-count-lines missing.txt", "outcome": "failed",
         "result": "Traceback: FileNotFoundError"},
    ])
    return tmp_path, skill_dir, db


def test_optimise_refuses_without_a_live_skill(tmp_path):
    out = []
    rc = optimize_skill("ghost-skill", home=tmp_path, skills_dir=tmp_path / "skills",
                        print_fn=out.append)
    assert rc == 2
    assert any("no live skill" in line for line in out)


def test_optimise_refuses_without_evidence(tmp_path, monkeypatch):
    """Clean usage must leave a working skill completely alone."""
    home = tmp_path
    skill_dir = home / "skills" / "word-count-lines"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    db = home / "traces.db"
    make_traces(db, [
        {"trace_id": "c1", "query": "word-count-lines a.txt", "result": "7 words", "outcome": "success"},
        {"trace_id": "c2", "query": "word-count-lines b.txt", "result": "3 words", "outcome": "success"},
    ])
    monkeypatch.setattr("jarvis.skills.optimize._traces_db", lambda h: db)
    out = []
    rc = optimize_skill("word-count-lines", home=home, skills_dir=skill_dir.parent,
                        local_engine=Proposer(IMPROVED), print_fn=out.append)
    assert rc == 0
    assert any("no failure signal" in line for line in out)
    assert (skill_dir / "SKILL.md").read_text() == SKILL_MD
    assert not (home / "skill-optimisations").exists()


def test_optimise_dry_run_changes_nothing(live_skill, monkeypatch):
    home, skill_dir, db = live_skill
    monkeypatch.setattr("jarvis.skills.optimize._traces_db", lambda h: db)
    out = []
    rc = optimize_skill("word-count-lines", home=home, skills_dir=skill_dir.parent,
                        local_engine=Proposer(IMPROVED), dry_run=True, print_fn=out.append)
    assert rc == 0
    assert any("--dry-run" in line for line in out)
    assert (skill_dir / "SKILL.md").read_text() == SKILL_MD


def test_optimise_parks_candidate_and_requires_approval(live_skill, monkeypatch):
    home, skill_dir, db = live_skill
    monkeypatch.setattr("jarvis.skills.optimize._traces_db", lambda h: db)
    out = []
    rc = optimize_skill(
        "word-count-lines", home=home, skills_dir=skill_dir.parent,
        local_engine=Proposer(IMPROVED), input_fn=lambda _p: "reject", print_fn=out.append,
    )
    assert rc == 1
    assert any("not approved" in line for line in out)
    assert (skill_dir / "SKILL.md").read_text() == SKILL_MD, "rejection must not touch live skill"
    parked = list((home / "skill-optimisations" / "word-count-lines").rglob("SKILL.md"))
    assert parked, "the candidate must be kept for review"
    analysis = json.loads(list(parked[0].parent.glob("analysis.json"))[0].read_text())
    assert analysis["bad_traces"] == 1
    assert analysis["traces_reviewed"] == 1


def test_optimise_promotes_only_after_approval_and_passing_tests(live_skill, monkeypatch):
    home, skill_dir, db = live_skill
    monkeypatch.setattr("jarvis.skills.optimize._traces_db", lambda h: db)
    out = []
    rc = optimize_skill(
        "word-count-lines", home=home, skills_dir=skill_dir.parent,
        local_engine=Proposer(IMPROVED), auto_approve=True, print_fn=out.append,
    )
    assert rc == 0
    promoted = (skill_dir / "SKILL.md").read_text()
    assert "Check the file exists first" in promoted
    ledger = json.loads((home / "skill-governance" / "jarvis-skill-approvals.json").read_text())
    assert ledger["word-count-lines"]["status"] == "approved"


def test_optimise_rejects_a_proposal_that_renames_the_skill(live_skill, monkeypatch):
    home, skill_dir, db = live_skill
    monkeypatch.setattr("jarvis.skills.optimize._traces_db", lambda h: db)
    renamed = dict(IMPROVED, skill_md=IMPROVED["skill_md"].replace(
        "name: word-count-lines", "name: totally-other"))
    out = []
    rc = optimize_skill("word-count-lines", home=home, skills_dir=skill_dir.parent,
                        local_engine=Proposer(renamed), print_fn=out.append)
    assert rc == 1
    assert any("renamed the skill" in line for line in out)


def test_optimise_rejects_unparseable_skill_md(live_skill, monkeypatch):
    home, skill_dir, db = live_skill
    monkeypatch.setattr("jarvis.skills.optimize._traces_db", lambda h: db)
    bad = dict(IMPROVED, skill_md="no frontmatter at all")
    out = []
    rc = optimize_skill("word-count-lines", home=home, skills_dir=skill_dir.parent,
                        local_engine=Proposer(bad), print_fn=out.append)
    assert rc == 1
    assert any("does not parse" in line for line in out)
    assert (skill_dir / "SKILL.md").read_text() == SKILL_MD
