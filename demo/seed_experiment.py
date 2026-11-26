#!/usr/bin/env python3
"""Create a demo experiment so the closeout skill has something real to close.

Follows the steps in Flagsmith's own "Set up with a coding agent" prompt:
multivariate flag with a keyed variation, an occurrence metric, then an
experiment that gets started. Uses the Flagsmith CLI's credentials.

Run from a directory bound with `flagsmith init` (or pass --env-key):
    python demo/seed_experiment.py
    python demo/seed_experiment.py --flag checkout_cta_v2     # a fresh one for a second take
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

FS = os.environ.get("FLAGSMITH_BIN", "flagsmith")


def cli(*args, body=None, parse=True, check=True):
    proc = subprocess.run([FS, *args], input=json.dumps(body) if body is not None else None,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        if not check:
            return None
        sys.exit(f"flagsmith {' '.join(args[:3])} failed:\n{(proc.stderr or proc.stdout).strip()}")
    return json.loads(proc.stdout) if parse and proc.stdout.strip() else proc.stdout


def rows(data):
    return data.get("results", []) if isinstance(data, dict) else (data or [])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--env-key")
    p.add_argument("--flag", default="checkout_cta")
    p.add_argument("--event", default="purchase")
    p.add_argument("--name", default="Checkout button copy")
    a = p.parse_args()

    if shutil.which(FS) is None:
        sys.exit("Install the Flagsmith CLI v2+ first: https://docs.flagsmith.com/integrating-with-flagsmith/CLI")

    cfg = cli("config", "--json")
    env = a.env_key or (cfg.get("environment") or {}).get("value")
    if not env:
        sys.exit("No environment bound. Run `flagsmith init` or pass --env-key.")
    base = f"api/v1/environments/{env}/"

    if not rows(cli("api", base + "warehouse-connections/")):
        sys.exit("This environment has no warehouse connection, so experiments can't record events. Enable the "
                 "managed warehouse under Environment Settings > Warehouse first: "
                 "https://docs.flagsmith.com/experimentation/connect-a-warehouse")

    if cli("flag", "get", a.flag, "--json", check=False) is not None:
        sys.exit(f"A flag named '{a.flag}' already exists. Pick a new name for a fresh experiment, for example:\n"
                 f"  python demo/seed_experiment.py --flag {a.flag}_v2")

    print(f"1/5 Creating multivariate flag '{a.flag}'")
    cli("feature", "create", a.flag, "--value", "control", "--enabled",
        "--description", "Checkout button copy test (closeout skill demo)", parse=False)
    cli("feature", "variant", "add", a.flag, "--value", "Buy now, only 2 left", "--key", "urgent",
        "--weight", "0", parse=False)
    feat = cli("feature", "get", a.flag, "--json")
    variant_id = next(v["id"] for v in feat["variants"] if v.get("key") == "urgent")

    existing = next((m for m in rows(cli("api", base + "experiment-metrics/"))
                     if (m.get("definition") or {}).get("event") == a.event or m.get("name") == a.event), None)
    if existing:
        print(f"2/5 Reusing metric '{existing.get('name')}' on event '{a.event}'")
        metric = existing
    else:
        print(f"2/5 Creating metric on event '{a.event}'")
        metric = cli("api", base + "experiment-metrics/", "--input", "-", body={
            "name": a.event, "aggregation": "occurrence", "direction": "up",
            "definition": {"version": 1, "event": a.event}})

    print("3/5 Creating experiment (50/50 split, 100% rollout)")
    exp = cli("api", base + "experiments/", "--input", "-", body={
        "feature": feat["id"], "name": a.name,
        "hypothesis": "Urgent checkout copy increases purchases by at least 10% within 3 days.",
        "metrics": [{"metric": metric["id"], "expected_direction": "increase"}],
        "experiment_rollout": {"enabled": True, "rollout_percentage": 100,
                               "feature_state_value": {"type": "string", "value": "control"},
                               "multivariate_feature_state_values": [
                                   {"multivariate_feature_option": variant_id, "percentage_allocation": 50}]}})

    print(f"4/5 Starting experiment #{exp['id']}")
    started = cli("api", base + f"experiments/{exp['id']}/start/", "-X", "POST")
    print(f"5/5 Status: {started.get('status')}")

    print(f"\nDone. Experiment id: {exp['id']}. Now send traffic with your environment's SDK key:")
    if os.name == "nt":
        print('  $env:FLAGSMITH_ENVIRONMENT_KEY="<sdk key>"')
        print(f"  python demo\\simulate_traffic.py --flag {a.flag}")
    else:
        print(f"  FLAGSMITH_ENVIRONMENT_KEY=<sdk key> python demo/simulate_traffic.py --flag {a.flag}")


if __name__ == "__main__":
    main()
