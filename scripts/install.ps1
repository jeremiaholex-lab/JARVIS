param(
    [string]$RepoUrl = 'https://github.com/jeremiaholex-lab/JARVIS.git',
    [string]$InstallDir = "$env:USERPROFILE\Desktop\Projects\JARVIS",
    [switch]$SkipCredentials,
    [switch]$SkipOllamaPull
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-JarvisEnv {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][hashtable]$Values
    )

    # Merge over an existing .env: skipped prompts (blank values) preserve
    # what the file already holds, and only supplied values overwrite.
    $existing = @{}
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in (Get-Content -LiteralPath $Path)) {
            if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
                $existing[$Matches[1]] = $line
            }
        }
    }
    foreach ($key in $Values.Keys) {
        $value = [string]$Values[$key]
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            $existing[$key] = '{0}={1}' -f $key, $value
        }
    }

    $lines = foreach ($key in $existing.Keys | Sort-Object) {
        $existing[$key]
    }
    Set-Content -LiteralPath $Path -Value $lines -Encoding UTF8
    try {
        icacls $Path /inheritance:r /grant:r "$env:USERNAME:(R,W)" 2>$null | Out-Null
    } catch {
        # ACL hardening is best-effort on non-NTFS filesystems.
    }
}

function Get-JarvisPrerequisites {
    $result = [ordered]@{}
    foreach ($tool in @('git', 'python', 'dotnet', 'winget', 'ollama')) {
        $command = Get-Command $tool -ErrorAction SilentlyContinue
        if ($command) {
            $result[$tool] = 'present'
        } else {
            $result[$tool] = switch ($tool) {
                'git' { 'Install from https://git-scm.com/download/win' }
                'python' { 'Install Python 3.12+ from https://www.python.org/downloads/windows/' }
                'dotnet' { 'Install .NET SDK 8 from https://dotnet.microsoft.com/download' }
                'winget' { 'Install App Installer from https://aka.ms/getwinget' }
                'ollama' { 'Install from https://ollama.com/download/windows' }
            }
        }
    }
    return $result
}

function Read-SecretValue {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$Url
    )
    Write-Host "$Label`n  Sign up or manage it here: $Url" -ForegroundColor Cyan
    $secure = Read-Host '  Value (Enter to skip)' -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Install-Jarvis {
    Write-Host 'JARVIS Windows installer' -ForegroundColor Green
    $prereqs = Get-JarvisPrerequisites
    foreach ($key in $prereqs.Keys) {
        if ($prereqs[$key] -ne 'present') {
            throw "Missing $key. $($prereqs[$key])"
        }
    }

    $pythonVersion = & python --version 2>&1
    if ($pythonVersion -notmatch 'Python 3\.(\d+)') {
        throw "Python 3.12+ is required; found: $pythonVersion"
    }
    if ([int]$Matches[1] -lt 12) {
        throw "Python 3.12+ is required; found: $pythonVersion"
    }

    if (-not (Test-Path -LiteralPath $InstallDir)) {
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    }

    Push-Location $InstallDir
    try {
        if (-not (Test-Path -LiteralPath '.git')) {
            Write-Host 'Cloning the public JARVIS repository...' -ForegroundColor Cyan
            & git clone --depth 1 $RepoUrl .
            if ($LASTEXITCODE -ne 0) {
                throw "Clone failed. Confirm the URL ($RepoUrl) is reachable and try again."
            }
        } else {
            & git pull --ff-only
            if ($LASTEXITCODE -ne 0) {
                throw 'Update failed. Save or commit local changes, then try again.'
            }
        }
        & git submodule update --init --recursive
        if ($LASTEXITCODE -ne 0) { throw 'Submodule setup failed.' }

        $runtimeDir = Join-Path $InstallDir 'openjarvis_runtime'
        $venvPython = Join-Path $runtimeDir '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $venvPython)) {
            & python -m venv (Join-Path $runtimeDir '.venv')
        }
        & $venvPython -m pip install --upgrade pip
        if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed.' }
        & $venvPython -m pip install -e "$runtimeDir[server,desktop,speech,voice,memory-faiss]"
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

        Push-Location $InstallDir
        & $venvPython -m pytest --collect-only -q
        if ($LASTEXITCODE -ne 0) { throw 'Test collection failed after install.' }
        Pop-Location

        if (-not $SkipOllamaPull) {
            Write-Host 'Pulling local model qwen3:1.7b...' -ForegroundColor Cyan
            & ollama pull qwen3:1.7b
            if ($LASTEXITCODE -ne 0) { throw 'Ollama model pull failed.' }
        }

        if (-not $SkipCredentials) {
            Write-Host "`nOptional credentials (press Enter to skip each one):" -ForegroundColor Cyan
            $secrets = [ordered]@{}
            $secrets['GEMINI_API_KEY'] = Read-SecretValue 'Gemini API key' 'https://aistudio.google.com/app/apikey'
            $secrets['OPENAI_API_KEY'] = Read-SecretValue 'OpenAI API key' 'https://platform.openai.com/api-keys'
            $secrets['ANTHROPIC_API_KEY'] = Read-SecretValue 'Anthropic API key' 'https://console.anthropic.com/settings/keys'
            $secrets['CROWLLM_API_KEY'] = Read-SecretValue 'CrowLLM API key' 'https://crowllm.com/'
            $secrets['TELEGRAM_BOT_TOKEN'] = Read-SecretValue 'Telegram bot token' 'https://t.me/BotFather'
            $secrets['JARVIS_NOTION_TOKEN'] = Read-SecretValue 'Notion integration token' 'https://www.notion.so/profile/integrations'
            $secrets['JARVIS_MICROSOFT_CLIENT_ID'] = Read-SecretValue 'Microsoft Entra client ID' 'https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade'
            Write-JarvisEnv -Path (Join-Path $InstallDir '.env') -Values $secrets
        }

        Write-Host "`nInstall complete." -ForegroundColor Green
        Write-Host 'Start JARVIS with:' -ForegroundColor Green
        Write-Host "  cd '$InstallDir\orb'; .\launch_orb.bat"
        Write-Host 'Then open http://127.0.0.1:8010/'
    } finally {
        Pop-Location
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    Install-Jarvis
}
