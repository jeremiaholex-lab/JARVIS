"""Tests for spoken-name to shortcut preparation (no Windows needed)."""
from __future__ import annotations

from jarvis.desktop.app_launch import prepare_open_app
from jarvis.desktop.app_resolver import AppEntry

INDEX = [
    AppEntry("Spotify", "C:/ProgramData/Start Menu/Spotify.lnk"),
    AppEntry("Google Chrome", "C:/ProgramData/Start Menu/Google Chrome.lnk"),
    AppEntry("Chrome Remote Desktop", "C:/ProgramData/Start Menu/Chrome Remote Desktop.lnk"),
    AppEntry("Visual Studio Code", "C:/ProgramData/Start Menu/Visual Studio Code.lnk"),
]


def test_spoken_name_resolves_to_shortcut_path():
    r = prepare_open_app("spotify", index=INDEX, aliases={})
    assert r == {"ok": True, "path": "C:/ProgramData/Start Menu/Spotify.lnk", "name": "Spotify"}


def test_alias_resolves_browser_to_chrome():
    r = prepare_open_app("browser", index=INDEX)
    assert r["ok"] and r["name"] == "Google Chrome"


def test_alias_code_resolves_to_vs_code():
    r = prepare_open_app("code", index=INDEX)
    assert r["ok"] and r["name"] == "Visual Studio Code"


def test_ambiguous_name_returns_candidates_and_no_path():
    r = prepare_open_app("chrome", index=INDEX, aliases={})
    assert r["ok"] is False and r["candidates"]
    assert "Google Chrome" in r["candidates"]
    assert "path" not in r


def test_unknown_name_fails_closed():
    r = prepare_open_app("zzqqxx", index=INDEX, aliases={})
    assert r["ok"] is False and r["candidates"] == []


def test_empty_name_fails_closed():
    assert prepare_open_app("  ", index=INDEX)["ok"] is False


def test_path_like_input_passes_through_to_policy():
    assert prepare_open_app("C:/Docs/report.pdf", index=INDEX) == {"ok": True, "path": None, "name": None}
    assert prepare_open_app("notepad.exe", index=INDEX) == {"ok": True, "path": None, "name": None}
