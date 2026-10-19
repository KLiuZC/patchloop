from collections import Counter
from copy import deepcopy
import hashlib
import json

import patchloop.ab_preflight as preparation


ROOT = preparation.ROOT
ORIGINAL = ROOT / "evals/profiles/command-policy-ab6/draft.json"
REVISED = ROOT / "evals/profiles/command-policy-ab6/draft-r2.json"
REMOVED = "psf__requests-1724"
REPLACEMENT = "psf__requests-5414"


def read_plan(path):
    return json.loads(path.read_bytes())


def test_preparation_defaults_to_second_draft(monkeypatch, capsys):
    assert preparation.DEFAULT_DRAFT == REVISED
    plan = read_plan(REVISED)
    seen = []

    def load(path, cache):
        seen.append(path)
        return plan, plan["instances"], "draft-hash"

    monkeypatch.setattr(preparation, "load_inputs", load)
    monkeypatch.setattr(preparation, "local_image", lambda *args: {"id": "local-image"})
    monkeypatch.setattr(preparation, "task_repository_state", lambda *args: {})
    monkeypatch.setattr(preparation, "execute_preflight", lambda *args, **kwargs: preparation.require(False, "must not execute"))
    assert preparation.main(["--instance", REPLACEMENT]) == 0
    assert seen == [REVISED]
    report = json.loads(capsys.readouterr().out)
    assert [item["instance_id"] for item in report["candidates"]] == [REPLACEMENT]
    assert report["model_requests"] == 0
    assert report["ready_for_model_run"] is False


def test_revision_keeps_original_and_replacement_evidence():
    plan = read_plan(REVISED)
    revision = plan["revision"]
    assert ROOT / revision["parent_path"] == ORIGINAL
    assert hashlib.sha256(ORIGINAL.read_bytes()).hexdigest() == revision["parent_sha256"]
    assert revision["parent_sha256"] == "2ded67b3ad26e84f70932d3150c7e198a52c0d0959801741a7cc718eea4f69a7"
    assert revision["number"] == 2
    assert len(revision["replacements"]) == 1
    replacement = revision["replacements"][0]
    assert replacement["removed"] == REMOVED
    assert replacement["added"] == REPLACEMENT
    evidence = ROOT / replacement["evidence_path"]
    assert hashlib.sha256(evidence.read_bytes()).hexdigest() == replacement["evidence_sha256"]
    report = json.loads(evidence.read_bytes())
    assert report["instance_id"] == REMOVED
    assert report["target_python"] == "2.7.18"
    assert report["full_preflight_status"] == "error"
    assert report["model_requests"] == 0


def test_only_first_candidate_changes_using_saved_stratum_order():
    original, revised = read_plan(ORIGINAL), read_plan(REVISED)
    before, after = original["instances"][0], revised["instances"][0]
    assert before["instance_id"] == REMOVED
    assert after["instance_id"] == REPLACEMENT
    assert revised["instances"][1:] == original["instances"][1:]
    assert (after["repo"], after["difficulty"]) == (before["repo"], before["difficulty"])
    selection = deepcopy(revised["selection"])
    selection["candidate_file"] = original["selection"]["candidate_file"]
    assert selection == original["selection"]
    stratum = next(group for group in selection["ranked_strata"]
                   if (group["repo"], group["difficulty"]) == (after["repo"], after["difficulty"]))
    assert stratum["ordered_instance_ids"][:2] == [REMOVED, REPLACEMENT]
    assert REPLACEMENT not in selection["excluded_snapshot_ids"]
    assert after["rank_sha256"] == hashlib.sha256(f"{selection['seed']}:{REPLACEMENT}".encode()).hexdigest()
    assert after["base_commit"] == "39d0fdd9096f7dceccbc8f82e1eda7dd64717a8e"
    assert after["version"] == "2.26"
    assert after["image"] == "swebench/sweb.eval.x86_64.psf_1776_requests-5414:latest"


def test_candidate_list_and_hash_order_track_replacement():
    original, revised = read_plan(ORIGINAL), read_plan(REVISED)
    identifiers = [item["instance_id"] for item in revised["instances"]]
    subset = ROOT / revised["selection"]["candidate_file"]
    listed = [line.strip() for line in subset.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert listed == identifiers == revised["dataset"]["selected_records_hash_order"]
    assert len(identifiers) == len(set(identifiers)) == 6
    assert REMOVED not in identifiers
    assert subset != ROOT / original["selection"]["candidate_file"]
    assert revised["dataset"]["selected_records_sha256"] != original["dataset"]["selected_records_sha256"]
    for key, value in original["dataset"].items():
        if key not in {"selected_records_hash_order", "selected_records_sha256"}:
            assert revised["dataset"][key] == value


def test_pairs_preserve_order_balance_and_use_new_run_ids():
    original, revised = read_plan(ORIGINAL), read_plan(REVISED)
    expected = deepcopy(original["pairs"])
    for pair in expected:
        pair["pair_id"] = pair["pair_id"].replace("cp6-", "cp6-plan2-", 1)
        if pair["instance_id"] == REMOVED:
            pair["instance_id"] = REPLACEMENT
        for arm in pair["arms"]:
            arm["run_id"] = arm["run_id"].replace("cp6-", "cp6-plan2-", 1)
    assert revised["pairs"] == expected
    assert len(expected) == len({pair["pair_id"] for pair in expected}) == 18
    assert Counter(pair["instance_id"] for pair in expected) == {item["instance_id"]: 3 for item in revised["instances"]}
    assert Counter(pair["arms"][0]["command_policy"] for pair in expected) == {"off": 9, "enforce": 9}
    run_ids = [arm["run_id"] for pair in expected for arm in pair["arms"]]
    old_ids = {arm["run_id"] for pair in original["pairs"] for arm in pair["arms"]}
    assert len(run_ids) == len(set(run_ids)) == 36
    assert not set(run_ids) & old_ids


def test_revision_does_not_change_experiment_or_authorize_models():
    original, revised = read_plan(ORIGINAL), read_plan(REVISED)
    for key in ("settings", "generation_settings", "environment_policy", "command_policy", "budget", "arms", "metrics", "freeze_required"):
        assert revised[key] == original[key]
    assert revised["status"] == "draft_preflight_required"
    assert revised["model_execution_authorized"] is False
    assert revised["budget"]["approved_spend_cny"] is None
    assert revised["budget"]["planned_solves"] == 36
    assert revised["readiness"] == original["readiness"]
    for candidate in revised["instances"]:
        assert candidate["image_id"] is None
        assert candidate["environment_preflight"] == "pending"
        assert candidate["gold_preflight"] == "pending"
