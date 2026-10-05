"""ClaimDesk evaluation suite (see docs/evals.md for the beginner guide).

Run the tools as modules from the project root, e.g.::

    uv run --no-sync python -m evals.build_eval_cases --offline evals/seeds/sample_eval_seed_claims.json
    uv run --no-sync python -m evals.generate_traces --dataset evals/datasets/pipeline_core.json

Files
    custom_metrics.py      deterministic graders (stdlib only; run inside agents-cli)
    expectations.py        prompt format, BigQuery world stubs, answer-key derivation
    build_eval_cases.py    BigQuery eval_seed_claims -> synthetic conversations + labels
    generate_traces.py     run the ADK workflow in-process over a dataset -> traces
    export_live_traces.py  BigQuery conversation_traces -> multi-turn live traces
    run_vertex_eval.py     grade traces with the Vertex AI Gen AI evaluation service
                           and store results in BigQuery / GCS
    eval_config.yaml       metrics for pipeline traces (agents-cli eval grade --config)
    eval_config_live.yaml  metrics for live voice-agent traces
    datasets/              hand-checked pipeline datasets (agents-cli EvaluationDataset JSON)
    seeds/                 a small local seed file so the builder runs without GCP
"""
