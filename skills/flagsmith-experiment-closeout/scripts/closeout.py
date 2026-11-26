#!/usr/bin/env python3
"""flagsmith-experiment-closeout: read results, decide, ship the winner, clean up.

Every call to Flagsmith goes through the official Flagsmith CLI (`flagsmith api`),
so credentials, self-hosted URLs and OIDC all work the way the CLI already does.
This script never reads, prints or stores an API key.

Read-only commands:   context, list, verdict, verify, cleanup-plan
Writing commands:     ship (gated by a confirm token), record (local file only)

Python 3.9+, standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verdict as V  # noqa: E402

STATE_DIR = Path(".flagsmith-closeout")
PROD_PATTERN = re.compile(r"\b(prod|production|live|main)\b", re.IGNORECASE)
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".next", ".claude", STATE_DIR.name}


class CliError(RuntimeError):
    pass


# ---------------------------------------------------------------- CLI access

def _bin() -> str:
    b = os.environ.get("FLAGSMITH_BIN", "flagsmith")
    if shutil.which(b) is None and not Path(b).exists():
        raise CliError("Flagsmith CLI not found. Install v2+: https://docs.flagsmith.com/integrating-with-flagsmith/CLI "
                       "(macOS/Linux: curl -fsSL https://get.flagsmith.com | sh; Windows PowerShell: "
                       "irm https://raw.githubusercontent.com/Flagsmith/flagsmith-cli/main/install.ps1 | iex)")
    return b


def run_cli(args: list[str], body: dict | None = None) -> str:
    cmd = [_bin(), *args]
    proc = subprocess.run(
        cmd,
        input=json.dumps(body) if body is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise CliError((proc.stderr or proc.stdout).strip() or f"flagsmith exited {proc.returncode}")
    return proc.stdout


def api(path: str, method: str = "GET", body: dict | None = None) -> object:
    args = ["api", path]
    if method != "GET":
        args += ["-X", method]
    if body is not None:
        args += ["--input", "-"]
    out = run_cli(args, body).strip()
    return json.loads(out) if out else None


def flag_state(name: str) -> dict | None:
    """The flag's live state in the bound environment: enabled, value and the
    environment's variant weights. Uses `flag get`, not `feature get`, because
    `feature get` reports project-level default weights."""
    try:
        out = json.loads(run_cli(["flag", "get", name, "--json"]))
        return out if isinstance(out, dict) else None
    except (CliError, json.JSONDecodeError):
        return None


def rollback_command(flag: str, before: dict | None) -> str | None:
    """One `flag update` that restores the saved state. A single command so the
    weights never sum past 100 between steps; --yes because agents have no TTY."""
    if not before:
        return None
    parts = ["flagsmith", "flag", "update", flag, "--enable" if before.get("enabled") else "--disable"]
    for var in before.get("variants") or []:
        ref = var.get("key") or var.get("id")
        weight = var.get("weight") or 0
        parts += ["--weight", f"{ref}={weight:g}"]
    return " ".join(parts + ["--yes"])


# ---------------------------------------------------------------- context

def get_context(env_key: str | None) -> dict:
    ctx: dict = {}
    try:
        cfg = json.loads(run_cli(["config", "--json"]))
    except (CliError, json.JSONDecodeError):
        cfg = {}

    def val(name: str):
        v = cfg.get(name)
        return v.get("value") if isinstance(v, dict) else v

    def nm(name: str):
        v = cfg.get(name)
        return v.get("name") if isinstance(v, dict) else None

    ctx["environment_key"] = env_key or val("environment")
    ctx["environment_name"] = nm("environment") or ""
    ctx["project"] = val("project")
    ctx["project_name"] = nm("project") or ""
    ctx["api_url"] = val("apiUrl") or "https://api.flagsmith.com/api/v1/"
    if not ctx["environment_key"]:
        raise CliError("No environment bound. Run `flagsmith init` in this repo or pass --env-key.")
    # Environment name lookup if config didn't carry it.
    if not ctx["environment_name"]:
        try:
            env = api(f"api/v1/environments/{ctx['environment_key']}/")
            ctx["environment_name"] = env.get("name", "") if isinstance(env, dict) else ""
            ctx["project"] = ctx["project"] or (env.get("project") if isinstance(env, dict) else None)
        except CliError:
            pass
    ctx["looks_like_production"] = bool(PROD_PATTERN.search(ctx["environment_name"] or ""))
    return ctx


def dashboard_link(ctx: dict, exp_id: int) -> str:
    api_url = ctx.get("api_url") or ""
    base = "https://app.flagsmith.com" if "api.flagsmith.com" in api_url else re.sub(r"/api/v1/?$", "", api_url.rstrip("/"))
    proj = ctx.get("project")
    if not str(proj or "").isdigit():
        return f"{base} (open Experiments in project {ctx.get('project_name') or proj})"
    return f"{base}/project/{proj}/environment/{ctx['environment_key']}/experiments/{exp_id}"


def exp_path(ctx: dict, exp_id: int | None = None, suffix: str = "") -> str:
    p = f"api/v1/environments/{ctx['environment_key']}/experiments/"
    if exp_id is not None:
        p += f"{exp_id}/"
    return p + suffix


# ---------------------------------------------------------------- commands

def cmd_context(a) -> int:
    ctx = get_context(a.env_key)
    print(json.dumps({k: ctx[k] for k in ("project", "project_name", "environment_name",
                                           "environment_key", "looks_like_production")}, indent=1))
    return 0


def cmd_list(a) -> int:
    ctx = get_context(a.env_key)
    data = api(exp_path(ctx) + ("?q=" + urllib.parse.quote(a.q) if a.q else ""))
    rows = (data.get("results", data) if isinstance(data, dict) else data) or []
    if a.status != "all":
        rows = [r for r in rows if r.get("status") == a.status]
    if not rows:
        print(f"No {a.status} experiments in environment '{ctx['environment_name']}'.")
        return 0
    for r in rows:
        feat = r.get("feature")
        flag = str((feat.get("name") or feat.get("id")) if isinstance(feat, dict) else feat)
        print(f"#{str(r.get('id')):<5} {str(r.get('status') or ''):<10} flag={flag:<24} "
              f"started={str(r.get('started_at') or '-')[:10]}  {r.get('name') or ''}")
    return 0


def fetch_all(ctx: dict, exp_id: int) -> tuple[dict, dict | None, dict | None]:
    exp = api(exp_path(ctx, exp_id))
    res = api(exp_path(ctx, exp_id, "results/")) or {}
    try:
        expo = api(exp_path(ctx, exp_id, "exposures/")) or {}
    except CliError:
        expo = {}
    return exp, res.get("results"), expo.get("exposures")


def refresh(ctx: dict, exp_id: int, wait: int) -> str:
    before = (api(exp_path(ctx, exp_id, "results/")) or {}).get("results") or {}
    notes = []
    results_requested = False
    for what in ("results", "exposures"):
        try:
            api(exp_path(ctx, exp_id, f"{what}/refresh/"), method="POST")
            results_requested = results_requested or what == "results"
        except CliError as e:
            msg = str(e).lower()
            if "429" in msg or "recently" in msg:
                notes.append(f"{what} refreshed in the last 5 minutes, using the latest figures")
            elif "final" in msg:
                notes.append(f"{what} are final")
            elif "before the experiment starts" in msg:
                notes.append("experiment hasn't started, nothing to refresh")
            else:
                raise
    if results_requested and wait:
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(min(10, max(1, deadline - time.time())))
            now = (api(exp_path(ctx, exp_id, "results/")) or {}).get("results") or {}
            if now.get("as_of") and now.get("as_of") != before.get("as_of"):
                return "; ".join(["results refreshed", *notes])
        notes.append(f"results not recomputed within {wait}s; showing the previous figures")
    return "; ".join(notes) or "refresh requested"


def cmd_verdict(a) -> int:
    ctx = get_context(a.env_key)
    note = refresh(ctx, a.id, a.wait) if a.refresh else None
    exp, res, expo = fetch_all(ctx, a.id)
    v = V.decide(exp, res, expo)
    if a.json:
        print(json.dumps(v.to_dict(), indent=1))
        return 0
    feat = exp.get("feature") or {}
    print(f"Experiment #{a.id} '{exp.get('name')}' on flag '{feat.get('name')}' "
          f"in '{ctx['environment_name']}' [{exp.get('status')}]")
    if exp.get("hypothesis"):
        print(f"Hypothesis: {exp['hypothesis']}")
    if res and res.get("as_of"):
        print(f"Results as of: {res['as_of']}" + (f" ({note})" if note else ""))
    print(V.render(v))
    print(f"Dashboard: {dashboard_link(ctx, a.id)}")
    return 0


def expected_token(exp_id: int, decision: str, variant_key: str | None) -> str:
    return f"{exp_id}:{decision}" + (f":{variant_key}" if decision == "variant" else "")


def cmd_ship(a) -> int:
    ctx = get_context(a.env_key)
    exp, res, expo = fetch_all(ctx, a.id)  # fresh read, never act on a stale verdict
    v = V.decide(exp, res, expo)
    ids = V._variant_ids(exp)

    if exp.get("status") not in ("running", "paused"):
        print(f"REFUSED: experiment status is '{exp.get('status')}'.")
        return 2
    if a.decision == "variant":
        if not a.variant or a.variant not in ids:
            print(f"REFUSED: --variant must be one of {sorted(ids)}.")
            return 2
    elif a.variant:
        print("REFUSED: --variant only applies to --decision variant.")
        return 2

    token = expected_token(a.id, a.decision, a.variant)
    if a.confirm != token:
        print(f"REFUSED: confirmation token mismatch. A human must approve with --confirm \"{token}\".")
        return 2

    agrees = (
        (v.code == V.SHIP_VARIANT and a.decision == "variant" and a.variant == v.recommended_variant_key)
        or (v.code == V.SHIP_CONTROL and a.decision == "control")
    )
    if not agrees and not a.override_reason:
        print(f"REFUSED: the data says {v.code}, which does not support '{token}'. "
              "If a human still wants this, pass --override-reason \"<why>\".")
        print(V.render(v))
        return 2
    if ctx["looks_like_production"] and not a.production_ok:
        print(f"REFUSED: environment '{ctx['environment_name']}' looks like production. "
              "Get a second, explicit human yes and pass --production-ok.")
        return 2

    feat = exp.get("feature") or {}
    before = flag_state(str(feat.get("name")))

    body = {"rollout_decision": a.decision}
    if a.decision == "variant":
        body["rollout_variant"] = ids[a.variant]

    if a.dry_run:
        print("DRY RUN, nothing changed. Would call:")
        print(f"  POST {exp_path(ctx, a.id, 'complete/')} {json.dumps(body)}")
        return 0

    after = api(exp_path(ctx, a.id, "complete/"), method="POST", body=body)

    STATE_DIR.mkdir(exist_ok=True)
    record = {
        "experiment_id": a.id,
        "experiment_name": exp.get("name"),
        "hypothesis": exp.get("hypothesis"),
        "flag": feat.get("name"),
        "environment": ctx["environment_name"],
        "decision": a.decision,
        "variant_key": a.variant,
        "approved_by": a.approved_by,
        "override_reason": a.override_reason,
        "shipped_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "verdict": v.to_dict(),
        "results_as_of": (res or {}).get("as_of"),
        "flag_before": before,
        "status_after": (after or {}).get("status"),
        "dashboard": dashboard_link(ctx, a.id),
    }
    (STATE_DIR / f"{a.id}.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    print(f"SHIPPED: experiment #{a.id} completed with decision '{token}'. Status: {record['status_after']}.")
    print(f"Saved the before-state and verdict to {STATE_DIR / f'{a.id}.json'} for rollback and the decision record.")
    print("Next: closeout.py verify, then cleanup-plan.")
    return 0


def cmd_verify(a) -> int:
    ctx = get_context(a.env_key)
    exp = api(exp_path(ctx, a.id))
    feat = (exp.get("feature") or {}).get("name")
    decision, variant_id = exp.get("rollout_decision"), exp.get("rollout_variant")
    print(f"Experiment #{a.id}: status={exp.get('status')} decision={decision} "
          f"variant_id={variant_id} ended_at={exp.get('ended_at')}")
    state = flag_state(str(feat))
    if state is None:
        print(f"Could not read the live state of flag '{feat}'.")
        return 1
    variants = state.get("variants") or []
    weights = ", ".join(f"{x.get('key') or x.get('id')}={x.get('weight') or 0:g}%" for x in variants) or "none"
    print(f"Flag '{feat}' in '{ctx['environment_name']}': enabled={state.get('enabled')} "
          f"value={state.get('value')!r} weights: {weights}")
    problems = []
    if exp.get("status") != "completed":
        problems.append("experiment is not completed")
    elif decision == "disable":
        if state.get("enabled"):
            problems.append("decision was disable but the flag is still enabled")
    else:
        if not state.get("enabled"):
            problems.append("flag is disabled")
        for x in variants:
            want = 100 if (decision == "variant" and x.get("id") == variant_id) else 0
            if abs((x.get("weight") or 0) - want) > 0.01:
                problems.append(f"variation '{x.get('key')}' is at {x.get('weight') or 0:g}%, expected {want}%")
    if problems:
        print("NOT DONE: " + "; ".join(problems))
        return 1
    served = "control" if decision == "control" else "the flag disabled" if decision == "disable" else \
        next((x.get("key") for x in variants if x.get("id") == variant_id), f"variant {variant_id}")
    print(f"OK: experiment closed and every user now gets {served}.")
    return 0


def reference_pattern(flag: str | None, variant_keys: list[str]) -> re.Pattern | None:
    """The flag name anywhere, but a variation key only as a quoted string
    literal ("urgent", 'urgent', `urgent`), so a common word like "urgent"
    in a comment or README doesn't flood the plan."""
    parts = [re.escape(flag)] if flag else []
    parts += [r"""["'`]""" + re.escape(k) + r"""["'`]""" for k in variant_keys if k]
    return re.compile("|".join(parts)) if parts else None


def _grep(root: Path, pattern: re.Pattern | None, limit: int = 40) -> list[str]:
    hits: list[str] = []
    if pattern is None:
        return hits
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                if p.stat().st_size > 1_000_000:
                    continue
                raw = p.read_bytes()
                if b"\0" in raw[:8192]:  # binary file (image, archive): not code
                    continue
                for i, line in enumerate(raw.decode("utf-8", errors="ignore").splitlines(), 1):
                    if pattern.search(line):
                        hits.append(f"{p.relative_to(root)}:{i}: {line.strip()[:120]}")
                        if len(hits) >= limit:
                            return hits
            except (OSError, UnicodeDecodeError):
                continue
    return hits


def cmd_cleanup_plan(a) -> int:
    ctx = get_context(a.env_key)
    exp = api(exp_path(ctx, a.id))
    if exp.get("status") != "completed":
        print("Experiment is not completed yet. Run `ship` first; the flag is locked while it runs.")
        return 1
    feat = exp.get("feature") or {}
    name = feat.get("name")
    ids = V._variant_ids(exp)
    decision = exp.get("rollout_decision")
    winner = next((k for k, i in ids.items() if i == exp.get("rollout_variant")), None)
    keep = winner if decision == "variant" else "control"
    losers = [k for k in ids if k != keep]
    print(f"CLEANUP PLAN for flag '{name}' (decision: {decision}, keep: {keep}). Nothing below has been run.")
    print(f"\n1. Code: these lines mention the flag or one of its variation keys in {Path(a.repo).resolve()}.")
    print(f"   Replace each experiment branch with the kept behaviour ({keep}), then deploy:")
    hits = _grep(Path(a.repo).resolve(), reference_pattern(name, list(ids)))
    print("\n".join(f"   {h}" for h in hits) if hits else "   (no references found)")
    step = 2
    if losers:
        print(f"\n{step}. After that deploy is live, remove the losing variations.")
        print("   Variations are shared by every environment in the project, so check no other")
        print("   environment still serves them first:")
        for k in losers:
            print(f"   flagsmith feature variant delete {name} {k} --yes")
        step += 1
    print(f"\n{step}. Once no code reads '{name}' in any environment, archive it:")
    print(f"   flagsmith feature update {name} --archive")
    print("\nAsk the user to confirm each step before running it. Every step after 1 changes the live flag.")
    return 0


def cmd_record(a) -> int:
    src = STATE_DIR / f"{a.id}.json"
    if not src.exists():
        print(f"No ship record at {src}. Run `ship` from this directory first.")
        return 1
    r = json.loads(src.read_text(encoding="utf-8"))
    v = r["verdict"]
    tpl = (Path(__file__).resolve().parent.parent / "assets" / "decision-record.md").read_text(encoding="utf-8")
    metric_rows = []
    for m in v["metrics"]:
        for arm in m["arms"]:
            if arm["key"] == V.CONTROL:
                continue
            metric_rows.append(
                f"| {m['name']} | {m['role']} | {arm['key']} | {arm['identities']} | "
                f"{V._fmt_pct(arm['lift'], True)} | "
                f"{V._fmt_pct(arm['ci_low'], True)} to {V._fmt_pct(arm['ci_high'], True)} | "
                f"{V._fmt_pct(arm['chance_good'])} |"
            )
    cmd = rollback_command(r["flag"], r.get("flag_before"))
    rollback = ("To put the flag back the way it was just before shipping:\n\n    " + cmd
                if cmd else "No before-state was saved, so restore the flag by hand from the dashboard.")
    srm = v.get("srm_p_value")
    srm_text = (f"{srm:.3g} ({'Flagsmith' if v.get('srm_source') == 'flagsmith' else 'computed by the skill'})"
                if srm is not None else "not checked (needs 100+ identities and a known split)")
    out = tpl.format(
        name=r["experiment_name"], exp_id=r["experiment_id"], flag=r["flag"], environment=r["environment"],
        hypothesis=r.get("hypothesis") or "(none recorded)",
        decision=r["decision"] + (f" -> {r['variant_key']}" if r.get("variant_key") else ""),
        verdict_code=v["code"], verdict_summary=v["summary"],
        approved_by=r.get("approved_by") or "(not given)",
        override=r.get("override_reason") or "None. The decision followed the data.",
        shipped_at=r["shipped_at"], as_of=r.get("results_as_of") or "n/a",
        srm=srm_text,
        identities=v["total_identities"],
        excluded=v.get("excluded_identities") if v.get("excluded_identities") is not None else "n/a",
        metric_rows="\n".join(metric_rows) or "| (no inference) | | | | | | |",
        warnings="\n".join(f"- {w}" for w in v.get("warnings") or []) or "- None",
        rollback=rollback, dashboard=r["dashboard"],
    )
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{r['flag']}-{r['experiment_id']}.md"
    dest.write_text(out, encoding="utf-8")
    print(f"Wrote decision record: {dest}")
    return 0


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="closeout.py", description=__doc__.splitlines()[0])
    p.add_argument("--env-key", help="Environment API key (default: from flagsmith.json / `flagsmith config`)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("context", help="Show the bound project/environment and whether it looks like production")

    s = sub.add_parser("list", help="List experiments in the environment")
    s.add_argument("--status", default="running", choices=["running", "paused", "created", "completed", "all"])
    s.add_argument("--q", help="Filter by experiment or flag name")

    s = sub.add_parser("verdict", help="Read results and decide (read-only)")
    s.add_argument("id", type=int)
    s.add_argument("--refresh", action="store_true", help="Request a recompute first (max once per 5 min)")
    s.add_argument("--wait", type=int, default=90, help="Seconds to wait for a refresh (default 90)")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("ship", help="Complete the experiment and serve the decision (needs a confirm token)")
    s.add_argument("id", type=int)
    s.add_argument("--decision", required=True, choices=["variant", "control", "disable"])
    s.add_argument("--variant", help="Variation key to roll out (with --decision variant)")
    s.add_argument("--confirm", required=True, help='Human approval token, e.g. "42:variant:green"')
    s.add_argument("--approved-by", help="Name of the person who approved")
    s.add_argument("--override-reason", help="Required when shipping against the verdict")
    s.add_argument("--production-ok", action="store_true", help="Second human yes for production environments")
    s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("verify", help="Confirm the experiment is completed and the flag serves the decision")
    s.add_argument("id", type=int)

    s = sub.add_parser("cleanup-plan", help="Find code references and print cleanup steps (runs nothing)")
    s.add_argument("id", type=int)
    s.add_argument("--repo", default=".")

    s = sub.add_parser("record", help="Write a markdown decision record from the ship record")
    s.add_argument("id", type=int)
    s.add_argument("--out", default="docs/experiments")

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # never crash printing a name with unusual characters
        except (AttributeError, ValueError):
            pass
    a = p.parse_args(argv)
    handlers = {"context": cmd_context, "list": cmd_list, "verdict": cmd_verdict, "ship": cmd_ship,
                "verify": cmd_verify, "cleanup-plan": cmd_cleanup_plan, "record": cmd_record}
    try:
        return handlers[a.cmd](a)
    except CliError as e:
        print(f"ERROR from Flagsmith CLI: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
