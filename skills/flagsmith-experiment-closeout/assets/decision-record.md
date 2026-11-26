# Experiment decision: {name}

| | |
|---|---|
| Experiment | #{exp_id} |
| Flag | `{flag}` |
| Environment | {environment} |
| Decision | **{decision}** |
| Approved by | {approved_by} |
| Shipped at (UTC) | {shipped_at} |
| Results as of | {as_of} |
| Dashboard | {dashboard} |

## Hypothesis

{hypothesis}

## Verdict

**{verdict_code}**: {verdict_summary}

Override: {override}

## Evidence

Identities enrolled: {identities}. Excluded (saw more than one variation): {excluded}. Sample ratio mismatch p-value: {srm}.

| Metric | Role | Variation | Identities | Lift vs control | 95% credible interval | Chance it moved the right way |
|---|---|---|---|---|---|---|
{metric_rows}

## Warnings

{warnings}

## Rollback

{rollback}

The experiment itself can't be restarted once completed. To test again, create a new experiment.
