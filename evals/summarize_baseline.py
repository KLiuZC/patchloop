"""Rebuild the 4B baseline tables from saved results, without model or Docker calls."""

import csv
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals/reports/4b-baseline"
RUNS = ["v30-deepseek-r1", "v30-deepseek-r2", "v30-deepseek-r3", "v30-glm", "v30-single-shot"]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def category(result, report):
    if result["resolved"]:
        return "resolved"
    if result.get("error"):
        return "run_error"
    note = result.get("note") or ""
    if "truncated" in note:
        return "output_truncated"
    if "did not apply" in note:
        return "patch_did_not_apply"
    if not result["patch_lines"]:
        return "empty_patch"
    if report.get("collection_error"):
        return "test_collection_blocked"
    if not report.get("report_present"):
        return "missing_report"
    return "tests_not_resolved"


def main():
    manifest = read_json(OUT / "manifest.json")
    # Verify archived inputs, not the live source tree: later experiments can change code.
    for name, digest in manifest["frozen_inputs_sha256"].items():
        actual = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        require(actual == digest, f"baseline input changed: {name}")
    subset = []
    metadata = {}
    for line in (ROOT / "evals/subsets/verified30.txt").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        iid, annotation = line.split("#", 1)
        iid = iid.strip()
        subset.append(iid)
        repo_version, difficulty = annotation.strip().split(" · ")
        metadata[iid] = {"repo": repo_version.split()[0], "difficulty": difficulty}
    require(len(subset) == len(set(subset)) == 30, "expected 30 unique instances")
    evidence = read_json(OUT / "evidence.json")
    require(set(evidence["gold-verified30"]["overall"]["resolved_ids"]) == set(subset), "gold validation mismatch")
    all_rows, runs = [], []
    grouped = defaultdict(lambda: defaultdict(lambda: [0, 0]))

    for rid in RUNS:
        summary = read_json(ROOT / "evals/results" / rid / "summary.json")
        results = {r["instance_id"]: r for r in summary["results"]}
        require(len(results) == len(summary["results"]) == 30, f"duplicate/missing results: {rid}")
        require(set(results) == set(subset), f"subset mismatch: {rid}")
        official = evidence[rid]["overall"]
        predictions = [json.loads(line) for line in (ROOT / "evals/results" / rid / "predictions.jsonl").read_text().splitlines() if line.strip()]
        patches = {p["instance_id"]: p["model_patch"] for p in predictions}
        require(len(patches) == len(predictions) == 30 and set(patches) == set(subset), f"predictions mismatch: {rid}")
        require({iid for iid, r in results.items() if r["resolved"]} == set(official["resolved_ids"]), f"verdict mismatch: {rid}")
        require({iid for iid, patch in patches.items() if not patch} == set(official["empty_patch_ids"]), f"empty patch mismatch: {rid}")
        counts = Counter()
        for iid in subset:
            r = results[iid]
            report = evidence[rid]["instances"][iid]
            require(r["patch_lines"] == patches[iid].count("\n"), f"patch length mismatch: {rid}/{iid}")
            if report["report_present"]:
                require(report["resolved"] == r["resolved"], f"instance verdict mismatch: {rid}/{iid}")
            outcome = category(r, report)
            counts[outcome] += 1
            at_limit = r["mode"] == "agent" and not r["finished"] and not r["error"] and r["turns"] == 40
            row = {
                "run_id": rid, "instance_id": iid, **metadata[iid],
                "resolved": r["resolved"], "category": outcome,
                "harness_failure_reason": official.get("failure_reasons", {}).get(iid, ""),
                "has_report": report["report_present"], "turns": r["turns"],
                "self_finished": r["finished"] if r["mode"] == "agent" else "",
                "at_turn_limit": at_limit, "patch_lines": r["patch_lines"],
                "cost_cny_estimate": r["cost"], "instance_seconds": r["seconds"],
                "prompt_tokens": r["prompt_tokens"], "cached_tokens": r["cached_tokens"],
                "completion_tokens": r["completion_tokens"],
                **{k: report.get(k, "") for k in ["f2p_pass", "f2p_fail", "p2p_pass", "p2p_fail"]},
                "error": r.get("error", ""), "note": r.get("note", ""),
            }
            all_rows.append(row)
            for dimension in ["repo", "difficulty"]:
                totals = grouped[dimension][(rid, metadata[iid][dimension])]
                totals[0] += int(r["resolved"])
                totals[1] += 1
        resolved = counts["resolved"]
        require(resolved == summary["resolved"] == official["resolved_instances"], f"total mismatch: {rid}")
        require(sum(bool(p) for p in patches.values()) == official["completed_instances"], f"report count mismatch: {rid}")
        selected = [row for row in all_rows if row["run_id"] == rid]
        runs.append({
            "run_id": rid, "model": summary["model"], "mode": summary["mode"],
            "instances": 30, "resolved": resolved, "resolve_rate_percent": resolved / 30 * 100,
            "cost_cny_estimate": summary["cost_cny"],
            "instance_seconds_sum": summary["seconds"], "wall_seconds": None,
            "mean_turns": statistics.mean(r["turns"] for r in results.values()),
            "self_finished": sum(r["finished"] for r in results.values()) if summary["mode"] == "agent" else None,
            "at_turn_limit": sum(r["at_turn_limit"] for r in selected),
            "official_reports": official["completed_instances"],
            "with_p2p_cases": sum(isinstance(r["p2p_pass"], int) and r["p2p_pass"] + r["p2p_fail"] > 0 for r in selected),
            "p2p_failure_count": sum(r["p2p_fail"] for r in selected if isinstance(r["p2p_fail"], int)),
            "categories": dict(sorted(counts.items())),
        })

    rates = [r["resolve_rate_percent"] for r in runs[:3]]
    stability = defaultdict(list)
    for iid in subset:
        successes = sum(row["resolved"] for row in all_rows if row["run_id"] in RUNS[:3] and row["instance_id"] == iid)
        stability[str(successes)].append(iid)
    by_group = {dim: [{"run_id": rid, "group": group, "resolved": v[0], "instances": v[1]} for (rid, group), v in groups.items()] for dim, groups in grouped.items()}
    aggregate = {
        "schema_version": 1, "runs": runs,
        "deepseek_repeats": {
            "mean_percent": statistics.mean(rates),
            "sample_sd_percentage_points": statistics.stdev(rates),
            "mean_cost_cny_estimate": statistics.mean(r["cost_cny_estimate"] for r in runs[:3]),
            "stability": dict(sorted(stability.items())),
        },
        "by_group": by_group,
        "scope": "Fixed 30-instance convenience subset; repeated runs are not independent new tasks.",
    }
    (OUT / "summary.json").write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n")
    with (OUT / "instances.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(all_rows[0]))
        writer.writeheader()
        writer.writerows(all_rows)

    lines = ["# 4B 基线结果", "", "由 `python evals/summarize_baseline.py` 从冻结的结果及官方判卷摘要生成。", "",
             "| 运行 | 解决 | 解决率 | 估算费用 | 空补丁 | 运行异常 | 自行收尾 | 40 轮上限 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in runs:
        empty = sum(row["patch_lines"] == 0 for row in all_rows if row["run_id"] == r["run_id"])
        finish = str(r["self_finished"]) if r["self_finished"] is not None else "—"
        lines.append(f"| {r['run_id']} | {r['resolved']}/30 | {r['resolve_rate_percent']:.1f}% | ¥{r['cost_cny_estimate']:.3f} | {empty} | {r['categories'].get('run_error', 0)} | {finish} | {r['at_turn_limit']} |")
    lines += ["", f"DeepSeek 三次均值 **{statistics.mean(rates):.1f}%**，样本标准差 **{statistics.stdev(rates):.1f} 个百分点**，平均估算费用 ¥{aggregate['deepseek_repeats']['mean_cost_cny_estimate']:.2f}。标准差描述这三次运行的波动，不是置信区间。", "",
              "## 观测到的失败类别", "", "类别描述结果证据，不自动等同于模型能力归因。运行异常优先于空补丁；各类别互斥。", "",
              "| 运行 | 测试未解决 | 测试导入受阻 | 空补丁 | 补丁应用失败 | 输出截断 | 运行异常 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in runs:
        c = r["categories"]
        lines.append("| " + r["run_id"] + " | " + " | ".join(str(c.get(k, 0)) for k in ["tests_not_resolved", "test_collection_blocked", "empty_patch", "patch_did_not_apply", "output_truncated", "run_error"]) + " |")
    lines += ["", "## 各仓库解决数", "", "| 仓库 | DS r1 | DS r2 | DS r3 | GLM | 单次直出 |", "| --- | --- | --- | --- | --- | --- |"]
    for group in sorted({m["repo"] for m in metadata.values()}):
        lines.append("| " + group + " | " + " | ".join(f"{grouped['repo'][(rid, group)][0]}/{grouped['repo'][(rid, group)][1]}" for rid in RUNS) + " |")
    lines += ["", "## 各难度解决数", "", "| 难度 | DS r1 | DS r2 | DS r3 | GLM | 单次直出 |", "| --- | --- | --- | --- | --- | --- |"]
    for group in ["<15 min fix", "15 min - 1 hour", "1-4 hours"]:
        lines.append("| " + group + " | " + " | ".join(f"{grouped['difficulty'][(rid, group)][0]}/{grouped['difficulty'][(rid, group)][1]}" for rid in RUNS) + " |")
    lines += ["", "## 使用边界", "",
              "- 30 题均通过 gold 校验；所有 30 题均保留在分母中，包括空补丁和 API 超时。人工诊断未补分或删题。",
              "- 题单覆盖 6 个仓库各 5 题，难度为 12/15/3；不是 Verified 全集的随机代表样本。",
              "- 单次直出没有源码和工具；分差反映源码访问、工具执行和多轮反馈的共同作用。",
              "- 自行收尾仅表示模型不再调用工具，不表示已经验证正确。空补丁不证明从未编辑文件，也可能是修改被撤回。",
              "- 官方 PASS_TO_PASS 集合未报告失败，不能推断全部原有测试通过；部分题没有此集合，部分测试模块还有不参与判分的失败。",
              "- 费用按项目内置价格估算，不是账单；失败请求用量可能未记录。耗时总和不是并行墙钟时间，报告不据此排速度名次。",
              "- 当前源码和配置为事后归档快照；历史运行未逐次保存代码哈希、数据集 revision 或完整请求配置。不能保证原样重新运行得到同样分数。",
              "- 该集合的失败已用于设计下一步改进，此后作为开发/对比集；最终泛化结论需另留未用于调参的题单。",
              "", "详细方法、诊断和下一步实验约定见 [README.md](README.md)。", ""]
    (OUT / "results.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Verified {len(all_rows)} instance results across {len(runs)} runs; wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
