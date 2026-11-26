# Demo video script (about 3 minutes)

Written to be read aloud. Screen actions are in brackets.

Before recording, do a full rehearsal on a throwaway flag. Ship, verify and record have only run against the test fake so far, so the first live ship should not be on camera.

    python demo\seed_experiment.py --flag checkout_cta_rehearsal
    python demo\simulate_traffic.py --flag checkout_cta_rehearsal --users 1500
    (wait two minutes, then in Claude Code: "close out the checkout_cta_rehearsal experiment", approve it, and let it run verify, record and cleanup-plan)

If all of that works, seed the experiment you will record. Experiment 634 stays untouched as your backup.

    python demo\seed_experiment.py --flag checkout_cta_v2
    python demo\simulate_traffic.py --flag checkout_cta_v2 --users 1500

Check the verdict says SHIP_VARIANT before you hit record. If it says INCONCLUSIVE, run the simulator again with the same flag to add more users, wait two minutes, and check again. Read the chance to win off the screen in section 3; it changes from run to run.

## 1. The problem (20 seconds)

[Show the Flagsmith experiment page with results]

Starting an A/B test is easy. Ending one well is where teams slip. People ship on numbers that aren't conclusive yet, miss a broken traffic split, and leave the old flag in the code for months. My skill handles that last mile.

## 2. What it is (15 seconds)

[Show the repo and SKILL.md]

This is flagsmith experiment closeout, an Agent Skill built on the Flagsmith CLI. It reads the results, decides, ships the winner only after I approve, checks the live flag, plans the cleanup, and writes a decision record.

## 3. The skill in action (60 seconds)

[Claude Code: type "close out the checkout experiment"]

I just ask the agent to close it out. It checks which environment I'm in, finds the experiment, and refreshes the results.

[Point at the VERDICT lines, then at the dashboard in another tab]

It checks the traffic split, the data floor, whether the credible interval clears zero, and the guardrails. Urgent wins with a [read the chance off the screen] percent chance to win, and these numbers match the Flagsmith dashboard exactly.

[Point at the SRM line]

Here's something I found while building it. For experiments created through the API, Flagsmith skips its own traffic split check. So my skill runs the same test itself, with the same formula, and tells you which one it used.

## 4. Safety (35 seconds)

[Agent shows the decision card and stops]

Now it stops and waits. It won't touch the flag unless I type the exact approval.

[Type a plain "yes" and show the refusal. Then show the production refusal from the tests or a dry run]

If I just say yes, it refuses. If I try to ship against the data, it asks for a reason. On a production environment it needs a second yes. None of these refused attempts make a single API call, and the tests prove it.

[Type: approve <id>:variant:urgent]

With the real approval, it ends the experiment through Flagsmith's complete endpoint and serves urgent to everyone.

## 5. Verify, record, clean up (30 seconds)

[Show verify printing OK, then the decision record, then cleanup plan with repo set to demo]

It reads the live flag and confirms urgent is at one hundred percent. It writes a decision record with the evidence, who approved it, and the exact rollback command. Then it lists every line of code that still reads the flag, so the flag doesn't turn into tech debt.

## 6. Close (10 seconds)

Flagsmith's own prompt starts your experiment. This skill finishes it safely. Thanks for watching.
