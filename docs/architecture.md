# 架构与运行机制

[返回项目首页](../README.md) · [使用指南](usage.md) · [API 参考](api.md)

## 模块职责

| 模块 | 职责 |
| --- | --- |
| 网页工作台 | 任务配置、状态查询、补丁与日志展示、人工审查和发布确认 |
| FastAPI | 保存任务与来源、读取产物、执行审查及交付接口 |
| SQLite | 持久化任务状态、来源快照、仓库准备、审查、交付和发布记录 |
| worker | 串行领取任务、检查运行条件、调用 RepairRunner、回写结果与清理容器 |
| Agent Loop | 请求模型、执行工具、回填结果、记录轨迹及停止原因 |
| RepairRunner | 准备固定基线的独立副本、收集补丁、调用验证器、保存产物 |
| 验证器 | 在不同的干净环境中分别验证基线和准确候选补丁 |
| GitHub 交付 | 核对批准与远端状态，推送准确提交，创建并记录 PR |

API 与 worker 是独立进程，共享同一数据库和本地数据目录。API 受理请求时不启动模型；worker 按创建时间领取包含执行配置的任务。每个数据库通过文件锁限制为一个 worker，当前不提供跨主机调度。

## 修复与验证

提交任务时固定代码 commit、目标、验证命令、路径策略及执行配置。worker 在调用模型前检查仓库准备状态与本地镜像，记录实际镜像 ID 和架构；修复及独立验证共用该镜像 ID。

本地仓库修复使用独立 Git 克隆，容器只挂载该次任务的副本。该副本有自己的 `.git`，避免 Git linked worktree 中指向宿主机的绝对路径在容器内失效。原仓库的工作区和索引不参与模型编辑。

Agent 根据模型返回的工具调用执行 `bash`，将输出和错误回填到下一轮请求。循环受模型轮数和单条命令时长限制，可开启剩余轮数提醒或显式 `finish` 工具。当前工具输出采用截断策略，消息历史尚无上下文压缩。

运行结束后，验证器分别准备干净基线和候选环境，应用收集到的准确补丁并运行固定命令。验证结论与模型的完成声明分开保存。路径策略在补丁验证和交付时检查，不是工具执行过程中的文件系统拦截。

## 状态分离

| 状态 | 含义 |
| --- | --- |
| 任务执行 | `queued`、`running`、`completed`、`interrupted`、`error` |
| 独立验证 | `not_run`、`passed`、`failed`、`inconclusive` |
| 人工审查 | `pending`、`accepted`、`rejected`、`stale` |
| 本地交付 | `preparing`、`completed`、`failed` |
| GitHub 发布 | `pending`、`blocked`、`uncertain`、`completed` |

例如，任务可以执行完成但验证失败；达到轮数上限的任务会标记为 `interrupted`，已有补丁仍可独立验证。验证通过仅表示配置的检查从基线失败变为候选通过，不能替代人工审查或官方评测结论。

修复产物中的 `review_status` 是运行结束时的快照。最新审查状态来自独立记录，绑定任务、基线、补丁和验证证据的摘要；证据变化后，原决定不再适用于当前产物。

本地交付根据有效批准生成独立分支与 Git linked worktree。GitHub 发布再核对远端基线和权限，并使用用户确认过的方案。执行状态、验证状态、批准和发布结果分别查询，避免将其中一个状态解释为整个流程已经完成。

## 数据目录与恢复

默认数据目录为 `data/`，不纳入 Git：

```text
data/
  tasks.sqlite3                 任务、来源、审查与交付记录
  repositories/<id>/repo/      固定版本的公开仓库副本
  task-runs/<task_id>/         补丁、配置、验证报告和轨迹
  deliveries/<task_id>/r<n>/   批准版本对应的交付工作区
```

API 和 worker 可以通过 `--db` 指定其他数据库。仓库、任务产物和交付默认放在数据库同级目录；worker 的 `--artifacts` 可单独指定产物根目录。记录包含绝对路径，迁移时需要同时考虑数据库、文件目录和 Git worktree 关系。

当前数据库 schema 为 v10。API 或 worker 启动时执行迁移，保留既有任务和证据；更新程序后需重启相关服务。旧版本程序不支持直接使用新 schema，应在升级前保留可恢复的数据备份。

worker 启动时检查中断任务：已保存且一致的完整终态可恢复到数据库，否则标记为 `interrupted`。容器按任务标签清理，失败时保留 `cleanup_pending`，停止领取下一题；后续启动先重试清理。恢复不重新调用模型，也不从中断轮次继续求解。强制终止前未保存的内容无法保证恢复。

GitHub 发布单独保存意图和执行检查点。写请求结果未知时，恢复先查询远端：匹配的分支或 PR 可补记；PR 创建结果仍未知时保持 `uncertain`。查询为空不等于创建失败，不能直接重复创建。详细约定见 [GitHub 发布与恢复](api.md#github-分支与-pr-发布)。

## 运行产物

| 产物 | 用途 |
| --- | --- |
| `task.json` | 固定基线、目标、验证与限制配置 |
| `runtime.json` | worker 记录的镜像 ID、架构及任务容器 |
| `events.jsonl` | 执行与验证阶段事件 |
| `trajectory/` | 模型回复、工具调用及用量记录 |
| `patch.diff` | 收集到的候选补丁 |
| `validation.json` | 基线与候选的独立验证结果 |
| `summary.json` | 执行终态、停止原因、用量与产物摘要 |
| `official.json` | SWE-bench 官方判卷结果，仅判卷入口生成 |

部分文件只在对应阶段产生。网页通过产物接口读取，接口将运行轨迹统一映射为 `trajectory.jsonl` 下载入口。缺失文件不会被当作通过记录。

## 代码目录

```text
src/patchloop/
  llm.py            模型提供商、接口调用与费用估算
  tools.py          bash / finish 工具定义与输出截断
  prompts.py        系统提示与预算提醒
  loop.py           模型与工具执行循环
  sandbox.py        本机和 Docker 命令执行器
  trajectory.py     JSONL 轨迹
  repair/           任务契约、工作副本、补丁策略、验证与官方判卷
  api/              任务、来源、审查、交付、发布及数据库
  worker.py         任务领取、执行、结果恢复与容器清理
  eval.py           批量修复评测
  baseline.py       单次直出对照
  subset.py         固定题单生成
  cli.py            命令行入口
frontend/           React / JavaScript 工作台与前端测试
examples/           示例仓库与执行镜像
evals/profiles/     验证配置与任务复现说明
evals/reports/      已归档的评测和验收证据
tests/              后端测试
```

## 开发检查

```bash
uv run pytest -q
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
```

项目测试与真实模型实验分别执行。网页生产构建输出到 `frontend/dist/`；静态部署需要 SPA 路由回退和 `/api` 反向代理，当前开发服务不提供公开部署所需的身份与权限管理。
