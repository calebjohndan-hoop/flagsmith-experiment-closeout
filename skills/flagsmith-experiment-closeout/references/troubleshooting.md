# Troubleshooting

## BLOCKED_SRM (sample ratio mismatch)

Traffic didn't split the way the experiment was configured. Check, in this order:

1. **One variation crashes or errors**, so its users never record an exposure. Look for errors that only happen in one branch.
2. **Redirects or early returns** that skip the `getExperimentFlag` / `get_experiment_flag` call for some users.
3. **Flicker**: the user is identified after the flag is requested, so they get bucketed twice. Look at the excluded-identities count.
4. **Dropped events**: short-lived processes that exit before flushing (`flushEvents()`, `close()`, `max_buffer_items=1` in Python).

Don't ship on an SRM result. Fix the cause, then start a new experiment.

## COLLECTING_DATA that never ends

- A variation has no key, so it records no exposures. Check `flagsmith feature get <flag> --json`.
- The conversion event name doesn't exactly match the metric's event (case-sensitive).
- Exposures and conversions use different identifiers.
- Self-hosted: the SDK still sends events to Flagsmith Cloud. Set the events URL to your instance's `/api/`.

## "SRM: not checked"

The skill needs at least 100 enrolled identities and the experiment's configured split. A completed experiment no longer has a rollout split, so its SRM can't be recomputed afterwards. Check SRM before shipping.

## Refresh refused (429)

Results can be recomputed at most once every 5 minutes. The script falls back to the latest figures and says so.

## REFUSED messages from `ship`

These are intentional. Relay them to the user. The fix is always a human decision (a new token, an override reason, or a second yes for production), never a workaround.
