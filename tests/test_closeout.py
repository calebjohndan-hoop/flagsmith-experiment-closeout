"""Offline tests: verdict logic on fixture payloads, and ship safety gates
against a fake Flagsmith CLI. Run: python3 -m unittest discover tests"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "flagsmith-experiment-closeout" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))
import verdict as V  # noqa: E402
import closeout  # noqa: E402
import fake_flagsmith  # noqa: E402


def fake_run_cli(args, body=None):
    """Stand-in for closeout.run_cli that talks to the fake Flagsmith CLI directly."""
    out, err = io.StringIO(), io.StringIO()
    stdin = io.StringIO(json.dumps(body) if body is not None else "")
    with mock.patch.object(sys, "stdin", stdin), redirect_stdout(out), redirect_stderr(err):
        code = fake_flagsmith.main(list(args))
    if code != 0:
        raise closeout.CliError((err.getvalue() or out.getvalue()).strip() or f"exit {code}")
    return out.getvalue()


def exp(status="running", metrics=None, options=None):
    return {
        "id": 1, "name": "t", "status": status,
        "feature": {"name": "f", "multivariate_options": options or [{"id": 11, "key": "b"}, {"id": 12, "key": "c"}]},
        "metrics": metrics or [{"metric": 1, "metric_name": "signup", "direction": "up", "expected_direction": "increase"}],
    }


def res(inference, srm=0.5, variants=None, extra_metrics=()):
    variants = variants or {"control": {"n": 500, "sum": 40}, "b": {"n": 500, "sum": 60}, "c": {"n": 500, "sum": 41}}
    return {"as_of": "x", "payload": {"srm_p_value": srm, "metrics": [
        {"metric_id": 1, "variants": variants, "inference": inference}, *extra_metrics]}}


def inf(lift, lo, hi, p):
    return {"lift": lift, "ci_low": lo, "ci_high": hi, "chance_to_win": p}


class VerdictTests(unittest.TestCase):
    def test_clear_winner(self):
        v = V.decide(exp(), res({"b": inf(0.5, 0.1, 0.9, 0.99), "c": inf(0.02, -0.2, 0.25, 0.55)}))
        self.assertEqual(v.code, V.SHIP_VARIANT)
        self.assertEqual((v.recommended_variant_key, v.recommended_variant_id), ("b", 11))

    def test_high_chance_but_ci_crosses_zero_is_inconclusive(self):
        v = V.decide(exp(), res({"b": inf(0.3, -0.01, 0.6, 0.96), "c": None}))
        self.assertEqual(v.code, V.INCONCLUSIVE)

    def test_srm_blocks_even_a_big_winner(self):
        v = V.decide(exp(), res({"b": inf(0.5, 0.1, 0.9, 0.999), "c": None}, srm=0.0002))
        self.assertEqual(v.code, V.BLOCKED_SRM)
        self.assertIsNone(v.recommended_decision)

    def test_collecting_data(self):
        v = V.decide(exp(), res({"b": None, "c": None}))
        self.assertEqual(v.code, V.COLLECTING_DATA)

    def test_all_losers_ship_control(self):
        v = V.decide(exp(), res({"b": inf(-0.3, -0.5, -0.1, 0.01), "c": inf(-0.2, -0.4, -0.05, 0.03)}))
        self.assertEqual((v.code, v.recommended_decision), (V.SHIP_CONTROL, "control"))

    def test_lower_is_better_metric(self):
        # Flagsmith already flipped chance_to_win for a "down" metric: 0.98 means 98% chance it fell.
        m = [{"metric": 1, "metric_name": "page_load_ms", "direction": "down", "expected_direction": "decrease"}]
        v = V.decide(exp(metrics=m), res({"b": inf(-0.2, -0.3, -0.1, 0.98), "c": None}))
        self.assertEqual((v.code, v.recommended_variant_key), (V.SHIP_VARIANT, "b"))

    def test_guardrail_blocks(self):
        metrics = [
            {"metric": 1, "metric_name": "signup", "direction": "up", "expected_direction": "increase"},
            {"metric": 2, "metric_name": "refunds", "direction": "down", "expected_direction": "not_increase"},
        ]
        refunds = {"metric_id": 2, "variants": {"control": {"n": 500, "sum": 5}, "b": {"n": 500, "sum": 20}},
                   # down metric: chance_to_win = P(lift<0) = 0.01, so refunds almost surely went up
                   "inference": {"b": inf(3.0, 1.0, 5.0, 0.01)}}
        v = V.decide(exp(metrics=metrics), res({"b": inf(0.5, 0.1, 0.9, 0.99), "c": None}, extra_metrics=[refunds]))
        self.assertEqual(v.code, V.BLOCKED_GUARDRAIL)

    def test_status_gates(self):
        self.assertEqual(V.decide(exp(status="completed"), None).code, V.ALREADY_COMPLETED)
        self.assertEqual(V.decide(exp(status="created"), None).code, V.NOT_RUNNING)
        self.assertEqual(V.decide(exp(), None).code, V.NO_RESULTS)

    def test_quarantine_warning(self):
        v = V.decide(exp(), res({"b": inf(0.5, 0.1, 0.9, 0.99), "c": None}),
                     {"payload": {"excluded_identities": 200}})
        self.assertTrue(any("more than one variation" in w for w in v.warnings))

    def test_null_inference_is_collecting_data_not_a_crash(self):
        v = V.decide(exp(), res({"b": inf(None, None, None, None), "c": {}}))
        self.assertEqual(v.code, V.COLLECTING_DATA)

    def test_rollback_text_does_not_assume_control(self):
        cmd = closeout.rollback_command("f", {"enabled": False, "variants": [{"key": "b", "weight": 30}]})
        self.assertEqual(cmd, "flagsmith flag update f --disable --weight b=30 --yes")

    def test_render_is_compact(self):
        v = V.decide(exp(), res({"b": inf(0.5, 0.1, 0.9, 0.99), "c": inf(0.02, -0.2, 0.25, 0.55)}))
        self.assertLess(len(V.render(v)), 900)


class ShipGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "writes.log"
        os.environ.update(FAKE_LOG=str(self.log), FAKE_SCENARIO="winner")
        # Call the fake CLI in-process instead of spawning it: identical on Windows, macOS and Linux.
        self._orig_run_cli = closeout.run_cli
        closeout.run_cli = fake_run_cli
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        closeout.run_cli = self._orig_run_cli
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = closeout.main(list(args))
        return code, buf.getvalue()

    def writes(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_wrong_token_refused_and_nothing_written(self):
        code, out = self.run_cli("ship", "42", "--decision", "variant", "--variant", "urgent", "--confirm", "yes")
        self.assertEqual(code, 2)
        self.assertIn('--confirm "42:variant:urgent"', out)
        self.assertEqual(self.writes(), [])

    def test_against_verdict_needs_override(self):
        code, out = self.run_cli("ship", "42", "--decision", "control", "--confirm", "42:control")
        self.assertEqual(code, 2)
        self.assertIn("override-reason", out)
        self.assertEqual(self.writes(), [])

    def test_srm_refuses_even_with_right_token(self):
        os.environ["FAKE_SCENARIO"] = "srm_flagsmith"
        code, out = self.run_cli("ship", "42", "--decision", "variant", "--variant", "urgent",
                                 "--confirm", "42:variant:urgent")
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED_SRM", out)

    def test_production_needs_second_yes(self):
        os.environ["FAKE_SCENARIO"] = "env_prod"
        args = ["ship", "42", "--decision", "variant", "--variant", "urgent", "--confirm", "42:variant:urgent"]
        code, out = self.run_cli(*args)
        self.assertEqual(code, 2)
        self.assertIn("production", out)
        code, _ = self.run_cli(*args, "--production-ok")
        self.assertEqual(code, 0)

    def test_dry_run_writes_nothing(self):
        code, out = self.run_cli("ship", "42", "--decision", "variant", "--variant", "urgent",
                                 "--confirm", "42:variant:urgent", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn('"rollout_variant": 501', out)
        self.assertEqual(self.writes(), [])

    def test_happy_path_ship_and_record(self):
        code, out = self.run_cli("ship", "42", "--decision", "variant", "--variant", "urgent",
                                 "--confirm", "42:variant:urgent", "--approved-by", "Cal")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.writes(), [{"method": "POST",
                                          "path": "api/v1/environments/envKEY123/experiments/42/complete/",
                                          "body": {"rollout_decision": "variant", "rollout_variant": 501}}])
        saved = json.loads(Path(".flagsmith-closeout/42.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["flag_before"]["variants"][0]["weight"], 0)  # live env weights, not project defaults
        code, out = self.run_cli("record", "42", "--out", "records")
        self.assertEqual(code, 0, out)
        text = Path("records/checkout_cta-42.md").read_text(encoding="utf-8")
        self.assertIn("SHIP_VARIANT", text)
        self.assertIn("Cal", text)
        self.assertIn("\u2192", text)  # non-ASCII hypothesis survives the round trip
        self.assertIn("flagsmith flag update checkout_cta --enable --weight urgent=0 --yes", text)
        self.assertIn("computed by the skill", text)


class SrmFallbackTests(unittest.TestCase):
    """Flagsmith returns srm_p_value = null for experiments whose split lives in
    the rollout override (seen live on experiment #634). The skill computes it."""

    def setUp(self):
        self.exp = {"id": 1, "status": "running",
                    "feature": {"name": "f", "multivariate_options": [{"id": 11, "key": "b"}]},
                    "metrics": [{"metric": 1, "metric_name": "purchase", "direction": "up",
                                 "expected_direction": "increase"}],
                    "experiment_rollout": {"multivariate_feature_state_values": [
                        {"multivariate_feature_option": 11, "percentage_allocation": 50}]}}

    def results(self, n_control, n_b, srm=None):
        return {"payload": {"srm_p_value": srm, "metrics": [{
            "metric_id": 1,
            "variants": {"control": {"n": n_control, "sum": 55}, "b": {"n": n_b, "sum": 105}},
            "inference": {"b": inf(0.874, 0.292, 1.455, 0.998)}}]}}

    def test_real_634_split_passes_and_is_labelled(self):
        v = V.decide(self.exp, self.results(743, 757))
        self.assertEqual(v.code, V.SHIP_VARIANT)
        self.assertEqual(v.srm_source, "skill")
        self.assertAlmostEqual(v.srm_p_value, 0.7177, places=3)
        self.assertIn("computed by this skill", V.render(v))

    def test_broken_split_blocks_when_flagsmith_is_silent(self):
        v = V.decide(self.exp, self.results(900, 600))
        self.assertEqual((v.code, v.srm_source), (V.BLOCKED_SRM, "skill"))

    def test_flagsmith_value_wins_over_computed(self):
        v = V.decide(self.exp, self.results(743, 757, srm=0.5))
        self.assertEqual((v.srm_source, v.srm_p_value), ("flagsmith", 0.5))

    def test_no_split_known_means_not_checked(self):
        exp = dict(self.exp, experiment_rollout=None)
        v = V.decide(exp, self.results(743, 757))
        self.assertIsNone(v.srm_p_value)
        self.assertIn("SRM: not checked", V.render(v))

    def test_below_100_identities_not_checked(self):
        v = V.decide(self.exp, self.results(40, 55))
        self.assertIsNone(v.srm_p_value)

    def test_uneven_configured_split(self):
        exp = dict(self.exp, experiment_rollout={"multivariate_feature_state_values": [
            {"multivariate_feature_option": 11, "percentage_allocation": 20}]})
        self.assertEqual(V.expected_shares(exp), {"b": 0.2, "control": 0.8})
        ok = V.decide(exp, self.results(1200, 300))     # matches 80/20
        bad = V.decide(exp, self.results(750, 750))     # 50/50 against an 80/20 config
        self.assertNotEqual(ok.code, V.BLOCKED_SRM)
        self.assertEqual(bad.code, V.BLOCKED_SRM)

    def test_matches_flagsmith_formula(self):
        # Reference values from scipy.stats.chisquare for the same inputs.
        self.assertAlmostEqual(V.srm_p_value([743, 757], [0.5, 0.5]), 0.71774, places=4)
        self.assertAlmostEqual(V.srm_p_value([100, 120, 80], [1 / 3] * 3), 0.0183156, places=6)


class CommandTests(ShipGateTests):
    """verify, cleanup-plan, list and refresh against the fake CLI."""

    def test_verify_ok_after_ship(self):
        os.environ["FAKE_SCENARIO"] = "completed_variant"
        code, out = self.run_cli("verify", "42")
        self.assertEqual(code, 0, out)
        self.assertIn("OK: experiment closed and every user now gets urgent", out)

    def test_verify_catches_wrong_weights(self):
        os.environ["FAKE_SCENARIO"] = "completed_bad"
        code, out = self.run_cli("verify", "42")
        self.assertEqual(code, 1)
        self.assertIn("expected 100%", out)

    def test_verify_running_is_not_done(self):
        code, out = self.run_cli("verify", "42")
        self.assertEqual(code, 1)
        self.assertIn("not completed", out)

    def test_cleanup_plan_only_losers_and_yes(self):
        os.environ["FAKE_SCENARIO"] = "completed_variant"
        Path("app").mkdir()
        Path("app/checkout.py").write_text('flag = fs.get_experiment_flag("checkout_cta", identifier=uid)\n',
                                           encoding="utf-8")
        code, out = self.run_cli("cleanup-plan", "42", "--repo", "app")
        self.assertEqual(code, 0, out)
        self.assertIn("checkout.py:1:", out)
        self.assertNotIn("variant delete checkout_cta urgent", out)  # the winner is kept
        self.assertIn("feature update checkout_cta --archive", out)
        self.assertEqual(self.writes(), [])  # a plan, not an action

    def test_cleanup_ignores_variant_word_in_prose_but_finds_it_in_code(self):
        os.environ["FAKE_SCENARIO"] = "completed_variant"  # urgent won; its branch in code still has to go
        Path("app").mkdir()
        Path("app/notes.md").write_text("This urgent fix was shipped last week.\n", encoding="utf-8")
        Path("app/ui.py").write_text('if variant == "urgent":\n    label = "Buy now"\n', encoding="utf-8")
        Path("app/logo.png").write_bytes(b"\x89PNG\0\0urgent checkout_cta")
        code, out = self.run_cli("cleanup-plan", "42", "--repo", "app")
        self.assertEqual(code, 0, out)
        self.assertIn("ui.py:1:", out)
        self.assertNotIn("notes.md", out)
        self.assertNotIn("logo.png", out)

    def test_list_survives_missing_flag_name(self):
        with mock.patch.object(closeout, "api", return_value={"results": [
                {"id": 7, "status": "running", "feature": {"id": 99, "name": None}, "name": None}]}), \
             mock.patch.object(closeout, "get_context", return_value={"environment_name": "Development",
                                                                      "environment_key": "envKEY123"}):
            code, out = self.run_cli("list")
        self.assertEqual(code, 0, out)
        self.assertIn("flag=99", out)

    def test_list_running(self):
        code, out = self.run_cli("list")
        self.assertEqual(code, 0, out)
        self.assertIn("#42", out)
        self.assertIn("flag=checkout_cta", out)

    def test_list_url_encodes_query(self):
        code, out = self.run_cli("list", "--status", "all", "--q", "checkout & co")
        self.assertEqual(code, 0, out)
        self.assertIn("?q=checkout%20%26%20co", self.writes()[0]["path"])

    def test_refresh_still_waits_when_only_exposures_throttled(self):
        os.environ["FAKE_SCENARIO"] = "throttled_exposures"
        with mock.patch.object(closeout.time, "sleep"):
            code, out = self.run_cli("verdict", "42", "--refresh", "--wait", "1")
        self.assertEqual(code, 0, out)
        self.assertIn("exposures refreshed in the last 5 minutes", out)
        self.assertIn("results not recomputed", out)  # proves it polled for the results refresh


if __name__ == "__main__":
    unittest.main()
