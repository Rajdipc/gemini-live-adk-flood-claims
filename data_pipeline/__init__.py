"""One-time data loading for ClaimDesk (run from your laptop, not Cloud Run).

WHAT LIVES HERE
    fetch_openfema.py     Step 1 - download FEMA NFIP v3 policies & claims
                          (OpenFEMA API) to ``data/raw/`` and, optionally,
                          upload them to your Cloud Storage bucket.
    load_to_bigquery.py   Step 2 - create the BigQuery dataset, load the raw
                          files into staging tables, then run ``sql/*.sql``.
    nfip_codes.py         FEMA code tables (deductible codes, cause of damage,
                          non-payment reasons...) and the rules used to
                          generate fictional policy numbers / names. The SQL
                          files contain the same tables; unit tests check that
                          both copies agree.
    schemas/*.json        Explicit BigQuery schemas for the raw staging tables
                          (used by the Python loader AND the ``bq load`` CLI).
    sql/*.sql             The transformations that build the tables the app
                          queries (``policy_registry``, ``loss_benchmarks``...).

The full beginner walkthrough is in ``docs/data_loading.md``.

Attribution: This product uses the FEMA OpenFEMA API, but is not endorsed by
FEMA. The Federal Government or FEMA cannot vouch for the data or analyses
derived from these data after the data have been retrieved from the Agency's
website(s).
"""
