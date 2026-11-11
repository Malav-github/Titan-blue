"""Long-running work behind the API: research runs and capability-manager requests.

Both are slow (local models) and use the same GPU, so they run one at a time on a single worker; a run that is
waiting its turn is shown as `queued`. Everything they do is already written to the append-only logs by the
runner and the manager, so the API only starts them and reads those logs.
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from api.registry import Registry
from connectors.registry import ConnectorRegistry
from runs import RunStore

_COUNT = threading.Lock()


def run_kind(events: list[dict]) -> str | None:
    """block | research, from the first event of a run. None for an unknown run."""
    for e in events:
        if e["type"] == "run_started":
            return "block" if "graph" in e["payload"] else e["payload"].get("kind") if e["payload"].get("kind") == "verify" else "research"
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

    _pending = 0                                       # jobs submitted and not yet finished (a restart waits for none)

    def _submit(self, fn, *args, **kwargs) -> None:
        if self.pool is None:
            fn(*args, **kwargs)
            return
        with _COUNT:
            self._pending += 1

        def job():
            try:
                fn(*args, **kwargs)
            finally:
                with _COUNT:
                    self._pending -= 1
        self.pool.submit(job)

    def busy(self) -> int:
        """How many jobs are running or waiting their turn."""
        return self._pending

    # -- research ---------------------------------------------------------------------------------------

    def start_research(self, question: str, subject: str, budgets: dict | None, allow_capabilities: bool,
                       require_approval: bool = True, ask_sources: bool = True, report_type: str | None = None,
                       rerun_of: str | None = None) -> str:
        run_id = uuid.uuid4().hex
        # The whole request is kept, so the run can be run again exactly as asked.
        self.runs.emit(run_id, "api", "queued", {"kind": "research", "question": question, "subject": subject, "report_type": report_type,
                                                 "budgets": budgets, "allow_capabilities": allow_capabilities,
                                                 "require_approval": require_approval, "ask_sources": ask_sources, "rerun_of": rerun_of})
        self._submit(self._start_research, run_id, question, subject, budgets, allow_capabilities, require_approval, ask_sources, report_type)
        return run_id

    def rerun_research(self, run_id: str) -> str:
        """A new run of the same request (question, subject, report type, budgets, options). The old run is kept as it is."""
        events = self.runs.events(run_id)
        asked = next((e["payload"] for e in events if e["type"] == "queued" and "question" in e["payload"]), {})
        started = next((e["payload"] for e in events if e["type"] == "run_started"), {})
        req = {**started, **{k: v for k, v in asked.items() if v is not None}}       # runs from before the request was kept: what the start recorded
        if not req.get("question"):
            raise ValueError("this run has no question to run again")
        return self.start_research(req["question"], req.get("subject") or "", req.get("budgets"), bool(req.get("allow_capabilities")),
                                   require_approval=req.get("require_approval", True), ask_sources=req.get("ask_sources", True),
                                   report_type=asked.get("report_type") or started.get("report_type"), rerun_of=run_id)

    def _start_research(self, run_id, question, subject, budgets, allow_capabilities, require_approval, ask_sources=True,
                        report_type=None) -> None:
        try:
            self.runner.start(question, subject=subject, require_approval=require_approval, budgets=budgets,
                              allow_capabilities=allow_capabilities, run_id=run_id, ask_sources=ask_sources, report_type=report_type)
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

    def stop_research(self, run_id: str) -> None:
        """A run that is not working right now (paused, or waiting for you): end it and write its report from what it has."""
        self._submit(self._stop_research, run_id)

    def _stop_research(self, run_id: str) -> None:
        try:
            self.runner.stop(run_id)
        except Exception as exc:
            self.runs.emit(run_id, "api", "run_failed", {"error": f"{type(exc).__name__}: {str(exc)[:300]}"})

    # -- verification (on request, after a research run) ------------------------------------------------------------------

    @property
    def verifier(self):
        from verification import Verifier
        return Verifier(self.runner)

    def start_verification(self, of_run_id: str, claim_ids: list[int] | None = None) -> str:
        verify_id = uuid.uuid4().hex
        self.runs.emit(verify_id, "api", "queued", {"kind": "verify", "of_run": of_run_id, "question": "Verification"})
        self._submit(self._start_verification, verify_id, of_run_id, claim_ids)
        return verify_id

    def _start_verification(self, verify_id: str, of_run_id: str, claim_ids) -> None:
        try:
            self.verifier.start(of_run_id, claim_ids, verify_id=verify_id)
        except Exception as exc:
            self.runs.emit(verify_id, "api", "run_failed", {"error": f"{type(exc).__name__}: {str(exc)[:300]}"})

    def resume_verification(self, verify_id: str) -> None:
        self.runs.emit(verify_id, "api", "queued", {"kind": "verify"})
        self._submit(self._resume_verification, verify_id)

    def _resume_verification(self, verify_id: str) -> None:
        try:
            self.verifier.resume(verify_id)
        except Exception as exc:
            self.runs.emit(verify_id, "api", "run_failed", {"error": f"{type(exc).__name__}: {str(exc)[:300]}"})

    def stop_verification(self, verify_id: str) -> None:
        """A paused verification: end it, keeping the claims verified so far."""
        started = next(e["payload"] for e in self.runs.events(verify_id) if e["type"] == "run_started")
        self.verifier._finish(verify_id, started["of_run"], stopped=True)

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
