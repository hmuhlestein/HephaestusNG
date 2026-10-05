"""A human's rejection must not resume the session that already said done.

get_session_id is deterministic on purpose -- "any phase retry -> same
session, agent picks up where it left off". That is right for a retry and
wrong for a rejection: by the time a human sends work back, the session's
conversation tail ends in the agent declaring success and a gate validating
it. Appending feedback to that asks the agent to contradict its own last
turn.

Observed live, workflow b84674fe on 2026-10-05: five feature_architect
agents shared one session ID. The fifth received a 2,930-character human
rejection inside a 22,957-character prompt, reported "done" four seconds
later without opening its task file (INSTRUCTIONS-CHECK warned exactly
that), and left features.json byte-identical. The spec gate then scored the
unchanged decomposition 0.75 "validated". The rejection vanished silently,
and the API had already answered {"success": true}.

The session id now folds in a redo nonce, and the launcher passes the
feedback's own updated_at -- which changes if and only if a human submitted
feedback, so the session rotates exactly when resuming is wrong.
"""

from src.autopilot.phases import get_session_id


BASE = dict(
    project_id="/Users/x/proj",
    design_slug="des-2e1ca1a1b3c0",
    phase_name="feature_architect",
    model="sonnet",
    workflow_id="b84674fe",
)


class TestResumeStillWorks:
    """The nonce must not disturb the continuity everything else relies on."""

    def test_same_inputs_give_the_same_session(self):
        assert get_session_id(**BASE) == get_session_id(**BASE)

    def test_absent_nonce_matches_an_empty_one(self):
        """Callers that never pass a nonce keep their existing sessions --
        otherwise adding this parameter would silently orphan every
        in-flight session on upgrade."""
        assert get_session_id(**BASE) == get_session_id(**BASE, redo_nonce="")

    def test_a_retry_without_feedback_resumes(self):
        """No feedback, no rotation: a plain retry still picks up where it
        left off, which is the documented and desirable behaviour."""
        first = get_session_id(**BASE)
        retry = get_session_id(**BASE, redo_nonce="")
        assert first == retry


class TestRejectionRotatesTheSession:
    def test_feedback_produces_a_fresh_session(self):
        before = get_session_id(**BASE)
        after = get_session_id(**BASE, redo_nonce="2026-10-05 14:47:02.119")
        assert before != after

    def test_a_second_rejection_rotates_again(self):
        """Two rounds of feedback must not collide -- the second redo would
        otherwise resume the first redo's 'done' turn, reproducing the bug
        one cycle later."""
        first = get_session_id(**BASE, redo_nonce="2026-10-05 14:47:02.119")
        second = get_session_id(**BASE, redo_nonce="2026-10-05 15:10:44.002")
        assert first != second

    def test_rotation_is_scoped_to_the_phase_that_was_rejected(self):
        """Rejecting the architect must not disturb an unrelated phase's
        session; the nonce travels with the task, not the workflow."""
        other = dict(BASE, phase_name="development")
        assert get_session_id(**other) == get_session_id(**other)

    def test_the_rotated_id_is_still_well_formed(self):
        """pi/claude receive this verbatim as --session-id."""
        sid = get_session_id(**BASE, redo_nonce="2026-10-05 14:47:02.119")
        assert sid.startswith("hephaestus-")
        assert " " not in sid
        assert all(c.isalnum() or c in "-_" for c in sid)
