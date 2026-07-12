# TODOS

## Bulk backfill of `city` for existing location rows

- **What:** A one-time, throttled (~1 req/s for Nominatim) script that reads every
  `users` row whose `location` has `{lat,lng}` but no `city`, reverse-geocodes it, and
  writes `city` back.
- **Why:** Removes the one-time ~1-5s latency an existing user hits on their first
  appointment draft or info retrieval after deploy (the lazy read-fallback geocode).
- **Pros:** Clean read path; no per-user first-hit latency.
- **Cons:** Extra script + a slow throttled batch + an ops step to run it. The inline
  read-fallback (`tools.resolve_location_city`) already self-heals, so value is marginal.
- **Context:** We display saved location as a city instead of lat/lng. Geocoding happens
  at write time (`tools.store_info`) and lazily on read for old rows
  (`tools.resolve_location_city`, which geocodes once then backfills). This script would
  just front-load that backfill for all existing rows at once.
- **Depends on:** `geocoding.reverse_geocode_city`, `tools.resolve_location_city` (shipped).
- **Priority:** Low.

## Mocked offline eval runner + full trajectory coverage

- **What:** Teach `evals/run_onehealth_langsmith_eval.py` to consume each row's
  `supabase_fixture` (and a new `nexhealth_fixture`) by monkeypatching module-level
  seams — `graph_nodes._nexhealth_request`, `create_client`, `store_info`, and for
  booking `reserve_appointment_booking` / `mark_appointment_booked` /
  `mark_appointment_failed` + `_model` — exactly as the runner already patches
  `send_message`. Gate on a `--offline` flag so evals run deterministically with no
  real NexHealth/Supabase/Postgres side effects.
- **Why:** Today every trajectory eval hits real services (`--allow-side-effects`
  required) and the `supabase_fixture` blocks in the dataset are decorative. Offline
  mode makes the suite CI-gate-able and lets view/booking scenarios run without
  booking real appointments.
- **Unlocks (deferred rows):** `view_appointments` empty-list + no-patient-record
  branches; booking idempotency rows `oh-trajectory-018`/`019` scoring green without
  pre-seeded Postgres ledger state; the 4 unused UX-assertion rows (cancel-before-write,
  privacy, no-slot, invalid-choice).
- **Also:** Re-baseline the existing 15 trajectory rows — they start with
  `start_thread`, but the live graph starts `receive_message -> ensure_user ->
  plan_next_turn`, so their `expected_trajectory` currently mismatches
  (`oh-trajectory-017` is already pinned to the real order).
- **Also:** Decide the fate of legacy `evals/onehealth_eval_dataset.csv` (22
  single-turn classification rows, wired to nothing; redundant with pytest routing
  tests) — delete or fold into the trajectory set.
- **Priority:** Medium (post-demo; needed before CI gating / clinic pilot).

## Agent latency reduction — deferred fixes (#2–#10)

Source: `.gstack/qa-reports/latency-architecture-report-2026-07-10.md`. Fix #1
(template short-circuit in `send_direct_response`) is being planned/implemented
separately (`docs/plans/latency-fix-01-template-shortcircuit.md`). The rest are
deferred here and are OUT OF SCOPE of that plan.

- **#2 Typing indicator + stream the writer (HIGH, perceived).** Fire a Telegram
  `send_chat_action("typing")` the moment an update arrives; stream/chunk the writer
  output instead of awaiting full completion. No model change. Biggest perceived-latency
  win. `tools.py:72` already has `_telegram_send_async` unused by the graph path.
- **#3 Merge planner + writer into one LLM call for no-DB routes (HIGH).** For
  direct_response / clarify (no DB action), classify-and-respond in a single call.
  Cuts 2 generations → 1 on a large share of turns. Depends on prompt redesign.
- **#4 Split models by task (MEDIUM).** Small fast model for the planner/classifier
  (`conversation_engine._model`), keep gpt-oss-120b only for content generation.
- **#5 Async nodes + singleton clients (MEDIUM).** Convert hot-path nodes to
  `async def` + `ainvoke`; make `ChatOpenRouter` (engine:147, nodes.py:85) and the
  Supabase client module-level singletons for HTTP keep-alive / pooling.
- **#6 Cache the user-exists check (MEDIUM).** `ensure_user` (nodes.py:865) SELECTs
  Supabase every message. Set a `user_ensured` state flag after first SELECT, or lazy-upsert.
- **#7 Structured-output overhead / flakiness (LOW-MED).** Planner + extractors use
  `with_structured_output` on gpt-oss (flaky per nodes.py:1324). Evaluate plain-JSON
  parse or a model with better tool-call reliability.
- **#8 Repair-loop 3rd LLM call (LOW).** `write_validated_message` (engine:243) can do
  write → repair-write → validate = 3 serial generations worst case. Cap or template.
- **#9 Prompt caching (LOW).** Large static system prompts (PLANNER_PROMPT,
  SHARED_WRITER_HEADER) re-sent every call with no `cache_control`. Enable provider caching.
- **#10 Checkpointer round trips (LOW).** `get_state` before every invoke + writes after
  each superstep. Revisit only if DB latency shows up in profiling.

- **Priority:** #2/#3 High, #4/#5/#6 Medium, #7-#10 Low. Sequence after fix #1 ships.

## Planner nondeterminism: appointment/view intents fall into `clarify`

- **Symptom:** The same user message routes differently across runs. In the live
  19-row eval suite, ~12 of 19 rows ended at `send_clarify` instead of proceeding to
  draft/schedule/book/view, so their trajectories never complete. Booking and viewing
  are the affected demo-critical flows.
- **Measured flake rate:** "show my upcoming appointments" (row `oh-trajectory-017`)
  routed to `view_appointments` only ~1 in 4-5 isolated runs; the rest went to
  `clarify`. Across two full-suite runs, individual rows flipped pass/fail with no
  input change (e.g. 017 = fail in run 1, full 1/1/1 pass in run 2).
- **Impact:** On stage Monday, "book…" / "show my appointments" may not trigger the
  intended flow on the first try. This is the single highest demo risk surfaced by the
  eval work. It is also why the live trajectory suite can't be a green gate yet
  (trajectory 2/19 even after the entry-node re-baseline).
- **Root cause (code):** intent is chosen by the LLM planner
  `plan_conversation_turn` (`src/conversation_engine.py:176`, `_model()` at `:147`,
  already `temperature=0.0`) via `with_structured_output(ConversationTurn)`. The prompt
  actively pushes toward clarify:
  - `src/conversation_engine.py:52` — "Generic booking requests with no appointment
    details go clarify, not appointment_book."
  - `src/conversation_engine.py:59` — "If confidence < 0.65, choose clarify."
  - `:197` / `:208` — on structured-output parse failure the turn defaults to
    `{"intent":"clarify","action":"clarify"}`.
  Temperature is already 0.0, so the variance is coming from the model backend
  (OpenRouter greedy decoding is not guaranteed) plus the low-confidence/generic rules
  firing inconsistently on clear requests. `coerce_turn` /
  `route_name_from_intent` (`src/conversation_policy.py:265-322`) then map the chosen
  intent to a node, so a `clarify` classification ends the flow at `send_clarify`.
- **Status:** Fix #1 SHIPPED (Provider_agent_alternative branch). Deterministic
  appointment-verb pre-routes added to `deterministic_turn_for_message`
  (`src/conversation_policy.py`). Routing-flake harness over the 6 appointment eval
  rows (N=10 each, real planner): **0/6 flaky** — `oh-trajectory-017`
  ("show my upcoming appointments") now `view_appointments` 10/10 (was ~1-in-4-5),
  and rows 001/016 ("book a filling/cleaning with Jonas Salk", no literal "appointment"
  noun) now `draft_appointment_details` 10/10 via the clinical-token path. #2-#4 remain.
- **Proposed fixes (in order of leverage for the demo):**
  1. **[DONE] Deterministic keyword/regex pre-route before the LLM.** For high-signal verbs,
     bypass the planner: `show|see|list|view|check ... appointment(s)` →
     `appointment_view`; `book|schedule|make ... appointment` (+ date/clinical detail, or
     a clinical token even without the noun) → `appointment_book`; `reschedule|move|push`
     and `cancel|delete|drop ... appointment` → the respective intents. Falls through to
     `plan_conversation_turn` only when no keyword rule matches, and is gated to idle step
     so `awaiting_*` sub-flow replies still route through the model. Highest reliability,
     no model dependency for the demo phrasings.
  2. **Loosen the clarify bias.** Lower the `confidence < 0.65` gate, and don't force
     "generic booking → clarify" when an explicit appointment verb + target is present.
  3. **Pin determinism.** Add a fixed seed if the OpenRouter model supports it, or pick
     a model/provider with greedy decoding; add 2-3 few-shot examples for view/book in
     the planner prompt so borderline confidence resolves consistently.
  4. **Add a stability eval.** Run each canonical intent phrasing N times and report a
     route flake-rate metric, so regressions in routing determinism are caught. This
     also lets the trajectory suite become a real gate once flake rate ~0.
- **Priority:** High for the demo (fix #1 is small and self-contained); #2-#4 follow.
