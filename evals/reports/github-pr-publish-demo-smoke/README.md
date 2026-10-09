# M4：GitHub Issue 到草稿 PR 的真实验收

验收日期：2026-10-09。任务编号：`3659f4fc-1eea-48cf-a744-b2bbebee4654`。

用户从网页导入自有测试仓库的 [Issue #1](https://github.com/KLiuZC/patchloop-publish-demo/issues/1)，运行 DeepSeek 修复，查看独立验证后接受补丁并完成本地交付。首次推送因实现中的认证配置失败；修复程序并重启 API 后，用户恢复同一发布请求，成功创建[草稿 PR #2](https://github.com/KLiuZC/patchloop-publish-demo/pull/2)。本次归档仅读取、核对和复制既有证据，没有重新运行模型、验证命令、审查、交付或发布。

## 验收结果

| 项目 | 记录 |
| --- | --- |
| 测试仓库 | `KLiuZC/patchloop-publish-demo`，公开、同仓库分支交付 |
| 问题 | `parse_version` 无法解析带单个小写 `v` 的版本号 |
| 固定基线 / PR 目标 | `05c8b8fd1a952b65a336976d0b3a42498899a718` / `main` |
| 模型 | DeepSeek / `deepseek-flash` |
| 镜像 / 架构 | `patchloop-toy` / `linux/arm64` |
| 预算 / 内存 | 最多 10 轮 / 2048 MB |
| 命令 / 验证超时 | 60 秒 / 180 秒 |
| 轮数提醒 / finish | 开启 / 关闭；实际未触发轮数提醒 |
| 修改范围 | 只允许 `calc.py`，保留默认测试保护 |
| 验证命令 | `python -m pytest -q` |
| 修复前 | `1 failed, 10 passed`，退出码 1 |
| 修复后 | `11 passed`，退出码 0 |
| 执行 / 验证 | `completed` / `passed` |
| 模型轮数 / 修复耗时 | 3 轮 / 8.4 秒 |
| Prompt / 缓存 / Completion | 5205 / 3712 / 467 tokens；缓存包含在 Prompt 中 |
| 内置估算费用 | ¥0.00343524，非平台账单 |
| 人工审查 / 本地交付 | 第 1 次审查 `accepted` / `completed`，交付完整 |
| GitHub 发布 | `completed`，PR #2，草稿、开放中、未合并 |
| 修改量 | 1 个提交，`calc.py` 增加 2 行，未改测试或配置 |

精确镜像 ID 在 [runtime.json](runtime.json)。运行参数与[验收 profile](../../profiles/github-publish-demo/README.md)、预检文件哈希及镜像 ID 相符；profile 中人工构造的正确/错误候选只是测试有效性预检，不是额外的模型答题。

## 补丁、批准和远端提交

模型在版本号解析前增加 `startswith("v")` 和单字符切片，保留已有数字段转换逻辑。可见测试覆盖普通版本号、小写单前缀，以及大写 `V`、重复 `v`、仅有 `v`、空字符串、空版本段和非数字版本段应继续报错。11 项检查不代表覆盖所有可能输入。

| 标识 | 值 |
| --- | --- |
| 审查编号 | `8bf76646-9598-4780-95d8-6e238e0fcb9b` |
| 本地交付编号 | `26aa657c-b075-49e8-be65-52eece04ca71` |
| 发布编号 | `bc40eddb-49f2-43de-b8e0-e425bd4bc23c` |
| 原发布请求编号 | `f2dd120a-0e63-41df-a7ae-4b09353a674c` |
| 修改分支 | `patchloop/3659f4fc-1eea-48cf-a744-b2bbebee4654/r1` |
| 本地及远端提交 | `2b03ef0ea97d05da438a6853a7403c60b5f8aeea` |
| 唯一父提交 | `05c8b8fd1a952b65a336976d0b3a42498899a718` |
| Git tree | `ff0f08b16f3217981d8ec30542900c04fd61927d` |
| Patch SHA-256 | `7e8d26ad8a3e25282720b78ea2b64b30e24f7a8ba7e6d9f21ffa29285d284d3f` |
| 发布方案 SHA-256 | `1e8eebd63b7f1fd89ea237920bf768ece8abc218b77fb4e4bf3abb9506d47cfa` |

只读核对确认：原始补丁、运行摘要、验证报告与任务的 SHA-256 匹配批准；交付和发布绑定同一批准。GitHub PR 的目标分支、head SHA、唯一父提交、tree、`calc.py` blob、标题、正文及草稿选项均与已确认方案一致。远端 `main` 仍停留在验证基线。

该任务只有 1 条审查、1 条本地交付和 1 条发布记录；按同一 head/base 查询 GitHub，只有 1 个匹配 PR，没有重复发布结果。这证明本次恢复关联了同一记录；数据库没有逐次 HTTP 请求审计，不能由一条记录推断精确的请求次数。

原下载仓库与独立交付工作区均干净，原 `calc.py` 保持基线内容。独立工作区位于 `data/deliveries/3659f4fc-1eea-48cf-a744-b2bbebee4654/r1`，继续保留。原始摘要里的 `review_status=pending` 是修复结束时快照，当前接受决定来自独立审查记录。运行时记下的 3 个容器均已不存在，`cleanup_pending=false`。

## 认证失败与恢复

这次暴露的是程序缺陷：`credential.interactive=false` 在本机 Git 2.54.0 下同时禁用了 `GIT_ASKPASS`，导致 Git 推送拿不到已配置的 Token。首次记录处于 `uncertain/push`，尚未发出 PR 创建请求；当时只读查询远端，仅有 `main`。

修复只允许带程序生成 askpass 的推送命令调用认证助手，仍禁用终端输入及用户 credential helper。原凭证的 `git push --dry-run` 通过后，用户重启服务并点击原方案恢复，最终得到 PR #2。没有更换修复补丁、重复运行模型或新建批准。

初次失败记录已被同一发布记录的恢复状态覆盖。本归档保存的是最终状态，故障经过来自当时的检查记录和代码修复，不补造原始失败 JSON、Git 日志或逐次发布耗时。详细证据边界见 [recovery-notes.md](recovery-notes.md)。

## 版本与时间口径

- `14f02c6` 是模型修复与首次发布时的应用参考提交。样例和验收 profile 当时已存在，但尚未提交；其内容通过预检哈希、冻结任务和测试仓库基线核对。
- 认证修复用于成功恢复后，才连同验收配置提交为 `caf82d6`。它是事后保存的修复版本，不是恢复前已冻结的完整实现快照。
- 本次归档参考 `486b437`；其中 Markdown 展示调整发生在 PR 创建之后。worker/API 没有逐进程源码指纹，不将该版本追认为运行时版本。
- worker 领取到终态回写为 **8.560210 秒**；排队的 **29.007508 秒**单独计算，不计入修复耗时。本地交付记录创建至完成为 **0.769899 秒**。
- 发布记录创建到完成为 **650.871250 秒**，包含故障诊断、代码修复、用户重启和恢复等待，不能作为发布操作的执行耗时。GitHub 记录 PR 创建于 `2026-10-09T14:14:08Z`。

代码实现阶段的回归结果继续保留在路线文档：认证修复专项 41 项、Markdown 展示之后前端 960 项。此次只做归档核对，没有重跑这些测试，也没有完成下一步的分支合并前检查。

## 证据文件

- [task-record.json](task-record.json)、[source-record.json](source-record.json)、[source-snapshot.json](source-snapshot.json)、[preparation-state.json](preparation-state.json)：准确任务配置、Issue 输入快照和仓库准备记录。
- [task.json](task.json)、[summary.json](summary.json)、[validation.json](validation.json)、[patch.diff](patch.diff)、[runtime.json](runtime.json)、[events.jsonl](events.jsonl)、[trajectory.jsonl](trajectory.jsonl)：从原任务目录逐字节复制的修复产物；本次轨迹较小，一并归档。
- [review-state.json](review-state.json)、[reviews.json](reviews.json)、[delivery-state.json](delivery-state.json)、[publication-state.json](publication-state.json)：独立批准、交付、原确认请求及最终发布状态。
- [github-pr.json](github-pr.json)、[github-pr-files.json](github-pr-files.json)、[github-commit.json](github-commit.json)、[github-main-ref.json](github-main-ref.json)、[github-head-ref.json](github-head-ref.json)、[github-pr-list.json](github-pr-list.json)：归档时的 GitHub GET 响应；远端未来变化不会改变这些快照。
- [github-issue.json](github-issue.json)、[github-repository.json](github-repository.json)：远端 Issue 与仓库身份核对。
- [checked-summary.json](checked-summary.json)：23 份证据文件的 SHA-256、核对结果、代码参考版本、用量及时间口径。

归档不含发布 Token，不复制 `.env` 或凭证配置。此次证明专用小任务的实际交付流程和修复后的恢复路径，不作为新的 SWE-bench 成绩，不代表 fork、组织权限、任意复杂仓库或所有断网场景均已真实验收。PR 保持草稿且未合并；没有把本地测试通过表述为 GitHub CI 通过。
