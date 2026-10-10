# API 参考

[项目首页](../README.md) · [使用指南](usage.md) · [架构与运行机制](architecture.md) · [前端交互约定](frontend-behavior.md)

默认服务地址为 `http://127.0.0.1:8000`，交互式文档为 `/docs`，完整字段定义为 `/openapi.json`。网页开发代理使用 `/api` 前缀，直接调用服务时不加此前缀。当前接口面向本机单用户，未提供登录与多用户权限隔离。

## 接口目录

| 功能 | 接口 |
| --- | --- |
| [本地任务](#本地任务接口) | `POST /tasks`、`GET /tasks`、`GET /tasks/{task_id}` |
| [Issue 来源](#公开-github-issue-来源) | `POST /sources/github/issues`、`GET /sources/github/issues/{source_id}`、`GET /sources/github/issues/{source_id}/snapshot` |
| [仓库准备](#公开仓库准备) | `POST /repositories/prepare`、`GET /repositories/{preparation_id}` |
| [Issue 任务](#从-issue-创建修复任务) | `POST /tasks/from-issue` |
| [人工审查](#人工审查接口) | `GET /tasks/{task_id}/review`、`GET /tasks/{task_id}/reviews`、`POST /tasks/{task_id}/reviews` |
| [本地交付](#路径保护与本地交付) | `GET /tasks/{task_id}/delivery`、`POST /tasks/{task_id}/delivery` |
| [PR 预览](#github-pr-发布预览) | `POST /tasks/{task_id}/github-delivery/preview` |
| [PR 发布](#github-分支与-pr-发布) | `POST /tasks/{task_id}/github-delivery/check`、`POST /tasks/{task_id}/github-delivery`、`GET /tasks/{task_id}/github-delivery` |
| [产物读取](#运行状态与产物) | `GET /tasks/{task_id}/artifacts/{name}` |

## 通用约定

写请求使用 `Content-Type: application/json`。路径中的任务、来源和请求编号为 UUID；示例中的 `<...>` 需替换为真实值。错误响应含 `detail`，参数校验错误可能返回字段错误列表。

| HTTP 状态 | 含义 |
| --- | --- |
| 200 | 查询成功，或复用已有请求记录 |
| 201 | 创建记录；是否完成实际工作需继续查看业务状态 |
| 404 | 任务、来源、准备记录或产物不存在 |
| 409 | 版本、证据、并发操作或请求内容冲突 |
| 422 | 请求字段、路径或不支持的输入无效 |
| 429 | 上游限流；可用时返回 `Retry-After` |
| 502 / 504 | 上游异常或超时 |
| 503 | 存储等依赖暂不可用 |

具体错误条件见各接口。已经受理的准备、交付和发布也可能通过响应对象中的失败状态及错误字段报告问题，不能只看 HTTP 状态。

`request_id` 只用于明确支持它的接口。重试保留原编号和完整参数；普通 `POST /tasks` 不提供幂等请求编号，每次调用都创建新任务。断开连接不等于服务端已经取消操作。

## 本地任务接口

### 用途

`POST /tasks` 固定本地 Git 版本并登记任务；`GET /tasks` 查询摘要列表；`GET /tasks/{task_id}` 读取完整记录。创建接口只保存任务，修复由独立 worker 执行。

### 请求

```json
{
  "repo_path": "examples/toy_repo",
  "goal": "支持版本号字符串的单个小写 v 前缀，不修改测试。",
  "verification_command": "python -m pytest -q",
  "execution": {
    "image": "patchloop-toy",
    "provider": "deepseek",
    "max_model_calls": 10
  }
}
```

`repo_path`、`goal`、`verification_command` 必填。路径为服务机器上的目录，支持绝对路径或相对于 API 启动目录的路径；保存时拆分为 Git 根目录和 `workdir`。`base_sha` 可指定已有提交或引用，省略时固定提交时的 HEAD；未提交改动不进入基线。执行及路径配置见[公共配置](#执行配置与路径策略)。

省略 `execution` 时只保存记录，不由 worker 执行。新任务不会因以后修改默认配置而自动变成可执行任务。

列表参数：`limit` 默认 20，范围 1–100；`status` 按执行状态筛选；`cursor` 使用上页的 `next_cursor`。更换筛选时清空游标，继续翻页时保持筛选不变。

### 响应

创建返回 201、完整任务记录及 `Location: /tasks/{task_id}`。单条查询返回同类记录；字段包括 `task_id`、固定基线、目标、配置、执行与验证状态、时间、错误及 `result`。

列表返回 `items` 与 `next_cursor`，按创建时间和任务编号从新到旧排列。列表只含摘要，不返回完整结果、执行配置或补丁；末页游标为 `null`。分页期间状态可能变化，不是冻结快照。

### 错误与重试

仓库或基线无效返回 422，任务不存在返回 404。重复 POST 会创建另一条任务；创建结果未知时先查询列表核对，不能自动重发。列表和详情读取不启动模型或领取任务。

## 执行配置与路径策略

以下为任务 API 的 `execution` 默认值，CLI 开关默认值另见[评测配置](evaluation.md#预算与结束方式)。

| 字段 | 默认值 | 约束或含义 |
| --- | --- | --- |
| `image` | 必填 | 已存在的本地 Docker 镜像 |
| `provider` | `deepseek` | `deepseek` 或 `glm` |
| `model` | 提供商默认模型 | 提交时解析并保存；不传密钥 |
| `platform` | `null` | 按本地镜像选择，或明确指定 `linux/amd64`、`linux/arm64` |
| `memory_mb` | 2048 | 256–32768 MB |
| `max_model_calls` | 30 | 1–200 轮 |
| `command_timeout_seconds` | 60 | 1–3600 秒 |
| `verification_timeout_seconds` | 180 | 1–3600 秒 |
| `budget_reminders` | `true` | 启用剩余轮数提醒 |
| `explicit_finish` | `false` | 要求显式调用 `finish` 结束 |

worker 不自动下载镜像；执行前固定镜像 ID，修复与验证共用该 ID。指定架构必须与本地镜像一致。当前没有整个任务的总时限。

`path_policy` 在提交时固定：

```json
{
  "path_policy": {
    "allowed_paths": ["src/**"],
    "protected_paths": ["tests/**", "**/tests/**"]
  }
}
```

`allowed_paths` 缺省为空，不额外限制允许范围。`protected_paths` 缺省保护 `tests/`、`testing/`、`test_*.py`、`*_test.py` 和 `conftest.py` 及嵌套路径；显式传入列表会替换默认列表。每组最多 100 个模式，每个最多 512 字符。

模式相对于仓库根目录，使用 POSIX 路径及 `fnmatchcase`，`*` 可匹配 `/`，末尾 `/` 自动补为 `/**`。自然语言要求不会自动转为路径规则。策略约束最终补丁与交付，不是命令执行时的文件系统拦截。

## 公开 GitHub Issue 来源

### 用途

`POST /sources/github/issues` 读取公开 Issue 并保存快照。两个 GET 分别读取来源详情和当时保存的仓库／Issue JSON；不会重新访问 GitHub。

### 请求

```json
{
  "request_id": "<新的 UUID>",
  "url": "https://github.com/pallets/flask/issues/6146"
}
```

仅支持 `https://github.com/<owner>/<repo>/issues/<number>`。大小写、编号前导零和末尾斜杠会规范化，查询参数与锚点不保存。不支持私有仓库、PR、重定向、评论或附件读取。

### 响应

首次返回 201，响应为 `source` 和 `replayed`，`Location` 指向来源详情。`source` 包含仓库／Issue 身份、标题、正文、状态、来源时间、读取时间及 `snapshot_sha256`。快照摘要基于规范化 JSON，不是 HTTP 原始字节；后续 GitHub 编辑不会改写旧快照。

### 错误与重试

同编号及规范化 URL 重试返回 200 和原快照，不重新读取；同编号换 URL 返回 409。需要最新内容时使用新编号。并发首次请求可能分别读取上游，最终只保存一条相同编号记录。

不存在返回 404，不支持的输入返回 422，限流为 429，连接／上游异常为 502，超时为 504。失败不保存半条来源，不自动重试。公开读取不采用发布 Token，限流与接收预算见[外部访问边界](architecture.md#外部访问边界)。

## 公开仓库准备

### 用途

`POST /repositories/prepare` 解析来源仓库的版本并下载独立副本；`GET /repositories/{preparation_id}` 查询记录及当前 checkout 完整性。此步骤不创建修复任务或安装依赖。

### 请求

```json
{
  "request_id": "<新的 UUID>",
  "source_id": "<已保存的来源 UUID>",
  "ref": null
}
```

`ref: null` 使用准备时的默认分支；也可填写分支名、标签或完整的 40 位 commit SHA，不接受 `HEAD~1` 等表达式。服务先固定 SHA 再下载；Issue 发布时间不决定代码基线。

### 响应

首次受理同步等待准备并返回 201，包含 `preparation` 和 `replayed`。记录保存来源、仓库、请求／解析版本、`base_sha`、代码目录、时间、`status`、`error` 和 `error_status`。

`status` 为 `preparing`、`ready` 或 `failed`。GET 返回 `preparation`、`checkout_intact` 和 `reason`；`ready` 只表示当时成功，`checkout_intact` 才表示当前目录、配置、HEAD 和工作区检查结果。准备中或失败时，该完整性字段为 `null`。

### 错误与重试

同编号、来源和 `ref` 重试返回 200 与原记录，不重新解析或下载。同编号改变内容或另一项准备持有锁时返回 409；后者释放后可沿用原编号重试。已结束的失败记录重放仍返回原失败，重新准备需新编号和目录。

来源或准备记录不存在返回 404，输入无效返回 422。准备开始后出现的失败可能保存在 `preparation.error_status`，需同时检查业务状态。服务重启后，在确认无其他进程持锁时将未完成准备标为失败，不自动续传或删除失败目录。下载限制见[外部访问边界](architecture.md#外部访问边界)。

## 从 Issue 创建修复任务

### 用途

`POST /tasks/from-issue` 将冻结的 Issue 来源、已准备代码与用户确认的执行配置关联为任务。网页操作见[使用指南](usage.md#github-issue-任务)。

### 请求

```json
{
  "request_id": "<新的 UUID>",
  "preparation_id": "<准备成功的 UUID>",
  "goal": "<确认后的修复要求>",
  "verification_command": "python -m pytest -q",
  "execution": {"image": "<匹配仓库依赖的本地镜像>", "provider": "deepseek"},
  "workdir": ".",
  "path_policy": {"allowed_paths": ["src/**"]}
}
```

`execution` 必填，`workdir` 默认仓库根目录，路径策略见公共配置。仓库路径、基线和来源从准备记录读取，不接受请求覆盖。镜像和验证命令需按仓库准备，示例镜像不适用于任意项目。

### 响应

首次返回 201，包含 `task`、`replayed: false` 及任务 `Location`。任务初始为 `queued`，`task.source` 保存来源、准备编号、固定版本及摘要；模型运行时收到确认目标和冻结 Issue 上下文。提交接口不调用模型。

### 错误与重试

同编号与相同配置返回 200 和已有任务，不再次排队；同编号更改配置返回 409。任务和请求映射在同一事务中保存。

准备记录不存在返回 404；准备未完成、目录或身份不匹配返回 409；字段无效返回 422。创建和 worker 执行前分别核对准备结果。已有请求的重放只查询原任务，即使 checkout 后来变化也不重新执行。

## 人工审查接口

### 用途

`GET /tasks/{task_id}/review` 获取当前有效状态与证据；`GET /tasks/{task_id}/reviews` 读取按版本号升序排列的历史；`POST /tasks/{task_id}/reviews` 保存新的接受或拒绝决定。

### 请求

先读取当前状态，按返回值构造提交：

| 字段 | 类型与来源 |
| --- | --- |
| `request_id` | 新决定的 UUID；同次重试保留 |
| `expected_revision` | GET 返回的 `revision`，整数，首次为 0 |
| `decision` | `accepted` 或 `rejected` |
| `comment` | 可选字符串，默认空，最多 4000 字符 |
| `evidence` | 完整复制 GET 返回的证据对象 |

`evidence` 包含 `base_sha`、`patch_sha256`、`validation_sha256`、`task_sha256`、`summary_sha256` 和 `record_sha256`。产物哈希按原始字节计算，任务记录按规范 JSON 计算；不能自行替换其中字段。

### 响应

当前状态包含 `status`、`revision`、`can_accept`、`can_reject`、`reason`、`evidence` 和 `latest_review`。状态为 `pending`、`accepted`、`rejected` 或 `stale`。

新决定返回 201，响应为 `review`、`current`、`replayed`。`review` 是该次请求对应的决定，`current` 才是当前状态。历史查询返回 `task_id` 与 `items`。原始运行摘要中的审查字段不随决定改写。

### 错误与重试

仅结束、清理完成且证据完整的非空补丁可审查。接受还要求无执行错误、基线明确失败、候选通过及验证总结果为 `passed`；轮数耗尽的任务满足这些条件时也可接受。以 `can_accept`／`can_reject` 判定操作能力。

同编号及相同内容返回 200，不重复追加；同编号不同内容、旧版本或证据变化返回 409。不存在为 404，无效字段为 422。并发同版本提交最多新增一条决定，旧请求重放不会回退新决定。

当前状态只比对最新决定与当前证据，不采用更早的批准覆盖新拒绝。证据缺失或变化时为 `stale`；API 读取不写失效事件，若内容与任务记录完全恢复，最新决定可再次匹配。前端观察到过期后仍要求显式重新加载，见[交互约定](frontend-behavior.md#审查与交付)。

## 路径保护与本地交付

### 用途

`GET /tasks/{task_id}/delivery` 查询计划与已有交付；`POST /tasks/{task_id}/delivery` 基于批准补丁生成本地分支和独立工作区。路径语义见[公共配置](#执行配置与路径策略)，Git 实现见[交付机制](architecture.md#交付与发布机制)。

### 请求

| 字段 | 类型与来源 |
| --- | --- |
| `request_id` | 新请求 UUID；恢复原请求时保持不变 |
| `review_id`、`review_revision` | GET 返回的批准编号及版本 |
| `evidence` | GET 返回的完整证据对象 |
| `target_head` | GET 返回的目标提交 |

新交付要求当前批准有效、验证通过且源仓库 HEAD 等于验证基线。HEAD 已改变时需新建任务、验证和审查，不自动变基或采用新目标。

### 响应

GET 包含 `can_deliver`、`reason`、批准、证据、路径策略、修改文件、目标分支／工作区以及已有 `delivery`。`delivery_intact` 表示当前输出是否仍与交付记录一致。

POST 新尝试返回 201，复用返回 200，包含 `delivery` 与 `replayed`。交付状态为 `preparing`、`completed`、`failed`；完成后记录分支、commit 和 tree。默认分支为 `patchloop/<task_id>/r<review_revision>`，工作区位于数据库同级 `deliveries/`。

### 错误与重试

任务不存在为 404，批准、证据、路径或目标冲突为 409，无效字段为 422。执行阶段失败也可能写入 `delivery.status=failed`，需检查错误字段。

同一审查保存一份交付，换请求编号也不创建第二份。进行中记录可显式恢复，持锁进程尚在运行时不启动重复交付。恢复核对已有分支、提交与目录，不覆盖用户内容或改用另一输出根目录。失败记录保留，处理原因后可通过新的审查版本发起新交付。

交付不调用模型、不重跑验证，也不推送或创建 PR。原仓库 HEAD、索引、已暂存、未暂存及未跟踪内容保持不变。

## GitHub PR 发布预览

### 用途

`POST /tasks/{task_id}/github-delivery/preview` 对当前批准与已完成本地交付生成离线方案，不访问 GitHub 或保存发布记录。

### 请求

```json
{"base_branch": "main", "draft": true}
```

`base_branch` 必填；可选 `title`、`body` 覆盖默认文本，分别最多 256、60000 字符且不能为空白，标题须为单行；`draft` 为布尔值，默认 `true`。目标仓库、提交、分支与证据从冻结来源和交付读取，不接受其他目标、补丁或凭证。

### 响应

| 字段 | 含义 |
| --- | --- |
| `status`、`publish_enabled`、`remote_checks` | 固定为 `preview`、`false`、`not_checked`，不表示已获发布权限 |
| `plan.repository`、`issue_url` | 已保存的来源身份 |
| `plan.base_branch`、`base_sha` | 用户选择的目标分支与本地验证基线 |
| `plan.head_branch`、`head_sha`、`tree_sha` | 已完成交付的准确版本 |
| `plan.review_id`、`review_revision`、`evidence` | 对应批准与证据 |
| `plan.changed_paths`、`verification` | 完整修改路径与已保存测试摘要 |
| `plan.title`、`body`、`draft` | 待确认的 PR 内容 |
| `plan_sha256`、`checked_at` | 方案摘要及检查时间；时间不参与摘要 |

默认正文包含要求、Issue 链接、修改文件和验证依据，不自动拼入完整 Issue 正文、本机路径或日志。文件多于 50 个时正文截取前 50 项，`changed_paths` 仍完整；要求与命令中原有文本保留，调用者须核对待发布内容。

### 错误与重试

缺失任务为 404，来源／批准／交付不一致为 409，请求无效为 422。服务检查唯一父版本、准确文件树和路径策略，结束检查后再次核对批准；不修复变更的工作区。

内容与证据相同时可重复生成相同摘要；分支、文本或批准变化会改变摘要。摘要不是授权令牌，正式发布仍需单独确认及远端核对。

## GitHub 分支与 PR 发布

### 用途

| 接口 | 行为 |
| --- | --- |
| `POST /tasks/{task_id}/github-delivery/check` | 按预览参数只读核对 GitHub 账号、仓库、目标分支及已有 PR |
| `POST /tasks/{task_id}/github-delivery` | 保存确认意图，推送准确提交并创建 PR；同请求也用于显式恢复 |
| `GET /tasks/{task_id}/github-delivery` | 查询已保存记录和凭证配置状态，不访问 GitHub 或自动恢复 |

只支持来源对应、当前账号可写的同一公开仓库，不自动 fork、覆盖已有分支或合并 PR。远端检查与发布读取服务端 `PATCHLOOP_GITHUB_TOKEN`，请求体不接受 Token；配置方法见[使用指南](usage.md#模型与凭证)。

### 请求

检查接口使用与离线预览相同的请求。发布接口提交已核对的方案：

```json
{
  "request_id": "<新的 UUID>",
  "preview": {"base_branch": "main", "draft": true},
  "plan_sha256": "<已核对方案的 SHA-256>",
  "confirmed": true
}
```

`confirmed` 必须明确为布尔值 `true`。预览中修改过的标题、正文和类型须原样包含在 `preview` 中；恢复使用记录里的完整 `submission`，不重建请求内容。

### 响应

检查返回 `preview`、`actor`、`remote_base_sha`、`remote_head_sha`、`can_publish` 和 `reason`。它核对仓库身份、公开与归档状态、权限和目标基线；首次发布要求没有同名分支、相关 PR 或已有发布记录。检查不预留远端状态，正式发布会再核对。

发布首次受理返回 201，复用返回 200，包含 `publication` 与 `replayed`。GET 返回 `task_id`、`configured` 和 `items`。

| `publication.status` | 含义 |
| --- | --- |
| `pending` | 意图或执行阶段已保存 |
| `blocked` | 凭证、权限、版本或已有内容阻止继续，保留已发生的操作 |
| `uncertain` | 写操作结果未知，需查询核对 |
| `completed` | 已记录匹配方案的 PR；不表示已合并或批准仍然有效 |

`phase` 为 `check`、`push`、`pull_request` 或 `completed`。`push_attempted`、`branch_pushed`、`pr_attempted` 记录可能已发出的请求与已核对结果，`actor` 绑定发布账号，PR 链接为 `pull_request.url`。GET 是历史记录查询，不保证远端对象仍处于记录时的状态。

### 错误与重试

输入无效或未确认返回 422，旧预览、并发或请求冲突返回 409。凭证及上游访问错误可能通过 HTTP 错误或发布对象的 `status`、`error` 返回，须同时检查二者。

同一请求不能改变内容，同一本地交付绑定一份已确认方案。复用方案返回首次确认的请求编号；恢复不得更换账号或参数。每个数据库串行执行发布。

| 中断位置 | 恢复规则 |
| --- | --- |
| 推送结果未知 | 查询远端分支，只有准确指向原提交才采用已有结果 |
| PR 创建结果未知 | 查询并核对仓库、分支、账号、提交、文本与类型，唯一匹配后补记 |
| 未查询到可能已创建的 PR | 保持 `uncertain`，不能因一次空结果再次创建 |
| 明确的远端拒绝 | 处理原因后可按原记录显式重试 |
| 推送后批准或目标变化 | 停止后续创建，保留已推送分支 |
| PR 已存在但基线变化 | 保留 PR 链接并标记待处理，不声称重新验证通过 |

已经发生的匹配发布可在恢复时补记，即使本地批准后来变化；这不授权新的写入。服务不自动删除远端分支或改写现有 PR。跨网络的检查与写入不是原子事务，具体 Git 与凭证隔离机制见[架构说明](architecture.md#交付与发布机制)。

## 运行状态与产物

任务的 `status` 为 `queued`、`running`、`completed`、`interrupted` 或 `error`；`validation_status` 为 `not_run`、`passed`、`failed` 或 `inconclusive`。`completed` 可同时对应验证失败，轮数耗尽仍会验证已有补丁。`cleanup_pending` 表示容器清理待确认，worker 在处理完成前不领取下一题。

`GET /tasks/{task_id}/artifacts/{name}` 返回 UTF-8 文本文件，允许的名称如下：

| 名称 | 内容 |
| --- | --- |
| `task.json`、`summary.json`、`validation.json` | 任务配置、运行摘要及独立验证结果 |
| `patch.diff` | 候选补丁 |
| `events.jsonl`、`trajectory.jsonl` | 阶段事件与逐轮轨迹 |
| `runtime.json`、`worker-error.json` | 实际镜像／容器信息及 worker 错误（产生时） |

任务、产物不存在或名称不受支持时返回 404，不生成占位结果。目录、worker 启停和恢复见[使用指南](usage.md#服务与-worker)及[数据目录与恢复](architecture.md#数据目录与恢复)。

字段模型与约束的源码入口：[models.py](../src/patchloop/api/models.py)、[来源模型](../src/patchloop/api/source_models.py)、[准备模型](../src/patchloop/api/repository_models.py)、[发布模型](../src/patchloop/api/github_delivery_models.py)。
