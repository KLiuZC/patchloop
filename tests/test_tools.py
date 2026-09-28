from patchloop.tools import bash, truncate


class FakeRunner:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def run(self, cmd, timeout):
        self.calls.append((cmd, timeout))
        return self.output


def test_truncate_keeps_head_and_tail():
    text = "a" * 3000 + "MID" + "b" * 3000
    out = truncate(text, limit=1000)
    assert out.startswith("a" * 500)
    assert out.endswith("b" * 500)
    assert "chars omitted" in out
    assert "MID" not in out


def test_truncate_leaves_short_text_alone():
    assert truncate("hello", limit=10) == "hello"


def test_bash_passes_command_and_timeout_to_runner():
    runner = FakeRunner("hi\n")
    assert bash("echo hi", runner, timeout=7) == "hi\n"
    assert runner.calls == [("echo hi", 7)]


def test_bash_truncates_long_output():
    out = bash("cat big", FakeRunner("x" * 10000))
    assert "chars omitted" in out


def test_bash_marks_empty_output():
    assert bash("true", FakeRunner("")) == "[no output]"
