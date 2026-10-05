"""Deterministic business rules.

WHY ARE RULES PLAIN PYTHON AND NOT LLM PROMPTS?
    In insurance, routing decisions must be *explainable and repeatable*:
    the same facts must always give the same answer, and an auditor must be
    able to read why. So the LLM only does what LLMs are good at (turning a
    messy conversation into structured facts, classifying) and these modules
    make every decision with ordinary, unit-tested code.

Module map
    required_fields.py  -> are the minimum intake facts present?
    water_source.py     -> surface flood (NFIP) or internal water (not NFIP)?
    evidence_rules.py   -> document catalog, checklist, first routing decision
    risk_signals.py     -> timing / high-loss / safety / SIU / NOAA signals
    packet_writer.py    -> the Markdown hand-off packet
"""
