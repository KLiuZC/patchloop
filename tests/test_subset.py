from patchloop.subset import DIFFICULTY_MIX, quotas, select_subset

REPOS = ["a/a", "b/b", "c/c"]


def fake_instances():
    rows = []
    for repo in REPOS:
        for difficulty in DIFFICULTY_MIX:
            for i in range(10):
                rows.append({"instance_id": f"{repo.split('/')[0]}__x-{difficulty[:2]}{i}", "repo": repo,
                             "difficulty": difficulty, "version": "1.0"})
    rows.append({"instance_id": "zz__unwanted-1", "repo": "zz/zz", "difficulty": "<15 min fix", "version": "1.0"})
    rows.append({"instance_id": "a__x-hard", "repo": "a/a", "difficulty": ">4 hours", "version": "1.0"})
    return rows


def test_quotas_add_up_and_follow_the_mix():
    assert quotas(30) == {"<15 min fix": 12, "15 min - 1 hour": 15, "1-4 hours": 3}
    assert sum(quotas(7).values()) == 7


def test_selection_respects_mix_cap_repos_and_exclusions():
    chosen = select_subset(fake_instances(), size=30, seed=3, repos=REPOS, cap=12, exclude=["a__x-<10"])
    assert len(chosen) == 30
    by_difficulty = {d: sum(1 for c in chosen if c["difficulty"] == d) for d in DIFFICULTY_MIX}
    assert by_difficulty == quotas(30)
    assert all(c["repo"] in REPOS for c in chosen)
    assert all(sum(1 for c in chosen if c["repo"] == r) <= 12 for r in REPOS)
    assert "a__x-<10" not in {c["instance_id"] for c in chosen}
    assert ">4 hours" not in {c["difficulty"] for c in chosen}


def test_selection_spreads_across_repos():
    chosen = select_subset(fake_instances(), size=30, seed=1, repos=REPOS, cap=30)
    counts = sorted(sum(1 for c in chosen if c["repo"] == r) for r in REPOS)
    assert counts == [10, 10, 10]


def test_selection_is_deterministic_per_seed():
    a = [c["instance_id"] for c in select_subset(fake_instances(), size=9, seed=7, repos=REPOS)]
    b = [c["instance_id"] for c in select_subset(fake_instances(), size=9, seed=7, repos=REPOS)]
    c = [c["instance_id"] for c in select_subset(fake_instances(), size=9, seed=8, repos=REPOS)]
    assert a == b and a != c


def test_cap_limits_total_when_pool_is_small():
    assert len(select_subset(fake_instances(), size=30, seed=1, repos=REPOS, cap=5)) == 15
