# API 参考

[返回项目首页](../README.md) · [使用指南](usage.md) · [架构与运行机制](architecture.md)

接口基于 FastAPI，默认监听 `http://127.0.0.1:8000`，交互式文档位于 `/docs`。当前面向本机单用户使用，未提供登录和多用户权限隔离。以下命令均在项目根目录执行。

## 目录

- [任务提交与查询](#本地任务接口)
- [GitHub Issue 来源](#公开-github-issue-来源)
- [仓库准备](#公开仓库准备)
- [从 Issue 创建任务](#从-issue-创建修复任务)
- [人工审查](#人工审查接口)
- [路径保护与本地交付](#路径保护与本地交付)
- [PR 发布预览](#github-pr-发布预览)
- [GitHub 发布与恢复](#github-分支与-pr-发布)
- [worker 与运行结果](#后台执行任务)

## 本地任务接口

任务接口使用 FastAPI 和 SQLite，支持提交修复请求、按编号查询和服务重启后读取历史记录。API 进程只接收请求；独立的 worker 进程在 Docker 中串行调用 RepairRunner。仅启动 API 不需要 Docker 或 API key，启动 worker 执行任务时才需要。

在项目根目录启动服务：

```bash
uv sync
uv run agent serve
```

默认地址为 `http://127.0.0.1:8000`，接口调试页面为 `http://127.0.0.1:8000/docs`。服务仅监听本机。默认数据库是启动目录下的 `data/tasks.sqlite3`，该目录已被 Git 忽略；`--db /绝对路径/tasks.sqlite3` 可指定存储位置，`--port 8001` 可更改端口。

保持服务终端运行，在另一个终端提交任务。下面的请求只登记任务，不提供执行配置，因此会一直保持 `queued`：

```bash
curl -sS -X POST http://127.0.0.1:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_path": "examples/toy_repo",
    "goal": "支持版本号字符串的 v 前缀，不改变现有版本号行为",
    "verification_command": "python -m pytest -q"
  }'
```

成功返回 HTTP 201，包含 `task_id`、`status: "queued"`、完整 `base_sha` 和创建时间。`repo_path` 支持服务所在机器上的绝对路径，或相对于服务启动目录的路径；存储时拆分为 Git 根目录 `repo_path` 与子目录 `workdir`。默认在提交时固定 HEAD，也可通过 `base_sha` 指定已有提交或引用；之后移动分支不会改变已提交的任务，工作区未提交改动不会纳入任务基线。

复制响应中的 `task_id`，查询任务：

```bash
curl -sS "http://127.0.0.1:8000/tasks/<task_id>"
```

按 Ctrl+C 停止服务，再使用同一数据库启动，原编号仍可查询。换启动目录时请用 `--db` 指向原数据库。每次 POST 都创建独立任务；重复提交会得到不同编号。任务不存在返回 404，参数或 Git 基线无效返回 422，存储暂时不可用返回 503。任务状态不代表补丁验证结论。

查询历史任务列表：

```bash
curl -sS "http://127.0.0.1:8000/tasks?limit=20"
curl -sS "http://127.0.0.1:8000/tasks?status=completed&limit=20"
```

`GET /tasks` 返回 `items` 和 `next_cursor`。每页默认 20 条，允许 1–100 条，按创建时间从新到旧排序，同一时间按任务编号倒序排列。`status` 可选 `queued`、`running`、`completed`、`interrupted` 或 `error`，表示执行状态；验证结论单独显示在每条记录的 `validation_status` 中。

存在下一页时，将 `next_cursor` 原样作为下一次请求的 `cursor` 参数，并保持同一个状态筛选。最后一页的 `next_cursor` 为 `null`；没有任务时返回空 `items`。更换筛选或刷新整个列表时清空游标。分页基于排序位置，新增较新的任务不会重复挤入后续页面；这不是冻结快照，任务状态仍可能在翻页期间变化。

列表只读取任务摘要，不返回完整 `result`、执行配置、补丁或验证日志。点击某一项后，通过 `GET /tasks/{task_id}` 和产物接口读取详情。查询列表不会领取任务、执行修复或调用模型。

升级启动时会统一旧记录的创建时间存储格式，以保证整秒和小数秒任务的排序正确；原有时间含义及任务详情保持不变。

## 公开 GitHub Issue 来源

支持读取公开 GitHub Issue，并保存独立的来源记录。它会记录仓库、Issue 编号与对象 ID、标题、正文、状态、来源更新时间、读取时间和 JSON 快照摘要。随后可准备固定版本代码，并通过 `/tasks/from-issue` 创建关联来源的修复任务。网页入口为侧边栏“GitHub Issue”（`/tasks/github`），API `/docs` 也保留直接操作入口。

| 接口 | 用途 |
| --- | --- |
| `POST /sources/github/issues` | 读取公开 Issue，保存一次来源快照 |
| `GET /sources/github/issues/{source_id}` | 查询已保存的来源详情 |
| `GET /sources/github/issues/{source_id}/snapshot` | 读取当时保存的仓库与 Issue JSON |

更新代码后重启 `uv run agent serve`，在 `/docs` 的 POST 接口填写以下内容；`request_id` 使用新生成的 UUID，可通过 `python3 -c 'from uuid import uuid4; print(uuid4())'` 获取：

```json
{
  "request_id": "<新生成的 UUID>",
  "url": "https://github.com/pallets/flask/issues/6146"
}
```

这个链接仅用于演示来源读取，不是选定的代码修复题。成功返回 201，`source.source_id` 用于后续查询，`Location` 响应头也提供查询路径。同一个 `request_id` 和规范化 URL 重试返回 200 及原快照，不重新访问 GitHub；同编号换 URL 返回 409。需要读取最新内容时，使用新的请求编号，旧快照保留。并发首次请求可能各自读取 GitHub，但最终只保存同编号的一条记录。

仅接受 `https://github.com/<owner>/<repo>/issues/<number>`。仓库名称大小写、编号前导零及末尾斜杠会规范化，查询参数和锚点不保存。私有仓库、PR、其他主机及重定向不受支持；仓库改名或迁移后需提供当前链接。正文作为来源材料保存，不执行其中的指令；不读取评论、附件、PR diff 或 CI 日志。

快照保存 GitHub 返回的仓库与 Issue JSON 对象，并以键排序、紧凑 UTF-8 JSON 计算 SHA-256；不是 HTTP 原始字节。来源与快照读取时会校验摘要；GitHub 上后续编辑不会改写历史记录。此步骤不克隆仓库、不固定代码 commit、不创建修复任务，也不调用模型、运行测试或发布 GitHub 内容。

读取使用未认证的公开 GET，不读取 `GITHUB_TOKEN`、`GH_TOKEN` 或模型密钥。GitHub 对未认证请求通常按来源 IP 限制为每小时 60 次；一次新导入需要两次请求，查看已保存来源或重放已完成请求不消耗这项额度。见 [GitHub 公开 Issue 接口](https://docs.github.com/en/rest/issues/issues#get-an-issue)与[速率限制说明](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)。

不存在返回 404，链接或对象不支持返回 422，限流返回 429（已知等待时间时附带 `Retry-After`），超时返回 504，连接或上游响应异常返回 502，数据库暂不可用返回 503。失败不保存半条来源，也不自动重试。每个响应最多 2 MiB，仅接受未压缩数据；导入设有 25 秒接收预算，单次连接/读写等待也有上限并受剩余预算约束。同步 I/O 不保证在第 25 秒强制中断，接收或处理完发现超预算会拒绝入库。

## 公开仓库准备

导入 Issue 后，可根据来源编号准备对应仓库。服务重新核对 GitHub 仓库 ID 与公开状态，将指定分支、标签或完整 commit SHA 解析为固定版本，先保存 SHA，再下载该版本代码。默认分支取准备时的 GitHub 信息；Issue 发布时间不等于代码基线时间，需要旧版本时应明确指定 `ref`。

| 接口 | 用途 |
| --- | --- |
| `POST /repositories/prepare` | 登记一次准备请求，解析版本并下载代码 |
| `GET /repositories/{preparation_id}` | 查询记录及当前代码目录是否完整 |

启动 API 后，在 `/docs` 的 POST 接口中填写以下内容。`source_id` 使用 Issue 导入响应中的来源编号，`request_id` 为新生成的 UUID：

```json
{
  "request_id": "<新生成的 UUID>",
  "source_id": "<已导入的 source_id>",
  "ref": null
}
```

`ref: null` 使用当前默认分支，也可填写分支名、标签名或完整的 40 位 commit SHA；不支持 `HEAD~1` 等 Git 版本表达式。首个请求同步等待解析与下载，返回 `preparation` 和 `replayed: false`；HTTP 201 表示记录已创建，是否下载成功以 `preparation.status` 为准：

| 字段 | 含义 |
| --- | --- |
| `preparation_id` / `source_id` | 仓库准备编号及对应问题来源 |
| `repository` / `clone_url` | 仓库身份及固定 GitHub 下载地址 |
| `requested_ref` / `resolved_ref` | 用户选择及实际解析的分支、标签或 SHA |
| `base_sha` / `resolved_at` | 固定的 commit SHA 及解析时间；解析前可为空 |
| `repo_path` | 服务管理的独立代码目录 |
| `status` | `preparing` 准备中、`ready` 准备成功、`failed` 失败 |
| `error` / `error_status` | 失败原因及对应错误类别，例如上游限流为 429 |

默认目录为数据库同级的 `repositories/<preparation_id>/repo`，默认数据库下即 `data/repositories/.../repo`。准备使用浅下载并检出固定 SHA，不跟随后续分支移动。每次新请求编号创建独立目录；同编号、同来源和同 `ref` 重试返回 200 与原记录，不重新解析或下载。同编号更改来源或 `ref` 返回 409；另一项准备正在运行时，新请求也返回 409，稍后保留原请求编号重试。重复提交已结束的失败请求只查询原失败，要重新尝试需新编号。

从响应中复制 `preparation_id`，通过 GET 查询。成功记录之外还返回 `checkout_intact` 和 `reason`：前者为 `true` 表示当前路径、Git 配置、HEAD 和工作区检查通过；本地改动、目录缺失或配置变化会得到 `false` 及原因，不自动重置。`status: ready` 保留当时的准备结果，与当前完整性分开表示。准备中或失败时该字段为 `null`。

记录支持服务重启后查询。服务启动或提交后续请求时，在确认没有其他进程持有准备锁后，将未完成记录标记为失败；不会继续执行半途留下的下载。失败目录保留供排查，不覆盖或自动删除，重新尝试使用新的独立目录。

本阶段仅支持公开 GitHub 仓库，拒绝重定向和仓库身份变化。Git 禁用用户 Git 配置、凭证助手、交互提示、hooks 和自动子模块操作，不安装依赖、不执行仓库代码。含子模块的版本会明确失败；Git LFS 只保留指针文件，不下载其外部对象。浅下载减少历史数据，但不是磁盘配额，大仓库仍可能占用较多空间。版本解析有 25 秒接收预算，Git 准备步骤共用 180 秒超时，超时会终止该次 Git 进程组。

此步骤无需 worker、Docker 或模型密钥，不创建修复任务。后续还需选择运行镜像、验证命令与修复范围，再关联固定来源和代码版本创建任务。版本解析使用 [GitHub commit 接口](https://docs.github.com/en/rest/commits/commits#get-a-commit)，浅下载行为见 [git fetch](https://git-scm.com/docs/git-fetch)。

## 从 Issue 创建修复任务

仓库准备成功后，启动 API，在 `/docs` 使用 `POST /tasks/from-issue`。需要填写确认后的目标、验证命令和执行配置；代码路径、基线 SHA 和 Issue 来源由服务从准备记录读取，不能在请求中覆盖。

```json
{
  "request_id": "<新生成的 UUID>",
  "preparation_id": "<准备成功的 preparation_id>",
  "goal": "<确认后的问题与期望行为>",
  "verification_command": "python -m pytest -q",
  "execution": {
    "image": "<已安装该仓库依赖的本地 Docker 镜像>",
    "provider": "deepseek",
    "max_model_calls": 10
  },
  "workdir": ".",
  "path_policy": {
    "allowed_paths": ["src/**"]
  }
}
```

示例中的镜像、目标及允许路径需要按实际仓库填写；`patchloop-toy` 仅适合准备过相应依赖的小型 Python 示例任务，不是任意仓库的运行环境。程序不自动安装依赖，不执行 Issue 正文中的命令。路径规则从仓库根目录计算；`workdir` 可选，默认为根目录，也可填写实际存在的子目录。缺省的保护规则继续禁止修改常见测试路径。

首次成功返回 201，响应包含 `task` 和 `replayed: false`，`task.status` 为 `queued`；`Location` 指向 `/tasks/{task_id}`。`task.source` 保存 Issue 内容和快照哈希、准备编号、固定 SHA、版本解析时间，查询任务不重新读取 GitHub。模型运行时会同时收到用户确认的目标和冻结的 Issue 上下文；正文作为问题材料，不提供扩大路径范围或发布内容的权限。

同请求编号及相同配置重试返回 200 和当前任务状态，不创建第二条任务；同编号换配置返回 409。新编号可以有意创建另一次修复尝试。任务和请求映射在同一 SQLite 事务中保存，失败时不会留下孤立任务。旧任务的来源保持为空，历史修复产物与审查记录不改写。

未找到准备记录返回 404；未完成准备、代码被修改、身份不匹配或目录无效返回 409；请求字段无效返回 422。创建前会重新检查目录、Git 配置、HEAD 和工作区；worker 在初始化模型和 Docker 前再检查一次，提交之后发生变更也会停止。重试已存在的请求仅查询保存的任务，即使代码已变化也不会重新排队。

创建请求只排队，不调用模型；需要运行时，使用返回的 `task.task_id`：

```bash
uv run agent worker --task-id "<返回的 task_id>"
```

这条命令会执行真实模型调用。任务详情页面可用 `/tasks/<task_id>` 查看进度、补丁、验证和人工审查，并在概览查看冻结的问题来源。

网页使用“GitHub Issue”入口，依次读取 Issue、准备代码、确认配置并创建任务。模型和预算复用普通任务表单，镜像默认留空，需要先准备相应依赖。刷新会查询原来源和代码状态，不会自动重发请求；断网后可用原编号重试，浏览器无法保存请求编号时不发送新请求。完整操作说明见 [前端 README](../frontend/README.md)。

真实任务验收的准备配置见 [Flask #6146 文档任务](../evals/profiles/github-flask-6146/README.md)：固定版本、运行镜像、路径策略、可粘贴的单行验证命令均已准备，并在隔离容器中完成基线及正反例检查。预检未调用模型；后续用户已完成真实修复、人工接受和本地分支交付，见 [M3 真实验收归档](../evals/reports/github-issue-flask6146-smoke/README.md)。候选三项离线检查通过，交付内容与批准补丁一致；尚未发布 GitHub PR。

## 人工审查接口

审查使用独立记录，绑定具体任务、基线、补丁和验证证据。不会修改 `result`、`summary.json` 或原始实验报告，也不会执行模型、运行测试或应用补丁。当前为本机单用户接口，网页详情的“人工审查”页签已接入；接受后可单独生成本地修改分支。

| 接口 | 用途 |
| --- | --- |
| `GET /tasks/{task_id}/review` | 当前有效审查状态、版本号、证据哈希和可操作性 |
| `POST /tasks/{task_id}/reviews` | 提交接受或拒绝，新增一条审查记录 |
| `GET /tasks/{task_id}/reviews` | 按版本号升序查看完整审查历史 |

先读取当前状态：

```bash
curl -sS "http://127.0.0.1:8000/tasks/<task_id>/review"
```

返回的 `status` 为 `pending`、`accepted`、`rejected` 或 `stale`；`can_accept` 和 `can_reject` 分别表示是否允许相应操作，`reason` 解释限制。`evidence` 包含 `base_sha` 及补丁、验证报告、任务配置、修复摘要和任务记录的 SHA-256。文件哈希按原始字节计算，任务记录哈希按固定排序的 JSON 计算。

在 `/docs` 的 `POST /tasks/{task_id}/reviews` 中填写：

| 请求字段 | 填写方式 |
| --- | --- |
| `request_id` | 每次新决定生成一个 UUID；同一次请求重试保留原值 |
| `expected_revision` | 刚读取的 `revision`，首次为 0 |
| `decision` | `accepted` 或 `rejected` |
| `comment` | 审查意见，最多 4000 字符，可为空 |
| `evidence` | 完整复制刚读取的证据对象，不自行计算或修改 |

只允许审查已结束、已清理且证据完整一致的非空补丁。接受还要求基线明确失败、候选测试通过、验证总结果为 `passed`，且无执行错误；达到轮数上限的 `interrupted` 任务若满足这些条件也可接受。未通过验证的完整补丁可以被拒绝。缺少产物、档案不一致、仍在运行或空补丁都不能审查。

新决定返回 201；完全相同的 `request_id` 和请求内容再次提交返回 200，不重复追加历史。同键不同内容、过期的版本号或变化的证据返回 409，需要重新查看当前状态。不存在的任务返回 404，无效参数返回 422，数据库故障返回 503。并发请求基于同一版本提交时，最多新增一个决定。

响应中的 `review` 是本次请求对应的历史决定，`current` 才是当前有效状态。旧请求在新决定之后重放，不会覆盖新决定。拒绝后可以重新审查：刷新版本号，使用新的请求编号提交下一条决定，旧记录仍保留。

当前状态由最新决定和当前证据比对得到：证据变化或缺失时为 `stale`，历史决定不改写。读取不会写入失效事件，也没有文件监视器；如果证据逐字节恢复且任务记录完全一致，最新决定可再次匹配。不会寻找更早的批准来覆盖较新的拒绝。提交前会复读证据，交付也会在创建分支前重新核对批准、证据及仓库状态。SQLite 事务不锁定外部文件。

现有 `GET /tasks/{task_id}` 中的 `result.review_status` 是修复结束时的原始快照，仍可能为 `pending`；最新审查应读取上述独立接口。网页概览与审查页都使用独立状态，分别展示测试结果和人工决定。

在网页审查页核对补丁和修复前后测试，填写意见，再点击“接受补丁”或“拒绝补丁”。资料和版本在提交前再次核对，变化时保留意见并要求重新加载。网络异常后可核对结果或使用同一请求重试，不自动重复提交；较旧的请求响应不会覆盖已知的新版本。具体说明见 [前端说明](../frontend/README.md)。


## 路径保护与本地交付

新任务的 `path_policy` 在提交时固定。`allowed_paths` 为空表示不额外限制修改范围；`protected_paths` 默认保护 `tests/`、`testing/`、`test_*.py`、`*_test.py` 和 `conftest.py` 及其嵌套路径。网页高级设置可逐行配置；API 示例：

```json
{"path_policy": {"allowed_paths": ["src/**"], "protected_paths": ["tests/**", "**/tests/**"]}}
```

规则使用仓库根目录相对 POSIX 路径和 `fnmatchcase` 匹配，`*` 也可匹配 `/`；末尾 `/` 自动补为 `/**`。文字中的路径要求不会自动转为规则，以提交的结构化配置为准。模型可以在隔离副本中尝试编辑，但最终补丁违规时验证失败且不能交付；不是命令级文件系统拦截。

路径检查使用 Git 临时索引应用补丁，再读取 NUL 分隔的真实变更路径，覆盖新增、删除、重命名两端、空格/引号、非 ASCII 与二进制补丁。交付额外拒绝 Git 控制文件以及符号链接、子模块等特殊 mode 的变更。旧任务没有冻结路径策略时保留原档案，在交付时使用默认测试保护规则；不倒改旧实验成绩或审查哈希。

| 接口 | 用途 |
| --- | --- |
| `GET /tasks/{task_id}/delivery` | 查询交付计划、可操作性、修改路径和已有结果 |
| `POST /tasks/{task_id}/delivery` | 从当前有效批准生成本地分支与独立工作区 |

POST 需要新的 `request_id`，以及刚查询到的 `review_id`、`review_revision`、完整 `evidence` 和 `target_head`。网页负责填入这些值，并在提交前重新查询。服务只接受当前有效且验证通过的批准；源仓库 HEAD 必须等于任务的验证基线。HEAD 已变化时，需要基于当前版本新建修复任务、验证并审查，首版不自动换基线或重放到新版本。

成功生成 `patchloop/<task_id>/r<review_revision>` 分支、一个以验证基线为唯一父提交的 commit，以及数据库同级 `deliveries/<task_id>/r<review_revision>` 下的 [Git linked worktree](https://git-scm.com/docs/git-worktree)。目标位置由服务管理；若在原仓库内部，必须已经被 Git 忽略。提交使用用户配置的 Git 作者信息，禁用签名、hooks 和内容 filters。原仓库的 HEAD、索引、暂存、未暂存和未跟踪文件保持不变；不自动 stash、切分支、push、合并或创建 PR。

状态检查对已初始化的子模块逐层使用各自的安全 Git 配置，避免执行子模块独有的 filters，同时保留对其未提交改动的识别。未初始化的子模块不因检查而初始化；单次最多检查 256 个工作区（含主仓库），越界路径或超限会停止检查。

创建分支前再次检查审查与证据；使用 [Git update-ref](https://git-scm.com/docs/git-update-ref) 的不解引用更新及空旧值检查，已有普通 ref、symbolic ref 或目录都不会被覆盖。新 commit 的 tree 必须等于基线加批准补丁后的 tree。

同一审查只保存一份交付记录，重复请求或换请求编号也返回同一结果。返回 201 表示新尝试，200 表示已有记录；真正结果以 `delivery.status` 的 `preparing/completed/failed` 为准。`delivery_intact` 另行表示当前分支与工作区是否仍保持交付时状态，用户之后的修改不会被重置。

网页核对到更高审查版本时，如果旧待确认请求没有对应的进行中记录，会解除它对新交付的阻塞。已有终态按原结果核对，匹配的进行中记录保留供恢复；仅补丁哈希或 HEAD 变化不会直接清除未确认请求。

进行中的记录可在网页点击“恢复交付”。进程仍在执行时不会启动第二份交付；如果中断发生在分支已经发布但数据库尚未回写之间，会核对并恢复同一提交。自身空工作区尚未填充的情况可继续完成；已有用户内容不会强制覆盖。改变输出根目录后不能把中断记录悄悄交付到新位置，需恢复原配置。

失败记录及工作区保留供检查，首版不自动清理或覆盖。处理原因后，可通过新的审查版本发起新交付。交付使用已保存的独立测试证据，不调用模型或重新执行验证命令；接受补丁与生成分支是两个独立操作。

## GitHub PR 发布预览

`POST /tasks/{task_id}/github-delivery/preview` 从当前有效批准和已经完成的本地交付生成 PR 方案。此接口只做本地检查，不访问 GitHub、不使用凭证、不调用模型、不重新运行验证，也不推送分支或创建 PR。预览不写入数据库，刷新后可重新生成；独立发布接口的记录保存在 v10 数据库中。

重启 API 加载新代码后，可以在 `http://127.0.0.1:8000/docs` 调用该接口，或将下面的 `<task_id>` 替换为已完成交付的 GitHub 任务编号：

```bash
curl -sS -X POST "http://127.0.0.1:8000/tasks/<task_id>/github-delivery/preview" \
  -H 'Content-Type: application/json' \
  -d '{"base_branch":"main"}'
```

`base_branch` 必填，由用户明确选择目标分支；不会将旧的默认分支快照视为远端当前状态。可选 `title`、`body` 会替换默认 PR 文本，并逐字保留输入；标题限制 256 字符且为单行，正文限制 60000 字符，均不接受空白内容。`draft` 默认为 `true`。这些是预览请求的内容，尚未发送给 GitHub。目标仓库、修改分支和提交从冻结来源及交付记录读取，不接受调用者传入其他仓库、提交、凭证或补丁。

| 返回字段 | 含义 |
| --- | --- |
| `status: preview` / `publish_enabled: false` | 已生成离线预览，此接口不提供发布操作 |
| `remote_checks: not_checked` | 未检查远端分支、写权限、仓库现状或 PR 是否已存在 |
| `plan.repository` / `issue_url` | 任务保存的仓库身份及问题来源，不是实时 GitHub 查询结果 |
| `plan.base_branch` / `base_sha` | 用户选择的目标分支、已经验证的本地基线；两者在远端是否对应尚未核对 |
| `plan.head_branch` / `head_sha` / `tree_sha` | 已完成本地交付的准确分支、提交与文件树 |
| `plan.review_id` / `review_revision` / `evidence` | 此方案绑定的批准版本及原始证据 |
| `plan.changed_paths` / `verification` | 真实修改文件，以及已保存验证的命令、前后状态和退出码 |
| `plan.title` / `body` / `draft` | 可供用户检查和调整的 PR 内容 |
| `plan_sha256` / `checked_at` | 完整方案的稳定摘要和本次核对时间；时间不参与摘要 |

默认正文列出原始修复要求、Issue 链接、修改文件及已保存的验证依据，不额外拼入原 Issue 长正文、本机仓库路径或完整运行日志；要求和验证命令中的原有文本仍会保留，发布前应核对预览。文件超过 50 个时，正文只列前 50 个，`changed_paths` 仍保留完整列表。命令、要求和文件名使用不会被内容中反引号提前结束的 Markdown 代码块；复制或展示不会执行这些内容。默认标题由 Issue 编号和标题生成，超长时截断；自定义文本超限返回 422。

预览要求来源与仓库身份一致、最新审查有效且已接受、验证通过、对应本地交付完成。再次检查交付分支和工作区、提交的唯一父版本，以及“基线加批准补丁”产生的准确文件树，同时重新核对路径策略。Git 检查结束后再检查批准和交付状态；旧批准、已变更的产物或工作区不会被自动修复或重新交付。缺失任务返回 404，无法满足上述条件返回 409，请求格式错误返回 422。

重复请求在内容与证据不变时返回相同 `plan_sha256`，仅核对时间更新；修改目标分支、PR 文本、草稿选项或批准证据都会产生不同摘要。该摘要不是授权令牌。后续发布必须单独保存用户确认，并重新查询远端仓库身份、权限和目标分支 SHA；如与验证基线不符，应停止并重新验证，不能凭旧预览直接推送。

分支名称通过 [Git 分支名检查](https://git-scm.com/docs/git-check-ref-format)；目标、来源分支、标题、正文和草稿选项对应 [GitHub 创建 PR 接口](https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request)的输入。远端检查与发布使用下方独立接口；网页入口位于 GitHub 任务的“人工审查 → 05 GitHub 发布”，交互与恢复说明见 [前端文档](../frontend/README.md)。

## GitHub 分支与 PR 发布

首版支持来源对应的同一个公开仓库，需要当前账号对该仓库有写权限；不自动 fork、不换仓库、不合并 PR。离线预览和已保存结果的查询无需 GitHub 凭证。实际远端检查及发布使用服务端的 `PATCHLOOP_GITHUB_TOKEN`；`uv run agent serve` 优先读取环境变量，未设置时只采用当前项目 `.env` 中这一项，不将模型密钥加载到 API 的进程环境。请求体不接受 token，修改凭证后需重启 API。

真实验收应使用自己可写的测试仓库。可配置仅针对该仓库的 fine-grained personal access token，授予 Contents 与 Pull requests 的读写权限；修改 workflow 文件还涉及 Workflows 权限。权限依据见 [Git 引用写入](https://docs.github.com/en/rest/git/refs#create-a-reference)和 [PR 创建](https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request)。仓库返回的 `permissions.push` 表示账号仓库权限，不能提前证明 token 已拥有所有写权限；实际操作仍可能被 token 范围或仓库规则拒绝。发布账号身份通过 GitHub `/user` 核对，本版不支持 GitHub App installation token。

| 接口 | 用途及副作用 |
| --- | --- |
| `POST /tasks/{task_id}/github-delivery/preview` | 离线检查并生成方案，不保存发布记录 |
| `POST /tasks/{task_id}/github-delivery/check` | 相同预览参数，只读查询 GitHub 账号、仓库、分支及已有 PR，不推送或创建 PR |
| `POST /tasks/{task_id}/github-delivery` | 明确确认后登记发布意图、推送准确提交并创建 PR；同请求用于显式恢复 |
| `GET /tasks/{task_id}/github-delivery` | 读取持久化历史及凭证是否配置，不访问 GitHub，也不触发恢复 |

远端检查要求仓库 ID 和名称匹配冻结来源，仍为可写且未归档的公开仓库，目标分支 SHA 与验证基线一致。返回 `preview`、`actor`、`remote_base_sha`、`remote_head_sha`、`can_publish` 和 `reason`。首次发布须没有同名远端分支、相关 PR 或已有发布记录；已有记录应按原请求恢复。检查不会预留远端分支，正式发布时必须再核对。

发布请求格式如下，`preview` 保留刚核对的参数，`plan_sha256` 使用预览返回值，`request_id` 生成一次后保留：

```json
{
  "request_id": "<新的 UUID>",
  "preview": {"base_branch": "main", "draft": true},
  "plan_sha256": "<已核对预览的 SHA-256>",
  "confirmed": true
}
```

此 POST 会产生实际 GitHub 写操作。首次受理返回 201，同一请求或同一方案的已有记录返回 200；这两个状态只表示记录已创建或被复用，不能代替 `publication.status`。缺少明确布尔确认、参数格式错误为 422，旧预览、并发发布或请求冲突为 409。服务对每个数据库串行执行发布，同一请求编号不能改变内容，同一本地交付绑定一份不可改写的已确认方案。复用已有方案时返回首次确认的请求编号，后续恢复使用该记录中的 `submission`。

| `publication.status` | 含义 |
| --- | --- |
| `pending` | 意图或执行阶段已保存，可查询进度；中断后仍保留 |
| `blocked` | 凭证、权限、版本或已有内容不满足条件，保留已发生的操作 |
| `uncertain` | 写请求的结果尚不能确认，恢复时先查询远端 |
| `completed` | 已记录匹配方案的 PR，链接在 `pull_request.url`；不表示已合并或批准仍然有效 |

`phase` 区分 `check`、`push`、`pull_request`、`completed`；`push_attempted`、`branch_pushed`、`pr_attempted` 记录请求是否可能发出及是否核对到分支，`actor` 绑定实际发布账号。重试不能悄悄换账号或方案。`GET` 返回历史记录快照，不代表重新查询过远端 PR 的最新状态。

推送使用临时裸仓库副本和明确的提交 SHA。源仓库的 HEAD、索引、暂存内容、文件和 remote 配置不变，推送也不跟随调用者的 remote 配置。通过 [空期望值的 ref lease](https://git-scm.com/docs/git-push#Documentation/git-push.txt---force-with-leaserefnameexpect)原子地要求目标分支不存在；虽然 Git 选项名含 `force`，这里不允许覆盖或快进已有分支。临时凭证助手只在 GitHub 的 HTTPS 认证提示中提供 token，token 不写入 Git 配置、脚本内容、命令行、数据库或错误文本。

认证推送允许调用程序生成的 `GIT_ASKPASS`，同时保留 `GIT_TERMINAL_PROMPT=0` 并清空用户 credential helper，避免终端输入或使用用户的其他凭证。[Git 的 `credential.interactive=false`](https://git-scm.com/docs/git-config#Documentation/git-config.txt-credentialinteractive) 会连同 askpass 一起禁用，因此只在无认证助手的准备命令中使用；此区别由真实 `git credential fill` 的离线回归覆盖。

推送响应丢失后，可核对同名远端分支是否已经指向记录的提交；匹配时继续 PR 阶段，不再次推送。创建 PR 的请求发出前先保存标记。若响应丢失，重试只查询同一仓库和分支的 PR，并核对账号、提交、标题、正文和草稿选项；找到唯一匹配项后补记结果。若仍找不到，保持 `uncertain`，不会因为一次查询为空就再次 POST。需要继续查询或人工检查 GitHub，不能把“未查到”当作“未创建”。明确的 4xx 拒绝可在处理原因后重试。

推送后若批准或目标基线变化，会停止 PR 创建，保留已经发布的分支。GitHub 的目标分支可在独立网络请求之间变化，无法将本地批准与远端创建合为原子事务；如创建响应已显示基线变化，记录 PR 链接并标记 `blocked`，要求重新验证。远端已有的匹配 PR 可在恢复时补记，即使之后本地审查已更新；这只记录已发生的发布，不授权进一步写入。服务不删除分支、不改写现有 PR、不自动合并。

新版 API 和 worker 启动时数据库升级到 v10，新增 `github_publications` 表，保留旧表及修复、审查、交付证据。应一并重启旧 API/worker 加载新代码；旧版本程序不支持 v10。启动和 GET 查询不会自动恢复发布，必须重试原确认请求。实现阶段使用模拟 GitHub 和临时裸仓库验证，之后已完成下方自有仓库的真实发布验收。

真实验收使用专用公开仓库 [KLiuZC/patchloop-publish-demo](https://github.com/KLiuZC/patchloop-publish-demo)，未向 Flask 等上游仓库投递演示 PR。样例、Issue 文案、凭证配置、固定基线及网页操作见 [M4 验收配置](../evals/profiles/github-publish-demo/README.md)。用户已完成真实修复、审查和草稿 PR 发布，原始证据及逐项核对见 [验收归档](../evals/reports/github-pr-publish-demo-smoke/README.md)。这是专用小任务的工程验收，不作为新的 benchmark 分数。

## 后台执行任务

worker 支持 macOS/Linux，每个数据库只允许一个 worker，按创建时间串行领取任务。API 在任务运行时仍可响应查询。已有数据库会自动升级并保留记录；旧记录及未提供 `execution` 的新记录只保存、不执行，需要修复时重新提交一条带执行配置的请求。

启动 API，并确认 Docker 和示例镜像可用：

```bash
uv run agent serve
```

在另一个终端检查环境：

```bash
docker info >/dev/null
docker image inspect patchloop-toy --format '{{.Architecture}}'
```

worker 从项目 `.env` 读取模型密钥。API 请求中只传模型名称，不传密钥。通过调试页面 `POST /tasks` 或 curl 提交：

```bash
curl -sS -X POST http://127.0.0.1:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_path": "examples/toy_repo",
    "goal": "支持版本号字符串的 v 前缀，不改变现有版本号行为。不要修改 tests/ 目录。",
    "verification_command": "python -m pytest -q",
    "execution": {
      "image": "patchloop-toy",
      "provider": "deepseek",
      "max_model_calls": 10,
      "command_timeout_seconds": 60,
      "verification_timeout_seconds": 180,
      "budget_reminders": true
    }
  }'
```

复制返回的编号，首次验收只执行这一题：

```bash
uv run agent worker --task-id "<task_id>"
```

`<task_id>` 替换为返回的 UUID。worker 会在前台终端打印任务状态，API 在另一终端继续运行。`--once` 表示最多领取一题后退出；不加这两个选项时，worker 持续等待并执行带配置的新任务。worker 启动后会产生模型调用费用。

`execution` 会保存提供商、提交时解析出的模型名、镜像、轮数、命令/验证超时、内存及提醒开关。默认 30 轮、2GB 内存、提醒开启、finish 关闭；`model` 可覆盖默认模型。镜像必须已存在，worker 不自动下载或构建镜像；执行时固定本地镜像 ID，修复与验证共用该 ID。`platform` 默认按本地镜像架构选择，也可指定 `linux/amd64` 或 `linux/arm64`，不匹配会在创建模型客户端前报错。

持续使用 `GET /tasks/{task_id}` 查询：

| 字段 | 含义 |
| --- | --- |
| `status` | `queued` 排队、`running` 运行、`completed` 执行完成、`error` 执行出错、`interrupted` 未完成或被中断 |
| `validation_status` | `not_run` 尚未验证、`passed` 验证通过、`failed` 未修复、`inconclusive` 证据不足 |
| `started_at` / `finished_at` | 开始和结束时间 |
| `artifacts_dir` | 本地日志、补丁和报告目录 |
| `result` | RepairRunner 原始结果，包含停止原因、验证详情、用量和补丁 |
| `error` | 执行或中断原因 |
| `cleanup_pending` | 该任务的容器清理尚未确认完成，worker 会暂停领取下一题 |

`completed` 可以同时对应 `validation_status=failed`；模型执行结束不能直接视为修复成功。达到轮数上限仍会验证已有补丁，执行状态为 `interrupted`。worker 命令正常退出表示本轮处理已结束，任务结果以查询字段为准。

产物默认保存在数据库同级的 `task-runs/<task_id>/`，可通过 worker 的 `--artifacts` 修改。API 和 worker 必须使用同一个 `--db` 文件。还可以通过 `GET /tasks/{task_id}/artifacts/{name}` 下载 `patch.diff`、`events.jsonl`、`trajectory.jsonl`、`task.json`、`summary.json`、`validation.json`、`runtime.json` 或 `worker-error.json`；未生成的产物返回 404。`runtime.json` 记录实际镜像 ID、架构和本题容器名。

Ctrl+C 或 SIGTERM 会停止 worker，并在释放修复工作区前尽可能保存已有补丁、用量和中断摘要。恢复时先核对已归档的任务、补丁哈希和验证报告；如果终态已归档但数据库未回写，就恢复该结果，否则标记为 `interrupted`。若进程被强杀，下一次 worker 启动时执行相同检查；未落盘的日志、补丁或报告无法保证恢复。

worker 会清理该任务带标签的容器；清理失败时保留 `cleanup_pending=true` 并停止，下一次启动先重试清理，完成后才领取新题。恢复不会自动重新答题，API 重启也不会改变正在运行的任务。当前没有自动续跑或整个任务的总时限。

已完成一次真实 DeepSeek 示例任务验收：接口提交后由 worker 在 Docker 中修复，基线 1 项失败、候选 2 项全部通过，4 轮结束，RepairRunner 耗时 8.1 秒。数据库与产物核对一致，任务容器已清理。详细配置、时间口径、版本记录和原始证据见 [worker 真实验收归档](../evals/reports/task-worker-toy-smoke/README.md)；这是单任务工程验收，与 SWE-bench 成绩分开记录。
