# Judgment test report

2026-09-30 08:45 · model `sonnet` · 3 runs per scenario · 234 s total

| Scenario | Expected | Verdicts | Pass | Consistency | Cites metrics | Unverified numbers | Avg time |
|---|---|---|---|---|---|---|---|
| strong_consensus_valid | ACCEPTED | ACCEPTED x3 | 3/3 | 100% | 3/3 | - | 11.1 s |
| close_tradeoff_timber | ACCEPTED | ACCEPTED x3 | 3/3 | 100% | 3/3 | - | 10.3 s |
| close_tradeoff_glass | ACCEPTED | ACCEPTED x3 | 3/3 | 100% | 3/3 | - | 9.1 s |
| rule_min_window | MODIFIED | MODIFIED x3 | 3/3 | 100% | 3/3 | - | 14.1 s |
| rule_glass_shading | MODIFIED | MODIFIED x3 | 3/3 | 100% | 3/3 | - | 16.5 s |
| troll_extreme | REJECTED/MODIFIED | REJECTED x3 | 3/3 | 100% | 3/3 | - | 20.2 s |
| split_vote | MODIFIED | MODIFIED x3 | 3/3 | 100% | 3/3 | - | 14.8 s |
| unfixable | REJECTED | REJECTED x3 | 3/3 | 100% | 3/3 | - | 19.5 s |
| contradicts_earlier_rejection | REJECTED | REJECTED x3 | 3/3 | 100% | 3/3 | - | 21.2 s |
| contradicts_earlier_modification | MODIFIED | MODIFIED x3 | 3/3 | 100% | 3/3 | - | 14.6 s |

**Overall: 30/30 runs passed.**

## Rationales (first run of each scenario)

- **strong_consensus_valid** (ACCEPTED): The vote passes both hard rules and all four goals, and consensus is strong, so it is applied as voted. Daylight rises 67.5 → 70.7 (min 50) and cooling eases 52.3 → 51.8 (max 62). Cost rises 90.9 → 94.5 (max 100) and carbon rises 58.2 → 61.7 (max 90), both still within budget.
  - consistency: This is the first decision in the session, so there are no earlier decisions to compare against.
- **close_tradeoff_timber** (ACCEPTED): The vote passes both hard rules and no goal fails, so it is applied as voted. Cooling is 62.1 against a max of 62, which is marginal, so the participants' choice wins. Raising windows from 40% to 55% lifts daylight from 67.5 to 78.7, while cooling rises from 52.3 to 62.1.
  - consistency: This is the first decision in the session, so there are no earlier decisions to compare against.
- **close_tradeoff_glass** (ACCEPTED): Both hard rules pass (glass shading 0.425 vs min 0.35) and no goal fails. Cooling is 62.6 vs max 62, which is only marginal, so the voters' choice stands. Compared with the current design: daylight 67.5 → 76.7, cost 90.9 → 99.1 (max 100), carbon 58.2 → 76.9 (max 90).
  - consistency: No previous decisions in this session; this is the first ruling.
- **rule_min_window** (MODIFIED): The voted 20% windows break the 25% minimum rule, and daylight fails at 43.0 (min 50). Raising windows one step to 25% gives daylight 50.5, and cooling (42.4), cost (89.1) and carbon (54.6) still pass. Material, roof and canopy stay as voted.
  - consistency: No previous decisions in this session, so there is nothing to compare against.
- **rule_glass_shading** (MODIFIED): Glass with 60% windows missed the shading rule (0.349, min 0.35) and failed cooling (71.8, max 62). Deepening only the canopy 1.5 → 2.5 m gives shading 0.451 and cooling 65.0 (marginal). Glass, 60% windows and roof angle stay as voted; cost is marginal at 100.5 (max 100).
  - consistency: No previous decisions in this session; this is the first ruling.
- **troll_extreme** (REJECTED): The all-glass, 60% windows, no-shading design breaks the shading rule (0.12 vs 0.35 needed) and fails cooling (86.9 vs max 62). The best fix within the limits (2 parameters, 3 steps each) is glass, 45% windows and a 1.5 m canopy. It passes the rules but cooling is still 68.1, so no valid modification exists.
  - consistency: First round of the session, so there are no earlier decisions to compare with.
- **split_vote** (MODIFIED): Concrete failed the carbon goal (94.0, max 90). Material consensus was weak and timber was a close second, so we switched to timber: carbon 94.0 → 63.2. All strongly-backed slider values stay as voted, and every goal now passes (cost 99.8, max 100).
  - consistency: No previous decisions in this session; this is the first ruling.
- **unfixable** (REJECTED): The glass, 55% windows, flat roof, no canopy design fails the shading rule (0.12, needs 0.35) and cooling (83.1, max 62). The best fix within the limits (2 parameters, 3 steps each) reaches shading 0.349 and cooling 69.3, so the rule and the cooling goal still fail. The current timber design stays (cooling 52.3, shading 0.349).
  - consistency: No previous decisions in this session; this is the first ruling.
- **contradicts_earlier_rejection** (REJECTED): The all-glass, no-shading design fails the shading rule (0.12, min 0.35) and overheats (cooling 86.9, max 62). The closest valid fixes within the limits still overheat (cooling 68.1 to 71.9), so the current timber design stays (cooling 52.3, daylight 67.5).
  - consistency: Same outcome as round 1: the same glass, 60%, flat, no-canopy proposal fails the same rule and goal, and no fix within the limits exists.
- **contradicts_earlier_modification** (MODIFIED): Voted 20% windows break the 25% minimum rule and daylight fails (42.7, min 50). One step to 25% fixes it: daylight 50.1, cooling 41.7 (max 62), cost 90.1, carbon 55.4. Timber, roof 20° and canopy 1.5 m stay as voted.
  - consistency: Same fix as round 1 (windows raised to 25% for the hard rule and daylight), but the roof now follows the 20° vote, which passes all goals.
