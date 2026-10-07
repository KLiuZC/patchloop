# RepairRunner 两题集成验证

日期：2026-10-07。代码提交：`e9eb3d2`，分支 `feat/repair-real-task`。本次没有改 Agent、提示词、工具或判卷逻辑，每题只运行一次，没有根据官方反馈重写补丁。

## 目标和范围

用已存在于 smoke5 开发集中的两道题，验证新入口 `agent repair-swebench --judge` 能完成：题目镜像启动、基线提交恢复、Agent 修复、独立可见验证、自动官方判卷和产物保存。两题来自 pytest 与 Flask，已有镜像可复用。

这是工程集成验证，不是新增留出集，不与之前 verified30 的统计混算。先前 pytest-7205 的“可见通过、官方未解决”记录仍保留，本次没有改写它。

## 配置与可见复现

- 模型：`deepseek-flash`，预算提醒开启、finish 关闭，每题最多 40 次模型请求，命令超时 180 秒，每容器 3GB，依次运行两题。
- 代码哈希、启动参数、实例基线提交、镜像 ID、验证脚本哈希在 [plan.json](plan.json) 中固定。
- pytest-7432：题面给出 `@pytest.mark.skip` 的位置报告问题。可见脚本比较有无 `--runxfail` 的结果，要求均指向测试文件、报告位置一致，不依赖隐藏测试名称。原始代码带 `--runxfail` 时指向 `src/_pytest/skipping.py`，断言失败。
- Flask-5014：题面要求空名称 Blueprint 抛出 ValueError。可见脚本检查空名被拒绝，并确认普通名称仍能注册到 Flask 应用。原始代码接受空名，断言失败。
- 两个脚本均在模型答题前于干净容器确认复现失败，随后固定脚本，模型没有收到官方测试补丁、标准答案或判卷反馈。脚本分别为 [pytest7432.sh](profiles/pytest7432.sh) 和 [flask5014.sh](profiles/flask5014.sh)。

## 实际结果

| 题目 | Agent 轮数 | 基线复现 | 候选可见验证 | 官方 F2P | 官方 P2P | resolved | 估算模型费用 |
| --- | ---: | --- | --- | --- | --- | --- | ---: |
| pytest-7432 | 9 | failed，exit 1 | passed，exit 0 | 1/1 | 77/77 | true | ¥0.0132 |
| Flask-5014 | 7 | failed，exit 1 | passed，exit 0 | 1/1 | 59/59 | true | ¥0.0065 |

两次均 `execution=completed`、`stop=no_tool_calls`、`visible=passed`、`official=passed`，没有执行/API 异常，没有空补丁。模型费用合计估算 ¥0.0197，来自项目价格表与返回用量，不等于平台账单。

完整 CLI 墙钟时间分别为 80.3 秒、57.3 秒，包含启动、加载题目、答题、可见验证及官方判卷，总计约 2.3 分钟。RepairRunner 自身耗时分别为 33.1 秒、21.9 秒，官方判卷分别为 35.6 秒、29.7 秒，口径与 CLI 总耗时不同。时间记录见 [launch-results.json](launch-results.json)。

pytest 补丁只修改 `src/_pytest/skipping.py`，将 skip 位置修正分支提前到 runxfail 分支之前。Flask 补丁只修改 `src/flask/blueprints.py`，在名称为空时抛出 ValueError。没有修改测试文件；原始镜像未改变，临时容器已清理。

## 产物核对

- `task.json`、`validation.json` 与官方记录绑定同一个实例和基线 SHA。
- `summary.json.patch`、`patch.diff`、自动生成的 predictions 和官方记录的补丁 SHA-256 一致。
- 两次模型轨迹的工具调用 ID 与工具返回一一配对，关闭 finish 的设置保持一致。
- 原始官方 `report.json` 确认补丁应用成功、resolved=true，F2P/P2P 数量与上述表格一致。

每题目录保存输入、修复摘要、可见验证、准确补丁和官方摘要/逐题报告：

- [pytest-7432 可见结果](pytest-dev__pytest-7432/validation.json)、[官方结果](pytest-dev__pytest-7432/official.json)、[逐题判卷](pytest-dev__pytest-7432/official-report.json)、[补丁](pytest-dev__pytest-7432/patch.diff)
- [Flask-5014 可见结果](pallets__flask-5014/validation.json)、[官方结果](pallets__flask-5014/official.json)、[逐题判卷](pallets__flask-5014/official-report.json)、[补丁](pallets__flask-5014/patch.diff)
- [核对汇总](checked-summary.json) 记录轮数、用量、补丁和轨迹哈希。

完整轨迹和过程日志保存在 `/tmp/patchloop-repair-smoke2-13l7rhjf/`；本归档只保存用于核对的产物，不包含模型推理全文。官方原始日志的位置由各 `official.json` 的 `harness_report_dir` 指明。

## 重现方式

以下命令会重新调用模型、使用新的产物目录，结果不保证与本次一致；仅查看本报告无需执行。运行前确认 Docker 和相应镜像已就绪。

```bash
PATCHLOOP_VERIFY_CMD=$(cat evals/reports/repair-real-task-smoke2/profiles/pytest7432.sh)
docker info >/dev/null && caffeinate -i uv run agent repair-swebench \
  -i pytest-dev__pytest-7432 -p deepseek --memory 3g \
  --max-model-calls 40 --command-timeout 180 --budget-reminders \
  --verify "$PATCHLOOP_VERIFY_CMD" --judge \
  --artifacts /tmp/patchloop-repair-smoke2-new
```

验证 Flask 时替换题号为 `pallets__flask-5014`，脚本改为 `profiles/flask5014.sh`。只对已有补丁重判用 `agent repair-judge --artifacts <原始具体题目目录>`，不再调用模型。

## 结论与后续

此轮证明了两种仓库的真实修复与自动判卷流程可以完整运行。加上 pytest-7205 的失败样例，已经观察到可见验证与官方结果一致、以及二者不同的两类产物。

本结果不意味着所有 SWE-bench 题目都能运行，也不意味着 RepairRunner 已具备完整的任务取消、异常中间补丁保存或生产级隔离能力。下一步先做当前分支的合并前审查，明确这些边界；随后再规划任务 API 与结果页面。新的未调参题单验证继续保留为独立工作。
