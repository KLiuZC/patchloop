from patchloop.repair.swebench import SWEbenchTask


def test_swebench_task_keeps_issue_public_and_test_data_private():
    task = SWEbenchTask.from_instance(
        {
            "instance_id": "pytest-dev__pytest-7205",
            "repo": "pytest-dev/pytest",
            "problem_statement": "Fix fixture output.",
            "image": "swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-7205",
            "base_commit": "abc123",
            "FAIL_TO_PASS": ["hidden::test_target"],
            "test_patch": "secret patch",
        },
        verification_command="pytest -q testing/test_setuponly.py::test_show_fixture_action_with_bytes",
    )

    assert task.image.endswith(":latest")
    assert task.workdir == "/testbed"
    assert task.base_commit == "abc123"
    assert "Fix fixture output." in task.prompt
    assert "hidden::test_target" not in task.prompt
    assert "secret patch" not in task.prompt
    assert task.to_dict()["instance_id"] == "pytest-dev__pytest-7205"
