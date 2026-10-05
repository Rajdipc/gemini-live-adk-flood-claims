# Eval datasets

Each file is an agents-cli / Vertex AI `EvaluationDataset`:
`{"eval_cases": [...]}` and nothing else at the top level. The SDK rejects any
other top-level key, so descriptions go in this README.

| File | Cases | What it covers |
|---|---|---|
| `pipeline_core.json` | 10 | Hand-checked NFIP-style intake scenarios (CO/TX/FL/LA/NC). One case per water source (surface flood, sump pump, sewer backup, internal plumbing, seepage, wind-driven rain), plus emergency safety, late report, high loss without evidence, and policy mismatch. Every routing outcome appears at least once. |
| `pipeline_edge.json` | 6 | Edge cases: missing facts, unclear water source, out of scope (car), a prompt-injection attempt, "am I covered?" (the agent must not promise coverage), and a corrected loss date. |

Each case has:

- **Standard fields:** `eval_case_id` and `prompt`. The prompt uses the same wrapper as `run_intake_pipeline`.
- **Extra fields** (passed through to metrics):
  - `expected` is the answer key.
  - `world` holds the stubbed BigQuery answers.
  - `session_state` holds server evidence captures.
  - `reference_time` is when the conversation takes place.
  - `golden_outputs` are the ideal LLM outputs, used only to derive and check `expected`.
  - `tags` and `source` are labels.

See `evals/expectations.py` for the full format. `tests/test_evals_offline.py` re-derives every `expected` block from the real rule code, so labels can't drift.

To add a case, see "Add a case from a bad production conversation" in `docs/evals.md`.
