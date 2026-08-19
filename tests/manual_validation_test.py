"""Manual validation test for ticket_id requirement.

Not a pytest test despite the *_test.py name (which matches pytest's
default collection pattern) -- this fires real HTTP requests at a live
server on localhost:8300 and is meant to be run directly:

    python tests/manual_validation_test.py

Everything below used to run at module level, which meant pytest
collecting this file (a bare `pytest tests/` run) executed these
requests.post(...) calls with no server listening -- hanging or failing
during collection, before any real test ran. Guarded behind
`if __name__ == "__main__"` so import/collection is a no-op.
"""

import requests


def main() -> None:
    print("=" * 70)
    print("COMPREHENSIVE ticket_id VALIDATION TEST SUITE")
    print("=" * 70)

    # Test 1: SDK agent WITHOUT ticket_id - should SUCCEED
    print("\n### Test 1: SDK Agent Without ticket_id (SHOULD SUCCEED) ###")
    response = requests.post(
        "http://localhost:8300/create_task",
        json={
            "task_description": "Test 1 - SDK without ticket_id",
            "done_definition": "Task completed",
            "ai_agent_id": "main-session-agent",
            "phase_id": "1",
            "priority": "medium",
        },
        headers={"Content-Type": "application/json", "X-Agent-ID": "main-session-agent"},
    )
    print(f"Status: {response.status_code}")
    print("Result: PASSED" if response.status_code == 200 else "FAILED")
    if response.status_code != 200:
        print(f"Error: {response.text}")

    # Test 2: MCP agent WITHOUT ticket_id - should FAIL
    print("\n### Test 2: MCP Agent Without ticket_id (SHOULD FAIL with 400) ###")
    response = requests.post(
        "http://localhost:8300/create_task",
        json={
            "task_description": "Test 2 - MCP without ticket_id",
            "done_definition": "Task completed",
            "ai_agent_id": "test-mcp-agent",
            "phase_id": "1",
            "priority": "medium",
        },
        headers={"Content-Type": "application/json", "X-Agent-ID": "ui-user"},
    )
    print(f"Status: {response.status_code}")
    print(
        "Result: PASSED"
        if response.status_code == 400 and "ticket_id" in response.text.lower()
        else "FAILED"
    )
    if response.status_code != 200:
        print(f"Error message: {response.text[:200]}")

    # Test 3: MCP agent WITH ticket_id - should SUCCEED
    print("\n### Test 3: MCP Agent With ticket_id (SHOULD SUCCEED) ###")

    # First create a ticket
    ticket_response = requests.post(
        "http://localhost:8300/api/tickets/create",
        json={
            "workflow_id": "98f8d41f-026f-4401-8246-7f431c908a8c",
            "title": "Test Ticket for Validation",
            "description": "Testing MCP agent with ticket_id",
            "ticket_type": "bug",
            "priority": "medium",
        },
        headers={"X-Agent-ID": "ui-user"},
    )
    if ticket_response.status_code == 200:
        ticket_id = ticket_response.json()["ticket_id"]
        print(f"Created ticket: {ticket_id}")

        # Now create task with ticket_id
        response = requests.post(
            "http://localhost:8300/create_task",
            json={
                "task_description": "Test 3 - MCP with ticket_id",
                "done_definition": "Task completed",
                "ai_agent_id": "test-mcp-agent",
                "phase_id": "1",
                "priority": "medium",
                "ticket_id": ticket_id,
            },
            headers={"Content-Type": "application/json", "X-Agent-ID": "test-mcp-agent"},
        )
        print(f"Status: {response.status_code}")
        print("Result: PASSED" if response.status_code == 200 else "FAILED")
        if response.status_code != 200:
            print(f"Error: {response.text}")
    else:
        print(f"FAILED to create ticket: {ticket_response.text}")

    print("\n" + "=" * 70)
    print("TEST SUITE COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
