from patchloop.tools import bash, truncate


def test_truncate_keeps_head_and_tail():
    text = "a" * 3000 + "MID" + "b" * 3000
    out = truncate(text, limit=1000)
    assert out.startswith("a" * 500)
    assert out.endswith("b" * 500)
    assert "chars omitted" in out
    assert "MID" not in out


def test_truncate_leaves_short_text_alone():
    assert truncate("hello", limit=10) == "hello"


def test_bash_runs_in_workdir(tmp_path):
    (tmp_path / "marker.txt").write_text("")
    assert "marker.txt" in bash("ls", tmp_path)


def test_bash_merges_stderr_and_reports_exit_code(tmp_path):
    out = bash("echo out; echo err >&2; exit 3", tmp_path)
    assert "out" in out
    assert "err" in out
    assert "[exit code 3]" in out


def test_bash_timeout_kills_command(tmp_path):
    out = bash("sleep 5", tmp_path, timeout=1)
    assert "timed out" in out
