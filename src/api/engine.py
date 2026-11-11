"""Run engine: one execution path for a single block and for a template graph.

A run is a graph of nodes. Everything it does is appended to the run log (runs.py); its state is rebuilt
from that log, so a run waiting for approval survives a restart and nothing about its history can be edited.
"""

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

from jsonschema import Draft202012Validator

import config
from api import executors as ex
from api.registry import BlockDef, Registry
from evidence import EvidenceStore
from runs import RunStore
from tools.archive import check_public_url

MAX_OUTPUT_BYTES = 1_000_000
NODE_ID = re.compile(r"^[a-z][a-z0-9_]*$")
VERDICTS = {"supported", "partially_supported", "not_supported"}
TERMINAL = {"finished", "stopped", "failed"}


class RunError(ValueError):
    """The run was refused before starting. `problems` lists everything wrong, so a form can show all of it."""
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def default_llm(task: str, model: str | None = None, temperature: float = 0):
    from llm import get_chat_model
    return get_chat_model(task=task, model=model, temperature=temperature)


class Engine:
    def __init__(self, registry: Registry, store: EvidenceStore, runs: RunStore, probes: dict,
                 llm=None, connectors=None, uploads_dir: Path | None = None, executors: dict | None = None,
                 background: bool = True):
        self.registry, self.store, self.runs, self.probes = registry, store, runs, probes
        self.llm = llm or default_llm
        self.connectors = connectors
        self.uploads_dir = uploads_dir or config.DATA_DIR / "uploads"
        self.executors = ex.EXECUTORS if executors is None else executors
        self.pool = ThreadPoolExecutor(max_workers=4) if background else None
        self._lock = threading.Lock()
        self._working: set[str] = set()

    # -- value checks: references must point at real evidence --------------------------------------------

    def check_value(self, type_name: str, value, check_network: bool = True):
        """Return the value in canonical form, or raise ValueError. A Claim or Source can only be a reference
        to something already in the evidence store, so free text can never pass as one."""
        if type_name in ("Text", "Query", "Model", "URL"):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"expected non-empty text, got {value!r}"[:120])
            if type_name == "URL" and check_network:
                check_public_url(value.strip())
            return value.strip()
        if type_name == "Source":
            sha = value.get("sha256") if isinstance(value, dict) else None
            if not isinstance(sha, str) or self.store.get_source(sha) is None:
                raise ValueError("unknown source: use the output of a fetch or file block")
            return ex.source_ref(self.store, sha)
        if type_name == "Claim":
            cid = value.get("claim_id") if isinstance(value, dict) else None
            if not isinstance(cid, int) or isinstance(cid, bool) or self.store.get_claim(cid) is None:
                raise ValueError("unknown claim: claims come only from the evidence store")
            return ex.claim_ref(self.store, cid)
        if type_name == "Claims":
            if not isinstance(value, list):
                raise ValueError("expected a list of claims")
            return [self.check_value("Claim", v) for v in value]
        if type_name == "Verdict":
            if not isinstance(value, dict) or value.get("verdict") not in VERDICTS:
                raise ValueError("a verdict must come from a support check")
            return value
        if type_name == "File":
            sha = value.get("upload_id") if isinstance(value, dict) else None
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha) or not (self.uploads_dir / f"{sha}.bin").exists():
                raise ValueError("unknown file: upload it first")
            return value
        if type_name == "Json":
            json.dumps(value)
            return value
        raise ValueError(f"no rule for port type {type_name}")

    # -- planning ----------------------------------------------------------------------------------------

    def single_block_graph(self, block_id: str, settings: dict | None, inputs: dict) -> tuple[dict, dict]:
        graph = {"nodes": [{"id": "n1", "block": block_id, "settings": settings or {}}], "edges": []}
        return graph, {f"n1.{k}": v for k, v in inputs.items()}

    def plan(self, graph: dict, run_inputs: dict, check_network: bool = True) -> dict:
        problems: list[str] = []
        nodes: dict[str, tuple[BlockDef, dict]] = {}
        resolved = []
        for n in graph.get("nodes", []):
            nid = n.get("id", "")
            if not NODE_ID.match(nid) or nid in nodes:
                problems.append(f"bad or duplicate node id {nid!r}")
                continue
            ref = n.get("block", "")
            bid, _, ver = ref.partition("@")
            block = self.registry.blocks.get((bid, int(ver))) if ver.isdigit() else self.registry.latest(bid)
            if block is None:
                problems.append(f"{nid}: unknown block {ref!r}")
                continue
            settings, errors = self.resolve_settings(block, n.get("settings") or {})
            problems += [f"{nid}: {e}" for e in errors]
            nodes[nid] = (block, settings)
            resolved.append({"id": nid, "block": f"{block.id}@{block.version}", "settings": settings})

        wiring: dict[tuple[str, str], tuple[str, str]] = {}
        deps: dict[str, set[str]] = {nid: set() for nid in nodes}
        for e in graph.get("edges", []):
            try:
                (sn, sp), (tn, tp) = e["from"].split("."), e["to"].split(".")
                src = next(p for p in nodes[sn][0].outputs if p.name == sp)
                dst = next(p for p in nodes[tn][0].inputs if p.name == tp)
            except (KeyError, ValueError, StopIteration):
                problems.append(f"bad edge {e!r}")
                continue
            if (tn, tp) in wiring:
                problems.append(f"{tn}.{tp} has more than one incoming edge")
            if not self.registry.is_assignable(src.type, dst.type):
                problems.append(f"{e['from']} ({src.type}) cannot connect to {e['to']} ({dst.type})")
            wiring[(tn, tp)] = (sn, sp)
            deps[tn].add(sn)

        try:
            order = list(TopologicalSorter(deps).static_order())
        except CycleError:
            problems.append("the graph has a cycle")
            order = list(nodes)

        values = {}
        for nid, (block, _) in nodes.items():
            for port in block.inputs:
                key = f"{nid}.{port.name}"
                if (nid, port.name) in wiring:
                    if key in run_inputs:
                        problems.append(f"{key} is wired and also given a value")
                elif key in run_inputs:
                    try:
                        values[key] = self.check_value(port.type, run_inputs[key], check_network)
                    except (ValueError, OSError) as exc:
                        problems.append(f"{key}: {exc}")
                elif port.required:
                    problems.append(f"{key} is required")
        for key in run_inputs:
            nid, _, pname = key.partition(".")
            if nid not in nodes or not any(p.name == pname for p in nodes[nid][0].inputs):
                problems.append(f"unknown input {key!r}")

        if not problems and check_network:
            problems += self.availability_problems(nodes)
        if problems:
            raise RunError(problems)
        return {"nodes": nodes, "order": order, "wiring": wiring, "values": values, "resolved": resolved}

    @staticmethod
    def resolve_settings(block: BlockDef, given: dict) -> tuple[dict, list[str]]:
        props = block.settings.get("properties", {})
        errors = [f"unknown setting {k!r}" for k in given if k not in props]
        merged = {k: p["default"] for k, p in props.items() if "default" in p} | {k: v for k, v in given.items() if k in props}
        errors += [f"setting {'.'.join(map(str, e.path)) or '?'}: {e.message}"
                   for e in Draft202012Validator(block.settings).iter_errors(merged)]
        return merged, errors

    def availability_problems(self, nodes: dict) -> list[str]:
        problems, seen = [], {}
        for nid, (block, _) in nodes.items():
            ref = block.executor.ref
            if ref not in self.executors and ref not in ex.ENGINE_HANDLED:
                problems.append(f"{nid}: block {block.id} cannot be run from the engine yet")
                continue
            for need in block.badges.needs:
                key = need.split(":")[0]
                if key not in self.probes:
                    problems.append(f"{nid}: cannot check requirement {need!r}")
                    continue
                if key not in seen:
                    seen[key] = self.probes[key]()
                if seen[key]:
                    problems.append(f"{nid}: {seen[key]}")
        return problems

    # -- running -----------------------------------------------------------------------------------------

    def start(self, graph: dict, run_inputs: dict, title: str) -> str:
        plan = self.plan(graph, run_inputs)
        import uuid
        run_id = uuid.uuid4().hex
        self.runs.emit(run_id, "engine", "run_started", {
            "question": title, "title": title, "graph": {"nodes": plan["resolved"], "edges": graph.get("edges", [])},
            "inputs": run_inputs, "llm_mode": config.LLM_MODE})
        self._submit(run_id)
        return run_id

    def resume(self, run_id: str, approved: bool, note: str = "") -> None:
        with self._lock:                      # two clicks must not record two decisions
            if self.runs.status(run_id) != "awaiting_approval":
                raise RunError(["this run is not waiting for approval"])
            waiting = [e for e in self.runs.events(run_id) if e["type"] == "awaiting_approval"][-1]
            self.runs.emit(run_id, waiting["node"], "approval_decided", {"approved": approved, "note": note})
            self.runs.emit(run_id, "engine", "resumed", {"approved": approved})
        self._submit(run_id)

    def _submit(self, run_id: str) -> None:
        if self.pool is None:
            self._execute(run_id)
        else:
            self._working.add(run_id)
            self.pool.submit(self._execute, run_id).add_done_callback(lambda f: self._working.discard(run_id))

    def busy(self) -> int:
        """How many block runs this process is working on (a restart waits for none)."""
        return len(self._working)

    def _execute(self, run_id: str) -> None:
        def emit_for(node: str):
            return lambda type_, payload: self.runs.emit(run_id, node, type_, payload)

        try:
            events = self.runs.events(run_id)
            started = next(e for e in events if e["type"] == "run_started")["payload"]
            plan = self.plan(started["graph"], started["inputs"], check_network=False)
            outputs = {e["node"]: e["payload"]["outputs"] for e in events if e["type"] == "node_finished"}
            decisions = {e["node"]: e["payload"] for e in events if e["type"] == "approval_decided"}
            for nid in plan["order"]:
                if nid in outputs:
                    continue
                block, settings = plan["nodes"][nid]
                emit = emit_for(nid)
                inputs = self._gather(nid, block, plan, outputs)
                if block.executor.ref in ex.ENGINE_HANDLED:
                    if nid not in decisions:
                        emit("awaiting_approval", {"prompt": settings.get("prompt", ""), "subject": inputs.get("subject")})
                        return
                    if not decisions[nid]["approved"]:
                        emit("run_stopped", {"reason": "rejected by the user", "note": decisions[nid].get("note", "")})
                        return
                    outputs[nid] = {"decision": decisions[nid]}
                    emit("node_finished", {"outputs": outputs[nid]})
                    continue
                emit("node_started", {"block": f"{block.id}@{block.version}", "inputs": inputs, "settings": settings})
                try:
                    ctx = ex.Context(store=self.store, run_id=run_id, node_id=nid, emit=emit, llm=self.llm,
                                     connectors=self.connectors, uploads_dir=self.uploads_dir)
                    produced = self.executors[block.executor.ref](ctx, inputs, settings)
                    outputs[nid] = self._check_outputs(block, produced)
                except Exception as exc:
                    emit("node_failed", {"error": f"{type(exc).__name__}: {str(exc)[:500]}"})
                    emit("run_failed", {"node": nid, "error": str(exc)[:500]})
                    return
                emit("node_finished", {"outputs": outputs[nid]})
            sinks = set(plan["nodes"]) - {s for s, _ in plan["wiring"].values()}
            self.runs.emit(run_id, "engine", "run_finished", {"outputs": {n: outputs[n] for n in sorted(sinks)}})
        except Exception as exc:                                   # the engine itself failed
            self.runs.emit(run_id, "engine", "run_failed", {"error": f"{type(exc).__name__}: {str(exc)[:500]}"})

    def _gather(self, nid: str, block: BlockDef, plan: dict, outputs: dict) -> dict:
        inputs = {}
        for port in block.inputs:
            if (nid, port.name) in plan["wiring"]:
                sn, sp = plan["wiring"][(nid, port.name)]
                inputs[port.name] = self.check_value(port.type, outputs[sn][sp], check_network=False)
            elif f"{nid}.{port.name}" in plan["values"]:
                inputs[port.name] = plan["values"][f"{nid}.{port.name}"]
        return inputs

    def _check_outputs(self, block: BlockDef, produced: dict) -> dict:
        out = {}
        for port in block.outputs:
            if port.name not in produced:
                raise RuntimeError(f"block {block.id} produced no {port.name!r}")
            out[port.name] = self.check_value(port.type, produced[port.name], check_network=False)
        if len(json.dumps(out)) > MAX_OUTPUT_BYTES:
            raise RuntimeError("the output is too large to record")
        return out

    # -- reading -----------------------------------------------------------------------------------------

    def describe(self, run_id: str) -> dict | None:
        events = self.runs.events(run_id)
        if not events:
            return None
        started = next((e for e in events if e["type"] == "run_started"), None)
        nodes: dict[str, str] = {}
        for e in events:
            if e["type"] == "node_started":
                nodes[e["node"]] = "running"
            elif e["type"] == "awaiting_approval":
                nodes[e["node"]] = "awaiting_approval"
            elif e["type"] == "node_finished":
                nodes[e["node"]] = "finished"
            elif e["type"] == "node_failed":
                nodes[e["node"]] = "failed"
        finished = next((e for e in reversed(events) if e["type"] == "run_finished"), None)
        return {"run_id": run_id, "status": self.runs.status(run_id), "title": started["payload"].get("title") if started else None,
                "graph": started["payload"].get("graph") if started else None, "nodes": nodes,
                "outputs": finished["payload"]["outputs"] if finished else None, "event_count": len(events)}
