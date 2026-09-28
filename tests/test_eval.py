from types import SimpleNamespace

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
