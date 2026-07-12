from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph

from booking_graph import build_booking_subgraph
from nodes import (
    await_user_location,
    classify_intent,
    correct_info,
    draft_appointment_details,
    draft_user_info_storage_details,
    interpret_user_confirmation,
    onboard,
    plan_next_turn,
    receive_message,
    retrieve_info,
    request_user_location,
    send_direct_response,
    send_clarify,
    send_correction_query,
    send_user_confirmation,
    start_thread,
    ensure_user,
    store_user_location,
    store_in_supabase,
    view_appointments,
)
from state import OneHealthAgentState
from langgraph.types import RetryPolicy


def build_graph(checkpointer=None):
    """Build and compile the OneHealth agent workflow."""
    workflow = StateGraph(OneHealthAgentState)

    workflow.add_node("receive_message", receive_message)
    workflow.add_node("ensure_user", ensure_user)
    workflow.add_node("start_thread", start_thread)
    workflow.add_node("request_user_location", request_user_location)
    workflow.add_node("await_user_location", await_user_location)
    workflow.add_node("onboard", onboard)
    workflow.add_node("plan_next_turn", plan_next_turn)
    workflow.add_node("classify_intent", classify_intent)
    workflow.add_node("store_user_location", store_user_location)
    workflow.add_node("send_direct_response", send_direct_response, retry_policy=RetryPolicy(max_attempts=3))
    workflow.add_node("retrieve_info", retrieve_info)
    workflow.add_node("view_appointments", view_appointments, retry_policy=RetryPolicy(max_attempts=3))
    workflow.add_node("send_clarify", send_clarify, retry_policy=RetryPolicy(max_attempts=3))
    workflow.add_node("draft_appointment_details", draft_appointment_details)
    workflow.add_node("draft_user_info_storage_details", draft_user_info_storage_details)
    workflow.add_node("send_user_confirmation", send_user_confirmation, retry_policy=RetryPolicy(max_attempts=3))
    workflow.add_node("interpret_user_confirmation", interpret_user_confirmation)
    workflow.add_node("send_correction_query", send_correction_query)
    workflow.add_node("correct_info", correct_info)
    workflow.add_node("store_in_supabase", store_in_supabase, retry_policy=RetryPolicy(max_attempts=3))

    # NexHealth scheduling flow (18 nodes) collapsed into one subgraph node.
    # Shared schema, so the compiled subgraph is added directly with no state
    # translation. Parent dispatchers route to "booking" instead of
    # "start_nexhealth_scheduling" (see conversation_policy.NODE_FOR_ROUTE and
    # interpret_user_confirmation).
    workflow.add_node("booking", build_booking_subgraph())

    workflow.add_edge(START, "receive_message")

    return workflow.compile(checkpointer)


graph = build_graph()

if __name__ == "__main__":
    graph.invoke({}, config={"configurable": {"thread_id": "1"}})
