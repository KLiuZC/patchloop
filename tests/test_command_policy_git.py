from dataclasses import replace

import pytest

from patchloop.command_policy import PolicyContext, assess_command
from patchloop.command_runtime import swebench_command_policy


CONTEXT = PolicyContext("/testbed", "/testbed", target_modules=("xarray",))


def test_bounded_log_requires_initial_history_isolation():
    command = "git log --oneline -5 && sed -n '600,700p' xarray/core/variable.py"
    assert assess_command(command, CONTEXT).classification == "block"
    isolated = replace(CONTEXT, isolated_history=True)
    decision = assess_command(command, isolated)
    assert decision.classification == "allow"
    assert set(decision.rule_ids) == {"baseline_git", "repository_operation"}


@pytest.mark.parametrize("command", [
    "git log -3", "git log --oneline -5", "git log -8 --oneline HEAD",
    "git log -n 5 --oneline", "git log -n5 HEAD", "git log --max-count=10 --oneline",
    "git log --max-count 3 --format='%H %ci %s' HEAD",
    "git -C /testbed log -5 --pretty=oneline --no-decorate",
    "git log -1 --format=-5", "git log -5 --format='-1 --all'",
])
def test_supported_metadata_queries_are_allowed_with_evidence(command):
    assert assess_command(command, replace(CONTEXT, isolated_history=True)).classification == "allow"


@pytest.mark.parametrize("command", [
    "git log", "git log --oneline", "git log -11", "git log -0", "git log -0005",
    "git log -n -1", "git log --max-count=-1", "git log -n 9999999999999999999999",
    "git log -n 1 -5", "git log -n5 --max-count=1", "git log -1 --format -5",
    "git log -5 --all", "git log --all -1", "git log -5 -S values", "git log -Gvalues -3",
    "git log -5 -p", "git log -5 --patch", "git log -5 --stat", "git log -5 --raw",
    "git log -5 --name-only", "git log -5 --numstat", "git log -5 --skip=10",
    "git log -5 --grep=fix", "git log -5 --branches", "git log -5 --tags",
    "git log -5 --reflog", "git log -5 --stdin", "git log -5 --notes",
    "git log -5 HEAD~1", "git log -5 HEAD^", "git log -5 main", "git log -5 origin/main",
    "git log -5 abcdef12", "git log -5 HEAD..main", "git log -5 -- xarray/core/variable.py",
    "git log -3 -- HEAD", "git log -1 HEAD --",
    "git reflog", "git fsck --lost-found", "git rev-list --all", "git show HEAD~1:file.py",
    "git log -3 && pip download xarray",
])
def test_metadata_exception_does_not_allow_history_search_or_extra_content(command):
    assert assess_command(command, replace(CONTEXT, isolated_history=True)).classification == "block"


@pytest.mark.parametrize("command", ["git log -1", "git log -n1 HEAD", "git log --max-count=1 --format='%H'"])
def test_single_head_metadata_stays_allowed_without_isolation_claim(command):
    assert assess_command(command, CONTEXT).classification == "allow"


def environment_report():
    base = "a" * 40
    instance = {"instance_id": "xarray-1", "repo": "pydata/xarray", "base_commit": base}
    report = {
        "instance_id": "xarray-1", "base_commit": base, "status": "passed",
        "checks": {"imports": {}, "git": {"head": base, "commit_objects": 1, "remotes": "",
                   "reflog": [], "alternates_present": False, "tracked_changes": ""}},
    }
    return instance, report


def test_context_builder_sets_isolation_only_from_complete_report_evidence():
    instance, report = environment_report()
    policy = swebench_command_policy("enforce", instance, report, "/testbed")
    assert policy.context.isolated_history is True
    assert policy.metadata()["context"]["isolated_history"] is True
    assert policy.inspect("git log --oneline -5", "metadata")["action"] == "execute"


@pytest.mark.parametrize("field,value", [
    ("head", "b" * 40), ("commit_objects", 2), ("commit_objects", True),
    ("remotes", "origin"), ("reflog", ["a" * 40]), ("alternates_present", True),
    ("tracked_changes", " M file.py"),
])
def test_inconsistent_history_evidence_keeps_the_narrow_rule(field, value):
    instance, report = environment_report()
    report["checks"]["git"][field] = value
    policy = swebench_command_policy("enforce", instance, report, "/testbed")
    assert policy.context.isolated_history is False
    assert policy.inspect("git log -3", "metadata")["action"] == "block"


@pytest.mark.parametrize("field", ["head", "commit_objects", "remotes", "reflog", "alternates_present", "tracked_changes"])
def test_missing_history_evidence_does_not_enable_bounded_logs(field):
    instance, report = environment_report()
    del report["checks"]["git"][field]
    policy = swebench_command_policy("enforce", instance, report, "/testbed")
    assert policy.context.isolated_history is False


@pytest.mark.parametrize("field,value", [("instance_id", "other"), ("base_commit", "b" * 40)])
def test_report_for_another_task_cannot_supply_isolation_evidence(field, value):
    instance, report = environment_report()
    report[field] = value
    assert swebench_command_policy("enforce", instance, report, "/testbed").context.isolated_history is False


@pytest.mark.parametrize("missing", ["instance", "report", "both"])
def test_missing_task_identity_never_counts_as_matching_proof(missing):
    instance, report = environment_report()
    if missing in {"instance", "both"}:
        del instance["instance_id"]
    if missing in {"report", "both"}:
        del report["instance_id"]
    assert swebench_command_policy("enforce", instance, report, "/testbed").context.isolated_history is False


def test_isolation_flag_does_not_weaken_repository_or_shell_boundaries():
    context = replace(CONTEXT, isolated_history=True)
    assert assess_command("git -C /other log -3", context).classification == "unknown"
    assert assess_command("git -c alias.peek='!git log --all' peek", context).classification == "unknown"
    with pytest.raises(ValueError, match="boolean"):
        replace(CONTEXT, isolated_history="yes")
