"""Long-running work behind the API: research runs and capability-manager requests.

Both are slow (local models) and use the same GPU, so they run one at a time on a single worker; a run that is
waiting its turn is shown as `queued`. Everything they do is already written to the append-only logs by the
runner and the manager, so the API only starts them and reads those logs.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor

from api.registry import Registry
from connectors.registry import ConnectorRegistry
from runs import RunStore


def run_kind(events: list[dict]) -> str | None:
    """block | research, from the first event of a run. None for an unknown run."""
    for e in events:
        if e["type"] == "run_started":
            return "block" if "graph" in e["payload"] else "research"
        if e["type"] == "queued":
            return e["payload"].get("kind")
    return None


class Jobs:
    def __init__(self, runs: RunStore, connectors: ConnectorRegistry, runner_factory=None, manager_factory=None,
                 background: bool = True):
        self.runs, self.connectors = runs, connectors
        self._runner_factory, self._manager_factory = runner_factory, manager_factory
        self._runner = self._manager = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="titan-job") if background else None

    # The graph objects open their databases when built, so they are created on first use, not at start-up.
    @property
    def runner(self):
        if self._runner is None:
            if self._runner_factory is None:
                from runner import Runner
                self._runner_factory = lambda: Runner(runs=self.runs, registry=self.connectors)
            self._runner = self._runner_factory()
        return self._runner

    @property
    def manager(self):
        if self._manager is None:
            if self._manager_factory is None:
                from capability import CapabilityManager
                self._manager_factory = lambda: CapabilityManager(registry=self.connectors, runs=self.runs, on_ready=self._on_ready)
            self._manager = self._manager_factory()
        return self._manager

    def _on_ready(self, run_id: str, connector_id: str, request_id: str):
        """A connector became active: let the research run that was waiting for it carry on."""
        return self.runner.resume(run_id, {"capability": connector_id})

    def _submit(self, fn, *args, **kwargs) -> None:
        if self.pool is None:
            fn(*args, **kwargs)
        else:
            self.pool.submit(fn, *args, **kwargs)

    # -- research ---------------------------------------------------------------------------------------

    def start_research(self, question: str, subject: str, budgets: dict | None, allow_capabilities: bool,
                       require_approval: bool = True) -> str:
        run_id = uuid.uuid4().hex
        self.runs.emit(run_id, "api", "queued", {"kind": "research", "question": question, "subject": subject})
        self._submit(self._start_research, run_id, question, subject, budgets, allow_capabilities, require_approval)
        return run_id

    def _start_research(self, run_id, question, subject, budgets, allow_capabilities, require_approval) -> None:
        try:
            self.runner.start(question, subject=subject, require_approval=require_approval, budgets=budgets,
                              allow_capabilities=allow_capabilities, run_id=run_id)
        except Exception as exc:                      # e.g. the models could not be reached
            self.runs.emit(run_id, "api", "run_failed", {"error": f"{type(exc).__name__}: {str(exc)[:300]}"})

    def resume_research(self, run_id: str, decision: dict | None) -> None:
        self.runs.emit(run_id, "api", "queued", {"kind": "research"})
        self._submit(self._resume_research, run_id, decision)

    def _resume_research(self, run_id: str, decision: dict | None) -> None:
        try:
            self.runner.resume(run_id, decision)
        except Exception as exc:
            self.runs.emit(run_id, "api", "run_failed", {"error": f"{type(exc).__name__}: {str(exc)[:300]}"})

    def report(self, run_id: str) -> dict | None:
        try:
            return self.runner.snapshot(run_id)["values"].get("report")
        except Exception:
            return None

    # -- capability manager -----------------------------------------------------------------------------

    def start_capability(self, need: str, known_api: dict | None = None) -> str:
        request_id = self.connectors.create_request(None, need, "requested from the app")
        self._submit(self._start_capability, request_id, need, known_api)
        return request_id

    def _start_capability(self, request_id: str, need: str, known_api: dict | None) -> None:
        try:
            self.manager.start(need, request_id=request_id, known_api=known_api)
        except Exception as exc:
            self.connectors.request_event(request_id, "failed", {"reason": f"{type(exc).__name__}: {str(exc)[:300]}"})

    def decide_capability(self, request_id: str, decision: dict) -> None:
        self._submit(self._decide_capability, request_id, decision)

    def _decide_capability(self, request_id: str, decision: dict) -> None:
        try:
            self.manager.resume(request_id, decision)
        except Exception as exc:
            self.connectors.request_event(request_id, "failed", {"reason": f"{type(exc).__name__}: {str(exc)[:300]}"})

    def gate(self, request_id: str) -> dict | None:
        """The approval gate of a request that is waiting for a person, or None."""
        try:
            waiting = self.manager.pending(request_id)
        except Exception:
            return None
        return waiting["gate"] if waiting else None
