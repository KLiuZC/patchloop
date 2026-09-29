import random
from collections import Counter
from pathlib import Path

# roughly the difficulty distribution of SWE-bench Verified itself
DIFFICULTY_MIX = {"<15 min fix": 0.4, "15 min - 1 hour": 0.5, "1-4 hours": 0.1}

# pure-Python repositories: their images build in minutes under x86 emulation and their tests need no network
DEFAULT_REPOS = [
    "django/django",
    "pytest-dev/pytest",
    "pylint-dev/pylint",
    "sphinx-doc/sphinx",
    "sympy/sympy",
    "pallets/flask",
    "pydata/xarray",
]


def quotas(size: int, mix: dict[str, float] = DIFFICULTY_MIX) -> dict[str, int]:
    """Integer quotas per difficulty adding up to size; the largest remainders get the leftovers."""
    raw = {k: size * share for k, share in mix.items()}
    q = {k: int(v) for k, v in raw.items()}
    leftovers = size - sum(q.values())
    for k, _ in sorted(raw.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True)[:leftovers]:
        q[k] += 1
    return q


def select_subset(
    instances: list[dict],
    size: int = 30,
    seed: int = 1,
    repos: list[str] = DEFAULT_REPOS,
    cap: int = 7,
    exclude: list[str] = (),
) -> list[dict]:
    """Stratified by difficulty and spread across repositories; deterministic for a given seed."""
    excluded = set(exclude)
    pool = [
        r for r in instances
        if r["repo"] in repos and r["instance_id"] not in excluded and r["difficulty"] in DIFFICULTY_MIX
    ]
    random.Random(seed).shuffle(pool)

    remaining = quotas(size)
    per_repo: Counter = Counter()
    chosen, chosen_ids = [], set()
    progress = True
    while sum(remaining.values()) and progress:
        progress = False
        for difficulty in DIFFICULTY_MIX:  # round-robin over buckets keeps the mix balanced as we go
            if not remaining[difficulty]:
                continue
            candidates = [
                r for r in pool
                if r["difficulty"] == difficulty and r["instance_id"] not in chosen_ids and per_repo[r["repo"]] < cap
            ]
            if not candidates:
                continue
            # least-represented repo first; ties fall to the shuffled order
            pick = min(candidates, key=lambda r: per_repo[r["repo"]])
            chosen.append(pick)
            chosen_ids.add(pick["instance_id"])
            per_repo[pick["repo"]] += 1
            remaining[difficulty] -= 1
            progress = True
    return chosen


def write_subset(chosen: list[dict], path: Path, header: list[str]) -> None:
    lines = [f"# {h}" for h in header] + [""]
    for r in sorted(chosen, key=lambda r: (r["repo"], r["instance_id"])):
        lines.append(f"{r['instance_id']:36} # {r['repo']} v{r['version']} · {r['difficulty']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def breakdown(chosen: list[dict]) -> dict[str, dict[str, int]]:
    table: dict[str, dict[str, int]] = {}
    for r in chosen:
        table.setdefault(r["repo"], {}).setdefault(r["difficulty"], 0)
        table[r["repo"]][r["difficulty"]] += 1
    return table
