"""The GUI's Doctor button: run the --doctor checks and recognise a first run.

Toolkit-independent. `first_run_reasons()` is cheap (file and variable checks only) so the window can call it
at start-up to decide whether the Doctor button should be highlighted; `run_report()` runs the full checks.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from modules import config_manager, mapping_manager
from modules.doctor_checks import DoctorReport, run_doctor

REQUIRED_VARIABLES = ("GMAIL_USER", "GMAIL_APP_PASSWORD")


def load_environment() -> None:
    """Read the project's .env into this process (the daemon does this on import; the GUI does not)."""
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=False))
    except Exception:
        pass  # a missing python-dotenv or .env simply shows up as missing variables below


def first_run_reasons(
    environment: Optional[Mapping[str, str]] = None,
    config_path: Optional[str] = None,
    mapping_path: Optional[str] = None,
) -> list[str]:
    """Why this looks like a first run (empty list = everything needed for normal use is there).

    environment defaults to os.environ after .env has been read.
    """
    if environment is None:
        load_environment()
        environment = os.environ
    reasons: list[str] = []
    if not os.path.exists(config_path or config_manager._config_file_path()):
        reasons.append("config.json is missing: built-in defaults are used until you save the Settings.")
    if not os.path.exists(mapping_path or mapping_manager._mapping_file_path()):
        reasons.append("mapping.json is missing: it is created when the first notification arrives or you add a repository.")
    missing = [name for name in REQUIRED_VARIABLES if not environment.get(name)]
    if missing:
        reasons.append(f"{' and '.join(missing)} not set: open Settings, choose 'Gmail & GitHub…' and enter your Gmail login.")
    return reasons


def run_report() -> DoctorReport:
    """The full --doctor report (reads the real files; takes a moment when a mapped drive is slow)."""
    load_environment()
    return run_doctor()


def summary_line(report: Mapping[str, Any]) -> str:
    """Turn a doctor report into the one-line verdict shown on the Doctor button and dialog."""
    errors, warnings = len(report["errors"]), len(report["warnings"])
    if errors:
        return f"{errors} problem(s) need fixing" + (f", {warnings} warning(s)." if warnings else ".")
    if warnings:
        return f"Everything needed is in place. {warnings} warning(s) to look at."
    return "Everything looks fine."


def needs_attention(reasons: list[str], report: Optional[Mapping[str, Any]] = None) -> bool:
    """Highlight the button for a first run, or when a doctor report found errors."""
    return bool(reasons) or bool(report and report["errors"])
