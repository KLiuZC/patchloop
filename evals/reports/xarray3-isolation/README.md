# xarray 三题隔离复验

[实验索引](../../../EXPERIMENTS.md) · [评测说明](../../../docs/evaluation.md) · [历史污染与环境策略](../environment-isolation/README.md) · [运行配置](../../profiles/xarray3-isolation/README.md)

运行日期：2026-10-13～14（Asia/Shanghai）；归档日期：2026-10-17。

## 结果与决定

xarray-2905、3993、4687 在隔离环境中完成 10 遍、30 次求解。DeepSeek 三组各重复三遍，GLM 基线一遍；每次只运行同三道题。所有环境预检通过，29 份非空补丁进入官方测试，1 份空补丁保留在统计分母中。没有模型调用错误、官方执行错误、基础设施失败或模糊失败。

| 配置 | 各遍解决数 | 总解决数 | 解决率 | 总模型轮数 | 平均轮数 | 上限停止 | 空补丁 | 总估算费用 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| DeepSeek 基线 | 1、2、1 / 3 | 4/9 | 44.4% | 295 | 32.8 | 4 | 1 | ¥0.696 |
| DeepSeek + 轮数提醒 | 2、1、3 / 3 | 6/9 | 66.7% | 274 | 30.4 | 3 | 0 | ¥0.655 |
| DeepSeek + 提醒 + finish | 2、3、1 / 3 | 6/9 | 66.7% | 278 | 30.9 | 3 | 0 | ¥0.849 |
| GLM 基线 | 1/3 | 1/3 | 33.3% | 91 | 30.3 | 0 | 0 | ¥0.802 |

后续 DeepSeek 对照继续采用开启提醒、关闭 finish，代码默认值保持不变。此次提醒组比基线多通过两次，总轮数少 21；finish 组与提醒组通过次数相同，费用约高 29.6%，未表现出额外的总体收益。

这些是三道已用于开发分析的题目上的重复观察，不是九道独立题，也不是 SWE-bench Verified 全集或新的留出集。GLM 只有一遍，模型间的稳定性不能由这次结果确定。扩大评测前需单独固定新题单和环境，不能用本次最好的单遍替换其他结果。

## 固定条件与版本

- 实现基线：`b11684340a12697e7ffc7dc89bcd1567ee6f7c39`。每次 `run_config.json` 的源码、依赖、题单和数据记录哈希均与原计划一致；30 份环境报告的 image ID 和 base commit 均匹配计划。
- DeepSeek 使用 `deepseek-flash`，GLM 使用 `glm-5.3-flash`；均为 Agent 模式，每题最多 40 次模型调用，命令超时 180 秒，求解并发 2，官方判卷并发 1。
- 每个容器 2 CPU、3GB 内存、linux/amd64，无网络；采用 `swebench-isolation-v1`。官方判卷使用原题目镜像，标准答案和隐藏测试不进入求解循环。
- Python 3.12.13、openai 3.19.2、datasets 5.0.1、typer 0.27.2。temperature、max_tokens、thinking、reasoning_effort 沿用提供商默认值，未锁定模型别名背后的权重版本。
- 十遍按预定顺序交替运行，没有任务级重试、恢复运行或挑选最好答卷；SDK 请求级重试设置为 3，不能据此推断实际发生了几次请求重试。

配置文件在运行前准备，配置与结果的首个 Git 提交为运行结束后的 `0ddf27671bf3f989d234ded0e10f0c9a168a97d9`。本报告依据各次保存的配置哈希核对一致性，不将事后提交说成运行前已提交。原 [plan.json](../../profiles/xarray3-isolation/plan.json) 保留 `status=planned`，完成状态另存于本报告的 [summary.json](summary.json)。

历史运行与本次运行的镜像来源、运行时间和模型服务版本可能不同；此次复验说明隔离后的实际表现，不能将成绩差异全部归因于缓存隔离。

## 十遍记录

每行的汇总链接指向原始运行目录，官方链接指向本次归档的原件。

| 顺序 | 运行 | 官方结果 | 解决数 | 模型轮数之和 | 墙钟耗时 | 估算费用 |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| 1 | [ds-base-r1](../../results/x3-clean-ds-base-r1/summary.json) | [官方](official/x3-clean-ds-base-r1/results.json) | 1/3 | 95 | 7分13秒 | ¥0.211 |
| 2 | [ds-budget-r1](../../results/x3-clean-ds-budget-r1/summary.json) | [官方](official/x3-clean-ds-budget-r1/results.json) | 2/3 | 82 | 6分59秒 | ¥0.183 |
| 3 | [ds-finish-r1](../../results/x3-clean-ds-finish-r1/summary.json) | [官方](official/x3-clean-ds-finish-r1/results.json) | 2/3 | 89 | 7分09秒 | ¥0.243 |
| 4 | [ds-budget-r2](../../results/x3-clean-ds-budget-r2/summary.json) | [官方](official/x3-clean-ds-budget-r2/results.json) | 1/3 | 82 | 7分55秒 | ¥0.187 |
| 5 | [ds-finish-r2](../../results/x3-clean-ds-finish-r2/summary.json) | [官方](official/x3-clean-ds-finish-r2/results.json) | 3/3 | 93 | 10分16秒 | ¥0.317 |
| 6 | [ds-base-r2](../../results/x3-clean-ds-base-r2/summary.json) | [官方](official/x3-clean-ds-base-r2/results.json) | 2/3 | 111 | 9分56秒 | ¥0.285 |
| 7 | [ds-finish-r3](../../results/x3-clean-ds-finish-r3/summary.json) | [官方](official/x3-clean-ds-finish-r3/results.json) | 1/3 | 96 | 9分10秒 | ¥0.289 |
| 8 | [ds-base-r3](../../results/x3-clean-ds-base-r3/summary.json) | [官方](official/x3-clean-ds-base-r3/results.json) | 1/3 | 89 | 6分34秒 | ¥0.199 |
| 9 | [ds-budget-r3](../../results/x3-clean-ds-budget-r3/summary.json) | [官方](official/x3-clean-ds-budget-r3/results.json) | 3/3 | 110 | 8分40秒 | ¥0.285 |
| 10 | [glm-base-r1](../../results/x3-clean-glm-base-r1/summary.json) | [官方](official/x3-clean-glm-base-r1/results.json) | 1/3 | 91 | 26分24秒 | ¥0.802 |

费用合计约 ¥3.002，按逐题 `cost` 的未取整值相加；显示到三位小数的各行相加可能有舍入差异。费用使用项目记录时的价格表，不是平台账单。墙钟耗时取 `invocation.wall_seconds`，包括本次求解和判卷，不包含各遍之间的人工等待；不能用并行任务的耗时之和替代墙钟耗时。

## 逐题表现与失败原因

| 题目 | DeepSeek 基线 | 仅提醒 | 提醒 + finish | GLM 基线 |
| --- | ---: | ---: | ---: | ---: |
| xarray-2905 | 3/3 | 3/3 | 3/3 | 0/1 |
| xarray-3993 | 1/3 | 2/3 | 1/3 | 1/1 |
| xarray-4687 | 0/3 | 1/3 | 2/3 | 0/1 |

13 次未解决由以下四种情况构成。具体官方断言、日志位置和补丁摘要见 [failure-evidence.json](failure-evidence.json)。

| 情况 | 次数 | 官方依据 |
| --- | ---: | --- |
| 3993 警告类型错误 | 5 | 发出 `DeprecationWarning`，F2P 测试要求 `FutureWarning` |
| 4687 属性来源错误 | 6 | 结果保留了 `cond` 的属性，F2P 测试要求 `x` 的属性 |
| 2905 未保留自定义对象 | 1 | GLM 将对象中数组类型的 `.values` 解包，导致原本零维的对象变成一维数据 |
| 4687 空补丁 | 1 | 基线 r3 用满 40 轮，没有修改源码；官方列为 `empty_patch` |

全部 29 份已测试补丁的 P2P 均通过：2905 每份 364 项、3993 每份 2398 项、4687 每份 1717 项。失败来自目标 F2P 或空补丁。原测试输出可能包含不在判分集合中的既有失败，不能直接把整段日志中的失败数当成官方结论。

GLM 在 2905 的第 22 轮构造了 `.values` 为数组的自定义对象，却把解包视为正确行为；4687 的第 25 轮也明确把 `cond` 属性优先视为预期。自测的预期可能重复模型的理解错误，增加自测数量不能代替独立验证。

## 结束行为

- DeepSeek 的 4687 九次全部达到 40 轮上限，其中三份补丁通过。提醒和 finish 都没有使这道题主动收尾。
- finish 组共六次显式提交，四次通过、两次失败；另三次达到上限，两次通过、一次失败。结束声明与补丁正确性是两项独立结果。
- 基线 r3 的 4687 在 40 轮内仅阅读、搜索和执行复现片段，没有生成补丁。基线 r2 的 2905 则在第 20 轮已完成修改、第 22 轮相关测试通过后继续扩大检查，最终用完预算。
- GLM 三题均自然结束；2905 在第 40 轮发出最终文字回复，属于 `no_tool_calls`，不计为 `max_turns`。

原始批次 `summary.json` 的 `judged=3` 表示三份预测均被纳入该轮判分；对基线 r3，官方只有两份补丁实际执行了测试，另有一份空补丁。本报告明确区分提交数、测试数和空补丁数，空补丁保持在解决率分母中。

## 隔离效果与访问证据

30 份环境报告均确认：指定 image ID 和基线匹配；求解初始 Git 只含一个 commit、没有远端及 reflog；xarray 从 `/testbed` 导入；声明的扫描范围没有发现额外同名源码或发行包，扫描无错误。

核对保存的命令与工具返回后，没有观察到成功读取额外版本 xarray 源码。仍可看到全盘搜索、下载尝试和 Git 历史查询；安装元数据与文件名没有全部隐藏。例如 finish r2 的 4687：

| 轮次 | 行为 | 工具返回 |
| --- | --- | --- |
| 24–25 | 找到并读取 conda 安装记录 | 返回新版包路径及文件名列表，未返回 Python 源码 |
| 26 | 读取新版 `where` 与 `test_where` 所在文件 | 两条读取命令均返回文件不存在 |
| 27–28 | 再次查看包缓存目录 | 目标目录不存在；安装位置只有当前可编辑包记录 |

Git 历史查询只获得指定基线；网络下载未获得新包。其他搜索结果包括依赖包的集成文件名以及模型自己创建的临时脚本，不把这些目录列表记为新版源码内容暴露。

[trajectory-review.json](trajectory-review.json) 为 30 次尝试逐一记录轨迹哈希及访问检查结论，并摘录相关命令、返回、轮次与行号。索引使用显式文本模式筛选，结论结合完整命令链复核；它不是 shell 语义分析器。检查只能覆盖已保存的工具返回，不能证明所有改名源码、压缩包、训练数据或其他信息渠道都不存在。安装记录仍可见这一边界也保留在报告中。

## 证据归档与复核

| 文件 | 内容 |
| --- | --- |
| [summary.json](summary.json) | 十遍及逐题的配置引用、官方判分、停止原因、F2P/P2P 数量、费用和补丁哈希 |
| [manifest.json](manifest.json) | 原始结果、配置、30 份本地轨迹、归档文件和压缩包内成员的 SHA-256 |
| [failure-evidence.json](failure-evidence.json) | 13 次未解决的分类与官方日志摘录 |
| [trajectory-review.json](trajectory-review.json) | 隔离访问检查及原始轨迹定位 |
| `official/<run-id>/results.json` | 官方批次汇总原件，共 10 份 |
| `official/<run-id>/<instance-id>/report.json` | 官方逐题报告原件，共 29 份；空补丁没有伪造报告 |
| `official/<run-id>/harness.tar.gz` | 对应官方目录的完整文件：原始日志、测试输出、补丁、执行脚本和报告 |

压缩包与公开 JSON 均从原官方目录按字节归档，没有重判或重新求解。原始轨迹继续保存在 `evals/results/<run-id>/traj/`，沿用不入 Git 的规则；公开报告提供摘录与哈希。归档总量约 5.8 MB，日志压缩仅改变存储形式。

归档时核对了十遍配置与官方解决题号、29 份报告与准确补丁、30 份轨迹和环境检查。启动配置相关回归测试 `uv run pytest -q tests/test_xarray3_recheck_plan.py` 为 6 passed；文档本地链接和差异格式检查通过。此次收尾只整理资料，没有修改 Agent 实现或发起新模型请求。

在项目根目录运行以下只读检查，不需要模型、Docker 或本地完整轨迹：

```bash
python3 - <<'PY'
import hashlib
import json
from pathlib import Path
import tarfile

root = Path('evals/reports/xarray3-isolation')
manifest = json.loads((root / 'manifest.json').read_text())
for name, expected in manifest['artifact_sha256'].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
for archive in manifest['archives']:
    with tarfile.open(root / archive['path'], 'r:gz') as stream:
        for name, expected in archive['member_sha256'].items():
            assert hashlib.sha256(stream.extractfile(name).read()).hexdigest() == expected, name
print('Archived evidence verified')
PY
```

## 与历史成绩的关系及下一步

历史 30 题原始成绩、将暴露成功暂按失败的敏感性分析、本次三题隔离后成绩分别保存。其余 27 题没有在新环境重跑，不把本次结果拼成“30 题清洁环境成绩”。原运行的摘要、补丁、轨迹及判卷全部保留。

本轮复验结束，不继续重跑这三题挑选更高分。收尾后进行分支合并前检查；能力改进优先评估命令策略与结构化工具，针对仓库外源码搜索、编辑错误和长时间不生成补丁建立单独对照。扩大题单前继续做对应镜像审计与 gold 预检，任务取消等产品工作沿用[路线图](../../../ROADMAP.md)单独安排。
