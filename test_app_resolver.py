"""Tests for app-name resolution (pure logic; no Windows needed)."""
from __future__ import annotations

from pathlib import Path

from jarvis.desktop.app_resolver import AppEntry, resolve, scan_shortcuts, start_menu_dirs

INDEX = [
    AppEntry("Google Chrome", "C:/ProgramData/Start Menu/Google Chrome.lnk"),
    AppEntry("Spotify", "C:/Users/me/Start Menu/Spotify.lnk"),
    AppEntry("Visual Studio", "C:/VS/Visual Studio.lnk"),
    AppEntry("Visual Studio Code", "C:/VSCode/Code.lnk"),
    AppEntry("Chrome Remote Desktop", "C:/CRD/Chrome Remote Desktop.lnk"),
    AppEntry("Microsoft Word", "C:/Office/Word.lnk"),
]


def test_exact_name_matches():
    r = resolve("Spotify", INDEX)
    assert r.status == "match" and r.entry.name == "Spotify"


def test_case_and_punctuation_are_ignored():
    r = resolve("  SPOTIFY!! ", INDEX)
    assert r.status == "match" and r.entry.name == "Spotify"


def test_typo_resolves_to_closest_app():
    r = resolve("spotfy", INDEX)
    assert r.status == "match" and r.entry.name == "Spotify"


def test_substring_resolves_short_name():
    r = resolve("word", INDEX)
    assert r.status == "match" and r.entry.name == "Microsoft Word"


def test_alias_maps_to_installed_name():
    r = resolve("code", INDEX, aliases={"code": "visual studio code"})
    assert r.status == "match" and r.entry.name == "Visual Studio Code"


def test_exact_name_beats_longer_match():
    r = resolve("visual studio", INDEX)
    assert r.status == "match" and r.entry.name == "Visual Studio"


def test_ambiguous_query_returns_candidates_not_a_guess():
    r = resolve("chrome", INDEX)
    assert r.status == "ambiguous" and r.entry is None
    names = {e.name for e in r.candidates}
    assert {"Google Chrome", "Chrome Remote Desktop"} <= names


def test_unknown_app_is_not_found():
    r = resolve("zzqqxx", INDEX)
    assert r.status == "not_found" and r.entry is None and r.candidates == ()


def test_empty_query_is_not_found():
    assert resolve("   ", INDEX).status == "not_found"


def test_start_menu_dirs_use_env_and_skip_missing():
    dirs = start_menu_dirs({"APPDATA": "C:/u/AppData/Roaming"})
    assert len(dirs) == 1
    assert "Start Menu" in str(dirs[0])


def test_scan_indexes_shortcuts_and_skips_uninstallers(tmp_path: Path):
    root = tmp_path / "Programs"
    (root / "Tools").mkdir(parents=True)
    (root / "Spotify.lnk").write_text("x")
    (root / "Tools" / "Notepad++.lnk").write_text("x")
    (root / "Uninstall Spotify.lnk").write_text("x")
    (root / "readme.txt").write_text("x")
    names = [e.name for e in scan_shortcuts([root, tmp_path / "missing"])]
    assert names == ["Notepad++", "Spotify"]
