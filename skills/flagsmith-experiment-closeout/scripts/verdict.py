"""Pure decision logic for closing out a Flagsmith experiment.

No network, no CLI: takes the JSON Flagsmith already returns and turns it into a
small, explainable verdict. Kept separate from closeout.py so it can be tested
offline against fixture payloads.

Thresholds mirror Flagsmith's own engine (api/experimentation/constants.py and
the Statistics Glossary): 95% chance to win, 50 identities and 5 conversions
per arm before a result exists, SRM alarm below p = 0.001.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any

WIN_THRESHOLD = 0.95
LOSE_THRESHOLD = 0.05
SRM_ALARM_P = 0.001
SRM_MIN_TOTAL_IDENTITIES = 100
QUARANTINE_WARN_SHARE = 0.05
CONTROL = "control"

# Verdict codes. SHIP_* are the only ones that allow `closeout.py ship`
# without an explicit human override.
SHIP_VARIANT = "SHIP_VARIANT"
SHIP_CONTROL = "SHIP_CONTROL"
INCONCLUSIVE = "INCONCLUSIVE"
COLLECTING_DATA = "COLLECTING_DATA"
NO_RESULTS = "NO_RESULTS"
BLOCKED_SRM = "BLOCKED_SRM"
BLOCKED_GUARDRAIL = "BLOCKED_GUARDRAIL"
NOT_RUNNING = "NOT_RUNNING"
ALREADY_COMPLETED = "ALREADY_COMPLETED"

GOAL_DIRECTIONS = {"increase", "decrease"}
GUARDRAIL_DIRECTIONS = {"not_increase", "not_decrease"}


@dataclass
class ArmResult:
    key: str
    identities: int
    value: float | None  # conversion rate / mean per identity
    lift: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    chance_good: float | None = None  # prob the arm moved the metric the hoped-for way


@dataclass
class MetricVerdict:
    metric_id: int
    name: str
    role: str  # goal | guardrail | info
    expected_direction: str
    arms: list[ArmResult] = field(default_factory=list)


@dataclass
class Verdict:
    code: str
    summary: str
    recommended_decision: str | None = None  # variant | control | None
    recommended_variant_key: str | None = None
    recommended_variant_id: int | None = None
    warnings: list[str] = field(default_factory=list)
    metrics: list[MetricVerdict] = field(default_factory=list)
    srm_p_value: float | None = None
    srm_source: str | None = None  # "flagsmith" | "skill" | None (not checked)
    total_identities: int = 0
    excluded_identities: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _p_lift_positive(chance_to_win: float, metric_direction: str) -> float:
    """Undo Flagsmith's direction flip: return the raw P(lift > 0).

    Flagsmith reports chance_to_win as P(lift < 0) for metrics whose direction
    is "down" (lower is better), and P(lift > 0) otherwise.
    """
    return 1.0 - chance_to_win if metric_direction == "down" else chance_to_win


def _chance_good(p_up: float, expected_direction: str) -> float:
    if expected_direction in ("increase", "not_decrease"):
        return p_up
    return 1.0 - p_up


def _ci_clear(arm: ArmResult, expected_direction: str) -> bool:
    if arm.ci_low is None or arm.ci_high is None:
        return False
    if expected_direction == "increase":
        return arm.ci_low > 0
    if expected_direction == "decrease":
        return arm.ci_high < 0
    return False


def chi_squared_survival(statistic: float, degrees_of_freedom: int) -> float:
    """P(X >= statistic) for a chi-squared distribution with integer df.

    Same exact recurrence Flagsmith uses in api/experimentation/stats.py, so the
    skill's p-value matches what the engine would report.
    """
    if statistic <= 0:
        return 1.0
    y = statistic / 2.0
    if degrees_of_freedom % 2:
        a, survival = 0.5, math.erfc(math.sqrt(y))
    else:
        a, survival = 1.0, math.exp(-y)
    while a + 1.0 <= degrees_of_freedom / 2.0:
        survival += math.exp(a * math.log(y) - y - math.lgamma(a + 1.0))
        a += 1.0
    return min(1.0, max(0.0, survival))


def srm_p_value(observed: list[int], expected_shares: list[float]) -> float | None:
    """Chi-squared goodness-of-fit test of observed arm sizes against the configured split."""
    total = sum(observed)
    if len(observed) < 2 or total == 0 or any(s <= 0 for s in expected_shares):
        return None
    statistic = sum((c - total * s) ** 2 / (total * s) for c, s in zip(observed, expected_shares))
    return chi_squared_survival(statistic, len(observed) - 1)


def expected_shares(experiment: dict[str, Any]) -> dict[str, float]:
    """Configured split from the experiment's own rollout: each variation's
    percentage_allocation, with control taking the remainder. Empty if unknown,
    if a variation has no key, or if the allocations don't add up."""
    rollout = experiment.get("experiment_rollout") or {}
    allocations = rollout.get("multivariate_feature_state_values") or []
    if not allocations:
        return {}
    id_to_key = {v: k for k, v in _variant_ids(experiment).items()}
    shares: dict[str, float] = {}
    allocated = 0.0
    for mv in allocations:
        key = id_to_key.get(mv.get("multivariate_feature_option"))
        if key is None:
            return {}
        pct = float(mv.get("percentage_allocation") or 0)
        shares[key] = pct / 100
        allocated += pct
    if allocated > 100:
        return {}
    shares[CONTROL] = (100 - allocated) / 100
    return shares


def _variant_ids(experiment: dict[str, Any]) -> dict[str, int]:
    feature = experiment.get("feature") or {}
    options = feature.get("multivariate_options") or feature.get("variants") or []
    out: dict[str, int] = {}
    for opt in options:
        key = opt.get("key")
        if key:
            out[key] = opt.get("id")
    return out


def _fmt_pct(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:+.1f}%" if signed else f"{x * 100:.1f}%"


def decide(
    experiment: dict[str, Any],
    results: dict[str, Any] | None,
    exposures: dict[str, Any] | None = None,
) -> Verdict:
    """Turn experiment + results (+ exposures) JSON into a Verdict.

    `experiment` is GET .../experiments/<id>/
    `results` is the `results` object from GET .../experiments/<id>/results/
    `exposures` is the `exposures` object from GET .../experiments/<id>/exposures/
    """
    status = experiment.get("status")
    if status == "completed":
        return Verdict(ALREADY_COMPLETED, "Experiment is already completed; results are final.")
    if status not in ("running", "paused"):
        return Verdict(NOT_RUNNING, f"Experiment status is '{status}'. Start it before closing it out.")
    if not results or not results.get("payload"):
        return Verdict(NO_RESULTS, "No results computed yet. Refresh results and check again in about a minute.")

    payload = results["payload"]
    warnings: list[str] = []
    if results.get("last_error_at"):
        warnings.append("Last results refresh errored; figures may be stale (see as_of).")

    metric_meta = {m.get("metric"): m for m in experiment.get("metrics") or []}
    metric_results = payload.get("metrics") or []

    # Identities per arm, taken from the first metric's variant stats.
    totals: dict[str, int] = {}
    for mr in metric_results:
        for key, stats in (mr.get("variants") or {}).items():
            totals[key] = max(totals.get(key, 0), int(stats.get("n", 0)))
    total_identities = sum(totals.values())

    excluded = None
    if exposures and exposures.get("payload"):
        excluded = exposures["payload"].get("excluded_identities")
        if excluded and total_identities and excluded / (total_identities + excluded) > QUARANTINE_WARN_SHARE:
            warnings.append(
                f"{excluded} identities were served more than one variation and were excluded. "
                "Users may be slipping between variations; check how the app identifies users."
            )

    # Sample ratio mismatch: prefer Flagsmith's own figure. Flagsmith skips the
    # check when the split lives only in the experiment's rollout override (the
    # setup its agent prompt creates), so fall back to computing it from the
    # experiment's configured split with the same formula.
    srm = payload.get("srm_p_value")
    srm_source = "flagsmith" if srm is not None else None
    if srm is None and total_identities >= SRM_MIN_TOTAL_IDENTITIES:
        shares = expected_shares(experiment)
        if shares and set(totals) <= set(shares):
            keys = [k for k in shares if shares[k] > 0]
            if set(totals) <= set(keys):
                srm = srm_p_value([totals.get(k, 0) for k in keys], [shares[k] for k in keys])
                srm_source = "skill" if srm is not None else None
    verdict_base = dict(srm_p_value=srm, srm_source=srm_source,
                        total_identities=total_identities, excluded_identities=excluded)

    # Build per-metric view.
    metrics: list[MetricVerdict] = []
    for mr in metric_results:
        meta = metric_meta.get(mr.get("metric_id"), {})
        expected = meta.get("expected_direction", "increase")
        direction = meta.get("direction", "up")
        role = "goal" if expected in GOAL_DIRECTIONS else "guardrail" if expected in GUARDRAIL_DIRECTIONS else "info"
        if direction == "informational":
            role = "info"
        mv = MetricVerdict(
            metric_id=mr.get("metric_id"),
            name=meta.get("metric_name", f"metric {mr.get('metric_id')}"),
            role=role,
            expected_direction=expected,
        )
        variants = mr.get("variants") or {}
        inference = mr.get("inference") or {}
        for key in sorted(variants, key=lambda k: (k != CONTROL, k)):
            stats = variants[key]
            n = int(stats.get("n", 0))
            arm = ArmResult(key=key, identities=n, value=(stats.get("sum", 0) / n) if n else None)
            inf = inference.get(key) or {}
            if inf.get("chance_to_win") is not None:
                p_up = _p_lift_positive(float(inf["chance_to_win"]), direction)
                arm.lift, arm.ci_low, arm.ci_high = inf.get("lift"), inf.get("ci_low"), inf.get("ci_high")
                arm.chance_good = _chance_good(p_up, expected)
            mv.arms.append(arm)
        metrics.append(mv)
    verdict_base["metrics"] = metrics

    # Gate 1: sample ratio mismatch makes every other number untrustworthy.
    if srm is not None and srm < SRM_ALARM_P:
        return Verdict(
            BLOCKED_SRM,
            f"Sample ratio mismatch (p = {srm:.2g}). Traffic did not split as configured, so the results "
            "can't be trusted. Investigate crashes in one variation, redirects that skip bucketing, or dropped events.",
            warnings=warnings,
            **verdict_base,
        )

    goals = [m for m in metrics if m.role == "goal"]
    if not goals:
        return Verdict(INCONCLUSIVE, "No goal metric (expected direction increase/decrease) on this experiment.",
                       warnings=warnings, **verdict_base)
    primary = goals[0]
    treatments = [a for a in primary.arms if a.key != CONTROL]

    # Gate 2: Flagsmith withholds inference below its data floor.
    if not treatments or all(a.chance_good is None for a in treatments):
        return Verdict(
            COLLECTING_DATA,
            f"Collecting data on '{primary.name}': every arm needs at least 50 identities "
            "(and 5 conversions for occurrence metrics) before a result exists. Keep it running.",
            warnings=warnings,
            **verdict_base,
        )

    winners = [a for a in treatments
               if a.chance_good is not None and a.chance_good >= WIN_THRESHOLD and _ci_clear(a, primary.expected_direction)]
    losers = [a for a in treatments if a.chance_good is not None and a.chance_good <= LOSE_THRESHOLD]
    ids = _variant_ids(experiment)

    if winners:
        best = max(winners, key=lambda a: (a.chance_good, abs(a.lift or 0)))
        # Gate 3: guardrails must not be clearly harmed by the winner.
        for g in (m for m in metrics if m.role == "guardrail"):
            arm = next((a for a in g.arms if a.key == best.key), None)
            if arm and arm.chance_good is not None and arm.chance_good <= LOSE_THRESHOLD:
                return Verdict(
                    BLOCKED_GUARDRAIL,
                    f"'{best.key}' wins '{primary.name}' ({_fmt_pct(best.chance_good)} chance) but breaks guardrail "
                    f"'{g.name}' (only {_fmt_pct(arm.chance_good)} chance it held). A human must decide the trade-off.",
                    recommended_variant_key=best.key,
                    recommended_variant_id=ids.get(best.key),
                    warnings=warnings,
                    **verdict_base,
                )
        return Verdict(
            SHIP_VARIANT,
            f"'{best.key}' beats control on '{primary.name}': lift {_fmt_pct(best.lift, True)} "
            f"(95% CI {_fmt_pct(best.ci_low, True)} to {_fmt_pct(best.ci_high, True)}), "
            f"{_fmt_pct(best.chance_good)} chance to win.",
            recommended_decision="variant",
            recommended_variant_key=best.key,
            recommended_variant_id=ids.get(best.key),
            warnings=warnings,
            **verdict_base,
        )

    if treatments and len(losers) == len(treatments):
        return Verdict(
            SHIP_CONTROL,
            f"Every variation loses to control on '{primary.name}' (each under {_fmt_pct(LOSE_THRESHOLD)} chance "
            "to win). Keep control.",
            recommended_decision="control",
            warnings=warnings,
            **verdict_base,
        )

    lead = max((a for a in treatments if a.chance_good is not None), key=lambda a: a.chance_good)
    return Verdict(
        INCONCLUSIVE,
        f"No clear winner yet on '{primary.name}'. Leader '{lead.key}' has {_fmt_pct(lead.chance_good)} chance to win, "
        f"lift {_fmt_pct(lead.lift, True)} (95% CI {_fmt_pct(lead.ci_low, True)} to {_fmt_pct(lead.ci_high, True)}). "
        "Keep collecting data, or end it deliberately with a human override.",
        warnings=warnings,
        **verdict_base,
    )


def render(v: Verdict) -> str:
    """Compact human/agent-readable text. Deliberately short to save tokens."""
    lines = [f"VERDICT: {v.code}", v.summary]
    if v.recommended_decision:
        tgt = f" -> {v.recommended_variant_key} (id {v.recommended_variant_id})" if v.recommended_variant_key else ""
        lines.append(f"RECOMMENDED: {v.recommended_decision}{tgt}")
    if v.srm_p_value is not None:
        who = "Flagsmith" if v.srm_source == "flagsmith" else "computed by this skill from the configured split"
        lines.append(f"SRM p-value: {v.srm_p_value:.3g} ({who}; alarm below {SRM_ALARM_P})")
    elif v.code not in (NOT_RUNNING, ALREADY_COMPLETED, NO_RESULTS):
        lines.append(f"SRM: not checked (needs {SRM_MIN_TOTAL_IDENTITIES}+ identities and a known split)")
    lines.append(f"Identities enrolled: {v.total_identities}"
                 + (f", excluded: {v.excluded_identities}" if v.excluded_identities is not None else ""))
    for m in v.metrics:
        lines.append(f"[{m.role}] {m.name} (expected {m.expected_direction})")
        for a in m.arms:
            if a.key == CONTROL:
                lines.append(f"  control   n={a.identities} value={a.value:.4g}" if a.value is not None
                             else f"  control   n={a.identities}")
            elif a.chance_good is None:
                lines.append(f"  {a.key:<9} n={a.identities} collecting data")
            else:
                lines.append(f"  {a.key:<9} n={a.identities} lift={_fmt_pct(a.lift, True)} "
                             f"CI=[{_fmt_pct(a.ci_low, True)}, {_fmt_pct(a.ci_high, True)}] "
                             f"chance={_fmt_pct(a.chance_good)}")
    for w in v.warnings:
        lines.append(f"WARNING: {w}")
    return "\n".join(lines)
