"""Execution decisions and reproducibility metadata for optional command policies."""
from dataclasses import asdict, dataclass
import hashlib
from importlib.metadata import version
import json
import posixpath
import re

from .command_policy import (
    BLOCK_RULES, MAX_COMMAND_BYTES, MAX_TREE_DEPTH, MAX_TREE_NODES, MAX_LOG_METADATA_COMMITS,
    PARSER_VERSION, POLICY_VERSION, PolicyContext, assess_command,
)
from .environment import EnvironmentCheckError, HIDDEN_PATHS, MODULES


MODES = ("off", "audit", "enforce")
PROFILE_VERSION = "swebench-command-context-v2"
CASE_SUITE_VERSION = 2
COUNT_FIELDS = ("checked", "allow", "block", "unknown", "would_block", "blocked", "execution_requests")
SCRATCH_ROOTS = ("/tmp",)
SAFE_ENVIRONMENT_VARIABLES = ("MPLCONFIGDIR", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTHONDONTWRITEBYTECODE")
DEPENDENCY_MARKERS = ("site-packages", "dist-packages")


def validate_command_policy(mode, evaluation_mode="agent"):
    if mode not in MODES:
        raise ValueError("--command-policy must be off, audit or enforce")
    if mode != "off" and evaluation_mode != "agent":
        raise ValueError("--command-policy requires --mode agent")


def command_policy_metadata(mode):
    validate_command_policy(mode)
    return {
        "mode": mode, "policy_version": POLICY_VERSION, "parser_version": PARSER_VERSION,
        "parser_packages": {name: version(name) for name in ("tree-sitter", "tree-sitter-bash")},
        "profile_version": PROFILE_VERSION, "development_cases_version": CASE_SUITE_VERSION,
        "limits": {"command_bytes": MAX_COMMAND_BYTES, "tree_nodes": MAX_TREE_NODES, "tree_depth": MAX_TREE_DEPTH,
                   "git_log_metadata_commits": MAX_LOG_METADATA_COMMITS},
        "context_defaults": {"cache_roots": list(HIDDEN_PATHS), "scratch_roots": list(SCRATCH_ROOTS),
                             "safe_environment_variables": list(SAFE_ENVIRONMENT_VARIABLES),
                             "dependency_origin_markers": list(DEPENDENCY_MARKERS)},
    }


def count_policy_event(counts, event):
    for name in COUNT_FIELDS:
        counts.setdefault(name, 0)
    counts["checked"] += 1
    counts[event["classification"]] += 1
    counts["would_block"] += int(event["would_block"])
    counts["blocked"] += int(event["action"] == "block")
    counts["execution_requests"] += int(event["action"] == "execute")


def _isolated_history(instance, report):
    base = instance.get("base_commit")
    instance_id = instance.get("instance_id")
    history = report["checks"].get("git", {})
    return (
        isinstance(base, str) and re.fullmatch(r"[0-9a-f]{40}", base) is not None
        and isinstance(instance_id, str) and bool(instance_id)
        and report.get("instance_id") == instance_id
        and report.get("base_commit") == base and isinstance(history, dict)
        and history.get("head") == base and type(history.get("commit_objects")) is int
        and history["commit_objects"] == 1 and history.get("remotes") == ""
        and history.get("reflog") == [] and history.get("alternates_present") is False
        and history.get("tracked_changes") == ""
    )


def swebench_command_policy(mode, instance, report, workdir):
    validate_command_policy(mode)
    if mode == "off":
        return None
    if not report or report.get("status") != "passed" or instance.get("repo") not in MODULES:
        raise EnvironmentCheckError("command policy context requires a passed SWE-bench preflight")
    roots = set()
    for imported in report["checks"]["imports"].values():
        origin = imported.get("origin")
        if not isinstance(origin, str) or not origin.startswith("/"):
            continue
        for marker in DEPENDENCY_MARKERS:
            prefix, separator, _ = origin.partition(f"/{marker}/")
            if separator:
                roots.add(posixpath.join(prefix, marker))
    context = PolicyContext(
        repository_root=workdir, cwd=workdir, target_modules=MODULES[instance["repo"]],
        dependency_roots=tuple(sorted(roots)), scratch_roots=SCRATCH_ROOTS, cache_roots=HIDDEN_PATHS,
        safe_environment_variables=SAFE_ENVIRONMENT_VARIABLES,
        isolated_history=_isolated_history(instance, report),
    )
    return CommandPolicy(mode, context)


@dataclass(frozen=True)
class CommandPolicy:
    mode: str
    context: PolicyContext | None = None

    def __post_init__(self):
        validate_command_policy(self.mode)
        if self.mode != "off" and not isinstance(self.context, PolicyContext):
            raise ValueError("an enabled command policy requires a PolicyContext")

    def metadata(self):
        data = command_policy_metadata(self.mode)
        if self.mode != "off":
            data["context"] = asdict(self.context)
            data["context_sha256"] = self.context_sha256()
        return data

    def context_sha256(self):
        encoded = json.dumps(asdict(self.context), sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(encoded).hexdigest()

    def inspect(self, command, tool_call_id):
        if self.mode == "off":
            return None
        decision = assess_command(command, self.context)
        blocked = self.mode == "enforce" and decision.classification == "block"
        return {
            "tool_call_id": tool_call_id, "mode": self.mode,
            "policy_version": POLICY_VERSION, "parser_version": PARSER_VERSION,
            "context_sha256": self.context_sha256(),
            "classification": decision.classification, "rule_ids": list(decision.rule_ids),
            "reason": decision.reason, "action": "block" if blocked else "execute",
            "would_block": self.mode == "audit" and decision.classification == "block",
        }

    def rejection(self, event):
        rules = ", ".join(rule for rule in event["rule_ids"] if rule in BLOCK_RULES)
        return (
            f"Command blocked [{rules}]. The command was not executed.\n"
            f"Work within {self.context.repository_root} or a specific dependency directory. "
            "Use the prepared environment and current baseline; implement and test the fix."
        )
