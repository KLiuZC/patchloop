# patchloop

一个从零写的最小 coding agent：给它一个仓库和一个任务，它通过 bash 自己定位、修改、跑测试，直到完成。目标是在公开 benchmark 上跑出可复现的数字，并把评测和轨迹分析做成配套工具。

当前状态：一个 `bash` 操作工具，可选 `finish` 交卷工具；命令可以在本机跑，也可以在一次性的 Docker 容器里跑；无 context 压缩。

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

finish 后续实验已完成，决定**保留为默认关闭的可选工具**。相较仅提醒组，finish 组平均解决率高 2.2 个百分点，但平均估算费用约高 14.1%，平均轮数 25.1→26.3、被上限停止次数 15/90→20/90，尚不能证明稳定的提分或收尾效率收益。后续推荐实验配置为仅传 `--budget-reminders`，不传 `--explicit-finish`；CLI 的两个开关仍各自默认关闭。详见 [finish 实验报告](evals/reports/4c-explicit-finish/README.md)。

详细结果、逐题表、判卷限制、源码快照及下一项实验约定见 [4B 基线归档](evals/reports/4b-baseline/README.md)。无需模型或 Docker 即可重新生成统计表：

```bash
python3 evals/summarize_baseline.py
```



## 运行

```bash
cp .env.example .env        # 填入 API key
uv sync
uv run agent run --workdir examples/toy_repo --provider glm \
  --task "运行 pytest，修复失败的测试。不要改动 tests/ 目录。"
```

默认用两家的 flash 模型（`glm-5.3-flash`、`deepseek-flash`），便宜、够开发用；正式评测时用 `-m glm-5.3` / `-m deepseek-v4-pro` 切到旗舰。跑完后用 `git checkout -- examples/toy_repo` 还原玩具仓库。每次运行的完整轨迹写在 `traj/<run_id>.jsonl`。

### 独立修复闭环

`agent repair` 会在固定 `base_sha` 的临时 Git worktree 中运行 Agent，收集候选补丁，然后从同一个干净基线重新应用补丁并运行固定验证命令。验证结果不会使用 Agent 工作区中的测试状态。

```bash
uv run agent repair \
  --repo examples/toy_repo \
  --goal "支持版本号字符串的 v 前缀，不改变现有版本号行为" \
  --verify "python -m pytest -q" \
  --provider deepseek \
  --budget-reminders \
  --artifacts /tmp/patchloop-repair-runs/toy-version-parser
```

Docker 模式把 Agent 和验证命令都放进指定镜像：

```bash
uv run agent repair \
  --repo examples/toy_repo \
  --goal "支持版本号字符串的 v 前缀，不改变现有版本号行为" \
  --verify "python -m pytest -q" \
  --sandbox docker --image patchloop-toy --platform linux/amd64 \
  --provider deepseek \
  --artifacts /tmp/patchloop-repair-runs/toy-version-parser
```

一次修复会产生 `task.json`、`events.jsonl`、`trajectory/`、`patch.diff`、`validation.json` 和 `summary.json`。验证器先在一个干净 worktree 中运行基线命令，再在第二个干净 worktree 中应用补丁并运行候选命令，避免测试缓存或生成文件污染结果。`validation.json` 同时保存 `baseline` 和 `candidate` 两次结果；如果基线本来就通过，最终状态会是 `inconclusive`，不会把没有复现的任务标成修复成功。`execution_status` 表示 Agent 是否完成执行，`validation_status` 表示候选补丁是否通过独立验证，`review_status` 表示人工审查状态。Agent 调用 `finish` 或回复文字都不能直接把验证状态变成通过。

### SWE-bench 单题适配器

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

### 对已有补丁只判卷

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

这个适配器已用原有 `pytest-7205` 补丁验收“只判卷”路径：可见 bytes 复现通过，官方 F2P 1/10、P2P 16/16，最终 0/1，表示**判卷流程成功执行，但候选补丁未解决题目**。详见 [单题实验报告](evals/reports/repair-swebench-pytest7205-v3/README.md)。

### 在 Docker 沙箱里跑

`--sandbox docker` 时命令全部在容器里执行，本机不跑 agent 的任何命令。`--workdir` 指定的目录会挂载到容器的 `/work`；不给 `--workdir` 则直接用镜像自带的仓库（SWE-bench 镜像的代码在 `/testbed`）。

```bash
docker build -t patchloop-toy examples/toy_image        # 带 git 和 pytest 的玩具镜像，只需一次
uv run agent run --sandbox docker --image patchloop-toy -w examples/toy_repo \
  -t "运行 pytest，修复失败的测试。不要改动 tests/ 目录。"

# 直接在 SWE-bench 的题目镜像里工作（无挂载，代码在 /testbed）
uv run agent run --sandbox docker --image swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-7205:latest \
  --platform linux/amd64 -t "..."
```

容器断网、限 2GB 内存、任务结束即销毁。每条命令经容器内的 `timeout` 和登录 shell 执行，所以镜像自带的 conda 环境是激活的。运行结束时 agent 的全部改动会以补丁形式写到 `traj/<run_id>.patch`，这就是交给评测程序的答卷。

## 评测（SWE-bench Verified）

前提：装好官方判卷程序（`uv tool install "git+https://github.com/SWE-bench/SWE-bench"`），在 `SWEBENCH_WORKDIR`（默认 `~/code/swebench-work`）下放好 `swe-bench-tasks` 题库；在 Apple Silicon 上题目镜像需要用它本地构建（`swebench eval verified --gold -i <id> --task-repo ./swe-bench-tasks -j 1`），标准答案跑不过的题不进入子集。

```bash
uv run agent eval -r smoke-deepseek -f evals/subsets/smoke5.txt -p deepseek        # agent 答题并判分
uv run agent eval -r smoke-single-shot -f evals/subsets/smoke5.txt -p deepseek --mode single-shot   # 无工具、一次直出的基线
```

题目子集用 `agent subset` 生成，固定随机种子、按难度分层、每个仓库封顶，文件头里带着重新生成的命令。`evals/subsets/smoke5.txt` 用于冒烟与开发。`evals/subsets/verified30.txt` 已完成基线评测；因为失败轨迹已经用于指导后续设计，此后作为开发/对比集，最终泛化验证需另留未用于调参的题单：

```bash
uv run agent subset -o evals/subsets/verified30.txt --size 30 --seed 20260929 --cap 7 -x evals/subsets/smoke5.txt
```

每道题在自己的镜像里起一个容器，agent 只看到 issue 原文（hints、隐藏测试、标准答案都不给），改动收集成补丁写进 `evals/results/<run-id>/predictions.jsonl`，再交给官方判卷程序打分。汇总在 `summary.json`（含每题轮数、token、缓存命中、费用估算、是否解决），轨迹与补丁在同目录的 `traj/`（不进 git）。同一个 run-id 重跑会跳过已写入答卷的题，**包括空补丁或请求失败的题**；需要重试失败题时应使用新 run-id，保留原记录。`seconds` 是各题耗时总和，并行运行时不等于全程耗时。

### 剩余轮数提醒实验

`agent run` 和 `agent eval` 支持 `--budget-reminders`，**默认关闭**。开启后，在剩余 20、5 次模型请求时各提醒一次；40 轮预算对应第 21、36 轮请求前，剩余数包含即将开始的一轮。一次回复中的多个工具调用仍只算一轮。预算较小时，只触发已经消耗至少一轮后能到达的阈值，例如 20 轮预算只在第 16 轮提醒，5 轮及以下不提醒。

提醒使用普通 user 消息，插在上一轮全部工具结果之后；不强制交卷、不增加工具、不改变原始系统提示词。模型提前收尾时不再提醒。提醒的原文、目标轮次和剩余数作为 `type=budget_reminder` 事件写入轨迹，每题结果记录 `budget_reminders_sent`。事件在请求发出前写入，即使该请求失败也能查到这次尝试。

```bash
# 开发集：检验提醒和完整判卷流程
caffeinate -i uv run agent eval -r smoke5-budget-deepseek \
  -f evals/subsets/smoke5.txt -p deepseek --workers 2 --memory 3g --budget-reminders

# 正式对比：每次使用新 run-id，保持其他参数一致
caffeinate -i uv run agent eval -r v30-budget-r1 \
  -f evals/subsets/verified30.txt -p deepseek --workers 2 --memory 3g --budget-reminders
```

`--mode single-shot` 不接受该开关。新评测会在答题前保存 `run_config.json`，包含模型、提醒配置、轮数/超时/并发、源码及依赖锁哈希、所选数据行哈希和实际 SDK 版本；不保存密钥或标准答案原文。配置一致才允许同一 run-id 续跑；源码、模型、题单或提醒开关改变时请换新 run-id。没有配置记录的历史运行也要求使用新 run-id，避免混入旧答卷。

`summary.json.invocation` 记录本次 `evaluate` 调用的开始/结束时间、实际墙钟耗时和跳过的已答题数，包含本次答题与判卷，不包含数据集加载时间，也不把多次中断的间隔累计为运行时间。旧的 `seconds` 字段保留为任务耗时之和。超时请求的用量、可变模型别名及镜像环境变化仍可能影响复现和费用估算。

开发集通过只能证明实现能工作。4C 三遍已完成：提醒减少了空补丁和达到轮数上限的任务，但解决率差异仍受模型随机性影响；`verified30` 已用于分析失败，最终泛化验证需另留新题单。详细结果见 [4C 报告](evals/reports/4c-budget-reminders/README.md)。

首次 [smoke5 验证](evals/results/smoke5-budget-deepseek/README.md) 已完成：4/5、5 道均自行收尾、3 次提醒按时发送，零 API 错误。未通过的一题在提醒前就已结束，失败来自字符串显示与官方断言不符；本次仅验证功能闭环，不作为提醒收益结论。

### 显式交卷工具（实验完成，默认关闭）

`agent run` 和 `agent eval` 支持 `--explicit-finish`，默认关闭。开启后，模型必须单独调用 `finish({"summary": "修复总结"})` 才会主动结束；程序保存总结，收集当前补丁，然后沿用原有评测流程。交卷不保证补丁正确，空补丁也允许提交，最终仍由官方判卷决定 `resolved`。

- `summary` 必须是非空字符串。参数错误会作为工具结果回传，让模型在剩余预算内修正。
- 单独的文字回复不再表示交卷，程序会提示调用 `finish`，仍受原来的轮数上限约束。
- 同一回复中将 `finish` 与其他工具一起调用时，交卷被拒绝，其余工具按原顺序执行；全部工具结果回传后，模型可以再单独交卷。
- 原有结束方式仍是默认。开启 `--budget-reminders --explicit-finish` 时，提醒的触发轮次不变，仅把收尾要求改为调用 `finish`。
- 该选项只适用于 agent 模式，与 `--mode single-shot` 同用会报参数错误；开启或关闭它须使用新的 run-id，以免混入不同配置的答卷。

运行配置和轨迹元数据保存 `explicit_finish`。纯文字回复后的提示记录为 `finish_required` 事件，`turn` 表示下一次模型请求的轮次。结果通过 `stop_reason` 区分 `finish`（显式交卷）、`no_tool_calls`（原模式文字结束）、`max_turns`（预算耗尽）；评测中的运行异常记为 `error`，单次直出记为 `single_shot`。`final_message` 保存模型交卷总结，不是独立验证过的修复结论。

smoke5 已通过 5/5，verified30 三遍为 23/23/20；默认值经检查仍为 `False`。功能可正常交卷，但当前对比没有达到节省轮数和费用的预期，因此暂不作为推荐主配置。没有传 `--explicit-finish` 时仍使用原来的文字结束方式，历史实验结果不变。

## 结构

```
src/patchloop/
  llm.py         OpenAI-compatible 客户端与 provider 表
  tools.py       bash/finish 工具定义与命令输出截断
  sandbox.py     命令在哪里执行：LocalRunner（本机）/ DockerRunner（容器）
  prompts.py     system prompt
  loop.py        agent 循环：调模型 → 执行工具 → 结果回填
  trajectory.py  JSONL 轨迹
  baseline.py    单次直出基线：模型盲写补丁，沙箱里 git apply
  eval.py        评测流水线：起题目容器 → 答题 → 交卷 → 官方判卷 → 汇总
  subset.py      可复现的选题：按难度分层、按仓库封顶、固定种子
  cli.py         命令行入口
  repair/        独立修复闭环：任务契约、worktree、产物与验证器
examples/toy_repo/   带一个已知 bug 的玩具仓库，用于冒烟
examples/toy_image/  玩具仓库对应的 Docker 镜像（python + git + pytest）
evals/subsets/       题目子集（每行一个 instance id）
evals/results/       每次评测的答卷与汇总
evals/reports/       已冻结的基线报告、证据及源码快照
evals/summarize_baseline.py  离线核对并重新生成 4B 统计
tests/               项目自身的单元测试：uv run pytest
```



## 路线

- [x] 最小 loop + bash 工具
- [x] Docker 沙箱
- [x] SWE-bench Verified 子集评测流水线
- [x] 30 题子集、两家模型及单次直出基线、失败归因与证据归档
- [x] 可开关的剩余轮数提醒、轨迹事件和运行配置记录
- [x] 剩余轮数提醒的 30 题单变量对比
- [x] 可选 finish 交卷工具、CLI 开关与本地功能验证
- [ ] context 管理与消融
- [x] finish 工具的真实模型验证与单变量对比（保留可选，默认关闭）
- [ ] 冻结仅提醒配置，在未用于调参的新题单上验证
- [ ] 结构化编辑工具的单变量实验
- [ ] 轨迹查看器、源码可见的单次直出对照、未用于调参的新题验证
