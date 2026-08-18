"""Tests for Phase 2 §4.10's MCP tool single source of truth
(src/mcp/server.py's MCP_TOOL_REGISTRY/_MCP_TOOLS/MCP_TOOL_NAMES).

Covers the structural consistency the registry now guarantees by
construction, the three historically-live "Unknown tool" bugs it fixes
(submit_result, submit_result_validation, give_validation_review), and the
prompt/YAML/validator-prompt drift check that would have caught all three
the moment they were introduced.
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp import server as mcp_server


class TestMCPToolRegistryConsistency:
    def test_core_registry_names_match_dispatch_dict(self):
        registry_names = {t.name for t in mcp_server.MCP_TOOL_REGISTRY}
        assert registry_names == set(mcp_server._MCP_TOOLS.keys())

    def test_dispatch_dict_handlers_match_registry_handlers(self):
        for t in mcp_server.MCP_TOOL_REGISTRY:
            assert mcp_server._MCP_TOOLS[t.name] is t.handler

    @pytest.mark.asyncio
    async def test_list_tools_core_section_matches_registry(self):
        result = await mcp_server.list_tools()
        core_names = [t["name"] for t in result["tools"] if not t["name"].startswith("devtools_")]
        assert core_names == [t.name for t in mcp_server.MCP_TOOL_REGISTRY]

    @pytest.mark.asyncio
    async def test_list_tools_devtools_required_matches_dispatch_registry(self):
        """Regression: every devtools tool's hand-written /tools schema
        used to claim session_id was required, but _handle_devtools_tool
        (the actual dispatch-time check) never enforced it -- session_id
        silently defaults to "default" if omitted. The schema was wrong
        for all 15 devtools tools, not a typo on one. list_tools() now
        derives "required" from _DEVTOOLS_TOOLS (the real source of truth
        for what's enforced) instead of a second hand-typed list."""
        result = await mcp_server.list_tools()
        devtools_entries = {t["name"]: t for t in result["tools"] if t["name"].startswith("devtools_")}
        assert set(devtools_entries) == set(mcp_server._DEVTOOLS_TOOLS.keys())
        for name, entry in devtools_entries.items():
            required_args, _handler = mcp_server._DEVTOOLS_TOOLS[name]
            assert entry["input_schema"]["required"] == required_args

    def test_devtools_connect_no_longer_falsely_requires_session_id(self):
        """The concrete instance of the above: devtools_connect's real
        required_args is [] (session_id defaults), but the old hand-typed
        schema said ["session_id"]."""
        required_args, _handler = mcp_server._DEVTOOLS_TOOLS["devtools_connect"]
        assert required_args == []

    def test_mcp_tool_names_covers_core_and_devtools(self):
        assert mcp_server.MCP_TOOL_NAMES == (
            {t.name for t in mcp_server.MCP_TOOL_REGISTRY}
            | set(mcp_server._DEVTOOLS_TOOLS.keys())
        )


class TestDispatchAcceptsBothCallShapesForEveryTool:
    """Different agentic CLIs' MCP adapters disagree on whether they
    prepend the server name (heph_) before presenting a tool to the agent
    -- some do, some don't, so a real agent may call either
    heph_<name>(...) or the bare <name>(...) for the exact same tool,
    depending on which CLI it's running in. 137f12b's defensive strip
    (`if tool_name.startswith("heph_"): tool_name = tool_name[5:]`) is
    what makes both work; this test asserts that guarantee holds for
    EVERY registered tool, not just the three newly-registered ones
    TestSubmitResultToolWrapper/etc. spot-check individually.
    """

    @pytest.mark.asyncio
    async def test_every_core_tool_dispatches_both_bare_and_prefixed(self):
        for spec in mcp_server.MCP_TOOL_REGISTRY:
            with patch.object(mcp_server, "_MCP_TOOLS", {spec.name: AsyncMock(return_value="ok")}):
                bare = await mcp_server.execute_tool({"tool": spec.name, "arguments": {}})
                prefixed = await mcp_server.execute_tool({"tool": f"heph_{spec.name}", "arguments": {}})
            assert bare == "ok", f"{spec.name}: bare call did not dispatch"
            assert prefixed == "ok", f"{spec.name}: heph_-prefixed call did not dispatch"

    @pytest.mark.asyncio
    async def test_every_devtools_tool_dispatches_both_bare_and_prefixed(self):
        for name in mcp_server._DEVTOOLS_TOOLS:
            with patch.object(mcp_server, "_handle_devtools_tool", new=AsyncMock(return_value="ok")) as mock_dt:
                bare = await mcp_server.execute_tool({"tool": name, "arguments": {}})
                prefixed = await mcp_server.execute_tool({"tool": f"heph_{name}", "arguments": {}})
            assert bare == "ok", f"{name}: bare call did not dispatch"
            assert prefixed == "ok", f"{name}: heph_-prefixed call did not dispatch"
            # Both calls must resolve to the SAME underlying tool name --
            # the strip, not a coincidence of two different lookups.
            assert mock_dt.call_args_list[0].args[0] == name
            assert mock_dt.call_args_list[1].args[0] == name


class TestPromptToolNameDriftCheck:
    """The plan's own bug-fix-history pass found six commits over five
    weeks, each fixing exactly one of three surfaces (the /tools listing,
    the dispatch dict, and agent-facing prompt text) after it drifted from
    the others. This test is that missing fourth surface: it scans every
    location a heph_<tool>(...) call is actually written for agents to
    follow, and asserts each referenced name is registered -- so the next
    drift is caught here, not by an agent hitting "Unknown tool" live.

    Scoped to config/prompts/**/*.yaml, config/workflows/**/*.yaml, and
    src/validation/ (confirmed, by a full-repo heph_ grep at the time this
    test was written, to be the only places a real tool-name reference
    appears -- src/mcp/devtools.py and worktree_integration.py also match
    a bare `heph_` regex but for an unrelated JS global and a local
    variable name, not a tool call, which is why this is scoped rather
    than scanning all of src/).

    Covers both call shapes the plan names ("by bare name, with or without
    a heph_ prefix"), but asymmetrically -- see the two regexes below. Both
    shapes are genuinely live, not one "real" and one "informal": different
    agentic CLIs' MCP adapters disagree on whether they prepend the server
    name before presenting a tool to the agent, so which form a prompt
    documents (and which form the agent actually sends) depends on which
    CLI is running it -- see TestDispatchAcceptsBothCallShapesForEveryTool,
    which is why 137f12b's dispatch-time strip exists and handles both
    directions already. The asymmetry here is about what this SCAN can
    reliably detect, not about which form matters more: heph_<name> is an
    unambiguous signal to grep for (the established convention), so ANY
    such reference is checked. A bare name has no equivalent signal:
    `\\bname\\(` alone matches constantly in these files' embedded code
    examples (confirmed empirically -- add(, error(, process(, run(,
    str(, etc. all matched a first attempt at this, none of them tool
    calls). So bare-name coverage is intentionally narrower: only the
    fixed set of core registry names is checked for a bare-call-shaped
    mention, which still catches the real risk (a tool renamed in the
    registry without updating an existing bare reference to its old name)
    without the false-positive flood a fully generic scan produced.
    """

    HEPH_TOOL_REF = re.compile(r"\bheph_([a-z][a-z0-9_]*)\b")
    # The core (non-devtools) registry names as of this test's writing --
    # devtools_* excluded, their own "devtools_" prefix is already as
    # unambiguous a signal as heph_ is, no bare-name ambiguity to guard
    # against for those.
    _CORE_NAMES_AT_WRITE_TIME = (
        "create_task", "save_memory", "search_memory", "get_task_status",
        "update_task_status", "complete_my_task", "create_ticket",
        "search_tickets", "update_ticket_status", "broadcast_message",
        "send_message", "submit_result", "submit_result_validation",
        "give_validation_review",
    )
    BARE_TOOL_REF = re.compile(
        r"(?<!heph_)\b(" + "|".join(_CORE_NAMES_AT_WRITE_TIME) + r")\s*\("
    )

    def _find_references(self):
        refs = []
        repo_root = Path(__file__).resolve().parent.parent
        locations = [
            (repo_root / "config" / "prompts", "*.yaml"),
            (repo_root / "config" / "workflows", "*.yaml"),
            (repo_root / "src" / "validation", "*.py"),
        ]
        for base, pattern in locations:
            for path in base.rglob(pattern):
                text = path.read_text()
                for m in self.HEPH_TOOL_REF.finditer(text):
                    refs.append((str(path.relative_to(repo_root)), m.group(1)))
                for m in self.BARE_TOOL_REF.finditer(text):
                    refs.append((str(path.relative_to(repo_root)), m.group(1)))
        return refs

    def test_every_heph_prefixed_reference_is_a_registered_tool(self):
        refs = self._find_references()
        assert refs, "scan found no heph_/bare-name references at all -- likely a path/pattern regression in this test itself"
        unknown = sorted({name for _path, name in refs if name not in mcp_server.MCP_TOOL_NAMES})
        assert unknown == [], (
            f"prompt/validator text references tool name(s) not in MCP_TOOL_NAMES: {unknown} "
            "-- either register them (Phase 2 §4.10 pattern: a thin _tool_* wrapper "
            "delegating to the real REST route) or fix the prompt text."
        )

    def test_bare_name_references_are_still_registered(self):
        """The bounded half of bare-name coverage described in this
        class's docstring: confirms the SIX bare (non-heph_-prefixed)
        tool-name-shaped references this codebase currently has (found by
        a full config/prompts+config/workflows scan at the time this test
        was written) still resolve. Doesn't catch a brand-new invalid bare
        name -- see the docstring for why that's not achievable without a
        much noisier scan -- but does catch the more likely drift: one of
        these SPECIFIC names getting renamed in the registry without this
        reference being updated."""
        repo_root = Path(__file__).resolve().parent.parent
        bare_only = set()
        for base, pattern in (
            (repo_root / "config" / "prompts", "*.yaml"),
            (repo_root / "config" / "workflows", "*.yaml"),
        ):
            for path in base.rglob(pattern):
                for m in self.BARE_TOOL_REF.finditer(path.read_text()):
                    bare_only.add(m.group(1))
        assert bare_only, "expected at least one bare-name reference in config/prompts or config/workflows"
        assert bare_only <= mcp_server.MCP_TOOL_NAMES

    def test_scan_finds_the_three_historically_live_bugs(self):
        """Confirms this check actually exercises the real bug class it
        exists to catch, not just an empty scan that trivially passes."""
        refs = self._find_references()
        names = {name for _path, name in refs}
        assert {"submit_result", "submit_result_validation", "give_validation_review"} <= names


class TestSubmitResultToolWrapper:
    @pytest.mark.asyncio
    async def test_dispatches_to_submit_result_with_agent_id_as_argument(self):
        mock_response = object()
        with patch("src.mcp.server.submit_result", new=AsyncMock(return_value=mock_response)) as mock_submit:
            result = await mcp_server._tool_submit_result({
                "agent_id": "agent-1",
                "markdown_file_path": "/tmp/result.md",
                "explanation": "Did the thing",
                "evidence": ["log line"],
            })

        assert result is mock_response
        _, kwargs = mock_submit.call_args
        assert kwargs["agent_id"] == "agent-1"
        request = mock_submit.call_args.args[0]
        assert request.markdown_file_path == "/tmp/result.md"
        assert request.explanation == "Did the thing"
        assert request.evidence == ["log line"]

    @pytest.mark.asyncio
    async def test_rejects_missing_required_arguments(self):
        with pytest.raises(Exception) as exc_info:
            await mcp_server._tool_submit_result({"agent_id": "agent-1"})
        assert "required" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_reachable_through_dispatch_with_heph_prefix(self):
        """The literal regression this item exists to fix: the exact call
        shape system_prompts.yaml documents (heph_submit_result(...)) must
        resolve through /tools/execute's dispatch, not 400 with "Unknown
        tool: submit_result" the way it used to."""
        mock_response = object()
        with patch("src.mcp.server.submit_result", new=AsyncMock(return_value=mock_response)):
            result = await mcp_server.execute_tool({
                "tool": "heph_submit_result",
                "arguments": {
                    "agent_id": "agent-1",
                    "markdown_file_path": "/tmp/result.md",
                    "explanation": "Did the thing",
                },
            })
        assert result is mock_response


class TestSubmitResultValidationToolWrapper:
    @pytest.mark.asyncio
    async def test_dispatches_without_requiring_agent_id(self):
        mock_response = object()
        with patch("src.mcp.server.submit_result_validation", new=AsyncMock(return_value=mock_response)) as mock_fn:
            result = await mcp_server._tool_submit_result_validation({
                "result_id": "result-1",
                "validation_passed": True,
                "feedback": "Looks good",
            })

        assert result is mock_response
        request = mock_fn.call_args.args[0]
        assert request.result_id == "result-1"
        assert request.validation_passed is True
        assert request.feedback == "Looks good"

    @pytest.mark.asyncio
    async def test_reachable_through_dispatch_with_heph_prefix(self):
        mock_response = object()
        with patch("src.mcp.server.submit_result_validation", new=AsyncMock(return_value=mock_response)):
            result = await mcp_server.execute_tool({
                "tool": "heph_submit_result_validation",
                "arguments": {
                    "result_id": "result-1",
                    "validation_passed": False,
                    "feedback": "Needs work",
                },
            })
        assert result is mock_response


class TestGiveValidationReviewToolWrapper:
    @pytest.mark.asyncio
    async def test_dispatches_with_agent_id_as_argument_and_defaults_validator_agent_id(self):
        mock_response = object()
        with patch("src.mcp.server.give_validation_review", new=AsyncMock(return_value=mock_response)) as mock_fn:
            result = await mcp_server._tool_give_validation_review({
                "agent_id": "validator-1",
                "task_id": "task-1",
                "validation_passed": True,
                "feedback": "Confirmed working",
            })

        assert result is mock_response
        _, kwargs = mock_fn.call_args
        assert kwargs["agent_id"] == "validator-1"
        request = mock_fn.call_args.args[0]
        assert request.task_id == "task-1"
        # validator_agent_id defaults to agent_id when the caller omits it.
        assert request.validator_agent_id == "validator-1"

    @pytest.mark.asyncio
    async def test_reachable_through_dispatch_with_heph_prefix(self):
        """The third historically-live bug, found alongside the other two
        while auditing every heph_ reference in the codebase rather than
        stopping at config/prompts/**/*.yaml."""
        mock_response = object()
        with patch("src.mcp.server.give_validation_review", new=AsyncMock(return_value=mock_response)):
            result = await mcp_server.execute_tool({
                "tool": "heph_give_validation_review",
                "arguments": {
                    "agent_id": "validator-1",
                    "task_id": "task-1",
                    "validation_passed": True,
                    "feedback": "Confirmed working",
                },
            })
        assert result is mock_response
