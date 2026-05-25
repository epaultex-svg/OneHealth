from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph

from nodes import (
    book_appointment,
    await_user_location,
    classify_intent,
    correct_info,
    draft_appointment_details,
    draft_user_info_storage_details,
    get_appointment_slots,
    get_appointment_type,
    get_location,
    get_patient,
    get_provider,
    interpret_user_confirmation,
    onboard,
    receive_message,
    request_user_location,
    select_appointment_type,
    select_appointment_slot,
    select_provider,
    send_direct_response,
    send_appointment_type_options,
    send_correction_query,
    send_provider_options,
    send_user_confirmation,
    send_slot_options,
    start_nexhealth_scheduling,
    start_thread,
    ensure_user,
    store_user_location,
    store_in_supabase,
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
    workflow.add_node("classify_intent", classify_intent)
    workflow.add_node("store_user_location", store_user_location)
    workflow.add_node("send_direct_response", send_direct_response)
    workflow.add_node("draft_appointment_details", draft_appointment_details)
    workflow.add_node("draft_user_info_storage_details", draft_user_info_storage_details)
    workflow.add_node("send_user_confirmation", send_user_confirmation)
    workflow.add_node("interpret_user_confirmation", interpret_user_confirmation)
    workflow.add_node("send_correction_query", send_correction_query)
    workflow.add_node("correct_info", correct_info)
    workflow.add_node("store_in_supabase", store_in_supabase, retry_policy=RetryPolicy(max_attempts=3))
    workflow.add_node("start_nexhealth_scheduling", start_nexhealth_scheduling)
    workflow.add_node("get_location", get_location, retry_policy=RetryPolicy(max_attempts=3))
    workflow.add_node("get_provider", get_provider, retry_policy=RetryPolicy(max_attempts=3))
    workflow.add_node("send_provider_options", send_provider_options)
    workflow.add_node("select_provider", select_provider)
    workflow.add_node("get_patient", get_patient, retry_policy=RetryPolicy(max_attempts=3))
    workflow.add_node("get_appointment_type", get_appointment_type, retry_policy=RetryPolicy(max_attempts=3))
    workflow.add_node("send_appointment_type_options", send_appointment_type_options)
    workflow.add_node("select_appointment_type", select_appointment_type)
    workflow.add_node("get_appointment_slots", get_appointment_slots, retry_policy=RetryPolicy(max_attempts=3))
    workflow.add_node("send_slot_options", send_slot_options)
    workflow.add_node("select_appointment_slot", select_appointment_slot)
    workflow.add_node("book_appointment", book_appointment, retry_policy=RetryPolicy(max_attempts=3))

    workflow.add_edge(START, "receive_message")

    return workflow.compile(checkpointer)


graph = build_graph()

if __name__ == "__main__":
    graph.invoke({}, config={"configurable": {"thread_id": "1"}})
