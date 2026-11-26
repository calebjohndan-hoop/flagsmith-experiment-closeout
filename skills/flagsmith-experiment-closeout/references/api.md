# API reference used by this skill

All calls go through `flagsmith api <path>`, which applies the CLI's credentials. Paths are relative to the API root. `<env>` is the environment API key.

| Purpose | Method and path | Body |
|---|---|---|
| List experiments | `GET api/v1/environments/<env>/experiments/?q=<text>` | none |
| Experiment detail (includes `experiment_rollout`, the configured split) | `GET api/v1/environments/<env>/experiments/<id>/` | none |
| Request results recompute | `POST .../experiments/<id>/results/refresh/` | none (202; 429 if within 5 minutes) |
| Read results | `GET .../experiments/<id>/results/` | none |
| Request exposures recompute | `POST .../experiments/<id>/exposures/refresh/` | none |
| Read exposures | `GET .../experiments/<id>/exposures/` | none |
| Complete and roll out | `POST .../experiments/<id>/complete/` | `{"rollout_decision": "variant", "rollout_variant": <option id>}` or `{"rollout_decision": "control"}` or `{"rollout_decision": "disable"}` |

`complete` ends the experiment, freezes the results as final, sets the flag's environment default so the chosen variation gets 100% weight (or all variations 0% for control, or disables the flag), and removes the experiment's rollout override. A completed experiment can't be restarted.

## Results payload

`GET .../results/` returns `{"results": {"as_of", "last_error_at", "refresh_requested_at", "is_final", "payload"}}`, where `payload` is:

```json
{
  "srm_p_value": 0.42,
  "metrics": [
    {
      "metric_id": 7,
      "variants": {"control": {"n": 1200, "sum": 96, "sum_squares": 96},
                   "urgent":  {"n": 1185, "sum": 113, "sum_squares": 113}},
      "inference": {"urgent": {"lift": 0.19, "ci_low": 0.04, "ci_high": 0.34, "chance_to_win": 0.99}},
      "conversions_timeseries": {}
    }
  ],
  "exposures_timeseries": {}
}
```

`inference[key]` is `null` while that arm is below the data floor.

## CLI commands used

- `flagsmith config --json` for the bound project and environment
- `flagsmith flag get <flag> --json` for the live environment state (before-state and verification). Not `feature get`, which shows project-level default weights
- `flagsmith feature variant delete <flag> <key> --yes` (cleanup, user-confirmed)
- `flagsmith feature update <flag> --archive` (cleanup, user-confirmed)
- `flagsmith flag update <flag> --enable --weight <key>=<pct> --yes` (rollback, one command so weights never pass 100% midway)
