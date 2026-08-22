"""PipelineState.mark_working/mark_idle/live_active_elapsed -- the Runtime
displayed on PipelineStatusCard must reflect actual working time (pausing
while idle between designs), not wall-clock since the pipeline started, and
must persist across backend restarts (state.json round-trip)."""

from unittest.mock import patch

from src.autopilot.orchestrator.state import PipelineState


class TestMarkWorkingIdle:
    def test_starts_idle_with_zero_elapsed(self):
        state = PipelineState()
        assert state.active_since is None
        assert state.live_active_elapsed() == 0.0

    def test_mark_working_opens_an_active_stretch(self):
        state = PipelineState()
        with patch("time.time", return_value=1000.0):
            state.mark_working()
        assert state.active_since == 1000.0
        assert state.active_elapsed == 0.0

    def test_mark_working_is_idempotent(self):
        """Calling mark_working while already active must not reset the
        stretch's start time -- that would silently discard already-elapsed
        active time on every poll that happens to call it again."""
        state = PipelineState()
        with patch("time.time", return_value=1000.0):
            state.mark_working()
        with patch("time.time", return_value=1050.0):
            state.mark_working()
        assert state.active_since == 1000.0

    def test_mark_idle_folds_the_stretch_into_active_elapsed(self):
        state = PipelineState()
        with patch("time.time", return_value=1000.0):
            state.mark_working()
        with patch("time.time", return_value=1090.0):
            state.mark_idle()
        assert state.active_since is None
        assert state.active_elapsed == 90.0

    def test_mark_idle_is_idempotent(self):
        """Calling mark_idle while already idle must not double-subtract or
        otherwise corrupt active_elapsed."""
        state = PipelineState()
        with patch("time.time", return_value=1000.0):
            state.mark_working()
        with patch("time.time", return_value=1090.0):
            state.mark_idle()
        with patch("time.time", return_value=1200.0):
            state.mark_idle()
        assert state.active_elapsed == 90.0

    def test_idle_stretches_are_not_counted(self):
        """The core behavior this exists for: time spent idle between two
        active stretches must not appear in the total."""
        state = PipelineState()
        with patch("time.time", return_value=0.0):
            state.mark_working()
        with patch("time.time", return_value=60.0):
            state.mark_idle()
        # A long idle gap -- must not accumulate.
        with patch("time.time", return_value=6000.0):
            state.mark_working()
        with patch("time.time", return_value=6030.0):
            state.mark_idle()
        assert state.active_elapsed == 90.0

    def test_live_active_elapsed_ticks_while_active(self):
        state = PipelineState()
        with patch("time.time", return_value=1000.0):
            state.mark_working()
        with patch("time.time", return_value=1025.0):
            assert state.live_active_elapsed() == 25.0

    def test_live_active_elapsed_freezes_while_idle(self):
        state = PipelineState()
        with patch("time.time", return_value=1000.0):
            state.mark_working()
        with patch("time.time", return_value=1090.0):
            state.mark_idle()
        with patch("time.time", return_value=5000.0):
            assert state.live_active_elapsed() == 90.0


class TestSerializationRoundTrip:
    def test_to_dict_includes_active_fields(self):
        state = PipelineState()
        with patch("time.time", return_value=1000.0):
            state.mark_working()
        state.active_elapsed = 42.0
        d = state.to_dict()
        assert d["active_elapsed"] == 42.0
        assert d["active_since"] == 1000.0

    def test_from_dict_restores_an_open_active_stretch(self):
        """A backend restart must not lose an in-progress active stretch --
        the status endpoint recomputes the live value from the persisted
        active_since, so it has to survive the round trip."""
        data = {"active_elapsed": 42.0, "active_since": 1000.0}
        state = PipelineState.from_dict(data)
        assert state.active_elapsed == 42.0
        assert state.active_since == 1000.0
        with patch("time.time", return_value=1030.0):
            assert state.live_active_elapsed() == 72.0

    def test_from_dict_defaults_missing_fields_to_idle(self):
        """state.json written before this field existed must not crash or
        misreport -- defaults to idle with zero accumulated time."""
        state = PipelineState.from_dict({})
        assert state.active_elapsed == 0.0
        assert state.active_since is None
        assert state.live_active_elapsed() == 0.0
