from __future__ import annotations

import json

import pytest

from jarvis.sentry import watcher


class FakeEngine:
    """Minimal stand-in for a model engine returning a JSON diagnosis."""

    def __init__(self, payload: dict):
        self.payload = payload

    def generate(self, messages, model=None, max_tokens=None, temperature=0.0):
        return {"content": json.dumps(self.payload)}


def _failure() -> watcher.Failure:
    return watcher.Failure(
        trace_id="trace-1",
        agent="sample-skill",
        summary="failed: skill named sample-skill is broken",
        raw="failure in skill sample-skill: output is wrong",
    )


def _no_sandbox(monkeypatch, tmp_path):
    """Keep the test focused on patch application, not real process isolation."""
    captured = {}

    def fake_run(test_script, *, workdir, timeout):
        captured["script"] = test_script
        captured["workdir"] = workdir
        captured["skill_text"] = (workdir / "skill" / "sample.py").read_text(encoding="utf-8")
        return type("Result", (), {
            "passed": True,
            "note": "stubbed sandbox",
        })()

    def fake_workdir(skill_name, *, candidate_root):
        work = candidate_root / skill_name / "run"
        work.mkdir(parents=True)
        return work

    monkeypatch.setattr(watcher, "run_skill_tests", fake_run)
    monkeypatch.setattr(watcher, "_candidate_workdir", fake_workdir)
    return captured


def _candidate_root(tmp_path):
    root = tmp_path / "skill-heals"
    root.mkdir()
    return root


def test_heal_failure_applies_a_unified_diff_before_sandbox_test(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "sample-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "sample.py").write_text(
        "def value():\n    return 'old'\n", encoding="utf-8"
    )
    captured = _no_sandbox(monkeypatch, tmp_path)
    candidate_root = _candidate_root(tmp_path)
    payload = {
        "diagnosis": "wrong return value",
        "fix": (
            "--- a/sample.py\n"
            "+++ b/sample.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def value():\n"
            "-    return 'old'\n"
            "+    return 'new'\n"
        ),
        "test_script": "print('checking patched skill')",
        "changed_files": ["sample.py"],
    }

    candidate = watcher.heal_failure(
        _failure(),
        local_engine=FakeEngine(payload),
        skills_dir=skills_dir,
        candidate_root=candidate_root,
    )

    assert captured["skill_text"] == "def value():\n    return 'new'\n"
    assert candidate.sandbox_passed is True


def test_heal_failure_applies_multi_hunk_unified_diff_with_line_shift(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "sample-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "sample.py").write_text(
        "def first():\n"
        "    return 1\n"
        "\n"
        "def middle():\n"
        "    return 2\n"
        "\n"
        "def last():\n"
        "    return 'old'\n",
        encoding="utf-8",
    )
    captured = _no_sandbox(monkeypatch, tmp_path)
    candidate_root = _candidate_root(tmp_path)
    payload = {
        "diagnosis": "update first and last functions",
        "fix": (
            "--- a/sample.py\n"
            "+++ b/sample.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def first():\n"
            "+    # added line shifts later hunks\n"
            "     return 1\n"
            "@@ -7,2 +8,2 @@\n"
            " def last():\n"
            "-    return 'old'\n"
            "+    return 'new'\n"
        ),
        "test_script": "print('checking multi-hunk diff')",
        "changed_files": ["sample.py"],
    }

    candidate = watcher.heal_failure(
        _failure(),
        local_engine=FakeEngine(payload),
        skills_dir=skills_dir,
        candidate_root=candidate_root,
    )

    assert captured["skill_text"] == (
        "def first():\n"
        "    # added line shifts later hunks\n"
        "    return 1\n"
        "\n"
        "def middle():\n"
        "    return 2\n"
        "\n"
        "def last():\n"
        "    return 'new'\n"
    )
    assert candidate.sandbox_passed is True


def test_heal_failure_applies_full_file_replacements_before_sandbox_test(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "sample-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "sample.py").write_text("old = True\n", encoding="utf-8")
    captured = _no_sandbox(monkeypatch, tmp_path)
    candidate_root = _candidate_root(tmp_path)
    payload = {
        "diagnosis": "replace module",
        "fix": "",
        "test_script": "print('checking patched skill')",
        "changed_files": {"sample.py": "value = 'new'\n"},
    }

    watcher.heal_failure(
        _failure(),
        local_engine=FakeEngine(payload),
        skills_dir=skills_dir,
        candidate_root=candidate_root,
    )

    assert captured["skill_text"] == "value = 'new'\n"


def test_heal_failure_refuses_a_patch_target_outside_the_scratch_skill(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "sample-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "sample.py").write_text("old = True\n", encoding="utf-8")
    captured = _no_sandbox(monkeypatch, tmp_path)
    candidate_root = _candidate_root(tmp_path)
    outside = tmp_path / "victim.txt"
    outside.write_text("original\n", encoding="utf-8")
    payload = {
        "diagnosis": "escape the scratch tree",
        "fix": "",
        "test_script": "print('checking patch boundary')",
        "changed_files": {f"../../{outside.name}": "overwritten\n"},
    }

    candidate = watcher.heal_failure(
        _failure(),
        local_engine=FakeEngine(payload),
        skills_dir=skills_dir,
        candidate_root=candidate_root,
    )

    assert outside.read_text(encoding="utf-8") == "original\n"
    assert captured["skill_text"] == "old = True\n"
    assert "escapes the scratch skill" in candidate.note
