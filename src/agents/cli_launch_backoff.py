"""Spacing and diagnosis for agent launches killed by a CLI binary swap.

Every agent Hephaestus starts is a CLI process invoked by name (claude,
pi, codex, ...). Those CLIs replace their own binary in place when they
update: for the few seconds that takes, the name doesn't resolve, and any
launch attempted in that window dies with the shell's own "command not
found" -- see _detect_launch_failure in launch_pipeline.py, which is what
turns that into a failed task.

The swap itself is brief and harmless. Two things made it fatal:

  - Retries were driven by the orchestrator's own sweep, which ticks
    every POLL_INTERVAL = 15s (phase_transitions.py). A task's whole
    retry budget therefore burned inside the same swap window -- five
    attempts across ~60s, none of them ever made outside it. The failure
    wasn't unlikely, it was certain.
  - The arbitration agent meant to rescue an exhausted phase is itself a
    CLI process (_trigger_arbitration, arbitration.py), and it was
    dispatched with no retry at all. One dispatch into the swap window
    failed the entire workflow, which is exactly the case arbitration
    exists to prevent.

This module holds the state and policy both paths consult:
note_launch_failure() records that a launch just died in a way consistent
with a binary swap and returns what was true of the binary at that moment
(for the log); cooldown_remaining() tells a would-be dispatcher how long
to leave launching alone.

Observed live: four runs ended this way across CLI versions 2.1.258 ->
.259 -> .260, and once on a re-install of the same version, one of them
4.5 hours in. Nothing in the logs recorded that the binary had changed,
so every occurrence looked like an unexplained launch failure after the
fact -- hence version_at_failure below. See IDB-2482.

DISABLE_AUTOUPDATER=1 (AGENT_LAUNCH_ENV_PREFIX, cli_interface.py) stops
the agents Hephaestus launches from triggering a swap themselves, but it
cannot stop the user's own interactive CLI sessions, a package manager,
or a manual re-install from doing it -- the same-version re-install above
was one of those. That is why the spacing here, not the env var, is the
part that has to hold.
"""

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Long enough that a fresh attempt lands outside the window that killed
# the last one. The swap replaces a ~200 MB binary, so the gap between
# attempts has to be counted in tens of seconds, not the sweep's 15 --
# and the first attempt after the cooldown is the one that has to
# succeed, so this is deliberately more than the observed window rather
# than just over it.
DEFAULT_LAUNCH_RETRY_COOLDOWN_SECONDS = 90

# Bounds `<cli> --version` at failure time. This runs on a path that is
# already failing, so it must never be the reason a failure takes longer
# to surface than it has to.
_VERSION_PROBE_TIMEOUT_SECONDS = 10

_lock = threading.Lock()

# cli_type -> monotonic timestamp of its last launch failure.
_last_failure: Dict[str, float] = {}

# cli_type -> version string seen the last time a launch under it
# actually worked. Captured lazily (once per process per CLI) so the
# failure path has something to compare against: "the binary is a
# different version than the one that was working" is the only direct
# evidence a swap happened, and it is unrecoverable after the fact.
_last_good_version: Dict[str, str] = {}


@dataclass(frozen=True)
class LaunchFailureFacts:
    """What was true of a CLI's binary at the moment a launch failed.

    Folded into the failure's log line and exception message so a swap is
    identifiable from the logs alone, rather than only from someone
    happening to watch it happen.
    """

    cli_type: str
    version_at_failure: str
    last_good_version: Optional[str]
    binary_on_path: bool

    @property
    def looks_like_binary_swap(self) -> bool:
        """Whether the evidence points at the binary having been replaced
        rather than at a CLI that is simply broken or not installed.

        Either half is enough on its own: the binary being missing right
        now is the swap caught mid-flight, and a version that differs
        from the last one known to work is the swap caught just after it
        landed. A CLI that was never installed produces neither -- it has
        no last-good version to differ from.
        """
        if not self.binary_on_path and self.last_good_version:
            return True
        return bool(
            self.last_good_version
            and self.version_at_failure != self.last_good_version
        )

    def describe(self) -> str:
        parts = [f"{self.cli_type} version at failure: {self.version_at_failure}"]
        if self.last_good_version:
            parts.append(f"last working version: {self.last_good_version}")
        parts.append(
            "binary on PATH: yes" if self.binary_on_path else "binary on PATH: NO"
        )
        if self.looks_like_binary_swap:
            parts.append(
                "consistent with the CLI replacing its own binary mid-run "
                "(transient -- retry spacing, not a real launch fault)"
            )
        return "; ".join(parts)


def cooldown_seconds() -> int:
    """Configured spacing between launch attempts after a swap-shaped
    failure. Read per call rather than captured at import so a running
    orchestrator picks up a config change without a restart -- and so the
    tests can set it without reaching into module state.
    """
    try:
        from src.core.simple_config import get_config

        configured = getattr(
            get_config().agents, "cli_launch_retry_cooldown_seconds", None
        )
        if configured is not None:
            return max(0, int(configured))
    except Exception as e:
        # Config is not worth failing a launch decision over: an
        # unreadable config here would otherwise turn a recoverable swap
        # into the unspaced retries this module exists to prevent.
        logger.debug(f"[CLI-SWAP] Falling back to the default cooldown: {e}")
    return DEFAULT_LAUNCH_RETRY_COOLDOWN_SECONDS


def capture_cli_version(cli_type: str) -> str:
    """`<cli_type> --version`, best effort, never raising.

    Returns a short human-readable string either way -- callers put this
    straight into a log line, and a probe that failed is itself the
    finding (a binary that isn't there right now is the swap in
    progress), so there is nothing here worth turning into an exception.
    """
    from src.interfaces.cli_interface import is_cli_tool_available

    if not is_cli_tool_available(cli_type):
        return "unavailable (not on PATH)"
    try:
        result = subprocess.run(
            [cli_type, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_PROBE_TIMEOUT_SECONDS,
        )
    except Exception as e:
        return f"unknown ({type(e).__name__} running --version)"
    output = (result.stdout or result.stderr or "").strip().splitlines()
    if not output:
        return f"unknown (--version exited {result.returncode} with no output)"
    return output[0].strip()[:120]


def note_launch_success(cli_type: str) -> None:
    """Record that a launch under cli_type worked: clears its cooldown and,
    the first time per process, remembers the version that worked.

    The version is what a later failure gets compared against, and it has
    to be captured while things are healthy -- after a swap there is no
    way left to find out what the previous version was.
    """
    with _lock:
        _last_failure.pop(cli_type, None)
        already_known = cli_type in _last_good_version
    if already_known:
        return
    version = capture_cli_version(cli_type)
    with _lock:
        # Only record a real reading. A probe that came back "unavailable"
        # or "unknown" is not a baseline, and storing it would make every
        # later comparison claim a version change that never happened.
        if not version.startswith(("unavailable", "unknown")):
            _last_good_version.setdefault(cli_type, version)


def note_launch_failure(cli_type: str) -> LaunchFailureFacts:
    """Record a launch failure for cli_type and gather what was true of its
    binary at that moment.

    Starts the cooldown unconditionally, whether or not the evidence
    points at a swap: the cost of spacing out the next attempt is one
    cooldown, and the cost of not spacing it is a phase burning its whole
    retry budget inside a window nothing could have launched in.
    """
    from src.interfaces.cli_interface import is_cli_tool_available

    on_path = is_cli_tool_available(cli_type)
    version = capture_cli_version(cli_type)
    with _lock:
        _last_failure[cli_type] = time.monotonic()
        last_good = _last_good_version.get(cli_type)
    facts = LaunchFailureFacts(
        cli_type=cli_type,
        version_at_failure=version,
        last_good_version=last_good,
        binary_on_path=on_path,
    )
    logger.warning(f"[CLI-SWAP] {cli_type} launch failed -- {facts.describe()}")
    return facts


def cooldown_remaining(cli_type: Optional[str] = None) -> float:
    """Seconds a caller should wait before attempting another launch; 0.0
    when there is nothing to wait for.

    cli_type=None asks about every CLI at once and returns the longest
    outstanding wait. That is deliberately conservative -- a `claude`
    swap says nothing about `pi` -- and it is the right trade at the
    dispatch sites that use it: they would have to resolve a phase's CLI
    config just to ask a narrower question, and the downside of the broad
    answer is one cooldown of delay on a retry that was already failing.
    """
    now = time.monotonic()
    window = cooldown_seconds()
    with _lock:
        if cli_type is not None:
            failed_at = _last_failure.get(cli_type)
            stamps = [failed_at] if failed_at is not None else []
        else:
            stamps = list(_last_failure.values())
    if not stamps:
        return 0.0
    remaining = max(window - (now - stamp) for stamp in stamps)
    return remaining if remaining > 0 else 0.0


def wait_out_cooldown(
    cli_type: Optional[str] = None,
    max_wait_seconds: Optional[float] = None,
    sleep_fn=time.sleep,
) -> float:
    """Block until the launch cooldown has elapsed. Returns how long it
    actually waited.

    For the one dispatcher that cannot simply come back next tick:
    arbitration gets a single dispatch and fails the whole workflow if it
    doesn't take (_trigger_arbitration, arbitration.py), so spending up to
    one cooldown here is strictly better than spending arbitration itself
    on a CLI that provably cannot launch right now. Every other caller
    should ask cooldown_remaining() and defer instead -- holding the
    orchestrator's sweep thread stalls every other workflow's turn with
    it.

    max_wait_seconds caps the block regardless of what the cooldown says,
    so a misconfigured cooldown cannot wedge the sweep; it defaults to one
    full cooldown window.
    """
    cap = cooldown_seconds() if max_wait_seconds is None else max_wait_seconds
    remaining = cooldown_remaining(cli_type)
    if remaining <= 0:
        return 0.0
    wait = min(remaining, cap)
    logger.warning(
        f"[CLI-SWAP] Holding {wait:.0f}s before launching "
        f"{cli_type or 'an agent'} -- a CLI launch failed recently and the "
        "binary may still be mid-replacement"
    )
    sleep_fn(wait)
    return wait


def reset_for_tests() -> None:
    """Drop all recorded state. Module-level state is per-process and
    intentionally not injected (every dispatch path is a different caller
    in a different thread); this is how a test gets a clean slate.
    """
    with _lock:
        _last_failure.clear()
        _last_good_version.clear()


def _seed_last_good_version_for_tests(cli_type: str, version: str) -> None:
    """Set the baseline a failure gets compared against, without running a
    real `--version` probe."""
    with _lock:
        _last_good_version[cli_type] = version


__all__ = [
    "DEFAULT_LAUNCH_RETRY_COOLDOWN_SECONDS",
    "LaunchFailureFacts",
    "capture_cli_version",
    "cooldown_remaining",
    "cooldown_seconds",
    "note_launch_failure",
    "note_launch_success",
    "reset_for_tests",
    "wait_out_cooldown",
]
