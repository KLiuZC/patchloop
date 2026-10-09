import pytest

from calc import parse_version


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1.2.3", (1, 2, 3)), ("0", (0,)), ("2.10", (2, 10)), ("01.002", (1, 2))],
)
def test_plain_versions(value, expected):
    assert parse_version(value) == expected


def test_single_lowercase_v_prefix():
    assert parse_version("v1.2.3") == (1, 2, 3)


@pytest.mark.parametrize("value", ["V1.2.3", "vv1.2.3", "v", "1..2", "1.a", ""])
def test_invalid_versions_still_raise(value):
    with pytest.raises(ValueError):
        parse_version(value)
