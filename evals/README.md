# OneHealth LangSmith Evaluators

Deterministic evaluators live in `onehealth_evaluators.py`:

- `trajectory_match_evaluator`: compares actual node trajectory to `expected_trajectory`
- `expected_state_match_evaluator`: checks `expected_state` as a subset of final graph state
- `user_experience_assertions_evaluator`: checks declared `ux_assertions` against captured outbound messages, trajectory, and final state

Run a LangSmith experiment against the uploaded dataset:

```bash
uv run python -m evals.run_onehealth_langsmith_eval \
  --dataset "OneHealth Trajectory Dataset" \
  --experiment-prefix "onehealth-trajectory" \
  --allow-side-effects
```

Run a local smoke check from `dataset.json`:

```bash
uv run python -m evals.run_onehealth_langsmith_eval \
  --local-json dataset.json \
  --limit 1 \
  --allow-side-effects
```

The runner uses the real OneHealth graph. It captures outbound Telegram messages
for evaluation instead of sending them, but live runs may still write Supabase
rows and book NexHealth appointments.

UX assertions live under `expected_result.ux_assertions` in dataset rows:

```json
{
  "ux_assertions": {
    "no_slot_recovery": true,
    "privacy_copy": true,
    "profile_privacy_copy": true,
    "cancel_flow": true,
    "invalid_choice_retry": true,
    "telegram_buttons": true,
    "view_lists_appointments": true,
    "booking_deduplicated": true
  }
}
```

- `view_lists_appointments`: passes when the trajectory hits `view_appointments`
  and either `viewed_appointments` is non-empty or the message lists an upcoming
  appointment. Used by `oh-trajectory-017`.
- `booking_deduplicated`: passes when `book_appointment` ran, an
  `appointment_booking_status` is set, and `book_appointment_result` is unset — the
  signature of a dedup that skipped the NexHealth POST. Used by `oh-trajectory-018`.

## Coverage notes

- **Viewing appointments** (`oh-trajectory-017`) is covered live, happy-path only
  (one seeded patient can't produce the empty-list or no-patient-record branches).
  Those branches are deferred to the mocked offline runner (see `docs/TODOS.md`).
- **Booking idempotency** is deterministically guaranteed by
  `tests/test_graph_regressions.py` (`test_book_appointment_dedups_existing_booked_reservation`,
  `test_book_appointment_retries_after_failed_reservation`). The `oh-trajectory-018`
  (duplicate) and `oh-trajectory-019` (retry-after-failure) dataset rows are the
  integration counterparts and form an **ordered pair**: they need the booking key
  already present in the Postgres `appointments` ledger (as `booked` / `failed`
  respectively) to score live, so run them after a happy booking or under the mocked
  runner. They are not part of the standard live smoke.
- Existing trajectory rows begin with `start_thread`; the live graph actually starts
  `receive_message -> ensure_user -> plan_next_turn`. `oh-trajectory-017` is pinned to
  the real order. Re-baselining the older rows is tracked in `docs/TODOS.md`.

Upload deterministic evaluators to the dataset:

```bash
langsmith evaluator upload evals/onehealth_evaluators.py \
  --name "OneHealth Trajectory Match" \
  --function trajectory_match_evaluator \
  --dataset "OneHealth Trajectory Dataset" \
  --replace \
  --api-key "$LANGSMITH_API_KEY"

langsmith evaluator upload evals/onehealth_evaluators.py \
  --name "OneHealth Expected State Match" \
  --function expected_state_match_evaluator \
  --dataset "OneHealth Trajectory Dataset" \
  --replace \
  --api-key "$LANGSMITH_API_KEY"

langsmith evaluator upload evals/onehealth_evaluators.py \
  --name "OneHealth UX Assertions" \
  --function user_experience_assertions_evaluator \
  --dataset "OneHealth Trajectory Dataset" \
  --replace \
  --api-key "$LANGSMITH_API_KEY"
```
