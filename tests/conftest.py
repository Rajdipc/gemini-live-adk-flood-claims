"""Shared pytest setup: make sure unit tests never touch real Google Cloud.

* A fake project id is set so settings validation passes.
* The in-memory storage backend is forced (no Firestore / GCS).
* Cloud Trace export is disabled.
* FEMA guidance search (Vertex AI Search) is disabled. A deployer's local
  ``.env`` often turns it on, and settings read ``.env``; forcing it off here
  keeps the unit tests' expected tool list stable on every machine.
"""

import os

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "unit-test-project")
os.environ["CLAIMDESK_STORAGE_BACKEND"] = "memory"
os.environ["CLAIMDESK_ENABLE_CLOUD_TRACE"] = "false"
os.environ["CLAIMDESK_ENABLE_GUIDANCE_SEARCH"] = "false"
os.environ.setdefault("CLAIMDESK_GCS_BUCKET", "unit-test-bucket")
