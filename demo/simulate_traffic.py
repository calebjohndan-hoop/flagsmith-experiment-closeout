#!/usr/bin/env python3
"""Send simulated users through the demo experiment.

Each simulated user gets the experiment flag (recording an exposure) and
converts with a probability that depends on the variation they saw. The page
allows simulated traffic for hackathon demos; in a real app these calls sit in
your checkout code.

    pip install "flagsmith>=6.3.0"
    $env:FLAGSMITH_ENVIRONMENT_KEY="..."   (Windows PowerShell)
    export FLAGSMITH_ENVIRONMENT_KEY=...    (macOS/Linux)
    python demo/simulate_traffic.py --users 1500
"""
import argparse
import os
import random
import sys
import uuid

try:
    from flagsmith import Flagsmith
except ImportError:
    sys.exit('Install the SDK first: pip install "flagsmith>=6.3.0"')


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--flag", default="checkout_cta")
    p.add_argument("--event", default="purchase")
    p.add_argument("--users", type=int, default=1500)
    p.add_argument("--control-rate", type=float, default=0.08)
    p.add_argument("--urgent-rate", type=float, default=0.12)
    p.add_argument("--seed", type=int)
    a = p.parse_args()

    key = os.environ.get("FLAGSMITH_ENVIRONMENT_KEY")
    if not key:
        sys.exit("Set FLAGSMITH_ENVIRONMENT_KEY to the environment's SDK key.")
    rng = random.Random(a.seed)
    rates = {"control": a.control_rate, "urgent": a.urgent_rate}

    fs = Flagsmith(environment_key=key, enable_events=True)
    seen = {"control": 0, "urgent": 0, "other": 0}
    bought = {"control": 0, "urgent": 0}
    run = uuid.uuid4().hex[:6]
    for i in range(a.users):
        uid = f"demo-{run}-{i}"
        flag = fs.get_experiment_flag(a.flag, identifier=uid)  # records the exposure
        variant = getattr(flag, "variant", None) or "other"
        seen[variant if variant in seen else "other"] += 1
        if variant in rates and rng.random() < rates[variant]:
            fs.track_event(a.event, identifier=uid)  # same identifier as the exposure
            bought[variant] += 1
        if (i + 1) % 250 == 0:
            fs.flush_events()
            print(f"  {i + 1}/{a.users} users sent")
    fs.close()

    print(f"Exposures: {seen}")
    print(f"Purchases: {bought}")
    if seen["other"] == a.users:
        print("No user was enrolled. Is the experiment started and the variation keyed?")
    print("Wait about two minutes, then ask your agent to close out the experiment, or run:")
    print("  python skills/flagsmith-experiment-closeout/scripts/closeout.py verdict <experiment id> --refresh")


if __name__ == "__main__":
    main()
