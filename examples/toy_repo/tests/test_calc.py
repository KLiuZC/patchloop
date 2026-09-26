from calc import parse_version


def test_plain():
    assert parse_version("1.2.3") == (1, 2, 3)


def test_v_prefix():
    assert parse_version("v1.2.3") == (1, 2, 3)
