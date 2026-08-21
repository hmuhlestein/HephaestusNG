"""Regression test for spawn_background_task (src/mcp/server/_shared.py).

asyncio.create_task() only holds a WEAK reference to the Task it returns --
per the stdlib docs' own "Important" note, an unreferenced task can be
garbage-collected mid-execution, silently, with no error, before the
coroutine finishes. update_task_status's fire-and-forget calls
(terminate_agents_and_process_queue, _dispatch_ready_dependents,
TaskCompletionService.spawn_validation) all used bare asyncio.create_task()
with no stored reference.

Confirmed live: a task's own agent-termination call intermittently never
ran. The task legitimately completed "done" and the orchestrator correctly
moved on to the phase's next task, but the completing agent was never told
to stop -- it sat idle until mechanical_recovery's frozen-agent detector
misread the idle silence as "frozen" and tried an in-session model-switch
rescue on work that had already finished minutes earlier. Multiple such
zombies piling up for the same phase is what looked, from the dashboard,
like several concurrent design_review agents running at once.

The GC race itself is inherently timing- and interpreter-version-dependent
-- not reliably reproducible with a synchronous gc.collect() call (CPython's
running event loop keeps its own internal registry of scheduled tasks
independent of caller references, so a naive repro doesn't actually fail
even with a bare, unreferenced asyncio.create_task()). This instead tests
the mechanism that closes the gap regardless of GC timing: a real,
explicit strong reference held for the task's full lifetime.
"""

import asyncio

import pytest


@pytest.mark.asyncio
async def test_holds_a_strong_reference_until_the_task_completes():
    from src.mcp.server import _shared

    ran = []
    release = asyncio.Event()

    async def work():
        await release.wait()
        ran.append(True)

    task = _shared.spawn_background_task(work())

    assert task in _shared._background_tasks, (
        "spawn_background_task must add the task to the tracking set "
        "immediately -- this strong reference is what stops the event "
        "loop's own weak reference from letting it be garbage-collected "
        "mid-execution"
    )

    release.set()
    await task

    assert ran == [True]
    assert task not in _shared._background_tasks, (
        "the done-callback must discard the task once it completes -- "
        "otherwise _background_tasks grows unbounded for the life of "
        "the process"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
