# Verdict rules

The verdict follows Flagsmith's own statistics engine (`api/experimentation/stats.py`, `constants.py`) and the Statistics Glossary, so the skill never disagrees with the dashboard's banner without saying why.

Gates are checked in this order. The first one that fires decides.

1. **Status.** `completed` gives `ALREADY_COMPLETED`. Anything other than `running` or `paused` gives `NOT_RUNNING`.
2. **Results exist.** No computed payload gives `NO_RESULTS`.
3. **Sample ratio mismatch.** If the SRM p-value is below 0.001, the observed split is a one-in-a-thousand event under the configured split. Result: `BLOCKED_SRM`. It's only checked once there are at least 100 identities.

   Flagsmith computes this from the flag's environment default weights. Experiments created through the API (including Flagsmith's own agent setup prompt) keep their split in the experiment's rollout override instead, so Flagsmith returns `srm_p_value: null` for them. This was confirmed on a live Flagsmith Cloud experiment, and Flagsmith's source has a TODO for it. When that happens, the skill computes the p-value itself from `experiment_rollout` with the same chi-squared formula Flagsmith uses, and labels the result as computed by the skill.
4. **Data floor.** Flagsmith returns no inference for an arm with under 50 identities, or under 5 conversions for occurrence metrics. If no treatment has inference on the primary metric, the result is `COLLECTING_DATA`.
5. **Winner.** On the primary goal metric (the first metric with expected direction `increase` or `decrease`), a treatment wins when:
   - its chance of moving the metric the hoped-for way is at least 95%, and
   - its 95% credible interval clears zero in that direction.
6. **Guardrails.** For the chosen winner, every guardrail metric (`not_increase` / `not_decrease`) must have more than a 5% chance of having held. Otherwise the result is `BLOCKED_GUARDRAIL`.
7. **All losing.** If every treatment has at most a 5% chance to win, the result is `SHIP_CONTROL`.
8. Otherwise the result is `INCONCLUSIVE`.

## Direction handling

Flagsmith's `chance_to_win` is already flipped for metrics whose direction is `down` (lower is better): it reports P(lift < 0). The script undoes that flip to get the raw P(lift > 0), then applies the experiment's `expected_direction`. This keeps goals and guardrails consistent even when a metric's direction and the experiment's expectation differ.

## Quarantined identities

Identities served more than one variation are excluded from every arm. If they exceed 5% of enrolled identities, the verdict adds a warning. This usually means the app isn't identifying users before requesting the flag.

## Why Bayesian makes repeated checks safe

Flagsmith uses a Bayesian engine. Checking the results often doesn't inflate the error rate the way repeated p-value checks do, so re-running `verdict` daily is fine.
