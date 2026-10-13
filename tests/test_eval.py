from types import SimpleNamespace

import json
import pytest

import patchloop.eval as evaluation
from patchloop.eval import InstanceResult, image_name, read_id_file, summarize, task_text


def test_image_name_prefers_dataset_field():
    assert image_name({"instance_id": "x__y-1", "image": "swebench/sweb.eval.x86_64.x_1776_y-1:latest"}) == (
        "swebench/sweb.eval.x86_64.x_1776_y-1:latest"
    )


def test_image_name_adds_tag_and_falls_back_to_convention():
    assert image_name({"instance_id": "x__y-1", "image": "swebench/sweb.eval.x86_64.x_1776_y-1"}).endswith(":latest")
    assert image_name({"instance_id": "pytest-dev__pytest-7205"}) == (
        "swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-7205:latest"
    )


def test_read_id_file_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "ids.txt"
    f.write_text("# header\n\na__b-1\nc__d-2  # trailing\n")
    assert read_id_file(f) == ["a__b-1", "c__d-2"]


def test_task_text_contains_only_the_issue():
    inst = {
        "repo": "org/proj",
        "problem_statement": "Thing breaks.",
        "hints_text": "SECRET HINT",
        "FAIL_TO_PASS": "['tests/test_x.py::test_secret']",
        "test_patch": "diff --git a/tests/test_x.py",
    }
    text = task_text(inst)
    assert "org/proj" in text and "Thing breaks." in text
    assert "SECRET" not in text and "test_x" not in text


def test_summarize_counts_resolved_and_cost():
    llm = SimpleNamespace(provider="p", model="m")
    results = [
        InstanceResult("a", "agent", turns=3, cost=0.1, seconds=10, resolved=True),
        InstanceResult("b", "agent", turns=5, cost=0.2, seconds=20, resolved=False),
        InstanceResult("c", "agent", error="boom", seconds=1),
    ]
    s = summarize(results, "run", "agent", llm)
    assert s["instances"] == 3 and s["judged"] == 2 and s["resolved"] == 1
    assert s["resolve_rate"] == 0.5 and s["errors"] == 1
    assert abs(s["cost_cny"] - 0.3) < 1e-9 and s["seconds"] == 31


def evaluation_llm():
    return SimpleNamespace(provider="fake", model="fake", temperature=None,
                           client=SimpleNamespace(timeout=120.0, max_retries=3))


def test_eval_records_config_forwards_flag_and_resumes_consistently(tmp_path, monkeypatch):
    seen = []

    def solve(instance, *args, **kwargs):
        seen.append((kwargs["budget_reminders"], kwargs["explicit_finish"]))
        return InstanceResult(instance["instance_id"], "agent", turns=40, budget_reminders_sent=2), ""

    monkeypatch.setattr(evaluation, "solve", solve)
    instances = [{"instance_id": "x__y-1", "problem_statement": "problem", "patch": "hidden-gold"}]
    kwargs = dict(instances=instances, llm=evaluation_llm(), run_id="r", out_dir=tmp_path,
                  budget_reminders=True, explicit_finish=True, judge=False)
    summary = evaluation.evaluate(**kwargs)
    assert seen == [(True, True)]
    assert summary["budget_reminders"] is True
    assert summary["explicit_finish"] is True
    assert summary["results"][0]["budget_reminders_sent"] == 2
    assert summary["invocation"]["wall_seconds"] >= 0
    assert summary["invocation"]["resumed_instances"] == 0
    saved = (tmp_path / "run_config.json").read_bytes()
    config = json.loads(saved)["config"]
    assert config["instance_ids"] == ["x__y-1"]
    assert config["explicit_finish"] is True
    assert len(config["source_sha256"]["loop.py"]) == 64
    assert "hidden-gold" not in saved.decode()
    again = evaluation.evaluate(**kwargs)
    assert seen == [(True, True)] and again["invocation"]["resumed_instances"] == 1
    assert (tmp_path / "run_config.json").read_bytes() == saved
    with pytest.raises(ValueError, match="new --run-id"):
        evaluation.evaluate(**{**kwargs, "budget_reminders": False})
    with pytest.raises(ValueError, match="new --run-id"):
        evaluation.evaluate(**{**kwargs, "explicit_finish": False})
    with pytest.raises(ValueError, match="new --run-id"):
        evaluation.evaluate(**{**kwargs, "instances": [{**instances[0], "problem_statement": "different"}]})
    assert seen == [(True, True)]


def test_run_config_rejects_changed_source_and_legacy_results(tmp_path):
    config = {"source_sha256": {"loop.py": "original"}, "budget_reminders": False}
    evaluation._save_run_config(tmp_path, config)
    with pytest.raises(ValueError, match="source changed"):
        evaluation._save_run_config(tmp_path, {**config, "source_sha256": {"loop.py": "edited"}})
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "predictions.jsonl").write_text('{}\n')
    with pytest.raises(ValueError, match="legacy results"):
        evaluation._save_run_config(legacy, config)


def test_single_shot_rejects_reminders_before_starting(tmp_path):
    with pytest.raises(ValueError, match="requires --mode agent"):
        evaluation.evaluate([], evaluation_llm(), "r", tmp_path, mode="single-shot", budget_reminders=True)
    assert not list(tmp_path.iterdir())


def test_single_shot_rejects_explicit_finish_before_starting(tmp_path):
    with pytest.raises(ValueError, match="--explicit-finish requires --mode agent"):
        evaluation.evaluate([], evaluation_llm(), "r", tmp_path, mode="single-shot", explicit_finish=True)
    assert not list(tmp_path.iterdir())


def test_solve_passes_reminders_to_loop_and_records_count(tmp_path, monkeypatch):
    class Runner:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def loop(*args, **kwargs):
        assert kwargs["budget_reminders"] is True
        assert kwargs["explicit_finish"] is True
        return SimpleNamespace(patch="diff\n", turns=22, finished=True, budget_reminders_sent=1,
                               prompt_tokens=100, cached_tokens=50, completion_tokens=10,
                               stop_reason="finish", final_message="Fixed issue")

    monkeypatch.setattr(evaluation, "solver_runner", Runner)
    monkeypatch.setattr(evaluation, "inspect_image", lambda *a: {"id": "pinned"})
    monkeypatch.setattr(evaluation, "prepare_swebench_environment", lambda *a: None)
    monkeypatch.setattr(evaluation, "run", loop)
    result, patch = evaluation.solve({"instance_id": "x__y-1", "repo": "x/y", "problem_statement": "bug"},
                                     evaluation_llm(), "agent", 40, 180, tmp_path, budget_reminders=True, explicit_finish=True)
    assert not result.error and result.budget_reminders_sent == 1 and patch == "diff\n"
    assert result.stop_reason == "finish" and result.final_message == "Fixed issue"
