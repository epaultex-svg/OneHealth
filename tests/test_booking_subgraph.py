"""Guards for the NexHealth booking subgraph extraction.

The booking flow (18 nodes) was collapsed into a single subgraph node named
"booking". These tests lock the invariants that extraction depends on:

1. The subgraph is compiled WITHOUT its own checkpointer, so it inherits the
   parent's. This is what lets select_appointment_slot's interrupt() pause and
   resume across the subgraph boundary.
2. A subgraph-internal interrupt surfaces at the PARENT graph's `.next` and
   `Command(resume=...)` propagates into the subgraph. webhook_worker.run_message
   resumes purely on `get_state().next` truthiness, so this mechanic is load
   bearing.
3. The real top-level graph exposes "booking" as one node and does not leak any
   interior booking node into the parent namespace.
"""

from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import agent
from booking_graph import build_booking_subgraph


def test_booking_subgraph_has_no_own_checkpointer():
    """Subgraph must inherit the parent checkpointer, not carry its own."""
    sub = build_booking_subgraph()
    assert sub.checkpointer is None


def test_subgraph_interrupt_surfaces_at_parent_and_resumes():
    """The exact contract webhook_worker relies on: a subgraph interrupt makes the
    parent's `.next` truthy, and Command(resume=...) advances past it."""

    class S(TypedDict, total=False):
        value: str
        resumed: str

    def ask(state: S):
        answer = interrupt({"prompt": "need input"})
        return {"resumed": answer}

    inner = StateGraph(S)
    inner.add_node("ask", ask)
    inner.add_edge(START, "ask")
    inner.add_edge("ask", END)
    sub = inner.compile()  # no checkpointer -> inherits parent

    parent = StateGraph(S)
    parent.add_node("booking", sub)
    parent.add_edge(START, "booking")
    parent.add_edge("booking", END)
    graph = parent.compile(checkpointer=InMemorySaver())

    config = {"configurable": {"thread_id": "t1"}}
    graph.invoke({"value": "go"}, config=config)

    # Parent sees the subgraph node as pending -> worker takes the resume path.
    snapshot = graph.get_state(config)
    assert bool(snapshot.next)

    graph.invoke(Command(resume="the answer"), config=config)
    final = graph.get_state(config)
    assert not final.next
    assert final.values["resumed"] == "the answer"


def test_top_level_graph_mounts_booking_without_leaking_interior_nodes():
    nodes = set(agent.build_graph().get_graph().nodes.keys())
    assert "booking" in nodes
    interior = {
        "start_nexhealth_scheduling",
        "get_institution",
        "select_appointment_slot",
        "book_appointment",
    }
    assert interior.isdisjoint(nodes)
