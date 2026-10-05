"""Shared pytest setup: make sure unit tests never touch real Google Cloud.

* A fake project id is set so settings validation passes.
* The in-memory storage backend is forced (no Firestore / GCS).
* Cloud Trace export is disabled.
"""

import os

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "unit-test-project")
os.environ["CLAIMDESK_STORAGE_BACKEND"] = "memory"
os.environ["CLAIMDESK_ENABLE_CLOUD_TRACE"] = "false"
os.environ.setdefault("CLAIMDESK_GCS_BUCKET", "unit-test-bucket")
