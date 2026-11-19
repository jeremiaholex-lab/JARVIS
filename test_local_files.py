"""Local read-only file tools: read_file / list_dir / search_files."""
import json

from jarvis.agent import tools as agent_tools
from jarvis.security.tiers import Tier
from jarvis.skills.files import LocalFiles


def test_local_file_tools_are_read_only_and_offered():
    available = agent_tools.build_tools()
    names = {t.name for t in available}
    for tool_name in ("read_file", "list_dir", "search_files"):
        tool = next(t for t in available if t.name == tool_name)
        assert tool.tier is Tier.READ
    schemas = {s["function"]["name"] for s in agent_tools.tool_schemas(available)}
    assert {"read_file", "list_dir", "search_files"} <= schemas


def test_read_file_pages_lines_and_flags_truncation(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("\n".join(f"line {i}" for i in range(1, 11)))
    out = LocalFiles().read_file(str(f), offset=3, limit=4)
    assert out["text"] == "line 3\nline 4\nline 5\nline 6"
    assert out["total_lines"] == 10
    assert not out["truncated_bytes"]


def test_read_file_refuses_binary(tmp_path):
    b = tmp_path / "blob.bin"
    b.write_bytes(b"\x00\x01\x02binary")
    try:
        LocalFiles().read_file(str(b))
        assert False, "binary read should be refused"
    except ValueError:
        pass


def test_read_file_refuses_secret_paths_by_name(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("API_KEY=abc")
    out = json.loads(agent_tools.execute(agent_tools.build_tools(), "read_file", {"path": str(secret)}))
    assert "error" in out and "credential" in out["error"].lower() or "secret" in out["error"].lower()


def test_list_dir_marks_directories(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("x")
    out = LocalFiles().list_dir(str(tmp_path))
    assert out["entries"] == ["sub/", "a.txt"]
    assert out["total"] == 2


def test_search_files_finds_matches_and_skips_secrets(tmp_path):
    (tmp_path / "code.py").write_text("x = 1\n# needle here\ny = 2\n")
    (tmp_path / ".env").write_text("NEEDLE=secret\n")
    out = LocalFiles().search("needle", str(tmp_path))
    assert out["matches"] == [f"{tmp_path / 'code.py'}:2: # needle here"]
    assert not out["more"]


def test_search_files_error_becomes_json_error_string(tmp_path):
    out = json.loads(agent_tools.execute(agent_tools.build_tools(), "search_files",
                                         {"pattern": "x", "root": str(tmp_path / "nope")}))
    assert "error" in out
