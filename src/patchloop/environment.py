"""Versioned, fail-closed checks for the SWE-bench solving environment."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess

from .sandbox import DockerRunner

POLICY_VERSION = "swebench-isolation-v2"
HIDDEN_PATHS = ("/opt/miniconda3/pkgs", "/opt/conda/pkgs", "/root/.cache", "/root/.conda/pkgs", "/home")
SCAN_ROOTS = ("/opt", "/usr/local/lib", "/usr/lib", "/root", "/home", "/tmp", "/var/tmp")
MODULES = {
    "pydata/xarray": ("xarray",), "pytest-dev/pytest": ("_pytest", "pytest"),
    "django/django": ("django",), "pallets/flask": ("flask",),
    "sympy/sympy": ("sympy",), "sphinx-doc/sphinx": ("sphinx",),
    "pylint-dev/pylint": ("pylint",), "psf/requests": ("requests",),
    "astropy/astropy": ("astropy",), "matplotlib/matplotlib": ("matplotlib",),
    "scikit-learn/scikit-learn": ("sklearn",), "mwaskom/seaborn": ("seaborn",),
}


class EnvironmentCheckError(RuntimeError):
    pass


def policy_metadata():
    probe = Path(__file__).with_name("environment_probe.py").read_bytes()
    return {"version": POLICY_VERSION, "hidden_paths": list(HIDDEN_PATHS),
            "scan_roots": list(SCAN_ROOTS), "probe_sha256": hashlib.sha256(probe).hexdigest()}


def inspect_image(image):
    result = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise EnvironmentCheckError(f"image is not available locally: {image}")
    data = json.loads(result.stdout)[0]
    return {"requested": image, "id": data["Id"], "platform": f"{data['Os']}/{data['Architecture']}",
            "repo_digests": data.get("RepoDigests", [])}


def solver_runner(image, workdir="/testbed", platform="linux/amd64", memory="3g"):
    return DockerRunner(image, workdir=workdir, platform=platform, memory=memory,
                        network="none", hidden_paths=HIDDEN_PATHS)


def probe_environment(runner, instance, *, prepare=False):
    repo = instance["repo"]
    if repo not in MODULES:
        raise EnvironmentCheckError(f"no environment audit profile for {repo}")
    base = instance.get("base_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise EnvironmentCheckError("environment audit requires a full base_commit")
    payload = {"workdir": runner.workdir, "base_commit": base, "modules": MODULES[repo],
               "scan_roots": SCAN_ROOTS, "hidden_paths": HIDDEN_PATHS,
               "prepare": prepare, "prepared": prepare}
    script = Path(__file__).with_name("environment_probe.py").read_text()
    command = "python -c " + shlex.quote(script) + " " + shlex.quote(json.dumps(payload))
    result = runner.run_result(command, timeout=180)
    prefix = "PATCHLOOP_ENVIRONMENT_REPORT="
    if result.exit_code != 0 or result.timed_out:
        raise EnvironmentCheckError(f"environment probe failed: {result.display(180)}")
    lines = [line[len(prefix):] for line in result.output.splitlines() if line.startswith(prefix)]
    if len(lines) != 1:
        raise EnvironmentCheckError("environment probe did not return one report")
    report = json.loads(lines[0])
    if not isinstance(report, dict) or report.get("status") not in {"passed", "failed", "error"}:
        raise EnvironmentCheckError("invalid environment probe report")
    return report


def prepare_swebench_environment(runner, instance, report_path, image):
    """Must complete before the first model request; reports stay outside its container."""
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {"instance_id": instance["instance_id"], "base_commit": instance.get("base_commit"),
              "image": image, "policy": policy_metadata(), "status": "error"}
    try:
        report["checks"] = probe_environment(runner, instance, prepare=True)
        report["status"] = report["checks"]["status"]
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if report["status"] != "passed":
        raise EnvironmentCheckError(f"solving environment did not pass preflight; see {path}")
    return report


def preflight_instance(instance, image, output, *, verify, memory="3g", platform="linux/amd64", timeout=180):
    """Audit a raw image and its isolated solver counterpart, without a model client."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("use an empty preflight output directory")
    info = inspect_image(image)
    report = {"instance_id": instance["instance_id"], "image": info, "policy": policy_metadata(),
              "base_commit": instance["base_commit"], "visible_command": verify, "status": "error"}
    try:
        with DockerRunner(info["id"], platform=platform, memory=memory) as runner:
            report["before"] = probe_environment(runner, instance)
        with solver_runner(info["id"], platform=platform, memory=memory) as runner:
            isolated = prepare_swebench_environment(runner, instance, output / "environment.json", info)
            report["after"] = isolated["checks"]
            health = runner.run_result(verify, timeout=timeout)
            report["visible_check"] = {"exit_code": health.exit_code, "timed_out": health.timed_out,
                                       "output": health.output}
            report["status"] = "passed" if health.exit_code == 0 and not health.timed_out else "failed"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    (output / "preflight.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report
