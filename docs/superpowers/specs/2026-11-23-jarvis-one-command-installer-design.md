# JARVIS one-command Windows installer design (approved scope: small change)

## Intent
Give the user one copyable PowerShell command that installs or updates their private JARVIS checkout, prompts for optional credentials with sign-up links, and leaves the app ready to launch.

## Approaches
1. One-line remote PowerShell bootstrap that downloads `scripts/install.ps1` from the raw GitHub URL and executes it. This is the closest match to "one command"; a remote script is a trust boundary, so the command should print the URL and the script should be readable in the repository first.
2. Local `.\scripts\install.ps1` command after cloning. Safer and auditable, but requires a separate clone step, so it is not truly one command on a fresh machine.
3. Use the vendored OpenJarvis installer. Rejected: it installs the upstream runtime, not this private repository, its orb shell, its settings, or its secret names.

Chosen: approach 1, with the script committed in this repository so the remote code and the reviewed code are the same.

## Design
- Bootstrap command downloads `scripts/install.ps1` from the `master` branch of `jeremiaholex-lab/JARVIS` and invokes it.
- Installer checks Windows, winget, git, Python 3.12+, and .NET SDK 8; installs missing prerequisites with winget and verifies the resulting commands.
- Installer clones into `$env:USERPROFILE\Desktop\Projects\JARVIS` when absent, otherwise runs `git pull --ff-only` and `git submodule update --init --recursive`.
- Installer installs Python dependencies through the vendored runtime's `.venv` using `uv`, and pulls `qwen3:1.7b` through Ollama.
- Credentials are optional because local inference works without them. Prompt with official links for Gemini, OpenAI, Anthropic, CrowLLM, Telegram, Notion, and Microsoft 365. Store only non-empty values in `.env`, with restrictive ACLs where possible. Never echo secret values.
- Do not commit `.env`, OAuth files, tokens, model weights, or local databases.
- Finish with `orb\launch_orb.bat` as the manual start command because an installer should not unexpectedly leave a long-running GUI process behind.
