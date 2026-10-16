# xarray 三题隔离后复验配置

状态：**已准备，尚未运行模型**。固定题目为 xarray-2905、3993、4687；三题的环境与 gold 预检已完成，见[验收报告](../../reports/environment-isolation/README.md)。本配置用于重新求解已发现污染访问的题目，不是新的留出集。

## 固定配置

机器可读配置见 [plan.json](plan.json)，题单见 [xarray3-isolation.txt](../../subsets/xarray3-isolation.txt)。准备时的实现提交为 `b116843`，源码、依赖锁、题单、预检报告和历史摘要按 SHA-256 固定；各题 image ID 也与已验收环境绑定。

| 配置 | 值 |
| --- | --- |
| 数据集 | SWE-bench Verified，本地缓存；启动前检查三条完整数据记录的哈希 |
| 模式 | Agent |
| DeepSeek | `deepseek-flash`，基线 / 提醒 / 提醒加 finish 各三遍 |
| GLM | `glm-5.3-flash`，基线一遍 |
| 每遍题数 | 3，按固定题单顺序加载 |
| 模型调用上限 | 每题 40 轮 |
| 单条命令超时 | 180 秒 |
| 求解并发 | 2 |
| 容器资源 | 2 CPU、3GB 内存、linux/amd64、无网络 |
| 环境策略 | `swebench-isolation-v1`，每题求解前重新执行隔离检查 |
| 官方判卷 | 每遍结束后执行，`-j 1`；工作目录 `~/code/swebench-work` |
| API 请求 | 超时 120 秒，SDK 最多重试 3 次 |

合计 **10 遍、30 次求解**，循环上限合计 1,200 轮。SDK 重试属于请求级重试，不计为额外 Agent 轮次；轮数上限不等于费用或整个任务的总时限。

为沿用历史请求形式，本次不新增 temperature、max_tokens、thinking 或 reasoning_effort 参数。它们由提供商默认值决定，模型别名也未锁定服务端权重版本。这个限制已写进配置；本次是环境修复后的复验，不能单独用于证明清理缓存对成功率的因果影响。推理强度消融另建计划。

## 运行前检查

在项目根目录执行：

```bash
uv run python evals/profiles/xarray3-isolation/run.py
```

默认只检查并列出运行编号，**不会初始化模型客户端或调用模型**。它检查源码与依赖版本、题单、离线数据记录、隔离策略、已验收镜像 ID 和 harness 路径。检查不通过时先处理差异，不通过修改旧计划绕过检查。

脚本使用本地 Hugging Face 缓存，不下载数据或镜像。实际求解由现有 `agent eval` 执行，届时按现有入口读取 `.env` 中所选提供商的凭据。Docker 需保持运行。

## 逐遍执行

先提交本配置，再手动运行第一遍：

```bash
caffeinate -i uv run python evals/profiles/xarray3-isolation/run.py \
  --run x3-clean-ds-base-r1
```

该命令只启动一次三题评测，结束后自动进行官方判卷。后续每次只替换 `--run` 后的编号，按下表顺序逐遍运行，不同时启动多遍。DeepSeek 各轮交替顺序，减少组别与运行先后的固定关联；它不等同于随机化试验。

| 顺序 | 运行编号 | 组别 | 对应历史运行 |
| --- | --- | --- | --- |
| 1 | `x3-clean-ds-base-r1` | DeepSeek 基线 | `v30-deepseek-r1` |
| 2 | `x3-clean-ds-budget-r1` | DeepSeek + 提醒 | `v30-budget-r1-retry` |
| 3 | `x3-clean-ds-finish-r1` | DeepSeek + 提醒 + finish | `v30-finish-r1` |
| 4 | `x3-clean-ds-budget-r2` | DeepSeek + 提醒 | `v30-budget-r2` |
| 5 | `x3-clean-ds-finish-r2` | DeepSeek + 提醒 + finish | `v30-finish-r2` |
| 6 | `x3-clean-ds-base-r2` | DeepSeek 基线 | `v30-deepseek-r2` |
| 7 | `x3-clean-ds-finish-r3` | DeepSeek + 提醒 + finish | `v30-finish-r3` |
| 8 | `x3-clean-ds-base-r3` | DeepSeek 基线 | `v30-deepseek-r3` |
| 9 | `x3-clean-ds-budget-r3` | DeepSeek + 提醒 | `v30-budget-r3` |
| 10 | `x3-clean-glm-base-r1` | GLM 基线 | `v30-glm` |

未开启的机制使用原 CLI 默认关闭值；每条命令均显式指定 provider、model、轮数、超时、并发、内存和判卷。十条命令已通过 CLI 参数解析检查。

## 中断与失败规则

- 每个编号只开始一次。启动脚本拒绝已存在的运行目录，避免误启动或无意重复判卷。
- 中断后先核对 `predictions.jsonl`、`results.jsonl` 与 `run_config.json`，再决定如何恢复；不删除旧结果来重跑。
- 空补丁、模型请求失败和未解决结果都保留。任务级自动重试次数为 0，不取最好的一次替换失败结果。
- 预检失败不是模型答错；先修复环境，记录处理方式，不能静默缩小分母。
- 第一遍完成后先核对轨迹是否仍有新的信息来源，再继续其余运行。

## 结果保存与核对

每遍结果保存在 `evals/results/<运行编号>/`：

| 文件 | 核对内容 |
| --- | --- |
| `run_config.json` | provider、model、机制开关、资源和环境策略是否符合计划 |
| `environment/<题号>.json` | 三题均通过预检，image ID 和 base commit 符合计划 |
| `traj/` | 求解过程与访问路径；本地保留，不自动纳入 Git |
| `predictions.jsonl` | 三道题各一条预测，保留空补丁 |
| `summary.json` | 各题停止原因、API 错误、用量、判卷结果、harness_run_id |

同时按 `harness_run_id` 核对官方 `results.json` 和逐题 `report.json`，检查题目是否全部完成、有无基础设施异常。CLI 正常结束不代替这些核对。

主报告单独列出三题的新结果，三遍的组分母为 9 次求解，GLM 为 3 次；仍然只有 3 道不同题。若另做旧 30 题报告的局部补跑汇总，采用：

```text
每遍局部补跑成功数 = 旧遍成功数 - 旧遍这三题的成功数 + 新遍三题成功数
每遍局部补跑解决率 = 局部补跑成功数 / 30
```

该汇总必须标注“局部补跑”：其他 27 题没有在新环境重跑。历史结果、敏感性分析、新三题结果与局部补跑汇总分别保存，不覆盖旧分数；开始前不承诺新解决率。
