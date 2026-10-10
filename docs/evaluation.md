# 评测与验收

[项目首页](../README.md) · [实验索引](../EXPERIMENTS.md) · [使用指南](usage.md)

本文说明评测范围、运行入口、结果判读和已完成的对照结果。逐次运行与产品验收通过实验索引访问，历史报告保留原始版本和证据。

## 目录

- [范围与口径](#范围与口径)
- [运行准备](#运行准备)
- [批量评测](#批量评测)
- [单题修复与独立判卷](#单题修复与独立判卷)
- [预算与结束方式](#预算与结束方式)
- [结果读取与复算](#结果读取与复算)
- [完整结果](#完整结果)
- [产品流程验收](#产品流程验收)

## 范围与口径

| 类型 | 用途 | 结果边界 |
| --- | --- | --- |
| gold 预检 | 用标准答案检查题目环境和判卷流程 | 不计作 Agent 修复成绩 |
| `smoke5` | 开发与功能冒烟 | 已用于设计和调试，不作为泛化成绩 |
| `verified30` | 固定 30 题的基线和策略对照 | 同一题单重复运行；已用于失败分析，不是新留出集 |
| 产品流程验收 | 核对网页、worker、审查和交付 | 独立于 SWE-bench 修复能力统计 |

`verified30` 覆盖 6 个仓库、每仓库 5 题，历史 gold 校验为 30/30。正式对照保留请求失败、空补丁和未解决题目；人工诊断不补分。新评估需在运行前固定题单、分母、模型、实现版本和重试规则，不根据模型结果事后换题。

模型只接收题面与允许访问的仓库环境，不向其提供 hints、测试补丁或标准答案；官方判卷反馈不回填本次修复循环。单次直出基线只接收题面，因此与工具 Agent 的差异包含源码访问、工具执行和多轮反馈的共同影响。

## 运行准备

以下 patchloop 命令在项目根目录执行。需要配置模型凭证、启动 Docker、准备题目镜像，并安装官方 harness：

```bash
uv tool install "git+https://github.com/SWE-bench/SWE-bench"
```

harness 工作目录按显式 `--harness-dir`、`SWEBENCH_WORKDIR`、默认 `~/code/swebench-work` 选择。需要本地构建镜像时，在该目录准备 `swe-bench-tasks` 任务仓库，并在 **harness 工作目录**执行 gold 预检，例如：

```bash
swebench eval verified --gold \
  -i pytest-dev__pytest-7205 \
  --run-id gold-preflight \
  --task-repo ./swe-bench-tasks -j 1
```

架构、依赖及构建并发按实际机器核对。旧的 Apple Silicon、Rosetta、磁盘容量和构建耗时观察保存在[历史环境备忘](history/experiments-through-2026-10-09.md#环境备忘)，不是所有环境的固定要求。

题单由 `agent subset` 按随机种子、难度分层与仓库上限生成。已有题单应直接使用；下面将历史生成参数输出到新文件，避免覆盖冻结题单：

```bash
uv run agent subset -o evals/subsets/verified30-reproduced.txt \
  --size 30 --seed 20260929 --cap 7 -x evals/subsets/smoke5.txt
```

数据集 revision 变化仍可能影响重建结果，应核对 ID、数据哈希和原记录。需要泛化评估时另行选题，并排除已参与设计或分析的问题。

## 批量评测

Agent 修复及官方判卷：

```bash
uv run agent eval -r smoke-deepseek-new \
  -f evals/subsets/smoke5.txt -p deepseek
```

同题单的单次直出对照：

```bash
uv run agent eval -r smoke-single-shot-new \
  -f evals/subsets/smoke5.txt -p deepseek --mode single-shot
```

开启轮数提醒的对照配置：

```bash
uv run agent eval -r v30-budget-new \
  -f evals/subsets/verified30.txt -p deepseek \
  --workers 2 --memory 3g --budget-reminders
```

每次新实验使用新的 run-id，并保持需要比较的其他条件一致。macOS 可在命令前加 `caffeinate -i` 防止闲置睡眠。两题并发和内存参数来自历史配置，应根据机器资源调整并记录。

同一 run-id 再次运行会跳过已保存预测记录的题目，包括空补丁和请求失败。需要重新求解时使用新 run-id；不要通过删除旧结果改变正式分母。保存的 `run_config.json` 必须与继续运行的配置相符，缺少配置记录的旧运行也需使用新编号。

## 单题修复与独立判卷

`agent repair-swebench` 在题目镜像 `/testbed` 中修复，再使用新容器执行可见验证；加 `--judge` 后将已保存补丁交给官方 harness。镜像需提前准备，此入口不会通过 `--task-repo` 重建镜像。

下面是 pytest-7205 的题面复现示例，不引用官方隐藏测试名称：

```bash
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
  --provider deepseek --budget-reminders --judge \
  --artifacts /tmp/patchloop-repair-runs/pytest-7205-new-attempt
```

每次新修复使用空的产物目录。已有目录会被拒绝；可见复现覆盖范围不等于官方测试范围，历史上该题存在可见通过但官方未解决的候选，见[单题报告](../evals/reports/repair-swebench-pytest7205-v3/README.md)。

仅对上述已保存补丁判卷，不重新调用模型或运行可见验证：

```bash
uv run agent repair-judge \
  --artifacts /tmp/patchloop-repair-runs/pytest-7205-new-attempt/pytest-dev__pytest-7205
```

`repair-judge` 接收具体题目目录，`repair-swebench` 接收其父目录。前者的 `--timeout`、后者的 `--judge-timeout` 设置官方单题测试超时，默认 1800 秒，不是下载和启动全过程的时限。只判卷需要 Docker 与 harness，不要求模型密钥；可使用 `.env` 中的 `HF_TOKEN`。

## 预算与结束方式

| 选项 | 行为 | 默认值与适用范围 |
| --- | --- | --- |
| `--budget-reminders` | 剩余 20、5 次模型请求时提醒，保留工具调用与结果配对 | `agent run`、`agent eval`、`agent repair` 默认关闭；`repair-swebench` 默认开启 |
| `--explicit-finish` | 要求模型单独调用 `finish(summary)` 主动结束 | 默认关闭；批量评测仅适用于 agent 模式 |

任务 API 的 `execution.budget_reminders` 默认开启，与上述部分 CLI 不同，见 [API 执行配置](api.md#执行配置与路径策略)。

提醒不增加预算或强制结束。40 轮预算对应第 21、36 次请求前；多个工具调用仍算一轮。阈值须在已消耗至少一轮后达到：20 轮预算仅在第 16 轮提醒，5 轮及以下不提醒。事件在请求发出前写入轨迹，包含触发轮次和剩余数。

显式结束要求非空 `summary`，且该次回复只调用 `finish`。参数错误或与其他工具同轮调用时拒绝结束，并将结果回填；纯文字回复会提示继续调用 `finish`。仍受轮数上限约束，结束声明不保证补丁正确或非空。`finish_required` 记录后续提示，`stop_reason` 区分自然结束、显式结束、预算耗尽和运行错误。

`--mode single-shot` 不接受提醒和显式结束开关。更改任一实验开关时使用新的 run-id。现有实验决定保留 `finish` 为默认关闭的可选工具，后续对照优先固定仅提醒配置。

## 结果读取与复算

| 产物 | 用途 |
| --- | --- |
| `evals/results/<run-id>/run_config.json` | 模型、预算、源码／依赖／数据哈希及 SDK 等运行信息，不保存密钥或标准答案原文 |
| `predictions.jsonl` | 官方判卷使用的逐题候选补丁 |
| `summary.json` | 逐题判卷结论、轮次、用量、费用及本次调用统计 |
| `traj/` | 轨迹与补丁，默认不纳入 Git |
| 单题目录的 `validation.json` | 干净基线和候选的可见验证 |
| 单题目录的 `official.json` | 最近一次官方判卷结果 |
| `official/<判卷ID>/` | 本次准确补丁、预测文件、harness 日志、报告及原始输出；重判保留旧尝试 |

`summary.json.invocation.wall_seconds` 是本次评测调用的墙钟耗时，包含求解与判卷，不包含此前的数据集加载；`seconds` 是各题耗时之和。Token 仅统计已返回的用量，超时请求可能存在未知账单；模型别名和镜像环境变化也会影响复现。

官方结果绑定 `instance_id`、基线、补丁哈希及判卷 ID。开始前核对补丁与原任务摘要、验证报告；创建快照或判卷期间原件变化会报错。原 `validation.json`、`summary.json` 和轨迹不由重判改写，读取历史官方结果时仍需核对补丁哈希。

| `official.status` | 含义 | 单题判卷 CLI 退出码 |
| --- | --- | --- |
| `passed` | 官方判为 resolved | 0 |
| `failed` | 官方明确未解决，包含空补丁或不能应用的补丁 | 1 |
| `error` | harness 启动／执行失败、缺少完整报告或明确基础设施异常；`resolved=null` | 2 |

官方 F2P 统计原本失败用例的修复情况，P2P 统计原本通过用例的回归情况。`no_tests_collected` 等模糊标签保留原文，不直接作为根因，也不覆盖官方逐题结论。`--judge` 开启时以官方判卷退出码为准，修复执行本身失败则跳过判卷。

无需模型或 Docker，可重新核对并生成 4B 统计：

```bash
python3 evals/summarize_baseline.py
```

该脚本会写入汇总文件；复算后核对 Git 差异，避免把格式或统计变化误记为新的实验运行。

## 完整结果

下表均来自固定 `verified30` 题单。每次 30 题，重复运行使用相同问题；标准差是这些运行之间的样本波动，不是总体置信区间。

| 配置 | 解决数 | 解决率 | 每次估算费用 |
| --- | --- | --- | --- |
| DeepSeek flash + Agent，3 次 | 20、19、20 / 30 | 均值 **65.6%**，样本标准差 1.9 个百分点 | 平均 ¥1.69 |
| DeepSeek flash + 轮数提醒，3 次 | 20、21、23 / 30 | 均值 **71.1%**，样本标准差 5.1 个百分点 | 平均 ¥1.64 |
| DeepSeek flash + 提醒 + finish，3 次 | 23、23、20 / 30 | 均值 **73.3%**，样本标准差 5.8 个百分点 | 平均 ¥1.87 |
| GLM flash + Agent，1 次 | 20/30 | **66.7%** | ¥4.05 |
| DeepSeek flash 单次直出，1 次 | 4/30 | **13.3%** | ¥3.70 |

这些结果不代表 Verified 全集。提醒组在现有实验中减少空补丁并改善收尾行为，解决率差异仍需新题单验证；显式 `finish` 未证明稳定的提分或效率收益。费用按历史价格表估算，不等于当前价格或账单。

完整分析及逐题证据分别见[基线报告](../evals/reports/4b-baseline/README.md)、[提醒实验](../evals/reports/4c-budget-reminders/README.md)、[finish 实验](../evals/reports/4c-explicit-finish/README.md)。报告中的原始限制和失败记录继续保留。

## 产品流程验收

网页、API、worker、审查、本地交付和 GitHub PR 的真实验收统一由[产品流程验收索引](../EXPERIMENTS.md#产品流程验收)导航。两仓库官方判卷集成、只重判旧补丁等记录见[修复与官方判卷集成](../EXPERIMENTS.md#修复与官方判卷集成)。

这些验收针对具体任务与流程，不加入上述基线分母。模拟请求测试、固定验证脚本预检和截图检查同样不作为真实模型修复成绩。
