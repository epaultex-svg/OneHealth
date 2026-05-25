# OneHealth LangSmith Evaluators

Deterministic evaluators live in `onehealth_evaluators.py`:

- `trajectory_match_evaluator`: compares actual node trajectory to `expected_trajectory`
- `expected_state_match_evaluator`: checks `expected_state` as a subset of final graph state

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

The runner uses the real OneHealth graph. Live runs may send Telegram messages,
write Supabase rows, and book NexHealth appointments.

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
```

