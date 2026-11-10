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


def research(strict: bool = True) -> dict:
    """The flowchart. `strict` (tests, start-up checks): the file must match the graph exactly, or ValueError. Not strict (the
    app): a mismatch (say a server still running older code after definitions changed) still draws the chart, from the real
    graph's steps and links, and says what differs in `problems`, instead of the chart disappearing."""
    spec = json.loads(PATH.read_text(encoding="utf-8"))
    nodes, edges = graph_structure()
    declared = {(e["from"], e["to"]) for e in spec["edges"]}
    problems = []
    if set(spec["nodes"]) != nodes:
        problems.append(f"nodes differ: only in the graph {sorted(nodes - set(spec['nodes']))}, only in the file {sorted(set(spec['nodes']) - nodes)}")
    if declared != edges:
        problems.append(f"edges differ: only in the graph {sorted(edges - declared)}, only in the file {sorted(declared - edges)}")
    if problems and strict:
        raise ValueError("definitions/harness/research.json does not match the research graph: " + "; ".join(problems))
    shown_nodes = [{"id": k, **v} for k, v in spec["nodes"].items() if k in nodes]
    placed = {n["id"] for n in shown_nodes}
    # steps the file does not describe yet are still drawn (a row below the rest), so the chart never loses a step
    lowest = max([n.get("row", 0) for n in shown_nodes] or [0])
    shown_nodes += [{"id": k, "label": k.replace("_", " ").title(), "does": "A step the chart's description does not cover yet.",
                     "col": i, "row": lowest + 1} for i, k in enumerate(sorted(nodes - placed))]
    shown_edges = [e for e in spec["edges"] if (e["from"], e["to"]) in edges]
    shown_edges += [{"from": a, "to": b, "control": a == b} for a, b in sorted(edges - declared)]
    return {"id": spec["id"], "title": spec["title"], "nodes": shown_nodes, "edges": shown_edges,
            "control_note": spec.get("control_note", ""), "problems": problems}
