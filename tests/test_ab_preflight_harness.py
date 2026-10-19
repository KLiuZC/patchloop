from copy import deepcopy
from dataclasses import dataclass, field, make_dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from patchloop import ab_preflight_harness as bridge


IMAGE_ID = "sha256:" + "a" * 64
SECRET = "FAKE_PRIVATE_GOLD_AND_TEST_BODY"
INSTANCE_ID = "fake__project-1"


@dataclass
class FakeSpec:
    instance_id: str
    image: str
    repo: str
    FAIL_TO_PASS: list
    PASS_TO_PASS: list
    eval_script: str
    eval_type: str = "pass_and_fail"
    image_assets: dict = field(default_factory=dict)


@pytest.fixture
def request_data():
    return {
        "instance": {
            "instance_id": INSTANCE_ID, "base_commit": "b" * 40, "repo": "fake/project",
            "version": "1", "image": "untrusted-tag:latest", "patch": SECRET + "\n",
            "test_patch": SECRET + " tests\n", "eval_script": SECRET + " script\n",
            "log_parser": "fake", "eval_type": "pass_and_fail",
            "FAIL_TO_PASS": '["target"]', "PASS_TO_PASS": ["regression"],
        },
        "image_id": IMAGE_ID, "platform": "linux/amd64", "memory": "3g", "cpus": 2.0, "timeout": 1800,
        "baseline_normalization_sha256": hashlib.sha256(Path(bridge.__file__).with_name("ab_baseline.py").read_bytes()).hexdigest(),
    }


class FakeNotFound(Exception):
    pass


class FakeContainers:
    def __init__(self):
        self.live = {}
        self.options = []
        self.removals = []
        self.actual_image = IMAGE_ID
        self.remove_error = False
        self.create_response_error = False

    def get(self, identifier):
        for container in self.live.values():
            if identifier in (container.id, container.name):
                return container
        raise FakeNotFound(identifier)

    def create(self, **kwargs):
        self.options.append(kwargs)
        container_id = f"container-{len(self.options)}"

        def remove(*, force):
            assert force is True
            self.removals.append(container_id)
            if self.remove_error:
                raise RuntimeError(SECRET)
            if container_id not in self.live:
                raise FakeNotFound(container_id)
            del self.live[container_id]

        container = SimpleNamespace(id=container_id, name=kwargs["name"],
                                    image=SimpleNamespace(id=self.actual_image), remove=remove,
                                    attrs={"Config": {"Labels": kwargs.get("labels", {})}})
        container.start = lambda: None
        container.exec_run = lambda *args, **kwargs: SimpleNamespace(exit_code=0, output=(
            "PATCHLOOP_BASELINE_REPORT=" + json.dumps({"status": "passed", "base_commit": "b" * 40,
                "before_head": "c" * 40, "after_head": "b" * 40, "base_tree": "d" * 40, "after_tree": "d" * 40})
        ).encode())
        self.live[container_id] = container
        if self.create_response_error:
            raise RuntimeError(SECRET)
        return container


def fake_report(spec, prediction, status_map):
    groups = {
        group: {"success": [case for case in getattr(spec, group) if status_map.get(case) == "PASSED"],
                "failure": [case for case in getattr(spec, group) if status_map.get(case) != "PASSED"]}
        for group in bridge.GROUPS
    }
    return {spec.instance_id: {
        "patch_is_None": prediction["model_patch"] is None,
        "patch_exists": prediction["model_patch"] is not None,
        "patch_successfully_applied": True, "infra_failure": False,
        "resolved": not any(group["failure"] for group in groups.values()), "tests_status": groups,
    }}


@pytest.fixture
def harness(monkeypatch):
    containers = FakeContainers()
    state = SimpleNamespace(calls=[], faults={}, classification=None, found=True, closed=False,
                            missing_image=False, platform="amd64", created_clients=[])

    def image_get(image):
        assert image == IMAGE_ID
        if state.missing_image:
            raise FakeNotFound(SECRET)
        return SimpleNamespace(id=IMAGE_ID, attrs={"Os": "linux", "Architecture": state.platform})

    def close():
        state.closed = True

    raw = SimpleNamespace(images=SimpleNamespace(get=image_get), containers=containers, close=close)

    def from_env(**kwargs):
        state.created_clients.append(kwargs)
        return raw

    def make_spec(instance):
        return FakeSpec(
            instance_id=instance["instance_id"], image=instance["image"], repo=instance["repo"],
            FAIL_TO_PASS=bridge._test_cases(instance["FAIL_TO_PASS"]),
            PASS_TO_PASS=bridge._test_cases(instance["PASS_TO_PASS"]), eval_script=instance["eval_script"],
        )

    def logs_eval(test_spec, log_fp):
        return json.loads(Path(log_fp).read_text())["statuses"], state.found

    def eval_report(test_spec, prediction, test_log_path, include_tests_status):
        assert include_tests_status is True
        status_map = json.loads(Path(test_log_path).read_text())["statuses"]
        return fake_report(test_spec, prediction, status_map)

    def run_instance(test_spec, pred, client, run_id, timeout=None, rewrite_reports=False,
                     skip_patch=False, task_repo=None):
        state.calls.append({"spec": test_spec, "pred": pred, "run_id": run_id, "timeout": timeout,
                            "rewrite_reports": rewrite_reports, "skip_patch": skip_patch, "task_repo": task_repo})
        phase = "baseline" if skip_patch else "gold"
        assert client.containers.get is not None
        container = client.containers.create(image=test_spec.image, name=client.container_name, user="root",
                                            detach=True, command="tail -f /dev/null", cap_add=["SYS_ADMIN"])
        container.start()
        fault = state.faults.get(phase)
        if fault == "raise":
            raise RuntimeError(SECRET)
        if fault == "interrupt":
            raise KeyboardInterrupt(SECRET)
        if fault == "none":
            return None
        status_map = {"target": "FAILED" if skip_patch else "PASSED", "regression": "PASSED"}
        exit_code = 1 if skip_patch else 0
        if fault in ("missing_target", "skipped_target", "error_target", "xfail_target"):
            status_map.pop("target")
            if fault != "missing_target":
                status_map["target"] = {"skipped_target": "SKIPPED", "error_target": "ERROR",
                                        "xfail_target": "XFAIL"}[fault]
        if fault == "all_pass":
            status_map["target"] = "PASSED"
        if fault == "gold_failure":
            status_map["target"] = "FAILED"
        if fault == "p2p_failure":
            status_map["regression"] = "FAILED"
        if fault == "bad_exit":
            exit_code = 0 if skip_patch else 1
        if fault == "timeout":
            exit_code = 124
        if fault == "missing_exit":
            exit_code = None
        report = fake_report(test_spec, pred, status_map)
        if fault == "missing_target":
            report[test_spec.instance_id]["resolved"] = False
        if fault == "forged_report":
            report[test_spec.instance_id]["resolved"] = not skip_patch
            report[test_spec.instance_id]["tests_status"]["FAIL_TO_PASS"]["failure"] = []
        if fault == "infra":
            report[test_spec.instance_id]["infra_failure"] = True
        log_dir = Path("logs/evaluation") / run_id / pred["model_name_or_path"] / test_spec.instance_id
        log_dir.mkdir(parents=True)
        (log_dir / "report.json").write_text(json.dumps(report))
        (log_dir / "eval.sh").write_text(test_spec.eval_script)
        (log_dir / "run_instance.log").write_text(SECRET)
        (log_dir / "test_output.txt").write_text(json.dumps({"statuses": status_map, "exit": exit_code}))
        if not skip_patch or fault == "baseline_patch":
            (log_dir / "patch.diff").write_text(pred["model_patch"])
        if fault == "missing_log":
            (log_dir / "test_output.txt").unlink()
        if fault == "wrong_patch":
            (log_dir / "patch.diff").write_text("different fake patch")
        if fault == "wrong_script":
            (log_dir / "eval.sh").write_text("different fake script")
        print(SECRET)
        if fault == "wrong_return":
            return "wrong__id", report
        return test_spec.instance_id, report

    api = SimpleNamespace(
        evaluator=SimpleNamespace(run_instance=run_instance), utils=SimpleNamespace(make_test_spec=make_spec),
        grading=SimpleNamespace(get_logs_eval=logs_eval, get_eval_report=eval_report,
                                parse_test_exit_code=lambda content: json.loads(content)["exit"],
                                _resolve_case=lambda case, sm: case if case in sm else None),
        infrastructure=SimpleNamespace(classify_logs=lambda *paths: state.classification),
        docker=SimpleNamespace(from_env=from_env, errors=SimpleNamespace(NotFound=FakeNotFound)),
        source_hashes={"fake_harness": "f" * 64},
    )
    loader = bridge._load_harness
    monkeypatch.setattr(bridge, "_load_harness", lambda: api)
    return SimpleNamespace(api=api, state=state, raw=raw, containers=containers, loader=loader)


def invoke(tmp_path, request_data):
    request_path = tmp_path / "private_request.json"
    request_path.write_text(json.dumps(request_data))
    output = tmp_path / "private-controls" / "fresh"
    return bridge.run_bridge(request_path, output), output, request_path


def test_bridge_runs_real_empty_baseline_and_gold_in_separate_pinned_containers(tmp_path, request_data, harness, capsys):
    original = deepcopy(request_data)
    result, output, request_path = invoke(tmp_path, request_data)
    assert result["status"] == "passed"
    assert result["instance_id"] == INSTANCE_ID and result["base_commit"] == "b" * 40
    assert result["image_id"] == IMAGE_ID and result["platform"] == "linux/amd64"
    assert result["request_sha256"] == hashlib.sha256(request_path.read_bytes()).hexdigest()
    assert request_data == original
    assert json.loads((output / "result.json").read_text()) == result
    baseline, gold = harness.state.calls
    assert baseline["pred"]["model_patch"] == "" and baseline["skip_patch"] is True
    assert gold["pred"]["model_patch"] == request_data["instance"]["patch"] and gold["skip_patch"] is False
    assert baseline["run_id"] != gold["run_id"]
    assert result["baseline"]["container_id"] != result["gold"]["container_id"]
    for call in harness.state.calls:
        assert call["spec"].image == IMAGE_ID
        assert call["timeout"] == 1800 and call["rewrite_reports"] is False and call["task_repo"] is None
    assert result["baseline"]["tests"]["FAIL_TO_PASS"] == {"passed": 0, "failed": 1}
    assert result["baseline"]["model_patch_applied"] is False
    assert result["gold"]["resolved"] is True and result["gold"]["model_patch_applied"] is True
    assert all(record["removed"] and record["image_id"] == IMAGE_ID for record in result["containers"])
    assert not harness.containers.live and harness.state.closed
    assert result["source_hashes"]["fake_harness"] == "f" * 64
    for relative, entry in result["artifacts"].items():
        assert entry["sha256"] == hashlib.sha256((output / relative).read_bytes()).hexdigest()
    assert SECRET not in json.dumps(result)
    assert SECRET in (output / "bridge.log").read_text()
    assert not capsys.readouterr().out
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "result.json").stat().st_mode & 0o777 == 0o600


def test_ambiguous_text_does_not_override_verified_target_results(tmp_path, request_data, harness):
    harness.state.classification = ("no_tests_collected", "ambiguous")
    result, _, _ = invoke(tmp_path, request_data)
    assert result["status"] == "passed"
    assert result["baseline"]["failure_classification"]["tier"] == "ambiguous"
    assert result["baseline"]["tests"]["FAIL_TO_PASS"]["failed"] == 1
    assert result["gold"]["resolved"] is True
    for options in harness.containers.options:
        assert options["image"] == IMAGE_ID and options["network_disabled"] is True
        assert options["platform"] == "linux/amd64" and options["mem_limit"] == "3g"
        assert options["nano_cpus"] == 2_000_000_000 and options["cap_drop"] == ["ALL"]
        assert options["cap_add"] == [] and options["security_opt"] == ["no-new-privileges:true"]
        assert not set(options) & {"mounts", "volumes", "privileged"}


@pytest.mark.parametrize("fault", ["error", "wrong_base", "wrong_tree", "missing_proof"])
def test_controls_reject_bad_normalization_before_grading(tmp_path, request_data, harness, fault):
    original = harness.containers.create
    def create(**kwargs):
        container = original(**kwargs)
        proof = {"status": "passed", "base_commit": "b" * 40, "before_head": "c" * 40,
                 "after_head": "b" * 40, "base_tree": "d" * 40, "after_tree": "d" * 40}
        if fault == "wrong_base":
            proof["after_head"] = "e" * 40
        elif fault == "wrong_tree":
            proof["after_tree"] = "e" * 40
        content = b"" if fault == "missing_proof" else ("PATCHLOOP_BASELINE_REPORT=" + json.dumps(proof)).encode()
        container.exec_run = lambda *args, **options: SimpleNamespace(exit_code=1 if fault == "error" else 0, output=content)
        return container
    harness.containers.create = create
    result, _, _ = invoke(tmp_path, request_data)
    assert result["status"] == "error" and result["gold"]["status"] == "not_run"
    assert not harness.containers.live


def test_controls_bind_normalizer_implementation_before_docker(tmp_path, request_data, harness):
    request_data["baseline_normalization_sha256"] = "0" * 64
    result, _, _ = invoke(tmp_path, request_data)
    assert result["status"] == "error" and result["error_code"] == "normalization_source_changed"
    assert not harness.state.created_clients


@pytest.mark.parametrize("phase,fault", [
    ("baseline", "none"), ("baseline", "raise"), ("baseline", "interrupt"),
    ("baseline", "all_pass"), ("baseline", "missing_target"), ("baseline", "skipped_target"),
    ("baseline", "error_target"), ("baseline", "p2p_failure"), ("baseline", "timeout"),
    ("baseline", "bad_exit"), ("baseline", "missing_exit"), ("baseline", "baseline_patch"),
    ("baseline", "missing_log"), ("baseline", "wrong_return"), ("baseline", "wrong_script"),
    ("baseline", "forged_report"), ("baseline", "infra"),
    ("gold", "gold_failure"), ("gold", "bad_exit"), ("gold", "p2p_failure"),
    ("gold", "wrong_patch"), ("gold", "raise"), ("gold", "xfail_target"),
])
def test_failed_controls_are_errors_and_always_cleaned(tmp_path, request_data, harness, phase, fault):
    harness.state.faults[phase] = fault
    result, output, _ = invoke(tmp_path, request_data)
    assert result["status"] == "error" and result[phase]["status"] == "error"
    assert result["error_code"]
    assert not harness.containers.live and harness.state.closed
    assert all(record["removed"] for record in result["containers"])
    assert SECRET not in (output / "result.json").read_text()
    if phase == "baseline":
        assert len(harness.state.calls) == 1 and result["gold"]["status"] == "not_run"


@pytest.mark.parametrize("setting,value", [("found", False), ("classification", ("network_unreachable", "environment")),
                                          ("missing_image", True), ("platform", "arm64")])
def test_invalid_environment_or_unexecuted_tests_never_pass(tmp_path, request_data, harness, setting, value):
    setattr(harness.state, setting, value)
    result, _, _ = invoke(tmp_path, request_data)
    assert result["status"] == "error"
    assert not harness.containers.live and harness.state.closed


def test_actual_container_image_must_match_and_mismatch_is_cleaned(tmp_path, request_data, harness):
    harness.containers.actual_image = "sha256:" + "c" * 64
    result, _, _ = invoke(tmp_path, request_data)
    assert result["status"] == "error" and result["error_code"] == "container_image_mismatch"
    assert result["containers"][0]["image_id"] != IMAGE_ID
    assert not harness.containers.live


def test_cleanup_failure_is_not_a_pass(tmp_path, request_data, harness):
    harness.containers.remove_error = True
    result, _, _ = invoke(tmp_path, request_data)
    assert result["status"] == "error" and result["error_code"] == "container_cleanup_failed"
    assert not result["containers"][0]["removed"]
    assert harness.state.closed


def test_create_response_error_recovers_and_removes_owned_container(tmp_path, request_data, harness):
    harness.containers.create_response_error = True
    result, _, _ = invoke(tmp_path, request_data)
    assert result["status"] == "error"
    assert len(result["containers"]) == 1 and result["containers"][0]["removed"]
    assert not harness.containers.live and harness.state.closed


def test_pending_cleanup_never_removes_another_developers_container(request_data, harness):
    client = bridge.LocalOnlyDockerClient(harness.raw, request_data, FakeNotFound)
    other = harness.containers.create(name="unrelated", image=IMAGE_ID)
    client.pending.append({"name": "unrelated", "phase": "baseline"})
    with pytest.raises(bridge.BridgeError, match="container_cleanup_failed"):
        client.cleanup()
    assert harness.containers.get(other.id) is other
    assert not harness.containers.removals


def test_container_name_collision_never_deletes_existing_container(request_data, harness):
    client = bridge.LocalOnlyDockerClient(harness.raw, request_data, FakeNotFound)
    client.begin("baseline", "fresh")
    other = harness.containers.create(name=client.container_name, image=IMAGE_ID)
    with pytest.raises(bridge.BridgeError, match="container_name_collision"):
        client.containers.create(image=IMAGE_ID, name=client.container_name, user="root",
                                 detach=True, command="tail -f /dev/null")
    client.cleanup()
    assert harness.containers.get(other.id) is other
    assert not harness.containers.removals


def test_existing_output_is_never_reused_or_modified(tmp_path, request_data, harness):
    _, output, request_path = invoke(tmp_path, request_data)
    saved = (output / "result.json").read_bytes()
    with pytest.raises(FileExistsError):
        bridge.run_bridge(request_path, output)
    assert (output / "result.json").read_bytes() == saved
    assert len(harness.state.calls) == 2


@pytest.mark.parametrize("field,value", [("image_id", "mutable:latest"), ("platform", "linux/arm64"),
                                       ("cpus", True), ("memory", "9g"), ("timeout", 0)])
def test_invalid_request_is_rejected_before_harness_import(tmp_path, request_data, monkeypatch, field, value):
    request_data[field] = value
    monkeypatch.setattr(bridge, "_load_harness", lambda: pytest.fail("must not import the harness"))
    result, _, _ = invoke(tmp_path, request_data)
    assert result["status"] == "error"


@pytest.mark.parametrize("field,value", [("patch", ""), ("instance_id", "../fake__task"),
                                       ("FAIL_TO_PASS", "[]"), ("FAIL_TO_PASS", '["target", "target"]'),
                                       ("eval_type", "fail_only"), ("image_assets", {"patch": []}),
                                       ("image_assets", '{"test_patch":[{"url":"https://invalid.test"}]}')])
def test_unsupported_instance_is_rejected_without_docker(request_data, field, value):
    request_data["instance"][field] = value
    with pytest.raises(bridge.BridgeError):
        bridge.validate_request(request_data)


def test_unknown_harness_version_is_rejected_before_imports(monkeypatch):
    monkeypatch.setattr(bridge.metadata, "version", lambda name: "5.0.3")
    monkeypatch.setattr(bridge.importlib, "import_module", lambda name: pytest.fail("no imports for unknown version"))
    with pytest.raises(bridge.BridgeError, match="unsupported_harness_version"):
        bridge._load_harness()


def test_unknown_interface_is_rejected():
    with pytest.raises(bridge.BridgeError, match="unsupported_harness_interface"):
        bridge._check_interface(lambda test_spec, pred: None, ("test_spec", "pred", "skip_patch"))


def test_supported_version_still_requires_exact_interfaces(tmp_path, harness, monkeypatch):
    spec_type = make_dataclass("TestSpec", [(name, object) for name in (
        "instance_id", "image", "eval_script_list", "repo", "version", "FAIL_TO_PASS", "PASS_TO_PASS",
        "log_parser", "eval_type", "image_assets",
    )])
    harness.api.evaluator.RUN_EVALUATION_LOG_DIR = Path("logs/evaluation")

    def classify_logs(*log_paths):
        return None

    harness.api.infrastructure.classify_logs = classify_logs
    modules = {
        "swebench.harness.run_evaluation": harness.api.evaluator,
        "swebench.harness.utils": harness.api.utils,
        "swebench.harness.grading": harness.api.grading,
        "swebench.harness.infra_failure": harness.api.infrastructure,
        "swebench.types": SimpleNamespace(TestSpec=spec_type),
        "swebench.harness.docker_utils": SimpleNamespace(),
        "swebench.harness.log_parsers": SimpleNamespace(),
        "docker": harness.api.docker,
    }
    for name, module in modules.items():
        path = tmp_path / (name + ".py")
        path.write_text("fake implementation\n")
        module.__name__ = name
        module.__file__ = str(path)
    monkeypatch.setattr(bridge.metadata, "version", lambda name: "5.0.2")
    monkeypatch.setattr(bridge.importlib, "import_module", modules.__getitem__)
    loaded = harness.loader()
    assert loaded.evaluator is harness.api.evaluator and loaded.source_hashes
    assert not harness.state.created_clients
    harness.api.evaluator.run_instance = lambda test_spec, pred: None
    with pytest.raises(bridge.BridgeError, match="unsupported_harness_interface"):
        harness.loader()


def test_local_client_denies_download_build_and_other_images(request_data, harness):
    client = bridge.LocalOnlyDockerClient(harness.raw, request_data, FakeNotFound)
    for operation in (client.images.pull, client.images.build):
        with pytest.raises(bridge.BridgeError):
            operation(IMAGE_ID)
    with pytest.raises(bridge.BridgeError, match="unapproved_image"):
        client.images.get("untrusted-tag:latest")
    client.begin("baseline", "fresh")
    with pytest.raises(bridge.BridgeError, match="unapproved_image"):
        client.containers.create(image="different:latest", name=client.container_name)
    with pytest.raises(FakeNotFound):
        client.containers.get("unrelated-developer-container")
    assert not harness.containers.options


def test_pure_evidence_helper_rejects_missing_target_even_when_report_says_failure(request_data, harness):
    spec = harness.api.utils.make_test_spec(request_data["instance"])
    statuses = {"regression": "PASSED"}
    verdict = fake_report(spec, {"model_patch": ""}, statuses)[INSTANCE_ID]
    with pytest.raises(bridge.BridgeError, match="target_test_missing"):
        bridge.validate_evidence(spec, verdict, statuses, 1, baseline=True,
                                 resolve_case=harness.api.grading._resolve_case)


@pytest.mark.parametrize("extra", [{"volumes": {"/host": {"bind": "/testbed"}}},
                                   {"privileged": True}, {"network_mode": "host"}])
def test_container_adapter_rejects_unexpected_options(request_data, harness, extra):
    client = bridge.LocalOnlyDockerClient(harness.raw, request_data, FakeNotFound)
    client.begin("baseline", "fresh")
    with pytest.raises(bridge.BridgeError, match="unsupported_container_options"):
        client.containers.create(image=IMAGE_ID, name=client.container_name, user="root",
                                 detach=True, command="tail -f /dev/null", **extra)
    assert not harness.containers.options


@pytest.mark.parametrize("status", ["SKIPPED", "XFAIL", "ERROR"])
def test_gold_requires_actual_passes_even_if_official_report_claims_resolution(request_data, harness, status):
    spec = harness.api.utils.make_test_spec(request_data["instance"])
    verdict = fake_report(spec, {"model_patch": SECRET}, {"target": "PASSED", "regression": "PASSED"})[INSTANCE_ID]
    with pytest.raises(bridge.BridgeError):
        bridge.validate_evidence(spec, verdict, {"target": status, "regression": "PASSED"}, 0,
                                 baseline=False, resolve_case=harness.api.grading._resolve_case)


def test_truncated_targets_require_unambiguous_executed_statuses(request_data, harness):
    spec = harness.api.utils.make_test_spec(request_data["instance"])
    spec.FAIL_TO_PASS = ["target["]
    verdict = fake_report(spec, {"model_patch": ""}, {"target[": "FAILED", "regression": "PASSED"})[INSTANCE_ID]
    statuses = {"target[1]": "FAILED", "target[2]": "FAILED", "regression": "PASSED"}

    def resolve(case, status_map):
        return next((name for name in status_map if name.startswith(case)), None)

    assert bridge.validate_evidence(spec, verdict, statuses, 1, baseline=True,
                                    resolve_case=resolve)["status"] == "passed"
    statuses["target[2]"] = "SKIPPED"
    with pytest.raises(bridge.BridgeError, match="ambiguous_target_status"):
        bridge.validate_evidence(spec, verdict, statuses, 1, baseline=True, resolve_case=resolve)


def test_cli_help_does_not_need_harness_or_model():
    process = subprocess.run([sys.executable, "-I", "-S", bridge.__file__, "--help"],
                             capture_output=True, text=True, timeout=10)
    assert process.returncode == 0 and "--input" in process.stdout and "--output" in process.stdout


def test_cli_prints_only_sanitized_status(tmp_path, request_data, harness, capsys):
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request_data))
    assert bridge.main(["--input", str(request_path), "--output", str(tmp_path / "out")]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "passed"}


def test_cli_error_status_does_not_expose_exception_text(tmp_path, request_data, harness, capsys):
    harness.state.faults["gold"] = "raise"
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request_data))
    assert bridge.main(["--input", str(request_path), "--output", str(tmp_path / "out")]) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "error"}
