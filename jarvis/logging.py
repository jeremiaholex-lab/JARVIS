"""JARVIS logging configuration (§6.x utility).

A structured, configurable logging framework for the JARVIS codebase.
Provides console and file handlers with JSON-formatted output for
structured log analysis.

Usage:
    from jarvis.logging import get_logger, setup_logging

    # At application entry point
    setup_logging(level="INFO", log_file="jarvis.log")

    # In modules
    logger = get_logger(__name__)
    logger.info("Processing request", extra={"request_id": "abc123"})
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional


class JSONFormatter(logging.Formatter):
    """JSON-formatted log records for structured analysis."""

    def format(self, record: logging.LogRecord) -> str:
        base = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            base["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "extra_data"):
            base["extra"] = record.extra_data
        import json
        return json.dumps(base, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable console output with optional JSON mode."""

    def __init__(self, fmt: str = None, datefmt: str = None, json_mode: bool = False):
        super().__init__(fmt or "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt)
        self._json_mode = json_mode

    def format(self, record: logging.LogRecord) -> str:
        if self._json_mode:
            return JSONFormatter().format(record)
        return super().format(record)


def get_logger(name: str) -> logging.Logger:
    """Get a configured logger by module name.

    Args:
        name: Typically __name__ of the calling module

    Returns:
        Configured Logger instance
    """
    return logging.getLogger(name)


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str | Path] = None,
    json_mode: bool = False,
    log_to_stderr: bool = True,
) -> logging.Logger:
    """Configure JARVIS logging system.

    Args:
        level: Log level - "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
        log_file: Optional path to write logs (appends if exists)
        json_mode: Use JSON format for structured log output
        log_to_stderr: Also log to stderr in addition to file

    Returns:
        Root logger for JARVIS
    """
    jarvis_logger = logging.getLogger("jarvis")
    jarvis_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    jarvis_logger.propagate = False  # Prevent duplicate logs

    # Clear any existing handlers
    jarvis_logger.handlers.clear()

    # Console handler (stderr by default)
    if log_to_stderr:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(getattr(logging, level.upper(), logging.INFO))
        console.setFormatter(ConsoleFormatter(json_mode=json_mode))
        jarvis_logger.addHandler(console)

    # File handler (optional)
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)  # File gets all details
        file_handler.setFormatter(JSONFormatter())
        jarvis_logger.addHandler(file_handler)

    return jarvis_logger


def setup_from_env() -> logging.Logger:
    """Configure logging from environment variables, for CLI entry points.

    Reads:
        JARVIS_LOG_LEVEL  — log level name (default "INFO")
        JARVIS_LOG_FILE   — path to an append-style JSON log file
                            (default: no file; stderr only)
        JARVIS_LOG_JSON   — "1" to also emit JSON on the console

    Call once near the top of each CLI `main()` before any JARVIS code runs,
    e.g.::

        from jarvis.logging import setup_from_env
        setup_from_env()
    """
    level = os.environ.get("JARVIS_LOG_LEVEL", "INFO")
    log_file = os.environ.get("JARVIS_LOG_FILE") or None
    json_mode = os.environ.get("JARVIS_LOG_JSON") in ("1", "true", "yes")
    return setup_logging(level=level, log_file=log_file, json_mode=json_mode)


def log_exception(logger: logging.Logger, msg: str, exc: Exception, **kwargs: Any) -> None:
    """Log an exception with full traceback.

    Args:
        logger: Logger instance to use
        msg: Log message
        exc: Exception object
        **kwargs: Extra data to include
    """
    logger.error(
        f"{msg}: {exc.__class__.__name__}: {exc}",
        extra={"extra_data": kwargs} if kwargs else None,
        exc_info=True,
    )


# Convenience loggers for major subsystems
def get_security_logger() -> logging.Logger:
    """Logger for security-related events (gate, auditor, audit trail)."""
    return get_logger("jarvis.security")


def get_rag_logger() -> logging.Logger:
    """Logger for RAG operations (indexing, search)."""
    return get_logger("jarvis.rag")


def get_skills_logger() -> logging.Logger:
    """Logger for skill pipeline events."""
    return get_logger("jarvis.skills")


def get_sentry_logger() -> logging.Logger:
    """Logger for sentry (auto-heal) events."""
    return get_logger("jarvis.sentry")


def get_orb_logger() -> logging.Logger:
    """Logger for ORB/Orb events (orb/server.py)."""
    return get_logger("jarvis.orb")


__all__ = [
    "get_logger",
    "setup_logging",
    "setup_from_env",
    "log_exception",
    "get_security_logger",
    "get_rag_logger",
    "get_skills_logger",
    "get_sentry_logger",
    "get_orb_logger",
    "JSONFormatter",
    "ConsoleFormatter",
]