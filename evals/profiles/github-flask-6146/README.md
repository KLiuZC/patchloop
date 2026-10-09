# GitHub Issue #6146：网页真实验收配置

目标是验证“网页导入 → 固定代码 → 创建任务 → worker 修改 → 独立验证 → 人工审查 → 本地交付”。这是文档修改的工程验收，不是 SWE-bench 跑分，也不是 Flask 运行时功能测试。

问题来源：[pallets/flask#6146](https://github.com/pallets/flask/issues/6146)。要求在部署文档的 Hosting Platforms 部分加入 Cloudflare 的 Flask 部署指南链接。此前这个 Issue 仅用于导入、下载联调；本配置为它增加了明确的文档验收标准。

## 运行前检查

在 patchloop 项目中先提交本次验收配置，使实际运行可对应到提交版本。确认后端已重启加载当前功能，Docker 已运行。

网页使用 `/tasks/github`。已读入这个 Issue 时可复用当前来源；“代码版本”填写下列完整 SHA 并准备，避免未来默认分支更新使问题已经被解决：

```text
d086db856be187255b8ec61ef409357393020f32
```

提交任务前确认页面显示“代码已就绪”，固定提交与上方相同。镜像内容可通过 `docker image inspect patchloop-toy` 核对，预检所用 image ID 和脚本哈希见 `preflight.json`；标签后来被重建时需重新检查环境。

## 网页填写

| 字段 | 值 |
| --- | --- |
| Issue 链接 | `https://github.com/pallets/flask/issues/6146` |
| 工作目录 | `.` |
| 模型提供方 | DeepSeek |
| 模型名称 | `deepseek-flash` |
| Docker 镜像 | `patchloop-toy` |
| 最大模型轮数 | `10` |
| 允许修改的路径 | `docs/deploying/index.rst` |
| 禁止修改的路径 | 保留默认测试保护 |
| 轮数提醒 / finish | 开启 / 关闭 |
| 单条命令 / 验证超时 | `60` / `180` 秒 |

修复要求：

```text
根据此 Issue，在 docs/deploying/index.rst 的 Hosting Platforms 列表中加入一个 Cloudflare Workers 链接，指向 https://developers.cloudflare.com/workers/languages/python/packages/flask/ 。沿用现有 reStructuredText 内联链接和列表风格，保留所有已有平台链接与 Self-Hosted Options 内容。只修改该文档，不修改源码或测试，不安装依赖，不访问网络。此任务为文档修改，用 Python 标准库检查文档内容，不运行 Flask 的应用测试套件。
```

`verify.sh` 是可读的原始验证脚本；网页“验证命令”为单行输入框，需要使用由它生成的单行版本 `verification_command.txt`，避免粘贴多行时丢失换行。在 macOS 的 patchloop 项目目录执行以下命令复制，再粘贴到网页“验证命令”：

```bash
pbcopy < evals/profiles/github-flask-6146/verification_command.txt
```

命令以内嵌 Python 执行三个离线检查，不需要将 profile 文件放入目标仓库。`patchloop-toy` 在本任务中仅提供 Python 和 Git，不负责运行 Flask，因此无需安装 Flask 的运行依赖。

创建后复制任务编号，由用户运行：

```bash
uv run agent worker --task-id "<本次返回的任务编号>"
```

这一步会调用模型。预检没有调用模型，也没有代用户创建或接受任务。10 轮限制的是模型调用次数，不是费用上限。

## 判定与范围

预检原始版本应为三项检查中一项失败：缺少 Cloudflare 链接；已存在的平台链接和自托管目录均通过。候选补丁需要让三项检查全部通过，并满足文件路径策略。

检查使用与当前文档一致的内联列表链接格式，不支持所有等价 reStructuredText 写法；要求中已经明确这一格式。它检查目标、位置、标签和已有导航内容，不验证外部网页在线状态、不执行 Sphinx 文档构建，也不证明 Cloudflare 的部署行为。

页面通过后，还应人工查看 diff：只在指定列表中加入链接、没有删除或改写无关说明，再决定是否接受与交付。生成的分支保留在本地，不向 Flask 仓库发布内容。

`preflight.json` 记录基线与人为构造的正反例检查。这些正反例只验证验收程序，不是 Agent 产物；后续真实修复和交付已完成，见 [M3 验收归档](../../reports/github-issue-flask6146-smoke/README.md)。
