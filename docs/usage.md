# 使用指南

[返回项目首页](../README.md) · [API 参考](api.md) · [评测与验收](evaluation.md)

下列命令均在项目根目录执行。

## 目录

- [模型与凭证](#模型与凭证)
- [网页任务流程](#网页任务流程)
- [状态与常见问题](#状态与常见问题)
- [独立修复与验证](#独立修复与验证)
- [直接执行 Agent](#直接执行-agent)
- [在 Docker 中直接执行](#在-docker-中直接执行)

## 模型与凭证

安装依赖、准备示例镜像和启动服务见[快速开始](../README.md#快速开始)。项目通过兼容 OpenAI 的接口接入模型，默认配置如下：

| 提供商 | 配置标识 | 密钥环境变量 | 默认模型 |
| --- | --- | --- | --- |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-flash` |
| GLM | `glm` | `GLM_API_KEY` | `glm-5.3-flash` |

可在网页或 API 的 `execution.model` 中指定模型；命令行使用 `--model` 覆盖默认值。可用模型及其权限以提供商账号配置为准。模型费用根据代码中的价格表和接口返回的用量估算，实际结算以提供商账单为准。

将配置写入项目根目录的 `.env`，或通过启动进程的环境变量提供。模型密钥由 worker 与修复 CLI 加载，网页请求只包含提供商和模型名称。`HF_TOKEN` 是下载评测数据时的可选配置，独立于模型凭证。

GitHub 远端检查与发布使用服务端的 `PATCHLOOP_GITHUB_TOKEN`。建议按目标仓库配置 Contents、Pull requests 读写权限，当前发布支持个人账号 Token；离线预览和读取已保存记录不需要此凭证。修改配置后重启对应服务。权限与恢复规则见 [GitHub 发布接口](api.md#github-分支与-pr-发布)。

本地分支交付会创建 Git commit，需要配置实际使用者的 Git 作者信息。模型修复、独立验证、人工审查与分支交付是分开的操作。

## 网页任务流程

### 本地仓库任务

1. 在“新建任务”中填写 Git 仓库目录、修复目标、验证命令和 Docker 镜像，选择已配置凭证的模型。
2. 根据任务范围设置允许与禁止修改的路径；默认保护常见测试文件。
3. 提交后打开任务详情，查看执行日志、补丁、修复前后的验证结果和用量。
4. 在“人工审查”中核对具体补丁和证据，提交接受或拒绝决定。
5. 接受后单独执行“生成修改分支”，查看对应的提交和独立工作区。

仓库路径相对于 API 启动目录，也支持服务器上的绝对路径。提交时固定 Git commit，未提交的工作区改动不会进入修复基线。建议先使用首页示例熟悉流程，再准备目标仓库的镜像与验证配置。

### GitHub Issue 任务

1. 在“GitHub Issue”页面输入公开 Issue 链接，读取标题和正文快照。
2. 准备代码，选择分支、标签或完整 commit SHA；成功后固定该版本。
3. 确认修复目标、运行镜像、验证命令及路径范围，创建任务。
4. 完成修复、独立验证、人工审查和本地分支交付。
5. 在“GitHub 发布”中生成方案，核对目标分支与 PR 内容，执行远端检查，再确认发布。

Issue 创建时间不等于代码基线时间；需要复现旧版本问题时，应选择对应的 commit。仓库准备只下载代码，依赖由预先构建的运行镜像提供。

真实操作示例见[公开文档任务](../evals/profiles/github-flask-6146/README.md)和[自有仓库 PR 交付配置](../evals/profiles/github-publish-demo/README.md)。前者已验收修复和本地交付，后者已验收草稿 PR 发布。向 GitHub 发布时应使用有写权限且明确用于该任务的仓库。

### 验证命令

验证命令用于复现目标问题，并检查候选修改。命令由用户确认，原样用于干净基线与候选环境。例如 `python -m pytest -q` 运行当前工作目录中的测试；也可使用针对目标行为编写的检查脚本。

命令需要在选定镜像中可执行，且覆盖待修复行为。基线未能明确复现失败时，结果为 `inconclusive`；候选通过现有检查也不代表所有行为都正确。详细判定约定见[独立修复与验证](#独立修复与验证)。

## 状态与常见问题

| 情况 | 检查方式 |
| --- | --- |
| 任务一直排队 | 确认 worker 正在运行、API 与 worker 使用同一数据库，并且任务包含 `execution` 配置 |
| 执行完成但验证失败 | 查看修复前后的测试输出；`completed` 只表示模型执行结束 |
| 验证结果为 `inconclusive` | 检查基线是否复现问题，以及依赖、测试收集、命令退出码是否正常 |
| 达到轮数上限 | 执行状态为 `interrupted`，已有补丁仍可进入独立验证 |
| 镜像不存在或架构不匹配 | 在本机准备镜像，检查配置中的镜像名称及 `platform`；worker 不自动拉取镜像 |
| 关闭网页后任务继续运行 | 网页与 worker 独立，关闭页面不会取消任务 |
| 容器清理待完成 | 检查 Docker，重新启动 worker 重试清理；未清理完成前不领取下一题 |
| 发布请求结果不明确 | 查询已保存记录，使用页面中的原请求恢复，避免重新创建发布意图 |

默认数据保存在 `data/`。自定义路径时，API 与 worker 都需要传入同一个 `--db`；详见[数据目录与恢复](architecture.md#数据目录与恢复)。

## 独立修复与验证

`agent repair` 会在固定 `base_sha` 的临时独立 Git 克隆中运行 Agent，收集候选补丁，然后从同一个干净基线重新应用补丁并运行固定验证命令。验证结果不会使用 Agent 工作区中的测试状态。

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
  --sandbox docker --image patchloop-toy \
  --provider deepseek \
  --artifacts /tmp/patchloop-repair-runs/toy-version-parser-docker
```

一次修复会产生 `task.json`、`events.jsonl`、`trajectory/`、`patch.diff`、`validation.json` 和 `summary.json`。验证器先在一个干净克隆中运行基线命令，再在第二个干净克隆中应用补丁并运行候选命令，避免测试缓存或生成文件污染结果。`validation.json` 同时保存 `baseline` 和 `candidate` 两次结果；如果基线本来就通过，最终状态会是 `inconclusive`，候选命令自身仍可记录为 `passed`。`execution_status` 表示 Agent 是否完成执行，`validation_status` 表示是否有证据表明补丁修复了复现的问题，`review_status` 表示人工审查状态。Agent 调用 `finish` 或回复文字都不能直接把验证状态变成通过。

可见验证命令使用退出码约定：0 为通过、1 为断言失败，其他码（例如 pytest 未收集到测试、用法错误、超时或命令不存在）为 `inconclusive`。验证器读取实际进程退出码，不解析日志中的文本标记。自定义脚本应遵守此约定，并保留测试命令的退出码；基线未明确失败时，整体结果不能是修复成功。

两种修复入口均拒绝复用非空产物目录。普通模型或工具异常会尽可能在清理沙箱前保存已有补丁、已收到的用量和轨迹终态，并写入验证报告及最终摘要；验证环境启动失败也会留档，已完成的基线结果会保留。用量仅包含服务商已返回的数据，失败请求的实际账单仍以服务商记录为准。

当前面向单用户串行运行，限制模型调用次数和单条命令时长；尚未实现整个任务的总时限、强制终止后的完整留档或重启续跑。

## 直接执行 Agent

`agent run` 在指定目录中直接执行工具命令，适合调试工具循环。与 `agent repair` 不同，这个入口不会自动创建独立工作副本，也不会执行修复结束后的基线/候选独立验证。

```bash
uv run agent run --workdir examples/toy_repo --provider deepseek \
  --task "运行 pytest，修复失败的测试。不要修改 tests/ 目录。"
```

该命令默认在本机执行并修改所选目录；应使用专门的工作副本。轨迹写入 `traj/<run_id>.jsonl`，补丁写入相应的 `.patch` 文件。执行完成后通过 Git 检查实际修改。

## 在 Docker 中直接执行

`agent run --sandbox docker` 将工具命令放在容器中执行。`--workdir` 指定的目录会以可写方式挂载到容器的 `/work`，修改会反映到该目录；不给 `--workdir` 时直接使用镜像自带的仓库（SWE-bench 镜像通常位于 `/testbed`）。

```bash
docker build -t patchloop-toy examples/toy_image        # 带 git 和 pytest 的示例镜像，只需一次
uv run agent run --sandbox docker --image patchloop-toy -w examples/toy_repo \
  -t "运行 pytest，修复失败的测试。不要改动 tests/ 目录。"

# 直接在 SWE-bench 的题目镜像里工作（无挂载，代码在 /testbed）
uv run agent run --sandbox docker --image swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-7205:latest \
  --platform linux/amd64 -t "..."
```

容器默认断网、限制 2GB 内存，任务结束后清理。命令通过容器内的 `timeout` 和登录 shell 执行，镜像中的 shell 初始化配置决定环境激活方式。运行结束时收集 Git 可识别的修改，写入 `traj/<run_id>.patch`。

示例镜像按构建主机架构生成；使用 x86_64 SWE-bench 镜像时指定 `--platform linux/amd64`。批量评测、单题修复和官方判卷见[评测与验收](evaluation.md)。
