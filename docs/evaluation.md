# 评测与验收

[返回项目首页](../README.md) · [使用指南](usage.md) · [实验记录](../EXPERIMENTS.md)

本文区分修复能力评测与产品流程验收：前者使用固定题单和官方判卷，后者核对具体任务从输入、验证到交付的记录。运行命令均以项目根目录为工作目录。

## 目录

- [已完成的评测](#已完成的评测)
- [批量评测](#批量评测)
- [SWE-bench 单题适配器](#swe-bench-单题适配器)
- [对已有补丁只判卷](#对已有补丁只判卷)
- [剩余轮数提醒实验](#剩余轮数提醒实验)
- [显式交卷工具](#显式交卷工具实验完成默认关闭)
- [产品流程验收](#产品流程验收)

## 已完成的评测

SWE-bench Verified 固定 30 题子集（6 个仓库各 5 题，gold 校验 30/30 通过）：


| 配置                         | 解决数           | 解决率                         | 每遍估算费用   |
| -------------------------- | ------------- | --------------------------- | -------- |
| DeepSeek flash + agent，3 遍 | 20、19、20 / 30 | 均值 **65.6%**，样本标准差 1.9 个百分点 | 平均 ¥1.69 |
| DeepSeek flash + 轮数提醒，3 遍 | 20、21、23 / 30 | 均值 **71.1%**，样本标准差 5.1 个百分点 | 平均 ¥1.64 |
| DeepSeek flash + 提醒 + finish，3 遍 | 23、23、20 / 30 | 均值 **73.3%**，样本标准差 5.8 个百分点 | 平均 ¥1.87 |
| GLM flash + agent，1 遍      | 20/30         | **66.7%**                   | ¥4.05    |
| DeepSeek flash 单次直出，1 遍    | 4/30          | **13.3%**                   | ¥3.70    |


这是特定子集的成绩，不代表 Verified 全集。单次直出只看题面，分差包含源码访问、工具执行与多轮反馈的共同收益。API 超时与空补丁保留在分母中，人工诊断不补分。费用按内置价格表估算，不是账单；标准差不是置信区间。

4C 的剩余轮数提醒实验在同一 30 题上运行三遍，解决数为 **20、21、23/30**，均值 **71.1%**（样本标准差 5.1 个百分点）。无提醒基线为 **20、19、20/30**，均值 65.6%。提醒组空补丁为 3/90、基线为 12/90；这是过程改善的证据，解决率差异仍需新题单验证，不能直接宣称提醒带来确定的提分。

finish 后续实验已完成，决定**保留为默认关闭的可选工具**。相较仅提醒组，finish 组平均解决率高 2.2 个百分点，但平均估算费用约高 14.1%，平均轮数 25.1→26.3、被上限停止次数 15/90→20/90，尚不能证明稳定的提分或收尾效率收益。后续推荐实验配置为仅传 `--budget-reminders`，不传 `--explicit-finish`；CLI 的两个开关仍各自默认关闭。详见 [finish 实验报告](../evals/reports/4c-explicit-finish/README.md)。

详细结果、逐题表、判卷限制、源码快照及下一项实验约定见 [4B 基线归档](../evals/reports/4b-baseline/README.md)。无需模型或 Docker 即可重新生成统计表：

```bash
python3 evals/summarize_baseline.py
```

## 批量评测

安装官方判卷程序：

```bash
uv tool install "git+https://github.com/SWE-bench/SWE-bench"
```

在 `SWEBENCH_WORKDIR`（默认 `~/code/swebench-work`）下准备 `swe-bench-tasks` 任务仓库。历史 Apple Silicon 环境使用任务仓库在本地构建题目镜像，在 harness 工作目录执行 `swebench eval verified --gold -i <id> --task-repo ./swe-bench-tasks -j 1` 完成预检。正式运行前冻结题单；评测开始后保留失败任务及其分类，不依据模型结果更换题目。

```bash
uv run agent eval -r smoke-deepseek -f evals/subsets/smoke5.txt -p deepseek        # agent 答题并判分
uv run agent eval -r smoke-single-shot -f evals/subsets/smoke5.txt -p deepseek --mode single-shot   # 无工具、一次直出的基线
```

题目子集用 `agent subset` 生成，固定随机种子、按难度分层、每个仓库封顶，文件头里带着重新生成的命令。`evals/subsets/smoke5.txt` 用于冒烟与开发。`evals/subsets/verified30.txt` 已完成基线评测；因为失败轨迹已经用于指导后续设计，此后作为开发/对比集，最终泛化验证需另留未用于调参的题单：

```bash
uv run agent subset -o evals/subsets/verified30.txt --size 30 --seed 20260929 --cap 7 -x evals/subsets/smoke5.txt
```

每道题使用独立的镜像容器，Agent 输入仅包含 Issue 原文，不包含 hints、隐藏测试或标准答案。候选补丁写入 `evals/results/<run-id>/predictions.jsonl`，由官方 harness 判卷。`summary.json` 保存逐题轮数、Token、缓存命中、费用估算和解决状态；轨迹与补丁位于同目录的 `traj/`，不纳入 Git。同一个 run-id 再次运行会跳过已保存预测记录的题目，**包括空补丁或请求失败的题目**；重试时应使用新 run-id，保留原记录。`seconds` 是各题耗时之和，并行运行时不等于整体墙钟耗时。

## SWE-bench 单题适配器

`agent repair-swebench` 从 Verified 数据集读取一题，在题目镜像的 `/testbed` 中运行 Agent，再启动新的同镜像容器验证补丁。Agent 只收到 issue 文本，不会收到测试补丁或隐藏测试名称。默认做可见验证，加 `--judge` 后自动把已保存补丁交给官方 harness，官方反馈不会进入本次模型循环。

```bash
# 复现来自题面；不使用仅在官方判卷阶段注入的测试名称。
PATCHLOOP_VERIFY_CMD=$(cat <<'PROFILE'
set -e
cat > /tmp/test_bw_issue.py <<'PYTEST'
import pytest

@pytest.mark.parametrize("data", [b"Hello World"])
def test_data(data):
    pass
PYTEST
python -bb -m pytest --setup-show -q /tmp/test_bw_issue.py
PROFILE
)

docker info >/dev/null && uv run agent repair-swebench \
  --instance pytest-dev__pytest-7205 \
  --verify "$PATCHLOOP_VERIFY_CMD" \
  --provider deepseek \
  --budget-reminders \
  --judge \
  --artifacts /tmp/patchloop-repair-runs/pytest-7205-new-attempt
```

每次新答题应使用空的产物目录；已有目录会被拒绝，避免覆盖历史补丁和判卷记录。镜像需要预先准备，本命令不会通过 `--task-repo` 重建镜像。

## 对已有补丁只判卷

`repair-judge` 不创建模型客户端、不重跑修复或可见测试。`--artifacts` 指向含有 `task.json`、`summary.json` 和 `patch.diff` 的**具体题目目录**，与 `repair-swebench` 接收的父目录不同：

```bash
docker info >/dev/null && uv run agent repair-judge \
  --artifacts /tmp/patchloop-repair-runs/swebench-pytest-7205-v3/pytest-dev__pytest-7205
```

官方 harness 工作目录按 `--harness-dir`、`SWEBENCH_WORKDIR`、`~/code/swebench-work` 的顺序选取。`repair-judge --timeout`（自动判卷对应 `--judge-timeout`）设置官方单题测试超时，默认 1800 秒，不是整个下载/启动阶段的总超时。需要本机已有 `swebench` CLI 与正常运行的 Docker；使用 `.env` 的 HF_TOKEN 时会传递给 harness，但不要求模型 API key。

新增产物：

```text
official.json                         最近一次判卷结果：独立于可见验证
official/<唯一判卷ID>/
  patch.diff                          本次提交的准确补丁快照
  predictions.jsonl                   自动生成的官方答卷格式
  harness.log                        判卷程序的标准输出及错误
  results.json / report.json          官方汇总及逐题报告（如已产生）
  test_output.txt / run_instance.log  原始测试及执行日志（如已产生）
  result.json                        本次判卷结果；重判不会覆盖旧尝试
```

官方结果保存 `instance_id`、`base_sha`、`patch_sha256`、可见验证状态、判卷 ID、F2P/P2P 计数和原始异常标签。开始前核对 patch 与原修复摘要/可见验证哈希；提交快照创建或判卷期间原件改变会报错。`validation.json`、原始 `summary.json` 和轨迹保持不变。读取或展示历史 `official.json` 时应确认其补丁哈希仍与当前 `patch.diff` 一致。

`official.status` 的含义：`passed` 是官方 resolved；`failed` 是官方明确未解决（包括空补丁或补丁不能应用）；`error` 是 harness 启动/执行失败、报告缺失或格式不完整、官方明确的基础设施异常，此时 `resolved=null`。`no_tests_collected` 等模糊标签原样保留，不覆盖逐题 resolved 结论，不自动当作根因。CLI 退出码分别为 0、1、2；`--judge` 开启时以官方判卷退出码为准，修复执行本身失败则跳过判卷。

这个适配器已用原有 `pytest-7205` 补丁验收“只判卷”路径：可见 bytes 复现通过，官方 F2P 1/10、P2P 16/16，最终 0/1，表示**判卷流程成功执行，但候选补丁未解决题目**。详见 [单题实验报告](../evals/reports/repair-swebench-pytest7205-v3/README.md)。

后续已在 pytest-7432 和 Flask-5014 各运行一次完整 `repair-swebench --judge`：均从可见基线失败到候选通过，并被官方判为 resolved；F2P 各 1/1，P2P 为 77/77 与 59/59。记录、复现脚本及补丁见 [两题集成验证](../evals/reports/repair-real-task-smoke2/README.md)。这是已有开发题上的工程验收，不是新的留出集成绩。

## 剩余轮数提醒实验

`agent run` 和 `agent eval` 支持 `--budget-reminders`，**默认关闭**。开启后，在剩余 20、5 次模型请求时各提醒一次；40 轮预算对应第 21、36 轮请求前，剩余数包含即将开始的一轮。一次回复中的多个工具调用仍只算一轮。预算较小时，只触发已经消耗至少一轮后能到达的阈值，例如 20 轮预算只在第 16 轮提醒，5 轮及以下不提醒。

提醒使用普通 user 消息，插在上一轮全部工具结果之后；不强制交卷、不增加工具、不改变原始系统提示词。模型提前收尾时不再提醒。提醒的原文、目标轮次和剩余数作为 `type=budget_reminder` 事件写入轨迹，每题结果记录 `budget_reminders_sent`。事件在请求发出前写入，即使该请求失败也能查到这次尝试。

```bash
# 开发集：检验提醒和完整判卷流程
uv run agent eval -r smoke5-budget-deepseek \
  -f evals/subsets/smoke5.txt -p deepseek --workers 2 --memory 3g --budget-reminders

# 正式对比：每次使用新 run-id，保持其他参数一致
uv run agent eval -r v30-budget-r1 \
  -f evals/subsets/verified30.txt -p deepseek --workers 2 --memory 3g --budget-reminders
```

macOS 上可在命令前加 `caffeinate -i`，防止运行期间系统闲置睡眠。

`--mode single-shot` 不接受该开关。新评测会在答题前保存 `run_config.json`，包含模型、提醒配置、轮数/超时/并发、源码及依赖锁哈希、所选数据行哈希和实际 SDK 版本；不保存密钥或标准答案原文。配置一致才允许同一 run-id 续跑；源码、模型、题单或提醒开关改变时请换新 run-id。没有配置记录的历史运行也要求使用新 run-id，避免混入旧答卷。

`summary.json.invocation` 记录本次 `evaluate` 调用的开始/结束时间、实际墙钟耗时和跳过的已答题数，包含本次答题与判卷，不包含数据集加载时间，也不把多次中断的间隔累计为运行时间。旧的 `seconds` 字段保留为任务耗时之和。超时请求的用量、可变模型别名及镜像环境变化仍可能影响复现和费用估算。

开发集通过只能证明实现能工作。4C 三遍已完成：提醒减少了空补丁和达到轮数上限的任务，但解决率差异仍受模型随机性影响；`verified30` 已用于分析失败，最终泛化验证需另留新题单。详细结果见 [4C 报告](../evals/reports/4c-budget-reminders/README.md)。

首次 [smoke5 验证](../evals/results/smoke5-budget-deepseek/README.md) 已完成：4/5、5 道均自行收尾、3 次提醒按时发送，零 API 错误。未通过的一题在提醒前就已结束，失败来自字符串显示与官方断言不符；本次仅验证功能闭环，不作为提醒收益结论。

## 显式交卷工具（实验完成，默认关闭）

`agent run` 和 `agent eval` 支持 `--explicit-finish`，默认关闭。开启后，模型必须单独调用 `finish({"summary": "修复总结"})` 才会主动结束；程序保存总结，收集当前补丁，然后沿用原有评测流程。交卷不保证补丁正确，空补丁也允许提交，最终仍由官方判卷决定 `resolved`。

- `summary` 必须是非空字符串。参数错误会作为工具结果回传，让模型在剩余预算内修正。
- 单独的文字回复不再表示交卷，程序会提示调用 `finish`，仍受原来的轮数上限约束。
- 同一回复中将 `finish` 与其他工具一起调用时，交卷被拒绝，其余工具按原顺序执行；全部工具结果回传后，模型可以再单独交卷。
- 原有结束方式仍是默认。开启 `--budget-reminders --explicit-finish` 时，提醒的触发轮次不变，仅把收尾要求改为调用 `finish`。
- 该选项只适用于 agent 模式，与 `--mode single-shot` 同用会报参数错误；开启或关闭它须使用新的 run-id，以免混入不同配置的答卷。

运行配置和轨迹元数据保存 `explicit_finish`。纯文字回复后的提示记录为 `finish_required` 事件，`turn` 表示下一次模型请求的轮次。结果通过 `stop_reason` 区分 `finish`（显式交卷）、`no_tool_calls`（原模式文字结束）、`max_turns`（预算耗尽）；评测中的运行异常记为 `error`，单次直出记为 `single_shot`。`final_message` 保存模型交卷总结，不是独立验证过的修复结论。

smoke5 已通过 5/5，verified30 三次分别解决 23、23、20 题（每次 30 题）；默认值经检查仍为 `False`。功能可正常交卷，但当前对比没有达到节省轮数和费用的预期，因此暂不作为推荐主配置。没有传 `--explicit-finish` 时仍使用原来的文字结束方式，历史实验结果不变。

## 产品流程验收

以下记录验证具体工程流程，与上面的修复能力评测分别统计。任务规模、验证范围、版本信息和限制保留在各自报告中。

| 场景 | 结果与证据 |
| --- | --- |
| API 与 worker | [真实示例任务](../evals/reports/task-worker-toy-smoke/README.md)：完成任务受理、后台修复、独立验证、持久化和容器清理 |
| 网页工作台 | [网页真实验收](../evals/reports/task-ui-toy-smoke/README.md)：提交、结果展示、刷新后查询；记录测试未覆盖的大写前缀行为变化 |
| 人工审查与本地交付 | [本地交付验收](../evals/reports/patch-review-delivery-toy-smoke/README.md)：人工接受后生成独立分支，交付内容与批准补丁一致 |
| 公开 Issue 输入 | [Flask #6146 文档任务](../evals/reports/github-issue-flask6146-smoke/README.md)：导入问题、准备版本、文档修复及本地交付，未发布上游 PR |
| GitHub PR 交付 | [示例仓库真实验收](../evals/reports/github-pr-publish-demo-smoke/README.md)：修复前 1 failed / 10 passed，修复后 11 passed；批准后发布草稿 PR，保留认证修复和原请求恢复记录 |
| 官方判卷集成 | [两仓库修复验收](../evals/reports/repair-real-task-smoke2/README.md)：两道已有开发题通过可见验证及官方判卷，不作为新留出集成绩 |

原始配置与逐次实验结论见 [EXPERIMENTS.md](../EXPERIMENTS.md)。本文只汇总已有结果，不将页面截图或文档更新计作新的模型实验。
