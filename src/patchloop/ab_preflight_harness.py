"""Private, model-free SWE-bench baseline/gold subprocess bridge."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import fields, is_dataclass, replace
import hashlib
import importlib
from importlib import metadata
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import signal
import traceback
from types import SimpleNamespace
import uuid


SUPPORTED_VERSION = "5.0.2"
GROUPS = ("FAIL_TO_PASS", "PASS_TO_PASS")


class BridgeError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise BridgeError(code)


def _test_cases(value):
    try:
        cases = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        raise BridgeError("invalid_test_cases") from None
    require(isinstance(cases, list) and all(isinstance(case, str) and case for case in cases),
            "invalid_test_cases")
    require(len(set(cases)) == len(cases), "duplicate_test_cases")
    return cases


def validate_request(request):
    """Validate configuration without importing the harness or contacting Docker."""
    require(isinstance(request, dict), "invalid_request")
    row = request.get("instance")
    require(isinstance(row, dict), "invalid_instance")
    for name in ("instance_id", "base_commit", "repo", "version", "image", "patch", "test_patch",
                 "eval_script", "log_parser", "eval_type"):
        require(isinstance(row.get(name), str) and bool(row[name].strip()), "missing_instance_field")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*__[A-Za-z0-9_.-]+", row["instance_id"]),
            "invalid_instance_id")
    require(re.fullmatch(r"[0-9a-f]{40}", row["base_commit"]), "invalid_base_commit")
    require(row["eval_type"] == "pass_and_fail", "unsupported_eval_type")
    groups = {group: _test_cases(row.get(group)) for group in GROUPS}
    require(bool(groups["FAIL_TO_PASS"]), "empty_fail_to_pass")
    require(not set(groups["FAIL_TO_PASS"]) & set(groups["PASS_TO_PASS"]), "overlapping_test_cases")
    assets = row.get("image_assets")
    if isinstance(assets, str) and assets:
        try:
            assets = json.loads(assets)
        except ValueError:
            raise BridgeError("invalid_image_assets") from None
    require(assets is None or assets == "" or isinstance(assets, dict) and not assets,
            "image_assets_not_supported")
    require(isinstance(request.get("image_id"), str) and
            re.fullmatch(r"sha256:[0-9a-f]{64}", request["image_id"]), "invalid_image_id")
    require(request.get("platform") == "linux/amd64", "unsupported_platform")
    require(request.get("memory") == "3g", "unsupported_memory")
    require(type(request.get("cpus")) in (int, float) and request["cpus"] == 2.0, "unsupported_cpus")
    require(type(request.get("timeout")) is int and request["timeout"] > 0, "invalid_timeout")
    return request


def validate_evidence(spec, verdict, status_map, test_exit_code, *, baseline, resolve_case):
    """Require observed target results, not merely unresolved or empty predictions."""
    require(isinstance(verdict, dict), "invalid_verdict")
    require(verdict.get("patch_is_None") is False and verdict.get("patch_exists") is True and
            verdict.get("patch_successfully_applied") is True, "invalid_execution_evidence")
    require(verdict.get("infra_failure") is False, "infrastructure_failure")
    require(verdict.get("resolved") is (not baseline), "unexpected_resolution")
    require(type(test_exit_code) is int, "missing_test_exit_code")
    require(test_exit_code > 0 and test_exit_code not in (124, 137) if baseline else test_exit_code == 0,
            "unexpected_test_exit_code")
    require(isinstance(status_map, dict) and bool(status_map) and
            all(isinstance(case, str) and isinstance(status, str) for case, status in status_map.items()),
            "missing_test_results")
    require("ERROR" not in status_map.values(), "test_execution_error")
    reported = verdict.get("tests_status")
    require(isinstance(reported, dict), "missing_test_groups")
    counts = {}
    for group in GROUPS:
        cases = getattr(spec, group)
        require(isinstance(cases, list), "invalid_spec_test_cases")
        observed = {"PASSED": [], "FAILED": []}
        for case in cases:
            key = resolve_case(case, status_map)
            require(key is not None and key in status_map, "target_test_missing")
            status = status_map[key]
            require(status in observed, "target_test_not_executed")
            if case not in status_map and case.count("[") > case.count("]"):
                require({value for name, value in status_map.items() if name.startswith(case)} == {status},
                        "ambiguous_target_status")
            observed[status].append(case)
        result = reported.get(group)
        require(isinstance(result, dict), "missing_test_group")
        for label, status in (("success", "PASSED"), ("failure", "FAILED")):
            reported_cases = result.get(label)
            require(isinstance(reported_cases, list) and
                    all(isinstance(case, str) for case in reported_cases), "invalid_test_group")
            require(sorted(reported_cases) == sorted(observed[status]), "report_parser_disagreement")
        require(not observed["FAILED"] if not baseline or group == "PASS_TO_PASS" else True,
                "target_tests_not_passing")
        counts[group] = {"passed": len(observed["PASSED"]), "failed": len(observed["FAILED"])}
    require(bool(spec.FAIL_TO_PASS), "empty_fail_to_pass")
    if baseline:
        require(counts["FAIL_TO_PASS"]["failed"] > 0, "baseline_did_not_reproduce")
    return {"status": "passed", "executed": True, "resolved": verdict["resolved"],
            "model_patch_applied": not baseline, "test_exit_code": test_exit_code, "tests": counts}


def _check_interface(function, names):
    require(callable(function) and tuple(inspect.signature(function).parameters) == names,
            "unsupported_harness_interface")


def _load_harness():
    require(metadata.version("swebench") == SUPPORTED_VERSION, "unsupported_harness_version")
    evaluator = importlib.import_module("swebench.harness.run_evaluation")
    utils = importlib.import_module("swebench.harness.utils")
    grading = importlib.import_module("swebench.harness.grading")
    infrastructure = importlib.import_module("swebench.harness.infra_failure")
    types = importlib.import_module("swebench.types")
    docker = importlib.import_module("docker")
    _check_interface(evaluator.run_instance, ("test_spec", "pred", "client", "run_id", "timeout",
                                            "rewrite_reports", "skip_patch", "task_repo"))
    parameters = inspect.signature(evaluator.run_instance).parameters
    require(parameters["skip_patch"].default is False and parameters["rewrite_reports"].default is False,
            "unsupported_harness_interface")
    _check_interface(utils.make_test_spec, ("instance",))
    _check_interface(grading.get_logs_eval, ("test_spec", "log_fp"))
    _check_interface(grading.get_eval_report, ("test_spec", "prediction", "test_log_path", "include_tests_status"))
    _check_interface(grading.parse_test_exit_code, ("content",))
    _check_interface(grading._resolve_case, ("case", "sm"))
    _check_interface(infrastructure.classify_logs, ("log_paths",))
    require(is_dataclass(types.TestSpec) and {field.name for field in fields(types.TestSpec)} == {
        "instance_id", "image", "eval_script_list", "repo", "version", "FAIL_TO_PASS", "PASS_TO_PASS",
        "log_parser", "eval_type", "image_assets",
    }, "unsupported_test_spec")
    require(evaluator.RUN_EVALUATION_LOG_DIR == Path("logs/evaluation"), "unsupported_harness_log_root")
    modules = (evaluator, utils, grading, infrastructure, types,
               importlib.import_module("swebench.harness.docker_utils"),
               importlib.import_module("swebench.harness.log_parsers"))
    sources = {module.__name__: _sha256(Path(module.__file__)) for module in modules}
    parser_root = Path(modules[-1].__file__).parent
    sources.update({"log_parsers/" + str(path.relative_to(parser_root)): _sha256(path)
                    for path in sorted(parser_root.rglob("*.py"))})
    return SimpleNamespace(evaluator=evaluator, utils=utils, grading=grading,
                           infrastructure=infrastructure, docker=docker, source_hashes=sources)


class LocalOnlyImages:
    def __init__(self, owner):
        self.owner = owner

    def get(self, image):
        require(image == self.owner.request["image_id"], "unapproved_image")
        result = self.owner.raw.images.get(image)
        require(result.id == image, "image_identity_mismatch")
        require(f"{result.attrs.get('Os')}/{result.attrs.get('Architecture')}" == self.owner.request["platform"],
                "image_platform_mismatch")
        return result

    def pull(self, *args, **kwargs):
        raise BridgeError("image_pull_forbidden")

    def build(self, *args, **kwargs):
        raise BridgeError("image_build_forbidden")


class LocalOnlyContainers:
    def __init__(self, owner):
        self.owner = owner

    def get(self, name):
        raise self.owner.not_found("Container lookup is not permitted")

    def create(self, **kwargs):
        owner = self.owner
        require(owner.phase is not None, "container_outside_phase")
        require(kwargs.get("image") == owner.request["image_id"], "unapproved_image")
        require(kwargs.get("name") == owner.container_name, "unapproved_container_name")
        require(not any(record["phase"] == owner.phase for record in owner.records), "duplicate_phase_container")
        require(set(kwargs) <= {"image", "name", "user", "detach", "command", "cap_add"},
                "unsupported_container_options")
        require(kwargs.get("user") == "root" and kwargs.get("detach") is True and
                kwargs.get("command") == "tail -f /dev/null", "unsupported_container_options")
        owner.images.get(kwargs["image"])
        try:
            owner.raw.containers.get(owner.container_name)
        except owner.not_found:
            pass
        else:
            raise BridgeError("container_name_collision")
        kwargs.update(platform=owner.request["platform"], network_disabled=True,
                      mem_limit=owner.request["memory"], nano_cpus=int(owner.request["cpus"] * 1_000_000_000),
                      cap_add=[], cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
                      labels={"patchloop.ab-preflight": owner.token})
        pending = {"name": owner.container_name, "phase": owner.phase}
        owner.pending.append(pending)
        container = owner.raw.containers.create(**kwargs)
        record = owner.track(container, owner.phase)
        owner.pending.remove(pending)
        record["image_id"] = container.image.id
        require(record["image_id"] == owner.request["image_id"], "container_image_mismatch")
        return NormalizedContainer(container, owner, record)


class NormalizedContainer:
    def __init__(self, container, owner, record):
        self.container = container
        self.owner = owner
        self.record = record

    def __getattr__(self, name):
        return getattr(self.container, name)

    def start(self):
        require("baseline_normalization" not in self.record, "container_already_normalized")
        self.container.start()
        helper = Path(__file__).with_name("ab_baseline.py")
        source = helper.read_text()
        require(hashlib.sha256(source.encode()).hexdigest() == self.owner.request["baseline_normalization_sha256"],
                "normalization_source_changed")
        payload = json.dumps({"root": "/testbed", "base_commit": self.owner.request["instance"]["base_commit"]})
        command = "python3 -c " + shlex.quote(source) + " " + shlex.quote(payload)
        response = self.container.exec_run(["timeout", "-s", "KILL", "180", "/bin/bash", "-lc", command],
                                           workdir="/testbed", user="root")
        prefix = "PATCHLOOP_BASELINE_REPORT="
        lines = [line[len(prefix):] for line in response.output.decode("utf-8").splitlines() if line.startswith(prefix)]
        require(response.exit_code == 0 and len(lines) == 1, "baseline_normalization_failed")
        proof = json.loads(lines[0])
        self.record["baseline_normalization"] = proof
        base = self.owner.request["instance"]["base_commit"]
        require(proof.get("status") == "passed" and proof.get("base_commit") == proof.get("after_head") == base and
                proof.get("base_tree") and proof.get("after_tree") == proof["base_tree"], "baseline_normalization_failed")


class LocalOnlyDockerClient:
    """Expose only locally pinned images and fresh, resource-limited containers."""

    def __init__(self, raw, request, not_found):
        self.raw = raw
        self.request = request
        self.not_found = not_found
        self.images = LocalOnlyImages(self)
        self.containers = LocalOnlyContainers(self)
        self.phase = None
        self.container_name = None
        self.token = uuid.uuid4().hex
        self.pending = []
        self.owned = []
        self.records = []

    def begin(self, phase, run_id):
        self.phase = phase
        self.container_name = f"sweb.eval.{self.request['instance']['instance_id'].lower()}.{run_id}"

    def track(self, container, phase):
        for owned, record in self.owned:
            if owned.id == container.id:
                require(record["phase"] == phase, "reused_container")
                return record
        record = {"phase": phase, "container_id": container.id, "image_id": None, "removed": False}
        self.owned.append((container, record))
        self.records.append(record)
        return record

    def cleanup(self):
        failures = []
        for pending in list(self.pending):
            try:
                container = self.raw.containers.get(pending["name"])
                require(container.attrs.get("Config", {}).get("Labels", {}).get("patchloop.ab-preflight") == self.token,
                        "unowned_container")
                record = self.track(container, pending["phase"])
                self.pending.remove(pending)
                record["image_id"] = container.image.id
            except self.not_found:
                self.pending.remove(pending)
            except Exception:
                traceback.print_exc()
                failures.append(pending["name"])
        for container, record in self.owned:
            if record["removed"]:
                continue
            try:
                container.remove(force=True)
            except Exception:
                traceback.print_exc()
            try:
                self.raw.containers.get(container.id)
            except self.not_found:
                record["removed"] = True
            except Exception:
                traceback.print_exc()
            if not record["removed"]:
                failures.append(container.id)
        require(not failures, "container_cleanup_failed")


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_phase(request, spec, harness, client, phase, phase_result):
    baseline = phase == "baseline"
    run_id = "ab-preflight-" + phase + "-" + uuid.uuid4().hex
    prediction = {"instance_id": spec.instance_id, "model_name_or_path": "ab6-" + phase,
                  "model_patch": "" if baseline else request["instance"]["patch"]}
    log_dir = Path("logs/evaluation") / run_id / prediction["model_name_or_path"] / spec.instance_id
    phase_result.update(status="running", run_id=run_id, log_dir=str(log_dir))
    require(not log_dir.exists(), "cached_harness_output")
    client.begin(phase, run_id)
    try:
        returned = harness.evaluator.run_instance(
            test_spec=spec, pred=prediction, client=client, run_id=run_id,
            timeout=request["timeout"], rewrite_reports=False, skip_patch=baseline, task_repo=None,
        )
    finally:
        client.cleanup()
    records = [record for record in client.records if record["phase"] == phase]
    require(len(records) == 1 and records[0]["removed"], "missing_container_evidence")
    require(records[0].get("baseline_normalization", {}).get("status") == "passed", "missing_baseline_evidence")
    phase_result.update(container_id=records[0]["container_id"], image_id=records[0]["image_id"])
    phase_result["baseline_normalization"] = records[0]["baseline_normalization"]
    require(isinstance(returned, tuple) and len(returned) == 2 and returned[0] == spec.instance_id,
            "harness_run_incomplete")
    for filename in ("report.json", "test_output.txt", "run_instance.log", "eval.sh"):
        require((log_dir / filename).is_file() and (log_dir / filename).stat().st_size > 0,
                "missing_harness_artifact")
    report = json.loads((log_dir / "report.json").read_text())
    require(isinstance(report, dict) and set(report) == {spec.instance_id} and returned[1] == report,
            "harness_report_mismatch")
    require((log_dir / "eval.sh").read_text() == spec.eval_script, "eval_script_mismatch")
    patch_file = log_dir / "patch.diff"
    if baseline:
        require(not patch_file.exists(), "baseline_patch_artifact")
    else:
        require(patch_file.is_file() and patch_file.read_bytes() == prediction["model_patch"].encode("utf-8"),
                "gold_patch_mismatch")
    test_log = log_dir / "test_output.txt"
    classification = harness.infrastructure.classify_logs(test_log, log_dir / "run_instance.log")
    if classification is not None:
        require(isinstance(classification, tuple) and len(classification) == 2 and
                classification[1] in {"environment", "ambiguous"}, "unknown_failure_classification")
        phase_result["failure_classification"] = {"reason": classification[0], "tier": classification[1]}
        require(classification[1] != "environment", "harness_failure_classification")
    status_map, found = harness.grading.get_logs_eval(spec, str(test_log))
    require(found is True, "unparseable_test_output")
    regenerated = harness.grading.get_eval_report(
        test_spec=spec, prediction=prediction, test_log_path=str(test_log), include_tests_status=True,
    )
    require(regenerated == report, "regraded_report_mismatch")
    phase_result.update(validate_evidence(
        spec, report[spec.instance_id], status_map,
        harness.grading.parse_test_exit_code(test_log.read_text()), baseline=baseline,
        resolve_case=harness.grading._resolve_case,
    ))


def run_bridge(input_path, output_dir):
    """Create a private, new output directory and return a sanitized acceptance result."""
    input_path = Path(input_path).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().absolute()
    previous_umask = os.umask(0o077)
    previous_directory = Path.cwd()
    try:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        result = {"schema_version": 1, "status": "error", "instance_id": None, "base_commit": None,
                  "image_id": None, "platform": None, "baseline": {"status": "not_run"},
                  "gold": {"status": "not_run"}, "containers": [], "source_hashes": {}, "artifacts": {}}
        with (output_dir / "bridge.log").open("x", encoding="utf-8") as log, redirect_stdout(log), redirect_stderr(log):
            raw = None
            client = None
            try:
                request_bytes = input_path.read_bytes()
                result["request_sha256"] = hashlib.sha256(request_bytes).hexdigest()
                request = validate_request(json.loads(request_bytes))
                row = request["instance"]
                result.update(instance_id=row["instance_id"], base_commit=row["base_commit"],
                              **{name: request[name] for name in ("image_id", "platform", "memory", "cpus", "timeout")})
                result["source_hashes"]["bridge"] = _sha256(Path(__file__))
                result["source_hashes"]["baseline_normalizer"] = _sha256(Path(__file__).with_name("ab_baseline.py"))
                require(result["source_hashes"]["baseline_normalizer"] == request.get("baseline_normalization_sha256"),
                        "normalization_source_changed")
                harness = _load_harness()
                result["harness_version"] = SUPPORTED_VERSION
                result["source_hashes"].update(harness.source_hashes)
                spec = replace(harness.utils.make_test_spec(row), image=request["image_id"])
                require(not spec.image_assets, "image_assets_not_supported")
                require(spec.instance_id == row["instance_id"] and spec.repo == row["repo"] and
                        spec.eval_type == "pass_and_fail" and all(
                            getattr(spec, group) == _test_cases(row[group]) for group in GROUPS),
                        "test_spec_mismatch")
                result["eval_script_sha256"] = hashlib.sha256(spec.eval_script.encode("utf-8")).hexdigest()
                os.chdir(output_dir)
                raw = harness.docker.from_env(timeout=request["timeout"], max_pool_size=4)
                client = LocalOnlyDockerClient(raw, request, harness.docker.errors.NotFound)
                result["containers"] = client.records
                client.images.get(request["image_id"])
                for phase in ("baseline", "gold"):
                    _run_phase(request, spec, harness, client, phase, result[phase])
                require(result["baseline"]["container_id"] != result["gold"]["container_id"],
                        "reused_container")
                result["status"] = "passed"
            except BaseException as exc:
                traceback.print_exc()
                result["error_code"] = exc.code if isinstance(exc, BridgeError) else "bridge_exception"
                for phase in ("baseline", "gold"):
                    if result[phase]["status"] == "running":
                        result[phase]["status"] = "error"
            finally:
                try:
                    if client is not None:
                        client.cleanup()
                except BaseException:
                    traceback.print_exc()
                    result.update(status="error", error_code="container_cleanup_failed")
                try:
                    if raw is not None:
                        raw.close()
                except BaseException:
                    traceback.print_exc()
                    result.update(status="error", error_code="docker_client_close_failed")
        for path in sorted(output_dir.rglob("*")):
            if path.is_file():
                result["artifacts"][str(path.relative_to(output_dir))] = {
                    "sha256": _sha256(path), "bytes": path.stat().st_size,
                }
        with (output_dir / "result.json").open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        return result
    finally:
        os.chdir(previous_directory)
        os.umask(previous_umask)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    def interrupted(signum, frame):
        raise BridgeError("interrupted")

    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        result = run_bridge(args.input, args.output)
    except BaseException:
        print(json.dumps({"status": "error"}))
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    print(json.dumps({"status": result["status"]}))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
