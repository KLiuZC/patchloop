def parse_version(s: str) -> tuple[int, ...]:
    """Parse a dotted version, optionally prefixed by one lowercase v."""
    return tuple(int(part) for part in s.split("."))
