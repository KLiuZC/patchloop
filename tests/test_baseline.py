from patchloop.baseline import extract_diff

DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


def test_extract_diff_from_fenced_block():
    assert extract_diff("Here you go:\n```diff\n" + DIFF + "```\nDone.") == DIFF


def test_extract_diff_from_unlabelled_fence():
    assert extract_diff("```\n" + DIFF + "```") == DIFF


def test_extract_diff_from_bare_text():
    assert extract_diff("Some words\ndiff --git a/x.py b/x.py\n" + DIFF) == "diff --git a/x.py b/x.py\n" + DIFF


def test_extract_diff_returns_empty_when_absent():
    assert extract_diff("I cannot help with that.") == ""
