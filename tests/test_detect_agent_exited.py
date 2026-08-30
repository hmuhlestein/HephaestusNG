"""Tests for Guardian.detect_agent_exited.

Regression: the trailing "%"/"$" heuristic matched ANY line ending that
way, including a legitimate progress or cost line from a healthy,
in-progress agent ("Building... 87 %", "Remaining: $45 $") -- not just
an actual shell prompt. guardian_dispatch.py restarts the agent on a
match (unless its task is already "done"), so this false-positived into
killing working agents. Fixed two ways: (1) a digit immediately before
the trailing "%"/"$" no longer counts (real prompts end in a path/
hostname/"~", never a bare number), and (2) when the CLI's own
health_check_pattern is still present, the agent plainly hasn't exited
regardless of what any other line looks like.
"""

from unittest.mock import Mock

from src.monitoring.guardian import Guardian


def _guardian():
    return Guardian(db_manager=Mock(), agent_manager=Mock(), llm_provider=Mock())


class TestDetectAgentExited:
    def test_detects_a_real_zsh_prompt(self):
        guardian = _guardian()
        output = "some prior output\nhmuhlestein@MacBookPro ~/project %"
        assert guardian.detect_agent_exited(output) is True

    def test_detects_a_dollar_prompt_with_a_typed_command(self):
        guardian = _guardian()
        assert guardian.detect_agent_exited("$ ls -la") is True

    def test_detects_bquote_and_repl_prompts(self):
        guardian = _guardian()
        assert guardian.detect_agent_exited("bquote> echo hi") is True
        assert guardian.detect_agent_exited(">>> print(1)") is True

    def test_does_not_flag_a_percentage_progress_line(self):
        """The exact false-positive shape: a digit right before the
        trailing '%' means this is a percentage, not a shell prompt."""
        guardian = _guardian()
        output = "Building frontend bundle...\nCompiling modules: 87 %"
        assert guardian.detect_agent_exited(output) is False

    def test_does_not_flag_a_dollar_amount_line(self):
        guardian = _guardian()
        output = "Budget check\nRemaining budget: $45 $"
        assert guardian.detect_agent_exited(output) is False

    def test_health_check_pattern_wins_over_a_matching_last_line(self):
        """Even if the last line coincidentally looks prompt-shaped, the
        CLI's own still-rendering ready UI proves the agent hasn't
        exited."""
        guardian = _guardian()
        output = "› some CLI status bar\nhostname project %"
        assert guardian.detect_agent_exited(output, health_check_pattern=r"›") is False

    def test_health_check_pattern_absent_still_flags_a_real_prompt(self):
        """Sanity check the new parameter isn't overbroad: with no health
        check pattern given (or it doesn't match), real prompt detection
        is unchanged."""
        guardian = _guardian()
        output = "hostname project %"
        assert guardian.detect_agent_exited(output, health_check_pattern=r"›") is True

    def test_empty_output_is_not_exited(self):
        guardian = _guardian()
        assert guardian.detect_agent_exited("") is False
