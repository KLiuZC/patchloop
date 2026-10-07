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
