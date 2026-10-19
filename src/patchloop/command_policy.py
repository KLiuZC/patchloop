"""Pure, opt-in command classification. This module never executes commands."""
from dataclasses import dataclass
import posixpath
import re
from typing import Literal

from tree_sitter import Language, Parser
import tree_sitter_bash


POLICY_VERSION = "command-policy-v2"
PARSER_VERSION = "tree-sitter/0.26.0 tree-sitter-bash/0.25.1"
MAX_COMMAND_BYTES = 32768
MAX_TREE_NODES = 4096
MAX_TREE_DEPTH = 64
MAX_LOG_METADATA_COMMITS = 10
BLOCK_RULES = frozenset({"network_or_install", "filesystem_search", "extra_project_source",
                         "evaluation_material", "history_search"})
UNKNOWN_RULES = frozenset({"opaque_script", "dynamic_shell", "unresolved_path", "unparsed_shell",
                           "unsupported_command", "input_limit"})


def _normal_path(path: str) -> str:
    return "/" + posixpath.normpath(path).lstrip("/")


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


@dataclass(frozen=True)
class PolicyContext:
    repository_root: str
    cwd: str
    target_modules: tuple[str, ...] = ()
    dependency_roots: tuple[str, ...] = ()
    scratch_roots: tuple[str, ...] = ("/tmp",)
    cache_roots: tuple[str, ...] = ()
    safe_environment_variables: tuple[str, ...] = ()
    isolated_history: bool = False

    def __post_init__(self):
        if not isinstance(self.isolated_history, bool):
            raise ValueError("isolated_history must be a boolean")
        for name in ("repository_root", "cwd"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
                raise ValueError(f"{name} must be an absolute container path")
            object.__setattr__(self, name, _normal_path(value))
        for name in ("target_modules", "dependency_roots", "scratch_roots", "cache_roots",
                     "safe_environment_variables"):
            values = getattr(self, name)
            if isinstance(values, str) or not all(isinstance(value, str) and value and "\0" not in value for value in values):
                raise ValueError(f"{name} must be a sequence of non-empty strings")
            if name.endswith("_roots"):
                if not all(value.startswith("/") for value in values):
                    raise ValueError(f"{name} must contain absolute container paths")
                values = tuple(_normal_path(value) for value in values)
            object.__setattr__(self, name, tuple(values))


@dataclass(frozen=True)
class PolicyDecision:
    classification: Literal["allow", "block", "unknown"]
    rule_ids: tuple[str, ...]
    reason: str


def _decision(rules: set[str]) -> PolicyDecision:
    if rules & BLOCK_RULES:
        selected = rules & (BLOCK_RULES | UNKNOWN_RULES)
        classification = "block"
        message = "Contains a statically identified out-of-scope operation."
    elif rules & UNKNOWN_RULES:
        selected = rules & UNKNOWN_RULES
        classification = "unknown"
        message = "Cannot fully classify this command within the supported subset."
    else:
        selected = rules or {"literal_data"}
        if selected - {"literal_data", "environment_metadata"}:
            selected = selected - {"literal_data", "environment_metadata"}
        classification = "allow"
        message = "No prohibited operation identified within the supported subset; not a safety guarantee."
    rule_ids = tuple(sorted(selected))
    return PolicyDecision(classification, rule_ids, message + " Rules: " + ", ".join(rule_ids))


def _value(node):
    if node.type in {"command_name", "concatenation"}:
        values = [_value(child) for child in node.named_children]
        return "".join(values) if values and all(value is not None for value in values) else None
    raw = node.text.decode()
    if node.type == "raw_string":
        return raw[1:-1]
    if node.type == "string":
        if any(child.type != "string_content" for child in node.named_children):
            return None
        return re.sub(r'\\([$`"\\\n])', lambda match: "" if match[1] == "\n" else match[1], raw[1:-1])
    if node.type in {"word", "number"} and not node.named_children:
        if raw.startswith("~") or re.search(r"(?<!\\)[$`{}]", raw):
            return None
        return re.sub(r"\\(.)", lambda match: "" if match[1] == "\n" else match[1], raw, flags=re.S)
    return None


def _join_cwd(first, second):
    return first if first == second else None


def _options(arguments, flags, valued):
    operands = []
    options = {}
    position = 0
    while position < len(arguments):
        argument = arguments[position]
        position += 1
        if argument == "--":
            operands.extend(arguments[position:])
            break
        if not argument.startswith("-") or argument == "-":
            operands.append(argument)
            continue
        if argument.startswith("--"):
            key, separator, value = argument[2:].partition("=")
            if key in flags and not separator:
                options.setdefault(key, []).append("")
            elif key in valued:
                if not separator:
                    if position == len(arguments):
                        return None
                    value = arguments[position]
                    position += 1
                options.setdefault(key, []).append(value)
            else:
                return None
            continue
        letters = argument[1:]
        for offset, key in enumerate(letters):
            if key in flags:
                options.setdefault(key, []).append("")
            elif key in valued:
                value = letters[offset + 1:]
                if not value:
                    if position == len(arguments):
                        return None
                    value = arguments[position]
                    position += 1
                options.setdefault(key, []).append(value)
                break
            else:
                return None
    return operands, options


def _bounded_metadata_log(options, isolated_history):
    normalized = []
    position = 0
    while position < len(options):
        option = options[position]
        position += 1
        if option in {"--", "--format", "--pretty"}:
            return False
        if option in {"-n", "--max-count"}:
            if position == len(options):
                return False
            normalized.extend((option, options[position]))
            position += 1
        elif re.fullmatch(r"-\d+", option):
            normalized.append("--max-count=" + option[1:])
        else:
            normalized.append(option)
    parsed = _options(normalized, {"oneline", "no-decorate"}, {"n", "max-count", "format", "pretty"})
    if parsed is None or parsed[0] not in ([], ["HEAD"]):
        return False
    counts = parsed[1].get("n", []) + parsed[1].get("max-count", [])
    if len(counts) != 1 or not re.fullmatch(r"[1-9][0-9]?", counts[0]):
        return False
    limit = MAX_LOG_METADATA_COMMITS if isolated_history else 1
    return int(counts[0]) <= limit


class _Assessment:
    def __init__(self, context):
        self.context = context
        self.rules = set()

    def walk(self, node, cwd):
        if node.type == "comment":
            return cwd, cwd
        if node.type == "program":
            if any(child.type == "&" for child in node.children):
                self.rules.add("dynamic_shell")
            flow = (cwd, cwd)
            for child in node.named_children:
                flow = self.walk(child, _join_cwd(*flow))
            return flow
        if node.type == "list":
            children = [child for child in node.named_children if child.type != "comment"]
            operators = [child.type for child in node.children if child.type in {"&&", "||"}]
            if len(children) != 2 or len(operators) != 1:
                self.rules.add("dynamic_shell")
                return None, None
            left = self.walk(children[0], cwd)
            if operators[0] == "&&":
                right = self.walk(children[1], left[0])
                return right[0], _join_cwd(left[1], right[1])
            right = self.walk(children[1], left[1])
            return _join_cwd(left[0], right[0]), right[1]
        if node.type == "pipeline":
            for child in node.named_children:
                self.walk(child, cwd)
            return cwd, cwd
        if node.type == "redirected_statement":
            for redirect in node.children_by_field_name("redirect"):
                self.redirect(redirect, cwd)
            body = node.child_by_field_name("body")
            return self.walk(body, cwd) if body else (cwd, cwd)
        if node.type == "command":
            return self.simple(node, cwd)
        self.rules.add("dynamic_shell")
        return None, None

    def redirect(self, node, cwd):
        if node.type == "heredoc_redirect":
            start = next(child for child in node.named_children if child.type == "heredoc_start")
            if not re.fullmatch(r"'[^']*'|\"[^\"$`]*\"", start.text.decode()):
                self.rules.add("dynamic_shell")
            else:
                self.rules.add("literal_data")
            return
        destination = node.child_by_field_name("destination")
        if node.type != "file_redirect" or destination is None:
            self.rules.add("dynamic_shell")
            return
        value = _value(destination)
        if value is None:
            self.rules.add("dynamic_shell")
        elif value.isdigit() and any(child.type in {">&", "<&"} for child in node.children):
            return
        elif value != "/dev/null":
            write_only = any(child.type in {">", ">>", ">|", "&>", "&>>", ">&"} for child in node.children)
            self.path(value, cwd, write_only=write_only)

    def path(self, value, cwd, recursive=False, write_only=False):
        if cwd is None and not value.startswith("/"):
            self.rules.add("unresolved_path")
            return
        path = _normal_path(posixpath.join(cwd or "/", value))
        if path.startswith(("/dev/tcp/", "/dev/udp/")):
            self.rules.add("network_or_install")
            return
        if _inside(path, self.context.repository_root):
            self.rules.add("repository_operation")
            return
        parts = path.split("/")
        if not write_only and any(_inside(path, root) for root in (*self.context.cache_roots, *self.context.dependency_roots)) and any(
            part == module or part.startswith(module + "-")
            for part in parts for module in self.context.target_modules
        ):
            self.rules.add("extra_project_source")
        elif not write_only and posixpath.basename(path) in {"gold.patch", "test.patch", "predictions.jsonl", "eval.sh"}:
            self.rules.add("evaluation_material")
        elif any(_inside(path, root) and path != root for root in self.context.dependency_roots):
            self.rules.add("dependency_read")
        elif recursive:
            self.rules.add("filesystem_search")
        elif any(_inside(path, root) for root in self.context.scratch_roots):
            self.rules.add("scratch_operation")
        else:
            self.rules.add("unresolved_path")

    def read_command(self, name, arguments, cwd):
        if name == "grep":
            parsed = _options(arguments, set("nrilREFivqHhsowxabczZIL") | {"recursive", "line-number", "ignore-case", "fixed-strings", "extended-regexp", "files-with-matches", "quiet"},
                              set("efABCm") | {"regexp", "file", "include", "exclude", "exclude-dir", "max-count", "context"})
            if parsed is None:
                self.rules.add("unsupported_command")
                return
            paths, options = parsed
            explicit_pattern = any(key in options for key in ("e", "regexp", "f", "file"))
            if not explicit_pattern:
                if not paths:
                    self.rules.add("unsupported_command")
                    return
                paths = paths[1:]
            for pattern_file in options.get("f", []) + options.get("file", []):
                self.path(pattern_file, cwd)
            recursive = bool(set(options) & {"r", "R", "recursive"})
            if not paths and recursive:
                paths = ["."]
        elif name == "sed":
            parsed = _options(arguments, set("nEi") | {"quiet", "silent"}, {"e", "expression"})
            if parsed is None:
                self.rules.add("opaque_script")
                return
            paths, options = parsed
            if "i" in options and paths and paths[0] == "":
                paths = paths[1:]
            scripts = options.get("e", []) + options.get("expression", [])
            if not scripts:
                scripts, paths = paths[:1], paths[1:]
            if not scripts or not all(re.fullmatch(r"(?:\d+|\$)(?:,(?:\d+|\$))?p", script) for script in scripts):
                self.rules.add("opaque_script")
                return
            recursive = False
        else:
            converted = ["--lines=" + arg[1:] if re.fullmatch(r"-\d+", arg) else arg for arg in arguments]
            parsed = _options(converted, set("nbsuvET") if name == "cat" else {"q", "v", "quiet", "verbose"},
                              set() if name == "cat" else {"n", "c", "lines", "bytes"})
            if parsed is None:
                self.rules.add("unsupported_command")
                return
            paths, _ = parsed
            recursive = False
        for path in paths:
            if path != "-":
                self.path(path, cwd, recursive)
        if not paths:
            self.rules.add("literal_data")

    def find_command(self, arguments, cwd):
        position = 0
        while position < len(arguments) and arguments[position] in {"-H", "-L", "-P"}:
            position += 1
        paths = []
        while position < len(arguments) and not arguments[position].startswith("-") and arguments[position] not in {"(", "!"}:
            paths.append(arguments[position])
            position += 1
        for path in paths or ["."]:
            self.path(path, cwd, recursive=True)
        valued = {"-name", "-iname", "-path", "-ipath", "-type", "-maxdepth", "-mindepth", "-mtime", "-size"}
        flags = {"-print", "-print0", "-prune", "-ls", "-a", "-o", "-and", "-or", "-not", "!", "(", ")"}
        while position < len(arguments):
            argument = arguments[position]
            if argument in valued and position + 1 < len(arguments):
                position += 2
            elif argument in flags:
                position += 1
            else:
                self.rules.add("unsupported_command")
                break

    def git_command(self, arguments, cwd):
        while arguments and arguments[0] == "-C":
            if len(arguments) < 3:
                self.rules.add("unsupported_command")
                return
            cwd = _normal_path(posixpath.join(cwd, arguments[1])) if cwd else None
            arguments = arguments[2:]
        if not arguments or arguments[0].startswith("-"):
            self.rules.add("dynamic_shell")
            return
        if cwd is None or not _inside(cwd, self.context.repository_root):
            self.rules.add("unresolved_path")
            return
        operation, *options = arguments
        if operation in {"log", "reflog", "fsck", "rev-list"}:
            if operation == "log" and _bounded_metadata_log(options, self.context.isolated_history):
                self.rules.add("baseline_git")
            else:
                self.rules.add("history_search")
            return
        if operation == "show":
            if len(options) == 1 and options[0].startswith("HEAD:"):
                self.rules.add("baseline_git")
            elif options and not options[0].startswith("-"):
                self.rules.add("history_search")
            else:
                self.rules.add("unsupported_command")
            return
        if operation == "cat-file" and any(option.startswith("--batch") for option in options):
            self.rules.add("history_search")
        elif operation in {"branch", "tag", "remote"}:
            self.rules.add("history_search")
        elif operation in {"status", "diff", "stash", "count-objects"}:
            revisions = options[:options.index("--")] if "--" in options else options
            if operation == "diff" and any(
                re.fullmatch(r"HEAD[~^].*|[0-9a-f]{7,40}|[^/]+\.\.[^/]+", value) for value in revisions
            ):
                self.rules.add("history_search")
            elif any(option.startswith(("--output", "--ext-diff", "--textconv", "--no-index")) for option in options):
                self.rules.add("unsupported_command")
            else:
                self.rules.add("baseline_git")
        elif operation == "rev-parse" and options in (["HEAD"], ["--show-toplevel"]):
            self.rules.add("baseline_git")
        else:
            self.rules.add("unsupported_command")

    def simple(self, node, cwd):
        name = node.child_by_field_name("name")
        executable = _value(name) if name else None
        arguments = [_value(child) for child in node.children_by_field_name("argument")]
        if executable is None or any(value is None for value in arguments):
            self.rules.add("dynamic_shell")
            return None, None
        if "/" in executable:
            directories = {"/bin", "/usr/bin", "/usr/local/bin"}
            directories.update(root.split("/lib/")[0] + "/bin" for root in self.context.dependency_roots if "/lib/" in root)
            if not executable.startswith("/") or posixpath.dirname(_normal_path(executable)) not in directories:
                self.rules.add("unsupported_command")
                return cwd, cwd
        for child in node.named_children:
            if child.type == "variable_assignment":
                key = child.child_by_field_name("name").text.decode()
                value = child.child_by_field_name("value")
                if key not in self.context.safe_environment_variables or value is None or _value(value) is None:
                    self.rules.add("dynamic_shell")
        name = posixpath.basename(executable)
        if name == "cd":
            if len(arguments) != 1 or arguments[0] == "-" or any(char in arguments[0] for char in "*?["):
                self.rules.add("unresolved_path")
                return None, cwd
            target = _normal_path(posixpath.join(cwd or "/", arguments[0])) if cwd or arguments[0].startswith("/") else None
            return target, cwd
        if name in {"bash", "sh", "zsh", "eval", "source", ".", "env", "command", "exec", "xargs"}:
            self.rules.add("dynamic_shell")
            return None, None
        if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", name):
            if arguments[:2] == ["-m", "pip"]:
                name, arguments = "pip", arguments[2:]
            elif arguments[:2] == ["-m", "pytest"]:
                self.rules.add("repository_operation")
                return cwd, cwd
            elif arguments == ["--version"]:
                self.rules.add("environment_metadata")
                return cwd, cwd
            else:
                self.rules.add("opaque_script")
                return cwd, cwd
        if name in {"curl", "wget"}:
            self.rules.add("network_or_install")
        elif name in {"pip", "pip3", "conda"}:
            if arguments and arguments[0] in {"install", "download", "update", "upgrade", "remove", "uninstall", "create"}:
                self.rules.add("network_or_install")
            elif arguments and arguments[0] in {"show", "list", "--version"}:
                self.rules.add("environment_metadata")
            else:
                self.rules.add("unsupported_command")
        elif name in {"echo", "printf", "true", "false"}:
            self.rules.add("literal_data")
        elif name == "git":
            self.git_command(arguments, cwd)
        elif name in {"grep", "sed", "head", "tail", "cat"}:
            self.read_command(name, arguments, cwd)
        elif name == "find":
            self.find_command(arguments, cwd)
        elif name in {"pwd", "ls"}:
            if name == "ls" and any(value == "--recursive" or (value.startswith("-") and "R" in value) for value in arguments):
                self.rules.add("unsupported_command")
            else:
                self.rules.add("environment_metadata")
        elif name == "pytest":
            self.rules.add("repository_operation")
        elif name == "rm":
            parsed = _options(arguments, {"f"}, set())
            if parsed is None or not parsed[0]:
                self.rules.add("unsupported_command")
            else:
                for path in parsed[0]:
                    self.path(path, cwd, write_only=True)
        else:
            self.rules.add("unsupported_command")
        return cwd, cwd


def assess_command(command: str, context: PolicyContext) -> PolicyDecision:
    if not isinstance(command, str) or not command.strip() or "\0" in command:
        return _decision({"unparsed_shell"})
    if len(command) > MAX_COMMAND_BYTES:
        return _decision({"input_limit"})
    try:
        source = command.encode()
    except UnicodeEncodeError:
        return _decision({"unparsed_shell"})
    if len(source) > MAX_COMMAND_BYTES:
        return _decision({"input_limit"})
    root = Parser(Language(tree_sitter_bash.language())).parse(source).root_node
    if root.has_error:
        return _decision({"unparsed_shell"})
    pending = [(root, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > MAX_TREE_NODES or depth > MAX_TREE_DEPTH:
            return _decision({"input_limit"})
        pending.extend((child, depth + 1) for child in node.children)
    assessment = _Assessment(context)
    assessment.walk(root, context.cwd)
    return _decision(assessment.rules)
