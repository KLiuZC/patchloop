# M2：真实模型修复、人工审查与本地交付验收

日期：2026-10-08（Asia/Shanghai）。任务编号：`b94d4679-27ab-4dc4-aeb9-28fa39d427fd`。

## 范围与版本

用户从网页新建玩具修复任务，启动指定任务的 worker，核对结果后接受补丁并生成本地修改分支。运行前实现已提交为 `1c885061356e30d2bb6f42188165293cde8d9edd`，工作区干净；玩具任务与实现位于同一仓库，本次待修复基线也是该提交。

这是 M2 的单任务工程验收，证明真实模型修复到本地交付的成功路径。没有 SWE-bench 官方判卷，不纳入开发集或留出集成绩。提交任务、运行、人工接受和交付均由用户完成；收尾仅查询、核对及归档，没有重新调用模型、运行 pytest、修改审查决定或重放交付请求。

## 配置与结果

| 项目 | 本次记录 |
| --- | --- |
| 工作目录 | `examples/toy_repo` |
| 模型 | `deepseek` / `deepseek-flash` |
| 验证命令 | `python -m pytest -q` |
| 镜像 / 平台 | `patchloop-toy` / `linux/arm64` |
| 镜像 ID | `sha256:a332fc2b8bd3fdb081f36325f89e7a4617c3192bea9cc46eddad7cf3729bec85` |
| 预算与内存 | 最多 10 轮 / 2048 MB |
| 工具 / 验证超时 | 60 秒 / 180 秒 |
| 提醒 / finish | 提醒开启，实际触发 0 次；finish 关闭 |
| 执行 / 验证 | `completed` / `passed` |
| 修复前 | 1 failed、1 passed，退出码 1 |
| 修复后 | 2 passed，退出码 0 |
| 轮数 / 停止原因 | 3 轮 / `no_tool_calls` |
| Prompt / 缓存 / Completion | 2431 / 1920 / 410 tokens，缓存为 Prompt 的子集 |
| 内置费用估算 | ¥0.0021894，非平台账单 |
| 当前人工审查 | `accepted`，第 1 次审查，意见为空 |
| 本地交付 | `completed`，`delivery_intact=true`，只有 1 条交付记录 |
| 清理 | `cleanup_pending=false`，无任务容器遗留 |

路径策略只允许 `examples/toy_repo/calc.py`，保护常见测试路径；完整规则保存在 [task.json](task.json) 与 [task-record.json](task-record.json) 中。只读重算 Git 候选文件树时，路径策略通过，唯一修改文件为 `calc.py`。

## 补丁与交付

补丁仅增加：

```python
if s.startswith("v"):
    s = s[1:]
```

代码检查确认只剥离一次小写 `v`，没有新增大写 `V` 分支；其余解析逻辑保留。指定测试仍只有两个，不能把代码检查推断扩大成已执行完整输入边界测试。原始仓库与交付分支的测试文件相同，没有手工改写候选补丁。

| 项目 | 值 |
| --- | --- |
| 审查 ID | `ec69990f-9ed1-4e06-8647-1262c9717acd` |
| 交付 ID | `5558a8cc-6c29-4869-b7b7-ad606a978250` |
| 分支 | `patchloop/b94d4679-27ab-4dc4-aeb9-28fa39d427fd/r1` |
| 提交 | `05f02c07b9c608a2f825e35a6f18fbb473c81c15` |
| 唯一父提交 | `1c885061356e30d2bb6f42188165293cde8d9edd` |
| Git tree | `9a210cca7e40ebe9bdd39bbf76d152ace5e311be` |
| 补丁 SHA-256 | `4710326746ac68ce7ed02987e4c06256d9ac865192b2ad2abe6091f1c2a42516` |

独立工作区：`data/deliveries/b94d4679-27ab-4dc4-aeb9-28fa39d427fd/r1`。交付提交相对基线的 Git diff 与批准补丁逐字节一致，tree 与只读应用补丁计算的 tree 一致。工作区处于上述交付分支且干净，与原工作区共用同一 Git 仓库。

归档前原工作区仍位于 `feat/patch-review` 的 `1c88506`，代码与测试保持基线内容。独立审查和交付没有改写原始 `summary.json`：其中 `review_status=pending` 是修复结束时的快照；当前人工审查的 `accepted` 由独立记录提供。

## 时间与页面检查

任务创建于 10:32:57.578685，worker 领取于 10:33:47.306036，结束于 10:33:56.071727。等待 49.727351 秒包含启动 worker 的时间，不计为修复耗时。RepairRunner 记录 8.6 秒，worker 领取到终态回写 8.765691 秒，Agent loop 记录 5.2 秒。

人工接受于 10:35:19.609241；交付记录创建于 10:35:24.788457，完成于 10:35:25.673784，两者相差 0.885327 秒。该交付时间窗口不含创建记录之前的计划与路径检查，不作为完整 HTTP 请求耗时。

网页显示已接受、本地交付已完成，生成按钮禁用；`can_deliver=false` 的原因是 `delivery already completed`，表示不再重复交付。测试输出与 API 原文一致，刷新后结果仍可读取，390px 窄屏无页面级横向溢出。只读检查前后数据库总数均为 4 条任务、2 条审查、1 条交付，当前任务对应 1 条审查和 1 条交付。

本次展示成功路径。受保护路径拦截、并发和中断恢复等异常分支，由实现阶段 484 项后端及 549 项前端测试覆盖；本次收尾未重新运行这些测试。

## 证据

- [task-record.json](task-record.json)、[review-state.json](review-state.json)、[reviews.json](reviews.json)、[delivery-state.json](delivery-state.json)：归档前四个 GET 接口的格式化响应，已与只读 SQLite 查询交叉核对。
- [task.json](task.json)、[summary.json](summary.json)、[validation.json](validation.json)、[patch.diff](patch.diff)、[runtime.json](runtime.json)、[events.jsonl](events.jsonl)：六份原始产物，按原始字节复制。
- [checked-summary.json](checked-summary.json)：证据哈希、Git 核对、时间、用量、容器清理及浏览器检查结果。

完整模型轨迹保留在本机 `data/task-runs/b94d4679-27ab-4dc4-aeb9-28fa39d427fd/trajectory/`，仅归档哈希与逐轮用量，不复制模型推理全文、整个数据库或工作区。交付分支和独立工作区保留供查看。本次归档修改只涉及文档与证据，未合并或推送交付分支。

M2 的真实模型审查与本地交付验收完成。提交归档后，进行 `feat/patch-review` 合并前检查与分支收尾。
