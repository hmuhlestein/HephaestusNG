"""Regression: commit_sha reaches `git show`/`git diff` as a bare
positional subprocess argument (list-form, so no shell injection) in
TicketService.link_commit / GET /commit-diff/{commit_sha}. Without format
validation, git itself would interpret a leading "--output=<path>" as an
option, letting an agent-supplied commit_sha write arbitrary content to a
path the server process can create -- an argument-injection primitive
reachable straight from the ticket-linking API with zero sanitization."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.mcp.server import app
from src.mcp.tickets_api import (
    ChangeTicketStatusRequest,
    LinkCommitRequest,
    ResolveTicketRequest,
)


class TestLinkCommitRequestRejectsNonHex:
    @pytest.mark.parametrize(
        "malicious_sha",
        [
            "--output=/tmp/pwned",
            "--upload-pack=evil",
            "abc def",
            "",
        ],
    )
    def test_rejects_non_hex_commit_sha(self, malicious_sha):
        with pytest.raises(ValidationError):
            LinkCommitRequest(
                ticket_id="ticket-1",
                commit_sha=malicious_sha,
                commit_message="msg",
            )

    def test_accepts_real_looking_sha(self):
        req = LinkCommitRequest(
            ticket_id="ticket-1", commit_sha="abc123def456", commit_message="msg"
        )
        assert req.commit_sha == "abc123def456"


class TestChangeStatusAndResolveRejectNonHex:
    def test_change_status_rejects_argument_injection_sha(self):
        with pytest.raises(ValidationError):
            ChangeTicketStatusRequest(
                ticket_id="ticket-1",
                new_status="done",
                comment="a valid comment here",
                commit_sha="--output=/tmp/pwned",
            )

    def test_change_status_allows_omitted_sha(self):
        req = ChangeTicketStatusRequest(
            ticket_id="ticket-1", new_status="done", comment="a valid comment here",
        )
        assert req.commit_sha is None

    def test_resolve_ticket_rejects_argument_injection_sha(self):
        with pytest.raises(ValidationError):
            ResolveTicketRequest(
                ticket_id="ticket-1",
                resolution_comment="a valid resolution comment",
                commit_sha="--output=/tmp/pwned",
            )


class TestCommitDiffEndpointRejectsNonHex:
    def test_get_commit_diff_rejects_argument_injection_sha(self):
        client = TestClient(app)
        response = client.get(
            "/api/tickets/commit-diff/--output=pwned",
            headers={"X-Agent-ID": "agent-1"},
        )
        assert response.status_code == 400
