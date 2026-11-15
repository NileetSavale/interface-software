"""What a calling agent experiences (docs/ARCHITECTURE.md §4, invocation model).

    invoke(artifact, inputs, deadline_s=30)  → a result, or needs_human with a run_id once the deadline passes
    get_result(run_id)                       → the result, or {status: running | paused, since}
    wait(run_id, timeout_s)                  → the same, after waiting up to timeout_s

The calling agent is usually in a live conversation. Blocking for a ten-minute handoff would time it out, and
aborting would waste the operator's work, so invoke blocks only until its deadline; the run carries on in the
background and its late result is kept for TTL_S, then dropped. In-memory, single process (§0): no queue.
"""
import asyncio
import time

from interfaze.artifact.schema import Artifact
from interfaze.evidence import now
from interfaze.replay.executor import Replayer
from interfaze.replay.result import NeedsHuman, RunPending

TTL_S = 15 * 60


class Runs:
    def __init__(self, operator_hub=None):
        self.hub = operator_hub
        self._running: dict[str, tuple[asyncio.Task, Replayer, str]] = {}
        self._finished: dict[str, tuple[object, float]] = {}

    async def invoke(self, artifact: Artifact, base_url: str, inputs: dict[str, str], deadline_s: float = 30, **kw):
        replayer = Replayer(artifact, base_url, inputs, operator_hub=self.hub, **kw)
        task = asyncio.create_task(replayer.run())
        self._running[replayer.run_id] = (task, replayer, now())
        task.add_done_callback(lambda t, run_id=replayer.run_id: self._done(run_id, t))
        try:
            return await asyncio.wait_for(asyncio.shield(task), deadline_s)
        except asyncio.TimeoutError:
            return self._pending(replayer.run_id)

    def get_result(self, run_id: str):
        self._expire()
        if run_id in self._finished:
            return self._finished[run_id][0]
        if run_id in self._running:
            return self._pending(run_id)
        return None  # never existed, or its result expired

    async def wait(self, run_id: str, timeout_s: float):
        if run_id in self._running:
            try:
                await asyncio.wait_for(asyncio.shield(self._running[run_id][0]), timeout_s)
            except asyncio.TimeoutError:
                pass
        return self.get_result(run_id)

    def _pending(self, run_id: str):
        _, replayer, since = self._running[run_id]
        intervention = replayer.session.intervention
        if intervention is not None:
            # The caller can tell its user "a colleague is checking this" and collect the result later
            return NeedsHuman(run_id=run_id, completed_at=now(), intervention_id=intervention.id,
                              at_step=intervention.step_id, reason=intervention.reason)
        return RunPending(run_id=run_id, status="running", since=since)

    def _done(self, run_id: str, task: asyncio.Task):
        self._running.pop(run_id, None)
        if not task.cancelled() and task.exception() is None:
            self._finished[run_id] = (task.result(), time.monotonic())

    def _expire(self):
        cutoff = time.monotonic() - TTL_S
        for run_id in [r for r, (_, at) in self._finished.items() if at < cutoff]:
            del self._finished[run_id]
