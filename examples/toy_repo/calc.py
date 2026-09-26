def parse_version(s: str) -> tuple[int, ...]:
    """Parse '1.2.3' or 'v1.2.3' into (1, 2, 3)."""
    return tuple(int(part) for part in s.split("."))
