# Worked examples: classification

Each example gives the key normalized facts and the correct
ClaimClassification values as "field = value" lines.

## Example 1: bayou flood, complete facts, no safety issue

Facts: water_entry_description = Buffalo Bayou rose and water came in through the front door; water_depth_inches = 10; estimated_loss_usd = 18000; all required fields present; no hazards.

- claim_type = home_flood
- water_source = surface_flood
- severity = medium (documents still to collect, moderate loss)

## Example 2: sump pump, dry street

Facts: water_entry_description = sump pump stopped overnight and the basement filled; neighbours were fine and the street was dry.

- claim_type = internal_water
- water_source = sump_pump_failure
- water_source_rationale = pump failure with no general flood outside

## Example 3: burst pipe described as a flood

Facts: loss_description = my kitchen flooded; water_entry_description = pipe under the sink burst.

- claim_type = internal_water
- water_source = internal_plumbing

## Example 4: sewer backup during a neighbourhood flood

Facts: water_entry_description = street was under two feet of water and the basement floor drain started pouring water.

- claim_type = unclear
- water_source = unknown
- water_source_rationale = both a general flood outside and a drain backup are described; the adjuster must determine the direct cause

## Example 5: hurricane roof damage plus rising water

Facts: water_entry_description = roof shingles blew off and rain came into the attic, and later storm surge came in on the ground floor.

- claim_type = unclear
- water_source = unknown
- water_source_rationale = two perils: wind-driven rain through the roof (not flood) and storm surge (flood); both recorded for the adjuster

## Example 6: electrical hazard

Facts: safety_facts = electrical present (panel under water, buzzing).

- severity = urgent (regardless of claim type)

## Example 7: car only

Facts: loss_description = my car was flooded in the parking lot; no damage to the home.

- claim_type = out_of_scope
- water_source = unknown

## Example 8: burn-scar mudflow

Facts: water_entry_description = heavy rain on the burn scar above us sent mud and water through the back of the house.

- claim_type = home_flood
- water_source = surface_flood (mudflow is part of the flood definition)
