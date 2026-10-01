# 剩余轮数提醒：smoke5 验证

日期：2026-09-30。模型 `deepseek-flash`，40 轮、命令超时 180 秒、2 题并发、每容器 3GB。命令：

```bash
uv run agent eval -r smoke5-budget-deepseek -f evals/subsets/smoke5.txt \
  -p deepseek --workers 2 --memory 3g --budget-reminders
```

## 结果

| 题目 | 模型请求轮数 | 实际提醒轮次 | 官方解决 |
| --- | --- | --- | --- |
| pytest-7205 | 14 | 无 | 否 |
| pytest-7236 | 23 | 21 | 是 |
| pytest-7324 | 36 | 21、36 | 是 |
| pytest-7432 | 9 | 无 | 是 |
| flask-5014 | 10 | 无 | 是 |

正式结果 **4/5**，零 API 错误、零空补丁，5 道均自行收尾，未到达 40 轮上限。估算 ¥0.289；`evaluate` 本次调用实际耗时 366.8 秒（包括答题与判卷，不包括启动及数据集加载）。累计任务耗时 386.0 秒，二者口径不同。

提醒原文在第 21、36 次模型请求前写入 `budget_reminder` 轨迹事件；前一轮的工具结果全部回填后才插入。此次共 3 次事件，后续 API 请求都正常完成。提前停止的任务没有提醒。源码哈希、事件轮次、工具调用 ID 与官方判卷已核对，摘要见 [verification.json](verification.json)；配置见 [run_config.json](run_config.json)。完整轨迹在本地 `traj/`，沿用不入 git 的规则。

## pytest-7205 的失败

这道题在第 14 轮结束，未收到提醒。模型修复了 bytes 参数转换时报错的问题，但保留了字符串直接输出的分支。官方测试希望字符串以 repr 形式显示：期望 `arg_same['foo']`，实际 `arg_same[foo]`。

官方 F2P 1/10、P2P 16/16；整个测试模块为 9 failed、17 passed。未修改原始补丁或人工补分。模型答题时原有测试通过，不代表新增验收断言也通过。

官方汇总另外把该题标为 `no_tests_collected`。检查本机 harness 的 `infra_failure.py`，该标签依据文本 `no tests ran|collected 0 items` 匹配；本题内部调用 `--setup-only` 会正常输出 `no tests ran`，但外层已经收集并执行了 26 个测试。因此这次标签不能解释为环境未能运行测试，应以外层断言失败为准。

## 能说明什么

本次验证说明开关、两次提醒、工具协议、日志、补丁提交和判卷的闭环正常；不能证明提醒提高或降低成功率。pytest-7324 在收到首次提醒后仍继续搜索缓存等位置，随后才完成修复，说明提醒不一定马上改变行为。

这是已有开发集的一次运行，尚未启动 verified30 三遍对照。后续使用新的 `v30-budget-r1/r2/r3`，固定其余设置，比较解决率、空补丁、达到上限、收尾和费用，保留所有结果。
