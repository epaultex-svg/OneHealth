"""NexHealth scheduling flow as a self-contained LangGraph subgraph.

Extracted from the flat agent graph for readability and independent testing. The
cluster is self-contained: a single entry (``start_nexhealth_scheduling``), exits
only to ``__end__``, and owns a disjoint slice of state (``nexhealth_*`` +
``patient_info`` + booking ``appt_details``).

Shared-schema pattern: the subgraph uses the same ``OneHealthAgentState`` as the
parent, so the compiled graph is added directly as a node in ``agent.py`` with no
state translation. It is compiled WITHOUT a checkpointer so it inherits the
parent's; that is what lets the ``select_appointment_slot`` interrupt pause and
resume across the subgraph boundary.

    START
      -> start_nexhealth_scheduling
      -> get_institution -> send_institution_options -> select_institution
      -> get_location    -> send_location_options    -> select_location
      -> get_provider    -> send_provider_options    -> select_provider
      -> get_patient
      -> get_appointment_type -> send_appointment_type_options -> select_appointment_type
      -> get_appointment_slots -> send_slot_options -> select_appointment_slot (interrupt)
      -> book_appointment -> __end__
"""

from langgraph.graph import START, StateGraph
from langgraph.types import RetryPolicy

from nodes import (
    book_appointment,
    get_appointment_slots,
    get_appointment_type,
    get_institution,
    get_location,
    get_patient,
    get_provider,
    select_appointment_slot,
    select_appointment_type,
    select_institution,
    select_location,
    select_provider,
    send_appointment_type_options,
    send_institution_options,
    send_location_options,
    send_provider_options,
    send_slot_options,
    start_nexhealth_scheduling,
)
from state import OneHealthAgentState


def build_booking_subgraph():
    """Build and compile the NexHealth scheduling subgraph.

    No checkpointer: the subgraph inherits the parent graph's checkpointer so
    interrupt()/Command(resume=...) propagate across the boundary.
    """
    sg = StateGraph(OneHealthAgentState)

    sg.add_node("start_nexhealth_scheduling", start_nexhealth_scheduling)
    sg.add_node("get_institution", get_institution, retry_policy=RetryPolicy(max_attempts=3))
    sg.add_node("send_institution_options", send_institution_options)
    sg.add_node("select_institution", select_institution)
    sg.add_node("get_location", get_location, retry_policy=RetryPolicy(max_attempts=3))
    sg.add_node("send_location_options", send_location_options)
    sg.add_node("select_location", select_location)
    sg.add_node("get_provider", get_provider, retry_policy=RetryPolicy(max_attempts=3))
    sg.add_node("send_provider_options", send_provider_options)
    sg.add_node("select_provider", select_provider)
    sg.add_node("get_patient", get_patient, retry_policy=RetryPolicy(max_attempts=3))
    sg.add_node("get_appointment_type", get_appointment_type, retry_policy=RetryPolicy(max_attempts=3))
    sg.add_node("send_appointment_type_options", send_appointment_type_options)
    sg.add_node("select_appointment_type", select_appointment_type)
    sg.add_node("get_appointment_slots", get_appointment_slots, retry_policy=RetryPolicy(max_attempts=3))
    sg.add_node("send_slot_options", send_slot_options)
    sg.add_node("select_appointment_slot", select_appointment_slot)
    sg.add_node("book_appointment", book_appointment, retry_policy=RetryPolicy(max_attempts=3))

    sg.add_edge(START, "start_nexhealth_scheduling")

    return sg.compile()
