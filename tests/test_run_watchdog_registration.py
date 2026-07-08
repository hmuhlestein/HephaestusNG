"""Tests for run_watchdog.py's service registration.

Regression: only "backend" and "monitor" were ever registered with the
watchdog -- the frontend (Vite dev server) had no automatic-recovery
supervision at all. Observed live: it stopped running with no crash trace
in its own log, and nothing brought it back until a human noticed the UI
was down and ran `heph restart` manually, twice in quick succession.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import run_watchdog  # noqa: E402


def _registered_service_names(argv):
    """Call run_watchdog.main() with controlled argv and return the set of
    service names registered with the watchdog, then bail out of the
    perpetual loop immediately via a KeyboardInterrupt from the first
    time.sleep call (main() catches it)."""
    registered = []
    original_register = run_watchdog.ProcessWatchdog.register_service

    def recording_register(self, name, callback):
        registered.append(name)
        return original_register(self, name, callback)

    with patch.object(sys, "argv", ["run_watchdog.py"] + argv), patch(
        "run_watchdog._start_backend"
    ), patch("run_watchdog._start_monitor"), patch(
        "run_watchdog._start_frontend"
    ), patch("run_watchdog._find_python", return_value="/usr/bin/python3"), patch(
        "run_watchdog.save_pid"
    ), patch(
        "run_watchdog.time.sleep", side_effect=KeyboardInterrupt
    ), patch.object(
        run_watchdog.ProcessWatchdog, "register_service", recording_register
    ):
        run_watchdog.main()

    return set(registered)


class TestFrontendRegistration:
    def test_frontend_registered_by_default(self):
        registered = _registered_service_names([])
        assert registered == {"backend", "monitor", "frontend"}

    def test_no_frontend_flag_skips_registration(self):
        registered = _registered_service_names(["--no-frontend"])
        assert registered == {"backend", "monitor"}

    def test_backend_only_skips_frontend_and_monitor(self):
        registered = _registered_service_names(["--backend-only"])
        assert registered == {"backend"}
