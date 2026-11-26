---
name: flagsmith-experiment-closeout
description: Closes out a running Flagsmith Experimentation A/B test end to end. Reads the results, checks sample ratio mismatch, data floors and guardrails, gives a ship / keep control / keep running verdict, completes the experiment and rolls out the winner after explicit human approval, verifies the flag, plans the flag and code cleanup, and writes a decision record. Use when the user asks whether an experiment has a winner, wants to end or conclude an A/B test, ship or roll out a winning variation, or clean up an experiment flag in Flagsmith.
license: MIT
compatibility: Requires the Flagsmith CLI v2+ (logged in, or FLAGSMITH_API_KEY set) and Python 3.9+. Works with Flagsmith Cloud and self-hosted Flagsmith with Experimentation enabled.
metadata:
  author: calebjohndan-hoop
  version: "1.0.0"
  hackathon: "WeMakeDevs Toggle Wars, Track 04"
allowed-tools: Bash(python3:*) Bash(python:*) Bash(flagsmith:*) Read Edit
---

# Flagsmith experiment closeout

Setting up an experiment is covered by Flagsmith's own agent prompt. This skill does the other half: **decide, ship, clean up, and leave a record**. All Flagsmith calls go through the official CLI via `scripts/closeout.py`, which prints short summaries instead of raw JSON.

`S` below means `python3 <this skill's directory>/scripts/closeout.py`. On Windows, use `python` if `python3` isn't found.

## Safety rules (always)

1. Steps 1 to 4 are read-only. Run them freely.
2. Never run `ship` until the user has typed approval for the exact decision. Show them the token and wait.
3. Never invent or reuse a confirm token. Ask again for each experiment.
4. If the environment looks like production, `ship` refuses without `--production-ok`. Only add it after a **second** explicit yes that names the environment.
5. If the verdict doesn't support the user's choice, `ship` refuses unless they give a reason. Pass it in `--override-reason` in their words.
6. Never print or copy API keys. Never edit `flagsmith.json` secrets into the repo.
7. Cleanup (step 7) changes the live flag. Run each command only after the user confirms it.

## Workflow

### 1. Check the connection

```bash
flagsmith --version   # must be v2 or later
S context
```

If no environment is bound, ask the user which project and environment to use, then run `flagsmith init --project <p> --environment <env key>`. Tell the user if `looks_like_production` is true.

### 2. Find the experiment

```bash
S list                 # running experiments
S list --status all --q checkout
```

If more than one matches, ask which one. Don't guess.

### 3. Get the verdict

```bash
S verdict <id> --refresh
```

`--refresh` asks Flagsmith to recompute (allowed once per 5 minutes) and waits up to 90 seconds. If it was refreshed recently, the script uses the latest figures and says so.

Act on the `VERDICT` line:

| Verdict | What it means | What you do |
|---|---|---|
| `SHIP_VARIANT` | A variation is over 95% likely to beat control, its credible interval clears zero, and no guardrail broke | Propose shipping it (step 4) |
| `SHIP_CONTROL` | Every variation is under 5% likely to beat control | Propose keeping control |
| `INCONCLUSIVE` | No clear winner yet | Recommend letting it run. Only end it if the user insists, with an override reason |
| `COLLECTING_DATA` | An arm has under 50 identities or 5 conversions | Keep running. Nothing to decide |
| `BLOCKED_SRM` | Traffic didn't split as configured. The SRM line says whether Flagsmith or the skill computed it (Flagsmith leaves it empty for experiments set up through the API, so the skill computes it from the configured split) | Do not ship. Help debug: see [troubleshooting](references/troubleshooting.md) |
| `BLOCKED_GUARDRAIL` | The winner hurt a guardrail metric | Explain the trade-off. The user decides |
| `NO_RESULTS`, `NOT_RUNNING`, `ALREADY_COMPLETED` | Nothing to close | Say so and stop, or skip to step 6 for a completed one |

Explain the verdict in plain words: the lift, the credible interval, the chance to win, and any `WARNING` lines. For what the numbers mean, see [verdict rules](references/verdict-rules.md).

### 4. Ask for approval

Show a short decision card, then stop and wait:

```
Experiment #42 "Checkout button copy" on flag checkout_cta (Development)
Verdict: SHIP_VARIANT. 'urgent' lifts purchase +18.2% (CI +6.1% to +30.3%), 99.1% chance to win.
Shipping will: end the experiment (results freeze, can't restart), serve 'urgent' to 100% of users, unlock the flag.
To approve, reply: approve 42:variant:urgent
```

Token format: `<id>:variant:<key>`, `<id>:control`, or `<id>:disable`. The verdict never recommends `disable`, so it always needs an `--override-reason`.

### 5. Ship

Optionally show the exact API call first:

```bash
S ship 42 --decision variant --variant urgent --confirm "42:variant:urgent" --approved-by "<name>" --dry-run
```

Then run it without `--dry-run`. The script re-reads the results right before acting, calls `POST .../experiments/42/complete/`, and saves the flag's before-state to `.flagsmith-closeout/42.json` for rollback. If it prints `REFUSED`, relay the reason. Don't work around it.

### 6. Verify and record

```bash
S verify 42
S record 42 --out docs/experiments
```

`verify` reads the flag's live state in this environment and checks it matches the decision (winner at 100%, or every variation at 0% for control). It must print `OK`. `record` writes a markdown decision record (hypothesis, evidence table, who approved, rollback commands). Tell the user where it is and suggest committing it.

### 7. Plan the cleanup

```bash
S cleanup-plan 42 --repo <path to the app's source>
```

This lists every line of code that reads the flag or uses one of its variation keys as a quoted string (for example `"urgent"`), plus the CLI commands to delete the losing variations and archive the flag. Point `--repo` at the application code, not the whole workspace. It runs nothing. Then:

1. Show the user the code changes that hard-code the kept behaviour. Edit only after they confirm.
2. After that code is deployed, run the variation delete and archive commands one at a time, each with confirmation.

## Edge cases

- **Paused experiments** can be shipped the same way.
- **Several winning variations**: the script picks the highest chance to win, then the larger lift. Mention the runner-up.
- **Lower-is-better metrics** (for example latency or errors): the script handles direction. Read "chance it moved the right way", not raw lift sign.
- **Self-hosted Flagsmith**: set `FLAGSMITH_API_URL`, and the dashboard link follows it.
- **Flag commands need `--yes`** when run by an agent (no terminal to confirm in). The commands the script prints already include it.
- **CLI errors** print as `ERROR from Flagsmith CLI:`. Show the user the message and stop. Don't retry with a raw curl.

API endpoints and payloads used: [references/api.md](references/api.md).
