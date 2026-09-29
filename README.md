# patchloop

一个从零写的最小 coding agent：给它一个仓库和一个任务，它通过 bash 自己定位、修改、跑测试，直到完成。目标是在公开 benchmark 上跑出可复现的数字，并把评测和轨迹分析做成配套工具。

当前状态：一个 `bash` 工具；命令可以在本机跑，也可以在一次性的 Docker 容器里跑；无 context 压缩。

## 运行

```bash
cp .env.example .env        # 填入 API key
uv sync
uv run agent run --workdir examples/toy_repo --provider glm \
  --task "运行 pytest，修复失败的测试。不要改动 tests/ 目录。"
```

默认用两家的 flash 模型（`glm-5.3-flash`、`deepseek-flash`），便宜、够开发用；正式评测时用 `-m glm-5.3` / `-m deepseek-v4-pro` 切到旗舰。跑完后用 `git checkout -- examples/toy_repo` 还原玩具仓库。每次运行的完整轨迹写在 `traj/<run_id>.jsonl`。

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

题目子集用 `agent subset` 生成，固定随机种子、按难度分层、每个仓库封顶，文件头里带着重新生成的命令。`evals/subsets/smoke5.txt` 是开发集（调提示词、试机制都在它上面看），`evals/subsets/verified30.txt` 是报告集（只用来出数字，不在它上面调东西）：

```bash
uv run agent subset -o evals/subsets/verified30.txt --size 30 --seed 20260929 --cap 7 -x evals/subsets/smoke5.txt
```

每道题在自己的镜像里起一个容器，agent 只看到 issue 原文（hints、隐藏测试、标准答案都不给），改动收集成补丁写进 `evals/results/<run-id>/predictions.jsonl`，再交给官方判卷程序打分。汇总在 `summary.json`（含每题轮数、token、缓存命中、费用估算、是否解决），轨迹与补丁在同目录的 `traj/`（不进 git）。同一个 run-id 重跑会跳过已答过的题。

## 结构

```
src/patchloop/
  llm.py         OpenAI-compatible 客户端与 provider 表
  tools.py       bash 工具的定义与输出截断
  sandbox.py     命令在哪里执行：LocalRunner（本机）/ DockerRunner（容器）
  prompts.py     system prompt
  loop.py        agent 循环：调模型 → 执行工具 → 结果回填
  trajectory.py  JSONL 轨迹
  baseline.py    单次直出基线：模型盲写补丁，沙箱里 git apply
  eval.py        评测流水线：起题目容器 → 答题 → 交卷 → 官方判卷 → 汇总
  subset.py      可复现的选题：按难度分层、按仓库封顶、固定种子
  cli.py         命令行入口
examples/toy_repo/   带一个已知 bug 的玩具仓库，用于冒烟
examples/toy_image/  玩具仓库对应的 Docker 镜像（python + git + pytest）
evals/subsets/       题目子集（每行一个 instance id）
evals/results/       每次评测的答卷与汇总
tests/               项目自身的单元测试：uv run pytest
```

## 路线

- [x] 最小 loop + bash 工具
- [x] Docker 沙箱
- [x] SWE-bench Verified 子集评测流水线
- [ ] 扩大子集、基线对比、失败归因
- [ ] context 管理与消融
