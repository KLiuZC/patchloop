# SWE-bench 单题 RepairRunner 验证：pytest-7205

日期：2026-10-05。分支：`feat/repair-real-task`。

## 结果

RepairRunner 成功完成了真实题目的容器链路：读取题目 → 恢复数据集指定的 base commit → 在 `/testbed` 中运行 Agent → 收集补丁 → 在新容器中验证候选补丁。

| 阶段 | 结果 |
| --- | --- |
| Agent 执行 | completed，40 轮后文字结束，生成补丁 |
| 可见复现验证 | baseline 失败，candidate 通过 |
| 官方 SWE-bench harness | patch successfully applied |
| 官方最终结果 | **0/1 resolved** |
| 官方 F2P | 1 通过，9 失败 |
| 官方 P2P | 16 通过，0 失败 |
| 基础设施错误 | 0 |
| Agent 估算费用 | ¥0.149 |
| Agent 记录用量 | 831,972 prompt / 29,681 completion tokens |

## 两次验证的区别

本地可见验证使用了独立的复现脚本：

```text
python -bb -m pytest --setup-show /tmp/test_bw_issue.py
```

基线触发 `BytesWarning`，候选补丁通过，说明 Agent 确实修复了题面中的 bytes 复现。

官方 harness 还应用了 FAIL_TO_PASS 测试补丁，检查更多参数显示场景。候选补丁只对 `bytes` 类型使用 `saferepr`：

```python
if isinstance(cached_param, bytes):
    cached_param = saferepr(cached_param)
```

因此 bytes 场景通过，但普通字符串参数仍输出为 `[foo]`，官方新增测试要求 `['foo']`，导致另外 9 个 F2P 测试失败。标准答案对所有参数统一使用 `saferepr`。

## 官方标签的解释

官方汇总把该题标为 `no_tests_collected`，但这不是本题的主要原因。测试输出显示外层测试文件已经执行，结果为 17 passed、9 failed；其中内部 `--setup-only`/`--setup-plan` 子进程会正常输出 `no tests ran`，触发了 harness 的文本分类规则。

本次应归类为：

```text
补丁应用成功；可见复现通过；官方扩展测试失败；修复语义不完整
```

不是 Docker、镜像、base commit、API 或 harness 基础设施失败。

## 证据位置

- RepairRunner 产物：`/tmp/patchloop-repair-runs/swebench-pytest-7205-v3/pytest-dev__pytest-7205/`
- 官方结果：`~/code/swebench-work/logs/evaluation/repair-swebench-pytest7205-v3/results.json`
- 逐题报告：`~/code/swebench-work/logs/evaluation/repair-swebench-pytest7205-v3/patchloop-repair-v3/pytest-dev__pytest-7205/report.json`
- 官方测试输出：同目录下的 `test_output.txt`

该次运行证明了真实 SWE-bench 适配器的执行和产物链路可用；不能把可见验证通过当作最终 resolved。下一步应保留这条失败样例，再接批量判卷或改进验证 profile，不把 hidden 测试反馈回同一轮 Agent。

## 官方判卷集成验收（2026-10-07）

已实现 `agent repair-judge`，直接读取本次 v3 的产物目录，自动生成官方预测格式并调用 harness；没有重新调用模型、修改补丁或重跑可见验证。`repair-swebench --judge` 为新任务提供答题后自动判卷的入口。

验收输出：

```text
visible=passed · official=failed · resolved=false · F2P=1/10 · P2P=16/16
```

用同一补丁进行了两次判卷，均得到上述结果，并保存为不同 ID 下的独立快照。这个重复是集成和重判路径验证，不是两次独立模型答题。原 task、patch、visible validation、summary、events 的 SHA-256 在前后均一致。最终判卷 ID、耗时和哈希见 [official.json](official.json)；准确输入见 [judged.patch](judged.patch)，官方原报告见 [official-report.json](official-report.json) 和 [official-results.json](official-results.json)。这些副本便于在 `/tmp` 清理后复核判分。

本次新增或调整的自动化测试覆盖：只判卷不初始化模型、可见失败后仍可官方判卷、原件和快照哈希、重复判卷不覆盖、报告缺失/损坏/矛盾、空补丁、缺少 harness、执行失败以及拒绝覆盖已有答题目录。全套 110 个测试通过。代码审查发现的快照时序和空报告字段问题已分别复现并修复。

原始 `validation_status=passed` 继续仅表示可见验证通过。官方结果单独保存，不把可见结论覆盖为失败，也不把官方失败伪装为通过。`no_tests_collected` 标签仍作为原始诊断线索保存；本次确定的失败表现仍是外层字符串参数断言不符合预期。
