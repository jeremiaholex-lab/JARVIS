# JARVIS One-Command Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide one copyable PowerShell command that installs or updates the private JARVIS Windows checkout and prompts for optional credentials.

**Architecture:** A committed `scripts/install.ps1` owns all installation logic. A separate `scripts/bootstrap.ps1` contains only the remote-download-and-invoke bootstrap text copied into the README; this keeps the one-liner short and reviewable. The installer uses winget for missing system prerequisites, git for the private repo, the vendored runtime's `.venv` with `uv` for Python dependencies, and a gitignored `.env` for secrets.

**Tech Stack:** PowerShell 5.1+, Windows 11, winget, git, Python 3.12+, uv, .NET SDK 8, Ollama.

**Spec:** `docs/superpowers/specs/2026-11-23-jarvis-one-command-installer-design.md`

## Global Constraints

- Windows 11 only.
- Python 3.12+ required.
- Default checkout: `$env:USERPROFILE\Desktop\Projects\JARVIS`.
- Repository: `https://github.com/jeremiaholex-lab/JARVIS.git`, private, default branch `master`.
- Default model: `qwen3:1.7b`.
- Secrets go only to `.env`; never echo secret values or commit `.env`, OAuth files, tokens, model weights, or local databases.
- Local inference must work with no API keys; all credential prompts are optional.
- Installer is report-and-prepare only at the end: do not launch a long-running GUI process automatically.
- Replace the incorrect `scripts/jarvis-install.sh` draft rather than leaving two competing installers.

## Review Focus

- Fresh machine: missing git, Python, .NET, winget, or Ollama must produce actionable links/messages instead of a cryptic failure. Pin with a Pester test for every prerequisite message.
- Existing checkout with local changes: `git pull --ff-only` must fail safely rather than overwrite work. Pin with a test that asserts the non-destructive failure message.
- Private auth: repository access failure must explain that the user needs a GitHub invitation. Pin with a test for the clone-failure message.
- Optional credentials: blank input must skip that key, and secrets must never print. Pin with tests for `.env` contents and console output.
- Credential value safety: values containing `=`, quotes, `#`, or spaces must be written without corruption. Pin with a test that writes and reads such a value.

---

### Task 1: Replace the broken shell installer with a PowerShell installer

**Files:**
- Delete: `scripts/jarvis-install.sh`
- Create: `scripts/install.ps1`
- Create: `tests/test_jarvis_installer.ps1`

**Interfaces:**
- Produces: `scripts/install.ps1` accepting `[-RepoUrl <string>] [-InstallDir <string>] [-SkipCredentials] [-SkipOllamaPull]`.
- Produces: helper functions `Write-JarvisEnv -Path <string> -Values <hashtable>` and `Get-JarvisPrerequisites` for tests.
- Consumes: no earlier tasks.

- [ ] **Step 1: Write Pester tests for prerequisite checks and `.env` writing**

Create `tests/test_jarvis_installer.ps1` with tests that dot-source `scripts/install.ps1` without running its main block, and assert:
  - `Write-JarvisEnv` writes only non-empty values.
  - `Write-JarvisEnv` preserves values containing `=`, `#`, quotes, and spaces.
  - `Write-JarvisEnv` applies a restrictive ACL on Windows and never writes a secret to the console.
  - `Get-JarvisPrerequisites` reports missing tools by name with install links for git, Python 3.12+, .NET SDK 8, winget, and Ollama.

- [ ] **Step 2: Run tests to verify they fail**

Run: `powershell -NoProfile -Command "Invoke-Pester .\tests\test_jarvis_installer.ps1"`
Expected: FAIL because `scripts/install.ps1` does not exist.

- [ ] **Step 3: Implement the installer**

Implement `scripts/install.ps1` in PowerShell 5.1-compatible syntax:
  - `Set-StrictMode -Version Latest`; `$ErrorActionPreference = "Stop"`.
  - Guard the main flow with `if ($MyInvocation.InvocationName -ne '.')` so Pester can dot-source helpers safely.
  - Check prerequisites and offer winget install only when winget exists; otherwise print official install links and stop.
  - Clone the private repo when absent; otherwise `git pull --ff-only`, then `git submodule update --init --recursive`.
  - Create `openjarvis_runtime\.venv` with `python -m venv`, install dependencies with `python -m pip install -e ".[server,desktop,speech,voice,memory-faiss]"`, and verify `python -m pytest --collect-only -q` succeeds.
  - Install Ollama via winget when missing, start it when needed, and pull `qwen3:1.7b` unless skipped.
  - Prompt for optional credentials with official sign-up URLs: `GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `CROWLLM_API_KEY`, `TELEGRAM_BOT_TOKEN`, `JARVIS_NOTION_TOKEN`, `JARVIS_MICROSOFT_CLIENT_ID`.
  - Use `Read-Host -AsSecureString` for secret values, convert to plain text only when writing `.env`, and never output entered values.
  - Print final instructions: `cd orb`, `.\launch_orb.bat`, `http://127.0.0.1:8010/`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `powershell -NoProfile -Command "Invoke-Pester .\tests\test_jarvis_installer.ps1"`
Expected: PASS.

- [ ] **Step 5: Syntax-check the installer**

Run: `powershell -NoProfile -Command "$errors = $null; [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path .\scripts\install.ps1), [ref]$null, [ref]$errors) > $null; if ($errors) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"`
Expected: exit code 0 with no errors.

- [ ] **Step 6: Commit**

```bash
git add tests/test_jarvis_installer.ps1 scripts/install.ps1 scripts/jarvis-install.sh
git commit -m "feat(install): add Windows one-command installer"
```

### Task 2: Add the one-line bootstrap and README instructions

**Files:**
- Create: `scripts/bootstrap.ps1`
- Modify: `README.md:150-189`

**Interfaces:**
- Consumes: `scripts/install.ps1` from Task 1.
- Produces: one-line bootstrap command printed in README and stored as an executable script that downloads installer source from `master`.

- [ ] **Step 1: Write a test for the bootstrap URL and command shape**

Add a Pester test asserting `scripts/bootstrap.ps1` uses `https://raw.githubusercontent.com/jeremiaholex-lab/JARVIS/master/scripts/install.ps1`, does not pipe a remote response directly into `Invoke-Expression`, and invokes the downloaded local script path.

- [ ] **Step 2: Run the test to verify it fails**

Run: `powershell -NoProfile -Command "Invoke-Pester .\tests\test_jarvis_installer.ps1"`
Expected: FAIL because `scripts/bootstrap.ps1` does not exist.

- [ ] **Step 3: Implement the bootstrap**

Create `scripts/bootstrap.ps1` that downloads the installer to a temporary local `.ps1`, verifies the file is non-empty, executes it, and removes the temporary file in a `finally` block.

- [ ] **Step 4: Update README getting-started section**

Replace the manual clone/dependency instructions with a "One-command install (Windows 11)" subsection containing the single bootstrap command and a short explanation of prompts, followed by the existing "Start the orb" instructions. Keep a manual setup subsection for developers.

- [ ] **Step 5: Run tests and verify links**

Run: `powershell -NoProfile -Command "Invoke-Pester .\tests\test_jarvis_installer.ps1"`
Expected: PASS.

Run: `python -c "from pathlib import Path; p=Path('README.md'); missing=[x for x in ['scripts/install.ps1','scripts/bootstrap.ps1'] if x not in p.read_text()]; raise SystemExit(missing)"`
Expected: exit code 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/bootstrap.ps1 README.md tests/test_jarvis_installer.ps1
git commit -m "docs: add one-command JARVIS install"
```
