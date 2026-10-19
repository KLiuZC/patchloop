"""Inspect candidates by default; explicit execution runs no-model validation only."""

from patchloop.ab_preflight import main


if __name__ == "__main__":
    raise SystemExit(main())
