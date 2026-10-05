Classify this normalized claim for a residential flood intake desk.

Normalized claim facts:
{claim_facts}

Required-field check:
{field_check}

claim_type (choose exactly one)
- home_flood: water damage to a home caused by rising surface water from outside (river, stream, lake or bayou overflow; accumulated rainfall that flowed in from outside; storm surge; flash flood; mudflow).
- internal_water: water damage that started inside the building or came up through the plumbing (burst pipe, appliance leak, sump pump failure, sewer or floor-drain backup, seepage through walls, rain through the roof).
- out_of_scope: not water damage to a home (auto, theft, travel, medical, liability, anything else).
- unclear: not enough information yet to tell.

water_source (choose exactly one)
- surface_flood, sump_pump_failure, sewer_or_drain_backup, internal_plumbing, seepage, roof_or_wind_driven_rain, unknown.
- Base it on water_entry_description first. If the claimant mentions both an outside flood and an inside source, choose unknown and explain why in water_source_rationale.

severity rubric
- low: complete facts, small loss, no safety issue.
- medium: missing documents or moderate complexity.
- high: large estimated loss, deep water inside the home, missing core facts, or complex cause.
- urgent: injury, unsafe or uninhabitable home, electrical or gas hazard, or time-critical mitigation.

Return only the structured ClaimClassification. This is classification, not a coverage decision.
