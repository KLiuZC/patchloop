from pathlib import Path
import runpy

import pytest

audit = runpy.run_path(str(Path(__file__).parents[1] / "evals/audit_environment_access.py"))
cache = audit["KNOWN_CACHE"]


@pytest.mark.parametrize("command,output,expected", [
    (f"ls {cache}", "xarray\ntests\n", False),
    (f"find {cache} -name '*.py' | head", f"{cache}/core/variable.py\n", False),
    (f"sed -n '1,20p' {cache}/core/variable.py", "sed: can't read file: No such file or directory", False),
    (f"grep -n 'def where' {cache}/computation/computation.py", "646:def where(cond, x, y, keep_attrs=None):\n", True),
    (f"sed -n '10,20p' {cache}/tests/test_computation.py", "    assert_identical(actual, expected)\n", True),
    ("cat /testbed/xarray/core/variable.py", "def where():\n", False),
])
def test_only_returned_content_counts_as_known_cache_exposure(command, output, expected):
    assert audit["content_read"](command, output) is expected
