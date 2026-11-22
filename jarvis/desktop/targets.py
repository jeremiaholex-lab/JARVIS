"""Conservative launch policy for desktop open actions.

This module is pure: it never launches a process or touches the filesystem.
Every rejection is decided before a proposal or card exists, so an unsafe
target cannot reach the confirmation/execution path at all.
"""
from __future__ import annotations

import re

from jarvis.desktop.actions import redact_sensitive

# Scripts and executable formats are blocked in v1, whatever their casing,
# bare or fully qualified. Common applications are reached through their
# non-executable launchers (such as .lnk shortcuts), never through .exe.
_BLOCKED_SUFFIXES: frozenset[str] = frozenset({
    ".exe", ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe", ".js",
    ".jse", ".wsf", ".wsh", ".hta", ".msi", ".msp", ".msc", ".scr", ".com",
    ".pif", ".jar", ".py", ".pyw", ".rb", ".pl", ".php", ".sh", ".bash",
    ".zsh", ".ps", ".lua", ".sql", ".jar", ".lnk.exe",
})

# Path corners that can smuggle an executable past a suffix check.
_BLOCKED_TAIL_PATTERN = re.compile(
    r"(?:\.(?:exe|bat|cmd|ps1|vbs|js|jse|wsf|wsh|hta|scr|com|pif|msi)"
    r"(?::|$))",
    re.IGNORECASE,
)

# Network paths are not local launch targets.
_UNC = re.compile(r"^[\\/]{2}")


def _reject(reason: str) -> tuple[bool, str]:
    return False, reason


def validate_launch_target(path: str) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for a launch/open target.

    Allowed: documents, folders, common applications, and ``.lnk`` shortcuts.
    Blocked: URLs, scripts, executables, sensitive contexts, and empty input.
    Reasons are short, English-only, and never echo a secret-bearing value.
    """
    target = str(path or "").strip()
    if not target:
        return _reject("No target was provided.")

    if redact_sensitive(target) != target:
        return _reject("Sensitive targets are not available for desktop actions.")

    # A URI scheme is anything before the first ':'; a Windows drive letter
    # ('C:\...') is not a scheme and must not be rejected as one.
    scheme = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", target)
    if scheme and len(scheme.group(1)) > 1:
        return _reject("URLs and URI schemes are not supported in v1.")
    if _UNC.match(target):
        return _reject("Network paths are not supported in v1.")
    # Drive-qualified paths are not URI schemes, but the drive letter is the
    # only ':' allowed: an extra colon would be an alternate data stream.
    if re.match(r"^[a-zA-Z]:[\\/]", target):
        if target.count(":") > 1:
            return _reject("Alternate streams and device paths are not supported.")
        # Drive-qualified paths still need the script/extension check below.
    # Relative paths may still try to smuggle a stream or device name.
    elif ":" in target:
        return _reject("Alternate streams and device paths are not supported.")

    lowered = target.casefold().rstrip(" .")
    if _BLOCKED_TAIL_PATTERN.search(lowered):
        return _reject("Scripts, executables, and URLs are not supported in v1.")
    for suffix in _BLOCKED_SUFFIXES:
        if lowered.endswith(suffix):
            return _reject("Scripts and executables are not supported in v1.")

    return True, "Allowed: document, folder, common application, or shortcut."


__all__ = ["validate_launch_target"]
