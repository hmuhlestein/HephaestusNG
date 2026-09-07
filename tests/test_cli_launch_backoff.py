"""Tests for src/agents/cli_launch_backoff.py -- the spacing and diagnosis
that stop a CLI binary swap from consuming an agent's whole retry budget.

The failure this covers: every agent Hephaestus starts is a CLI process
invoked by name, those CLIs replace their own binary in place when they
update, and for the seconds that takes the name doesn't resolve. The
orchestrator's sweep ticks every 15s, so all of a task's retries landed
inside one swap window and the phase reported itself exhausted for a cause
that had already resolved -- with nothing in the log recording that the
binary had changed at all. Four runs were lost this way, one 4.5 hours in.
See IDB-2482.
"""

import pytest

from src.agents import cli_launch_backoff as backoff

# A name no PATH lookup can ever resolve, so is_cli_tool_available() is
# False and capture_cli_version() short-circuits without a subprocess.
MISSING_CLI = "definitely-not-an-installed-cli-idb2482"


@pytest.fixture(autouse=True)
def _clean_state():
    backoff.reset_for_tests()
    yield
    backoff.reset_for_tests()


class TestCooldownSeconds:
    def test_reads_the_configured_value(self, monkeypatch):
        class _Agents:
            cli_launch_retry_cooldown_seconds = 45

        class _Config:
            agents = _Agents()

        monkeypatch.setattr(
            "src.core.simple_config.get_config", lambda: _Config()
        )
        assert backoff.cooldown_seconds() == 45

    def test_falls_back_to_the_default_when_config_is_unreadable(self, monkeypatch):
        """A launch decision must not depend on config being loadable. If it
        did, an unreadable config would restore the unspaced retries this
        module exists to prevent -- the exact opposite of failing safe."""

        def _boom():
            raise RuntimeError("config not initialized")

        monkeypatch.setattr("src.core.simple_config.get_config", _boom)
        assert (
            backoff.cooldown_seconds()
            == backoff.DEFAULT_LAUNCH_RETRY_COOLDOWN_SECONDS
        )

    def test_never_returns_a_negative_window(self, monkeypatch):
        class _Agents:
            cli_launch_retry_cooldown_seconds = -30

        class _Config:
            agents = _Agents()

        monkeypatch.setattr(
            "src.core.simple_config.get_config", lambda: _Config()
        )
        assert backoff.cooldown_seconds() == 0


class TestCooldownLifecycle:
    def test_no_cooldown_before_anything_has_failed(self):
        assert backoff.cooldown_remaining() == 0.0
        assert backoff.cooldown_remaining("claude") == 0.0

    def test_a_launch_failure_starts_the_cooldown(self, monkeypatch):
        monkeypatch.setattr(backoff, "cooldown_seconds", lambda: 90)
        backoff.note_launch_failure(MISSING_CLI)
        remaining = backoff.cooldown_remaining(MISSING_CLI)
        assert 0 < remaining <= 90

    def test_a_successful_launch_clears_the_cooldown(self, monkeypatch):
        monkeypatch.setattr(backoff, "cooldown_seconds", lambda: 90)
        backoff.note_launch_failure(MISSING_CLI)
        assert backoff.cooldown_remaining(MISSING_CLI) > 0
        backoff.note_launch_success(MISSING_CLI)
        assert backoff.cooldown_remaining(MISSING_CLI) == 0.0

    def test_the_cooldown_expires_once_its_window_has_passed(self, monkeypatch):
        """A zero-length window is the same code path as a window that has
        simply elapsed -- both must read as "nothing to wait for" rather
        than leaving a task held forever."""
        monkeypatch.setattr(backoff, "cooldown_seconds", lambda: 0)
        backoff.note_launch_failure(MISSING_CLI)
        assert backoff.cooldown_remaining(MISSING_CLI) == 0.0

    def test_one_clis_failure_does_not_start_another_clis_cooldown(self, monkeypatch):
        monkeypatch.setattr(backoff, "cooldown_seconds", lambda: 90)
        backoff.note_launch_failure(MISSING_CLI)
        assert backoff.cooldown_remaining("some-other-cli") == 0.0

    def test_asking_about_every_cli_returns_the_longest_outstanding_wait(
        self, monkeypatch
    ):
        """cooldown_remaining(None) is what the dispatch sites use, since
        asking a narrower question would mean resolving a phase's CLI
        config first. It has to answer for whichever CLI is still held."""
        monkeypatch.setattr(backoff, "cooldown_seconds", lambda: 90)
        backoff.note_launch_failure(MISSING_CLI)
        assert backoff.cooldown_remaining() > 0


class TestLaunchFailureFacts:
    def test_a_missing_binary_against_a_known_good_version_reads_as_a_swap(self):
        facts = backoff.LaunchFailureFacts(
            cli_type="claude",
            version_at_failure="unavailable (not on PATH)",
            last_good_version="2.1.259 (Claude Code)",
            binary_on_path=False,
        )
        assert facts.looks_like_binary_swap

    def test_a_changed_version_reads_as_a_swap(self):
        facts = backoff.LaunchFailureFacts(
            cli_type="claude",
            version_at_failure="2.1.260 (Claude Code)",
            last_good_version="2.1.259 (Claude Code)",
            binary_on_path=True,
        )
        assert facts.looks_like_binary_swap

    def test_a_cli_that_was_never_installed_does_not_read_as_a_swap(self):
        """The distinction that makes the label worth anything: with no
        version ever known to work there is nothing to have been swapped,
        and calling it a transient swap would send someone chasing an
        update that never happened instead of installing the CLI."""
        facts = backoff.LaunchFailureFacts(
            cli_type="droid",
            version_at_failure="unavailable (not on PATH)",
            last_good_version=None,
            binary_on_path=False,
        )
        assert not facts.looks_like_binary_swap

    def test_an_unchanged_version_does_not_read_as_a_swap(self):
        facts = backoff.LaunchFailureFacts(
            cli_type="claude",
            version_at_failure="2.1.259 (Claude Code)",
            last_good_version="2.1.259 (Claude Code)",
            binary_on_path=True,
        )
        assert not facts.looks_like_binary_swap

    def test_describe_carries_the_version_and_the_path_state(self):
        facts = backoff.LaunchFailureFacts(
            cli_type="claude",
            version_at_failure="2.1.260 (Claude Code)",
            last_good_version="2.1.259 (Claude Code)",
            binary_on_path=True,
        )
        described = facts.describe()
        assert "2.1.260" in described
        assert "2.1.259" in described
        assert "binary on PATH: yes" in described
        assert "mid-run" in described


class TestVersionCapture:
    def test_an_absent_binary_is_reported_as_absent_not_as_an_error(self):
        assert backoff.capture_cli_version(MISSING_CLI) == "unavailable (not on PATH)"

    def test_a_real_probe_returns_the_first_output_line(self, monkeypatch):
        monkeypatch.setattr(
            "src.interfaces.cli_interface.is_cli_tool_available", lambda _c: True
        )

        class _Completed:
            returncode = 0
            stdout = "2.1.260 (Claude Code)\nextra noise\n"
            stderr = ""

        monkeypatch.setattr(
            backoff.subprocess, "run", lambda *a, **k: _Completed()
        )
        assert backoff.capture_cli_version("claude") == "2.1.260 (Claude Code)"

    def test_a_probe_that_raises_never_propagates(self, monkeypatch):
        """This runs on a path that is already failing. A version probe
        that raised would replace a diagnosable launch failure with an
        unrelated traceback."""
        monkeypatch.setattr(
            "src.interfaces.cli_interface.is_cli_tool_available", lambda _c: True
        )

        def _boom(*_a, **_k):
            raise TimeoutError("probe hung")

        monkeypatch.setattr(backoff.subprocess, "run", _boom)
        assert "TimeoutError" in backoff.capture_cli_version("claude")


class TestLastGoodVersionBaseline:
    def test_a_successful_launch_records_the_working_version(self, monkeypatch):
        monkeypatch.setattr(
            backoff, "capture_cli_version", lambda _c: "2.1.259 (Claude Code)"
        )
        backoff.note_launch_success("claude")
        monkeypatch.setattr(
            backoff, "capture_cli_version", lambda _c: "2.1.260 (Claude Code)"
        )
        facts = backoff.note_launch_failure("claude")
        assert facts.last_good_version == "2.1.259 (Claude Code)"
        assert facts.version_at_failure == "2.1.260 (Claude Code)"
        assert facts.looks_like_binary_swap

    def test_an_unreadable_probe_is_not_stored_as_a_baseline(self, monkeypatch):
        """Storing "unavailable"/"unknown" as the last good version would
        make every later failure claim a version change that never
        happened -- worse than having no baseline at all."""
        monkeypatch.setattr(
            backoff, "capture_cli_version", lambda _c: "unavailable (not on PATH)"
        )
        backoff.note_launch_success("claude")
        monkeypatch.setattr(
            backoff, "capture_cli_version", lambda _c: "2.1.260 (Claude Code)"
        )
        facts = backoff.note_launch_failure("claude")
        assert facts.last_good_version is None
        assert not facts.looks_like_binary_swap

    def test_the_baseline_is_captured_once_and_not_re_probed(self, monkeypatch):
        """One subprocess per CLI per process. note_launch_success runs on
        every single agent launch, so re-probing there would add a
        subprocess to the hot path for a value that does not change."""
        probes = []

        def _probe(cli_type):
            probes.append(cli_type)
            return "2.1.259 (Claude Code)"

        monkeypatch.setattr(backoff, "capture_cli_version", _probe)
        backoff.note_launch_success("claude")
        backoff.note_launch_success("claude")
        backoff.note_launch_success("claude")
        assert probes == ["claude"]


class TestWaitOutCooldown:
    def test_returns_immediately_when_nothing_is_outstanding(self):
        slept = []
        assert (
            backoff.wait_out_cooldown(sleep_fn=slept.append) == 0.0
        )
        assert slept == []

    def test_waits_for_the_outstanding_cooldown(self, monkeypatch):
        monkeypatch.setattr(backoff, "cooldown_seconds", lambda: 90)
        backoff.note_launch_failure(MISSING_CLI)
        slept = []
        waited = backoff.wait_out_cooldown(sleep_fn=slept.append)
        assert waited > 0
        assert slept and slept[0] == waited

    def test_never_blocks_longer_than_its_cap(self, monkeypatch):
        """A misconfigured cooldown must not be able to wedge the
        orchestrator's sweep thread -- this is the one caller that blocks
        it at all."""
        monkeypatch.setattr(backoff, "cooldown_seconds", lambda: 6000)
        backoff.note_launch_failure(MISSING_CLI)
        slept = []
        waited = backoff.wait_out_cooldown(max_wait_seconds=5, sleep_fn=slept.append)
        assert waited == 5
        assert slept == [5]
