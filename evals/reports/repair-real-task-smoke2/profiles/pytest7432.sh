set -e
python - <<'VISIBLE_CHECK'
import re
import subprocess
import sys
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="patchloop-visible-") as directory:
    case = Path(directory) / "test_it.py"
    case.write_text("import pytest\n@pytest.mark.skip\ndef test_skip_location():\n    assert 0\n")
    locations = []
    for extra in ([], ["--runxfail"]):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-rs", *extra, str(case)],
            capture_output=True, text=True, timeout=60,
        )
        print("COMMAND:", " ".join(result.args))
        print(result.stdout + result.stderr)
        assert result.returncode == 0, "pytest invocation did not complete normally"
        lines = [line for line in result.stdout.splitlines() if line.startswith("SKIPPED ")]
        assert len(lines) == 1, "expected exactly one reported skip"
        locations.append(lines[0])
    for location in locations:
        assert re.search(r"test_it\.py:\d+:", location), location
    assert locations[0] == locations[1], "--runxfail changed skip location"
print("VISIBLE CHECK PASSED: skip location is unchanged by --runxfail")
VISIBLE_CHECK
