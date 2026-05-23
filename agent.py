from langgraph.graph import START, END, StateGraph

from nodes import (
    appointment_website_search,
    await_user_login,
    classify_intent,
    correct_info,
    draft_appointment_details,
    draft_user_info_storage_details,
    interpret_user_confirmation,
    request_user_location,
    schedule_appointment,
    send_correction_query,
    send_user_confirmation,
    start_thread,
    start_user_login,
    store_in_supabase,
    check_cookies,
)
from state import OneHealthAgentState
from langgraph.types import RetryPolicy


def build_graph(checkpointer=None):
    """Build and compile the OneHealth agent workflow."""
    workflow = StateGraph(OneHealthAgentState)

    workflow.add_node("start_thread", start_thread)
    workflow.add_node("request_user_location", request_user_location)
    workflow.add_node("classify_intent", classify_intent)
    workflow.add_node("draft_appointment_details", draft_appointment_details)
    workflow.add_node("draft_user_info_storage_details", draft_user_info_storage_details)
    workflow.add_node("send_user_confirmation", send_user_confirmation)
    workflow.add_node("interpret_user_confirmation", interpret_user_confirmation)
    workflow.add_node("send_correction_query", send_correction_query)
    workflow.add_node("correct_info", correct_info)
    workflow.add_node("store_in_supabase", store_in_supabase, retry_policy=RetryPolicy(max_attempts=3))
    workflow.add_node("appointment_website_search", appointment_website_search, retry_policy=RetryPolicy(max_attempts=3))
    workflow.add_node("check_cookies", check_cookies)
    workflow.add_node("start_user_login", start_user_login)
    workflow.add_node("await_user_login", await_user_login)
    workflow.add_node("schedule_appointment", schedule_appointment)

    workflow.add_edge(START, "start_thread")
    workflow.add_edge("start_user_login", "await_user_login")
    workflow.add_edge("schedule_appointment", END)

    if checkpointer is not None:
        return workflow.compile(checkpointer=checkpointer)
    return workflow.compile()


graph = build_graph()

if __name__ == "__main__":
    graph.invoke({}, config={"configurable": {"thread_id": "1"}})
