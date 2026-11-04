"""The research pipeline as a flowchart. Nodes and edges come from the real LangGraph graph (so they cannot drift from the
code); labels, plain-language descriptions and the layout come from definitions/harness/research.json."""

import json

from config import PROJECT_ROOT

PATH = PROJECT_ROOT / "definitions" / "harness" / "research.json"


def graph_structure() -> tuple[set[str], set[tuple[str, str]]]:
    """(node names, edges) of the compiled research graph, without START and END."""
    from runner import Runner
    graph = Runner.__new__(Runner)._build(None).get_graph()
    nodes = {n for n in graph.nodes if not n.startswith("__")}
    edges = {(e.source, e.target) for e in graph.edges if not e.source.startswith("__") and not e.target.startswith("__")}
    return nodes, edges


def research() -> dict:
    spec = json.loads(PATH.read_text(encoding="utf-8"))
    nodes, edges = graph_structure()
    declared = {(e["from"], e["to"]) for e in spec["edges"]}
    problems = []
    if set(spec["nodes"]) != nodes:
        problems.append(f"nodes differ: only in the graph {sorted(nodes - set(spec['nodes']))}, only in the file {sorted(set(spec['nodes']) - nodes)}")
    if declared != edges:
        problems.append(f"edges differ: only in the graph {sorted(edges - declared)}, only in the file {sorted(declared - edges)}")
    if problems:
        raise ValueError("definitions/harness/research.json does not match the research graph: " + "; ".join(problems))
    return {"id": spec["id"], "title": spec["title"],
            "nodes": [{"id": k, **v} for k, v in spec["nodes"].items()], "edges": spec["edges"]}
