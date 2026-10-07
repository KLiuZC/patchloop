# 任务 API 与后台 worker：玩具仓库真实验收

日期：2026-10-07（Asia/Shanghai）。任务编号：`72806f28-8d03-4ce6-93fc-dff92e13fce4`。

## 目标与范围

通过接口调试页面提交 `examples/toy_repo` 的版本号解析任务，由独立 worker 在 Docker 中调用真实 DeepSeek 模型修复，随后查询数据库中的结果。验收目标是任务提交、后台领取、独立验证、结果回写与产物保存这一条完整链路。

本次是一个已有玩具任务的工程验收，没有运行 SWE-bench 官方判卷，不纳入 smoke5、verified30 或留出集的成功率统计。

## 配置与版本

| 项目 | 本次记录 |
| --- | --- |
| 提供商 / 模型 | `deepseek` / `deepseek-flash` |
| 任务目录 | `examples/toy_repo`，位于 patchloop Git 仓库内 |
| 待修复基线 | `fc575689a09ca4c44f653e4056db074f46ce0ea8` |
| 验证命令 | `python -m pytest -q` |
| 镜像 | `patchloop-toy`，实际平台 `linux/arm64` |
| 实际镜像 ID | `sha256:a332fc2b8bd3fdb081f36325f89e7a4617c3192bea9cc46eddad7cf3729bec85` |
| 预算 | 最多 10 次模型请求，内存 2048 MB |
| 超时 | 工具命令 60 秒，验证命令 180 秒 |
| 提醒 / finish | 提醒开启，finish 关闭；实际发送提醒 0 次 |

模型在第 4 轮结束，尚未达到本配置的提醒触发轮次，因此本次不能用于验证提醒触发或衡量其收益。`task.json` 中的 `task_timeout_seconds=600` 是保留字段，当时没有执行整个任务的总时限。

运行在 15:40:12 结束，worker 实现随后于 15:45:07 提交为 `4f936f06c91dddf94cd37c4ee8df2dc2d87f9699`（分支 `feat/task-worker`）。该提交是事后保存的实现版本；运行时没有保存源码哈希或源码快照，不能将它描述为运行前已冻结的代码。`base_sha` 标识待修复仓库的基线，不代表 worker 的实现版本。

## 结果

| 项目 | 结果 |
| --- | --- |
| 执行状态 | `completed` |
| 修复前独立验证 | 1 passed、1 failed，exit 1 |
| 应用补丁后独立验证 | 2 passed，exit 0 |
| 最终验证状态 | `passed` |
| 停止原因 | `no_tool_calls`，模型以文字回复结束 |
| 模型轮数 | 4 |
| Prompt / 缓存命中 / Completion tokens | 3589 / 2304 / 388（缓存命中是 Prompt 的子集） |
| 项目内置费用估算 | ¥0.00288308，非平台账单 |
| 人工审查状态 | `pending`，本次收尾没有更新审查状态或应用补丁 |
| 清理状态 | `cleanup_pending=false`，收尾核对时没有该任务的遗留容器 |

补丁只修改 `examples/toy_repo/calc.py`：遇到以 `v` 开头的输入，先去掉一个前缀字符，再沿用原有的分段整数解析。实际 diff 没有修改 `tests/`，原始仓库中的 `calc.py` 仍保持基线内容。禁止修改测试的要求来自任务文本；本次 `protected_paths` 为空，不将模型遵守要求描述为强制路径保护的验证。

补丁 SHA-256：`4710326746ac68ce7ed02987e4c06256d9ac865192b2ad2abe6091f1c2a42516`。

## 时间口径

- 创建任务：15:38:55.709115；worker 领取：15:40:04.310666；数据库终态回写：15:40:12.546146。
- 创建到领取为 68.601551 秒，包含等待启动 worker 的时间，不作为队列调度性能结果。
- 领取到终态回写为 8.235480 秒；RepairRunner 记录 8.1 秒，其中 Agent loop 记录 6.6 秒，验证报告记录 1.0 秒。这些是不同层级的计时，不能相加作为总耗时。

## 证据与核对

- [task-record.json](task-record.json)：只读导出该条 SQLite 记录，按 API 响应结构序列化，包含执行配置及数据库中的最终结果；它不是另行捕获的原始 HTTP 响应。
- [task.json](task.json)：RepairRunner 的固定任务、基线及验证配置。
- [summary.json](summary.json)、[validation.json](validation.json)、[patch.diff](patch.diff)：原始修复摘要、两次独立验证及准确补丁。
- [runtime.json](runtime.json)、[events.jsonl](events.jsonl)：实际镜像、三个容器名称和运行生命周期事件。
- [checked-summary.json](checked-summary.json)：核对结果、上述文件哈希、时间、用量及原始轨迹哈希。

除导出的数据库记录和生成的核对摘要外，上述六份运行产物按原始字节复制。已确认数据库结果等于修复摘要、摘要中的验证等于验证文件、补丁与摘要及轨迹相同，任务与验证使用同一基线提交。逐轮用量相加与最终用量一致，四轮工具数量依次为 1、1、1、0。

完整模型轨迹保留在本机 `data/task-runs/72806f28-8d03-4ce6-93fc-dff92e13fce4/trajectory/`，本归档记录其哈希及逐轮用量，不复制模型推理全文、整个数据库或环境配置。原始运行产物保持不变。

实现阶段已完成 190 项项目测试，覆盖任务领取互斥、中断补丁保存、完整归档结果恢复及清理失败阻止下一任务等情况。本次真实运行走的是成功路径，这些异常恢复结论来自开发阶段的回归测试；本次收尾仅核对和归档，没有重新调用模型或重跑评测。

## 结论与后续

本阶段已完成一次真实模型下的 API → worker → Docker 修复 → 独立验证 → 数据库查询验收。结果作为任务接口与后台执行的功能证据保存，补丁仍为待人工审查的产物。

提交本归档后收尾 `feat/task-worker` 分支。下一阶段在独立分支开发任务工作台，补充任务列表接口及提交、列表、详情页面，继续复用现有修复与验证流程。
