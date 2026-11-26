#!/usr/bin/env python3
"""A stand-in for the Flagsmith CLI used by the tests.

Serves canned JSON for the endpoints the skill calls and logs every write to
$FAKE_LOG, so tests can prove that refused actions never reach the API.

The default "winner" scenario uses the real figures from a live Flagsmith Cloud
experiment (#634: 743 control / 757 urgent, 55 / 105 purchases), including the
fact that Flagsmith returned srm_p_value = null for it.

Scenarios ($FAKE_SCENARIO): winner | srm_flagsmith | broken_split | env_prod |
completed_variant | completed_bad | throttled_exposures
"""
import json
import os
import sys

ENV = "envKEY123"
SCEN = "winner"
LOG = None


def experiment():
    status = "completed" if SCEN.startswith("completed") else "running"
    return {
        "id": 42, "name": "Checkout button copy", "status": status,
        "hypothesis": "Urgent checkout copy increases purchases by at least 10% → within 3 days.",
        "feature": {"id": 9, "name": "checkout_cta", "multivariate_options": [{"id": 501, "key": "urgent"}]},
        "metrics": [{"metric": 7, "metric_name": "purchase", "direction": "up", "expected_direction": "increase"}],
        "experiment_rollout": None if status == "completed" else {
            "enabled": True, "rollout_percentage": 100.0,
            "feature_state_value": {"type": "string", "value": "control"},
            "multivariate_feature_state_values": [{"multivariate_feature_option": 501, "percentage_allocation": 50}],
        },
        "rollout_decision": "variant" if status == "completed" else None,
        "rollout_variant": 501 if status == "completed" else None,
        "ended_at": "2026-11-28T10:00:00Z" if status == "completed" else None,
    }


def results_payload():
    if SCEN == "broken_split":
        variants = {"control": {"n": 900, "sum": 70, "sum_squares": 70}, "urgent": {"n": 600, "sum": 80, "sum_squares": 80}}
        inference = {"urgent": {"lift": 0.71, "ci_low": 0.2, "ci_high": 1.2, "chance_to_win": 0.999}}
    else:
        variants = {"control": {"n": 743, "sum": 55, "sum_squares": 55}, "urgent": {"n": 757, "sum": 105, "sum_squares": 105}}
        inference = {"urgent": {"lift": 0.874, "ci_low": 0.292, "ci_high": 1.455, "chance_to_win": 0.998}}
    return {"srm_p_value": 0.00001 if SCEN == "srm_flagsmith" else None,
            "metrics": [{"metric_id": 7, "variants": variants, "inference": inference}]}


def flag_state():
    if SCEN == "completed_variant":
        weight = 100
    elif SCEN == "completed_bad":
        weight = 40
    else:
        weight = 0
    return {"feature": "checkout_cta", "enabled": True, "value": "control",
            "variants": [{"id": 501, "key": "urgent", "value": "Buy now, only 2 left", "weight": weight}]}


def log(entry):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def main(argv):
    global SCEN, LOG
    SCEN = os.environ.get("FAKE_SCENARIO", "winner")  # re-read each call so tests can switch scenarios
    LOG = os.environ.get("FAKE_LOG")
    if argv[:1] == ["config"]:
        env_name = "Production" if SCEN == "env_prod" else "Development"
        print(json.dumps({"project": {"value": 12, "name": "Demo"}, "environment": {"value": ENV, "name": env_name},
                          "apiUrl": {"value": "https://api.flagsmith.com/api/v1/"}}))
        return 0
    if argv[:2] == ["flag", "get"]:
        print(json.dumps(flag_state()))
        return 0
    if argv[:1] == ["api"]:
        path = argv[1]
        method = argv[argv.index("-X") + 1] if "-X" in argv else "GET"
        body = json.loads(sys.stdin.read()) if "--input" in argv else None
        if method != "GET":
            log({"method": method, "path": path, "body": body})
        base = f"api/v1/environments/{ENV}/experiments/"
        if method == "GET" and (path == base or path.startswith(base + "?q=")):
            log({"method": "GET", "path": path})
            print(json.dumps({"count": 1, "results": [experiment()]})); return 0
        if method == "GET" and path == f"{base}42/":
            print(json.dumps(experiment())); return 0
        if method == "GET" and path == f"{base}42/results/":
            print(json.dumps({"results": {"as_of": "2026-11-26T12:29:06Z", "payload": results_payload()}})); return 0
        if method == "GET" and path == f"{base}42/exposures/":
            print(json.dumps({"exposures": {"payload": {"excluded_identities": 0}}})); return 0
        if method == "POST" and path == f"{base}42/complete/":
            done = dict(experiment(), status="completed", rollout_decision=body["rollout_decision"],
                        rollout_variant=body.get("rollout_variant"))
            print(json.dumps(done)); return 0
        if method == "POST" and path == f"{base}42/exposures/refresh/" and SCEN == "throttled_exposures":
            print("HTTP 429: A refresh was requested recently. Try again later.", file=sys.stderr); return 1
        if method == "POST" and path.endswith("refresh/"):
            return 0
        print(f"404 {method} {path}", file=sys.stderr)
        return 1
    print(f"unsupported fake command {argv}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
