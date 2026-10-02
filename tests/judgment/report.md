# Judgment test report

2026-10-01 18:23 · model `sonnet` · 2 runs per scenario · 42 s total

| Scenario | Expected | Verdicts | Pass | Consistency | Cites metrics | Unverified numbers | Avg time |
|---|---|---|---|---|---|---|---|
| troll_extreme | REJECTED/MODIFIED | REJECTED x2 | 2/2 | 100% | 2/2 | - | 21.4 s |
| contradicts_earlier_rejection | REJECTED | REJECTED x2 | 2/2 | 100% | 2/2 | - | 16.6 s |

**Overall: 4/4 runs passed.**

## Rationales (first run of each scenario)

- **troll_extreme** (REJECTED): The vote breaks the 50% minimum glass rule (40%), and daylight 53.6, cost 98.6, carbon 98.3 and heritage 27.6 all fail. Fixing the glass and lanterns still leaves carbon 84.4 (max 70) and heritage 36.7 (min 70) failing. The 87.5% aluminium vote is strong consensus and can't be changed, so no valid modification exists and the current design stays.
  - consistency: No earlier decisions this session; this is round 1.
- **contradicts_earlier_rejection** (REJECTED): Aluminium on half the SE wall with 2 lanterns fails daylight (65.3, min 70), carbon (84.4, max 70) and heritage (36.7, min 70). Opening more lanterns (8) fixes daylight and carbon (74.3, 65.4), but heritage stays at 49.9. Aluminium has strong consensus (82.5%) and can't be changed, and no valid design exists within the 2-parameter limit, so the building stays as it is.
  - consistency: Same outcome as round 1: the aluminium infill drives the heritage failure, and no change within the limits can fix it.
