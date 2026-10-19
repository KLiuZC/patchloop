from dataclasses import replace

import pytest

from patchloop.command_policy import PolicyContext, assess_command


CONTEXT = PolicyContext("/testbed", "/testbed", target_modules=("xarray",))


@pytest.mark.parametrize("header,rule", [
    ("cat <<'EOF' && pip download xarray", "network_or_install"),
    ("cat <<'EOF' || pip download xarray", "network_or_install"),
    ("cat <<'EOF' | pip download xarray", "network_or_install"),
    ("cat <<'EOF' | cat && pip download xarray", "network_or_install"),
    ("cat <<'EOF' < /tmp/gold.patch", "evaluation_material"),
    ("cat <<'EOF' /tmp/gold.patch", "evaluation_material"),
    ("cat <<'EOF' > /tmp/output && pip download xarray", "network_or_install"),
    ("cat <<'EOF' && cd / && find .", "filesystem_search"),
    ("cd / <<'EOF' && find .", "filesystem_search"),
    ("cat 3<<'EOF' && git log --all", "history_search"),
    ("cat <<EOF && pip download xarray", "network_or_install"),
])
def test_heredoc_headers_do_not_hide_commands_redirects_or_arguments(header, rule):
    decision = assess_command(header + "\nexample\nEOF", CONTEXT)
    assert decision.classification == "block"
    assert rule in decision.rule_ids


@pytest.mark.parametrize("command,classification", [
    ("cat <<'EOF'\npip download xarray\nEOF", "allow"),
    ("cat <<'EOF' | cat\n$(pip download xarray)\nEOF", "allow"),
    ("cat <<'EOF' > /tmp/output\nexample\nEOF", "allow"),
    ("cat <<'EOF' calc.py\nexample\nEOF", "allow"),
    ("cat <<'EOF'\nEOF\ncat calc.py", "allow"),
    ("cat <<'EOF' | cd /\nexample\nEOF\nfind .", "allow"),
    ("cd / <<'EOF' | cat\nexample\nEOF\nfind .", "allow"),
    ("cat <<'EOF' || cd /\nexample\nEOF\nfind .", "unknown"),
    ("cat <<'EOF' | cd / && find .\nexample\nEOF", "allow"),
    ("cat <<'EOF' || cd / && find .\nexample\nEOF", "unknown"),
    ("python - <<'EOF'\nprint('hi')\nEOF", "unknown"),
    ("cat <<EOF\n$(pip download xarray)\nEOF", "unknown"),
    ("cat <<'EOF' && cat calc.py\nexample\nEOF\ncat <<'NEXT'\nfind /\nNEXT", "allow"),
    ("cat <<'EOF' &&\nfind /\nEOF\ncat calc.py", "allow"),
    ("cd / <<'EOF' &&\ntrue\nEOF\nfind .", "block"),
    ("cat <<'EOF' |\npip download xarray\nEOF\ncat calc.py", "allow"),
    ("cat <<'EOF' && \\\ncat calc.py\nfind /\nEOF", "allow"),
    ("cat <<'EOF' 'calc\nfile.py'\nfind /\nEOF", "allow"),
    ("cat <<'EOF' \"calc\nfile.py\"\nfind /\nEOF", "allow"),
    ("cat <<'EOF' # \\\nfind /\nEOF", "allow"),
    ("cat <<'EOF' $'calc\nfile.py'\ntext\nEOF\nfind /", "block"),
    ("cat <<'EOF' ${value:-calc\nfile.py}\ntext\nEOF\nfind /", "block"),
    ("cat <<'EOF' &&\ncat <<'NEXT'\nNEXT\nEOF\nfind /", "block"),
    ("cat <<'EOF' &&\ncat <<'NEXT'\nNEXT\nEOF\ncat calc.py", "allow"),
    ("cat <<'true' &&\ncat <<'pwd'\ntrue\nfind /\npwd\ntrue\ntrue", "block"),
    ("cat <<-'EOF' &&\n\tcat <<'NEXT'\n\tEOF\nfind /\nNEXT\nEOF", "block"),
    ("cat <<'EOF'\nEOF \nfind /\nEOF", "allow"),
    ("cat <<EOF\nEO\\\nF\nfind /", "unknown"),
    ("cat <<EOF\nEO\\\nF\nEOF\nfind /", "block"),
    ("cat <<'EOF'\ncontent\rEOF\nfind /\nEOF", "allow"),
    ("cat <<-EOF\nEO\\\n\tF\nfind /\nEOF", "unknown"),
    ("cat <<'true' &&\ncat <<'pwd'\ntrue\ncat <<'NEXT' && find /\npwd\ntrue\nNEXT", "block"),
])
def test_heredoc_data_and_shell_directory_flow_remain_distinct(command, classification):
    assert assess_command(command, CONTEXT).classification == classification


@pytest.mark.parametrize("command", [
    "git diff origin/main --", "git diff main~1 --", "git diff main -- calc.py",
    "git diff HEAD origin/main -- calc.py", "git diff --cached release --",
    "git diff refs/heads/main", "git diff HEAD~1", "git diff origin/main..HEAD",
    "git diff --color HEAD~1 --", "git diff --word-diff HEAD~1 --",
    "git diff --color origin/main --", "git diff --word-diff=plain main --",
])
def test_diff_rejects_explicit_nonbaseline_revisions(command):
    assert assess_command(command, CONTEXT).classification == "block"


@pytest.mark.parametrize("command", [
    "git diff", "git diff --stat", "git diff HEAD", "git diff --cached HEAD -- calc.py",
    "git diff -- main~1", "git diff -- origin/main", "git diff -U5 HEAD -- calc.py",
    "git diff --word-diff-regex='HEAD~1' -- calc.py", "git diff --no-ext-diff -- calc.py",
    "git diff --color HEAD -- calc.py", "git diff --word-diff HEAD -- calc.py",
])
def test_diff_paths_and_option_values_are_not_references(command):
    assert assess_command(command, CONTEXT).classification == "allow"


@pytest.mark.parametrize("command", [
    "git diff calc.py", "git diff main", "git diff --output=/tmp/result HEAD",
    "git diff --no-index /tmp/left /tmp/right", "git diff --unsupported main --",
    "git diff ./calc.py~", "git diff ./a..b.py", "git diff /testbed/calc.py~",
])
def test_diff_ambiguous_or_unsupported_options_stay_unknown(command):
    assert assess_command(command, CONTEXT).classification == "unknown"


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("directory_options", ["-C /testbed", "-C /tmp -C /testbed", "-C relative -C /testbed"])
def test_absolute_git_directory_recovers_after_uncertain_cd(isolated, directory_options):
    context = replace(CONTEXT, isolated_history=isolated)
    command = f"cd /tmp; git {directory_options} log -5"
    assert assess_command(command, context).classification == ("allow" if isolated else "block")
    assert assess_command(f"cd /tmp; git {directory_options} log --all", context).classification == "block"


@pytest.mark.parametrize("command", [
    "cd /tmp; git -C relative log --all", "cd /tmp; git -C /other log --all",
    "cd /tmp; git -C /testbed -C .. log --all",
])
def test_relative_or_external_git_directories_remain_unknown(command):
    assert assess_command(command, CONTEXT).classification == "unknown"


def test_heredoc_normalization_has_a_fixed_pass_limit(monkeypatch):
    import patchloop.command_policy as policy

    command = "cat <<'true' &&\ncat <<'pwd'\ntrue\ncat <<'NEXT' && find /\npwd\ntrue\nNEXT"
    monkeypatch.setattr(policy, "MAX_HEREDOC_PASSES", 1)
    decision = policy.assess_command(command, CONTEXT)
    assert decision.classification == "unknown"
    assert decision.rule_ids == ("input_limit",)
