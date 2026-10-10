# 前端交互约定

[前端开发指南](../frontend/README.md) · [使用指南](usage.md) · [API 参考](api.md)

本文记录影响请求一致性、证据核对和内容展示的前端行为。接口字段与服务端判定以 API 参考为准，具体操作步骤见使用指南；样式参数由组件与样式文件维护。

## 目录

- [状态与数据来源](#状态与数据来源)
- [请求与恢复](#请求与恢复)
- [会话存储](#会话存储)
- [审查与交付](#审查与交付)
- [内容展示](#内容展示)
- [交互与可访问性](#交互与可访问性)
- [实现与测试索引](#实现与测试索引)

## 状态与数据来源

- 执行完成、验证通过、人工接受和交付完成分别展示，不通过其中一个状态推断其他阶段成功。
- 当前审查从独立审查接口读取；任务 `result.review_status` 保留修复结束时的快照，不作为最新决定。
- GitHub 任务展示冻结的 Issue 来源、确认后的目标和代码基线。来源与任务不匹配时提示错误，不用新的 GitHub 内容替换旧记录。
- 审查中的任务要求来自已核对的任务与档案；Issue 正文单独展示，不通过字符串标记截断模型上下文来推导用户要求。
- 运行期间轮询状态和日志，进入终态后重新读取最终轨迹。不可解析或截断内容保留提示及原始文件入口。
- 费用使用后端估算值，未知时显示 `—`。GitHub 发布记录来自保存结果，不表示页面已经重新查询远端 PR 的当前状态。

## 请求与恢复

### 查询与显式操作

列表、状态和产物使用查询请求。进入页面、刷新、轮询或恢复会话时，只读取已有记录，不自动提交任务、接受补丁、生成分支或发布 PR。

预览和远端检查虽不发布内容，仍由用户显式触发。写请求不自动重试；查询请求按查询客户端或具体流程配置重试。查询与写请求均允许在浏览器报告离线时尝试访问本机 API，避免将写操作暂存到恢复联网后自动发送。

表单校验失败时定位对应字段；提交期间禁用重复操作。路径和验证命令保留有效空格，模型与镜像名称遵循接口约束。“填入示例”只修改表单，实际提交由提交按钮触发。

### 结果不明确时的处理

| 流程 | 核对与恢复规则 |
| --- | --- |
| 普通本地任务 | 创建接口每次提交生成新任务；结果未知时先检查列表，不自动再次提交 |
| Issue 导入、仓库准备、关联任务 | 保留阶段、请求编号和完整内容；显式重试使用原编号，确认成功后进入下一阶段 |
| 人工审查 | 保留请求与证据，通过状态和历史核对结果，再由用户选择是否重试原提交 |
| 本地交付 | 先查询已有记录；匹配的进行中交付提供显式恢复，不自动发起第二次交付 |
| GitHub 发布 | 沿用未确认请求或服务端保存的 `submission`；恢复先核对已有记录，再按后端规则继续 |

界面不得将所有失败都解释为“没有发生操作”。返回值无效、连接中断或响应丢失时，也需要按结果未知处理。完整接口语义见 [API 参考](api.md)。

### 迟到响应与页面离开

- 新决定、完成记录或更晚执行检查点已经可见时，迟到响应不能将状态回退到旧版本或进行中。
- 审查证据被观察为过期后，同一版本的旧响应不能恢复批准状态；需要用户明确重新加载资料。
- 用户离开页面时，仍在进行的审查、交付或发布前置检查不得在结束后继续发起写请求。
- 已经发送的请求可能在服务端完成；重新进入页面后查询结果，关闭页面不视为取消操作。

## 会话存储

会话草稿用于当前标签页的恢复，不替代服务端记录，也不提供跨标签页同步保证。

| 内容 | 保存方式 | 存储不可用时 |
| --- | --- | --- |
| Issue 流程、配置与待确认请求 | `sessionStorage`，保留内存副本 | 新请求发送前必须成功保存并回读，否则停止发送并提示 |
| 尚未提交的审查意见 | 按任务保存到当前 QueryClient 缓存 | 可在页面切换中保留，不承诺刷新后恢复 |
| 待确认的审查请求 | `sessionStorage` 与内存副本 | 可在当前页面生命周期内核对及重试，刷新恢复不保证 |
| 待确认的本地交付请求 | `sessionStorage` 与内存副本 | 保留内存请求，后续也可查询服务端交付记录 |
| PR 编辑草稿 | 按任务保存到 `sessionStorage` | 可继续编辑当前页面内容 |
| 待确认的 PR 发布请求 | 发送前写入并回读 `sessionStorage` | 保存失败则不发送发布请求 |

清除待确认请求时需匹配请求编号，避免迟到响应删除另一条请求。损坏的会话内容不能静默转换为新的写请求。服务端已有匹配记录时，以该记录中保存的原始参数核对和恢复。

## 审查与交付

### Issue 来源与代码准备

Issue 导入后保存标题和正文快照。编辑来源只修改 Issue 链接；取消编辑保留当前准备状态，重新读取成功后清除页面对旧准备结果的关联。旧服务端来源与准备记录继续保留。

仓库准备成功且当前 checkout 完整时才能创建关联任务。准备失败、目录变化或切换到尚未准备的版本时禁用创建。镜像默认留空，必须明确配置；路径策略相对于仓库根目录。

加载、失败与未确认请求显示在对应操作区域。恢复会话只查询旧记录；新流程不取消服务端已有操作，已创建的任务提供详情入口。

### 人工审查

1. 加载审查状态及任务配置、运行摘要、补丁和验证报告，核对内容摘要与关联关系，再次读取审查状态以确认版本一致。
2. 用户查看和编辑意见期间固定这组证据；下载补丁使用已核对的内容。
3. 提交前重新检查版本和证据。发生变化时保留意见并要求重新加载，不自动采用新补丁提交决定。
4. 资料缺失、不一致或状态读取失败时禁用对应操作；接受和拒绝遵循服务端返回的能力标志。

最新决定与历史记录分别展示。接受或拒绝只保存决定，不执行本地交付或 GitHub 发布。详情中的流程为任务要求、检查修改、审查决定、本地交付；GitHub 来源任务另提供发布区域。

### 本地交付

生成分支前核对批准版本、证据、目标 HEAD 和交付计划。已完成交付展示对应分支、提交与工作区；进行中的交付保留恢复入口，已有文件变化时显示限制而不覆盖。

新审查版本出现且旧待确认请求没有匹配的进行中记录时，可解除旧请求对新交付的阻塞。匹配的进行中记录仍需显式恢复；仅补丁哈希或 HEAD 变化不足以清除未确认请求。

历史交付恢复与创建新交付分开处理：前者用于核对已经发生的输出，后者仍要求当前批准有效。状态读取失败时不得继续采用此前计划。

### GitHub 发布

发布方案必须与当前批准、本地交付及冻结来源对应。前端核对规范 JSON 的方案摘要、关联提交和文件列表；分支、文本、PR 类型或证据变化后清除旧预览、检查结果和确认。

用户确认后，发送前重新查询发布记录并核对远端状态。请求包含明确确认及原方案摘要；网页不接收 Token。缺少后端凭证时仍可预览和查询已保存记录，但不能执行需要凭证的操作。

已保存记录通过原始 `submission` 恢复，不能更换账号、参数或发布内容。PR 创建请求已经发出但结果未知时，后端按约定查询匹配结果；前端不能因一次查询为空就重建请求。明确失败并核对无匹配记录后，才解除相应待确认请求并返回编辑。

旧批准对应的记录作为历史展示。PR 已存在但基线变化时，保留链接和待处理状态，不将其展示为当前补丁已重新验证。发布后端的详细恢复边界见 [GitHub 发布接口](api.md#github-分支与-pr-发布)。

## 内容展示

| 内容 | 展示与复制约定 |
| --- | --- |
| Issue 与已确认 PR 正文 | 共用 Markdown 组件，支持原文切换；复制保留原始 Markdown，查看不修改来源或发布记录 |
| Markdown 链接与附件 | 忽略原始 HTML，过滤危险协议与带凭证链接；相对地址按关联 Issue／PR 解析，外链新标签打开，图片显示为附件链接而不自动加载 |
| 日志、错误和审查意见 | 以文本展示，不将内容作为 HTML；模型推理字段不直接展示在逐轮日志中 |
| 代码补丁 | 展示准确 diff；长内容允许滚动，保留原始补丁下载 |
| 验证命令 | 在受支持的语法范围内静态生成预览；复制始终返回原始命令，不包含行号或展示格式 |
| 长路径与标识 | 可缩略展示或折叠，复制保留完整值 |

Markdown 渲染与代码高亮按需加载，加载失败时回退到纯文本。Markdown 标题在页面内使用适当的标题层级，避免与页面主标题混淆。

验证命令预览只静态解析明确支持的 `python -c` 字面量参数；对于仅包含 `exec(<普通字符串字面量>)` 的脚本可还原字符串换行。变量展开、管道或不支持的格式保留原文，额外 Python 语句继续展示。此过程不执行代码，也不证明命令语法或行为正确。

命令卡片折叠时限制显示高度；“展开全部”后取消内部纵向滚动限制，鼠标位于代码区域时页面仍可上下滚动。查看、格式化、展开与复制都不改变验证配置、产物或证据摘要。

## 交互与可访问性

- 页面标题、表单分组和状态反馈使用明确标签，加载及错误信息靠近对应操作。
- 表单字段关联提示与错误信息；提交校验失败时展开相关区域并聚焦错误字段。
- 共享选择框支持方向键、Enter、Escape 和焦点返回；自动架构和全部状态的空值仅在组件内适配，不改变 API 参数含义。
- 详情页签支持键盘切换；从概览跳转到代码、报告或审查时同步焦点。
- 窄屏调整排列方式，长代码与表格在内容区域内滚动；支持减少动态效果、减少透明度及增强对比度偏好。

## 实现与测试索引

| 范围 | 实现 | 相关测试 |
| --- | --- | --- |
| 查询与普通任务 | [api.js](../frontend/src/lib/api.js)、[query-client.js](../frontend/src/lib/query-client.js)、[NewTask.jsx](../frontend/src/pages/NewTask.jsx) | [api.test.js](../frontend/src/lib/api.test.js)、[workbench.test.jsx](../frontend/src/test/workbench.test.jsx) |
| Issue 流程 | [GithubTask.jsx](../frontend/src/pages/GithubTask.jsx)、[github-draft.js](../frontend/src/lib/github-draft.js) | [github-workflow.test.jsx](../frontend/src/test/github-workflow.test.jsx) |
| 人工审查 | [ReviewPanel.jsx](../frontend/src/components/ReviewPanel.jsx)、[reviews.js](../frontend/src/lib/reviews.js)、[review-draft.js](../frontend/src/lib/review-draft.js) | [review-panel.test.jsx](../frontend/src/test/review-panel.test.jsx)、[reviews.test.js](../frontend/src/lib/reviews.test.js) |
| 本地交付 | [DeliveryPanel.jsx](../frontend/src/components/DeliveryPanel.jsx)、[delivery.js](../frontend/src/lib/delivery.js) | [delivery-panel.test.jsx](../frontend/src/test/delivery-panel.test.jsx) |
| GitHub 发布 | [GitHubPublicationPanel.jsx](../frontend/src/components/GitHubPublicationPanel.jsx)、[publications.js](../frontend/src/lib/publications.js)、[publication-draft.js](../frontend/src/lib/publication-draft.js) | [publication-panel.test.jsx](../frontend/src/test/publication-panel.test.jsx)、[publications.test.js](../frontend/src/lib/publications.test.js) |
| Markdown | [MarkdownBody.jsx](../frontend/src/components/MarkdownBody.jsx)、[MarkdownRenderer.jsx](../frontend/src/components/MarkdownRenderer.jsx) | [IssueBody.test.jsx](../frontend/src/components/IssueBody.test.jsx) |
| 命令预览 | [VerificationCommand.jsx](../frontend/src/components/VerificationCommand.jsx)、[verification-command.js](../frontend/src/lib/verification-command.js) | [VerificationCommand.test.jsx](../frontend/src/components/VerificationCommand.test.jsx)、[verification-command.test.js](../frontend/src/lib/verification-command.test.js) |
| 选择框与补丁 | [Select.jsx](../frontend/src/components/Select.jsx)、[DiffViewer.jsx](../frontend/src/components/DiffViewer.jsx) | [Select.test.jsx](../frontend/src/components/Select.test.jsx)、[DiffViewer.test.jsx](../frontend/src/components/DiffViewer.test.jsx) |
