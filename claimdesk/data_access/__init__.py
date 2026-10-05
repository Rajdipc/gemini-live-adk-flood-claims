"""Data access layer: everything that talks to BigQuery lives in this package.

Keeping queries here (and out of the rules) means:
  * rules stay pure Python functions that are trivial to unit-test, and
  * you can find every SQL statement the app runs in one place.
"""
