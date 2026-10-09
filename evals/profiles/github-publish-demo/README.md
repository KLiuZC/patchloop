# M4：自有仓库的真实 PR 发布验收

目标是完整验证“GitHub Issue → 固定版本修复 → 独立测试 → 人工接受 → 本地交付 → 草稿 PR”。这是一个已知小问题的工程验收，不作为 SWE-bench 或泛化成功率统计。

本配置已于 2026-10-09 完成真实验收：实际来源为 [Issue #1](https://github.com/KLiuZC/patchloop-publish-demo/issues/1)，任务为 `3659f4fc-1eea-48cf-a744-b2bbebee4654`，结果为[草稿 PR #2](https://github.com/KLiuZC/patchloop-publish-demo/pull/2)。完整证据、认证恢复经过及实现版本限制见 [M4 归档](../../reports/github-pr-publish-demo-smoke/README.md)。下文保留运行前的配置与操作步骤供复现；`preflight.json` 和 `repository.json` 保持准备时的快照，后者的待验收字段不代表当前状态。

## 仓库与基线

- 公开测试仓库：[KLiuZC/patchloop-publish-demo](https://github.com/KLiuZC/patchloop-publish-demo)。仅上传了示例目录的 6 个文件，Issues 已启用。
- 本地初始化副本：`/Users/jackliu/code/patchloop-publish-demo`，与 patchloop 主仓库独立。
- 目标分支：`main`。
- 固定基线：`05c8b8fd1a952b65a336976d0b3a42498899a718`。
- 样例来源：[examples/publish_demo](../../../examples/publish_demo)。`calc.py` 故意保留前缀解析问题，初始测试为 `1 failed, 10 passed`。

本目录的预检资料和配置先提交到 patchloop，再进行模型验收，以便记录运行时的实现版本。验收期间不在测试仓库的 `main` 上提交其他修改，也不合并最终草稿 PR；发布后端会检查目标分支仍等于验证基线。

## 先创建 Issue

在测试仓库的 [New issue](https://github.com/KLiuZC/patchloop-publish-demo/issues/new) 页面填入：

- 标题：[issue-title.txt](issue-title.txt)。
- 正文：[issue-body.md](issue-body.md)。

可以在 patchloop 目录依次执行以下命令复制，粘贴到对应字段后手动提交：

```bash
pbcopy < evals/profiles/github-publish-demo/issue-title.txt
pbcopy < evals/profiles/github-publish-demo/issue-body.md
```

两条命令之间应先粘贴标题；第二条会替换剪贴板内容。也可由用户在终端执行这一条创建 Issue：

```bash
gh issue create --repo KLiuZC/patchloop-publish-demo \
  --title "parse_version 无法解析带单个小写 v 前缀的版本号" \
  --body-file evals/profiles/github-publish-demo/issue-body.md
```

记录 GitHub 实际返回的 Issue 链接，导入时使用该链接，不假定 Issue 编号。

## 配置发布凭证

打开 [fine-grained token 创建页](https://github.com/settings/personal-access-tokens/new?name=patchloop-publish-demo&target_name=KLiuZC&expires_in=7&contents=write&pull_requests=write)，检查以下设置：

| 项目 | 值 |
| --- | --- |
| Resource owner | `KLiuZC` |
| Repository access | `Only select repositories`，只选择 `patchloop-publish-demo` |
| Contents | `Read and write` |
| Pull requests | `Read and write` |
| Metadata | 保留自动提供的只读权限 |
| Expiration | 本次演示建议 7 天 |

这个例子不修改 workflow 文件，也不需要通过发布凭证创建 Issue，因此不额外申请 Workflows 或 Issues 写权限。GitHub CLI 的登录只用于仓库初始化，应用发布使用上述单独凭证。权限与仓库限制说明见 [GitHub Token 文档](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)。

由用户在 patchloop 的 `.env` 中新增或修改这一项，保留已有模型配置：

```dotenv
PATCHLOOP_GITHUB_TOKEN=<新生成的 token>
```

token 不放入测试仓库、Issue、任务描述、截图或验收记录。修改后在原后端终端按 Ctrl+C，再启动：

```bash
uv run agent serve
```

网页“尚未配置 GitHub 发布凭证”提示消失只说明已读取配置；账号、仓库权限及分支版本仍需在发布前通过“检查远端”核对。

## 网页创建修复任务

打开 `http://127.0.0.1:5173/tasks/github`，导入上一步创建的 Issue。准备代码时填写上述完整基线 SHA，确认“代码已就绪”。

| 字段 | 值 |
| --- | --- |
| 修复要求 | [goal.txt](goal.txt) 的完整内容 |
| 工作目录 | `.` |
| 验证命令 | `python -m pytest -q` |
| 提供方 / 模型 | `deepseek` / `deepseek-flash` |
| Docker 镜像 | `patchloop-toy` |
| 平台 | 按镜像自动选择；已预检为 `linux/arm64` |
| 最大模型轮数 | `10` |
| 内存 / 单条命令超时 / 验证超时 | `2048 MB` / `60 秒` / `180 秒` |
| 允许修改的路径 | `calc.py` |
| 禁止修改的路径 | 保留默认测试保护 |
| 轮数提醒 / finish | 开启 / 关闭 |

确认提交后，记录任务编号，由用户手动启动该任务：

```bash
uv run agent worker --task-id "<本次任务编号>"
```

这一步会调用模型；此前仓库准备和本目录预检都没有模型调用。不要同时启动一个无任务编号的 worker 去领取其他历史排队任务。

## 审查与发布

1. 任务应返回 `completed/passed`。独立验证应为修复前 `1 failed, 10 passed`，修复后 `11 passed`。
2. 在“人工审查”查看 diff，只允许 `calc.py` 改动，确认没有扩大到大写 `V` 或多个 `v`，再接受补丁。
3. 点击“生成修改分支”，确认本地交付完成且完整，记录分支与提交 SHA。
4. 在“05 GitHub 发布”保留目标 `main`、类型“草稿 PR”，生成预览，核对仓库为 `KLiuZC/patchloop-publish-demo`。可编辑 PR 标题和正文，编辑后重新生成预览。
5. 点击“检查远端”，确认发布账号、目标分支及检查结果。勾选明确确认后，再点击“确认并发布 PR”。这一步会真实推送分支并创建 GitHub PR。
6. 打开返回的 PR，核对目标是 `main`、只有批准的文件修改、head 提交等于本地交付提交。保持草稿状态，先不合并。
7. 刷新详情页并重新进入人工审查，确认仍显示同一个 PR。通过原请求恢复时，只能关联原有发布记录，不应产生第二个分支或 PR。

最终归档应保存任务、批准、本地交付和发布记录，以及实际 PR 链接、目标/来源 SHA、补丁与验证哈希；不保存 token。PR 创建成功只代表交付完成，不等于代码已经合入 `main`。

## 预检结果

[preflight.json](preflight.json) 固定了样例、Issue 文案、修复要求、命令及镜像的哈希。使用已有 `patchloop-toy`，没有构建新镜像；容器断网，样例只读挂载，正反例在容器临时目录中运行，原始文件不变。

| 检查对象 | 预期结果 |
| --- | --- |
| 初始代码 | `1 failed, 10 passed` |
| 人为构造的单个小写前缀修复 | `11 passed` |
| 错误地去掉多个 `v` | `1 failed, 10 passed` |
| 错误地同时支持大写 `V` | `1 failed, 10 passed` |

这些正反例只验证测试能区分要求内外的修改，不是 Agent 修复成绩。准备阶段没有创建修复任务、运行模型、接受补丁或发布 PR。真实 Issue 与 PR 的编号应在实际操作后记录。
