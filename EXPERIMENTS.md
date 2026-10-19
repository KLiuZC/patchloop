# 实验索引

[项目首页](README.md) · [评测方法与完整结果](docs/evaluation.md) · [发展路线](ROADMAP.md)

本文按实验目的提供结果入口。原始配置、逐次运行、Token、费用、版本限制和失败归因保留在对应报告中；2026-09-26 至 2026-10-09 的完整台账见[历史实验记录](docs/history/experiments-through-2026-10-09.md)。

## 阅读约定

- 历史 xarray 轨迹存在读取新版源码的环境污染，涉及基线、提醒、finish 和 GLM 组，见[访问清单与隔离验收](evals/reports/environment-isolation/README.md)。2026-10-13～14 已完成三题隔离后的 10 遍、30 次求解，结果见[复验报告](evals/reports/xarray3-isolation/README.md)；其余 27 题未在新环境重跑，历史答卷和判卷保持原样。
- 修复能力评测与产品流程验收分别统计；预检、脚本化模型测试和只重判已有补丁不计作新的模型修复成绩。
- `smoke5` 与 `verified30` 均已用于开发。30 题重复实验不代表 Verified 全集，也不构成未见任务的泛化结论。
- 官方分母、失败记录和原始结论保留。人工补齐依赖或诊断不回填正式分数，后续更正通过报告说明。
- 费用为记录时的价格表估算，实际账单以提供商为准；轮次、任务耗时之和、墙钟耗时与人工等待分别解释。
- 运行前固定版本与事后保存版本分开记录，缺失信息不补造。

## 基线与策略对照

| 日期 | 实验 | 范围与结论入口 |
| --- | --- | --- |
| 2026-09-26～09-29 | 初始修复、Docker、gold 预检与 smoke5 | 用于验证工具和评测流程，逐次记录见[历史台账](docs/history/experiments-through-2026-10-09.md#实验记录) |
| 2026-09-29～09-30 | 4B 固定 30 题基线 | DeepSeek 三次、GLM 一次、单次直出一次；[汇总与逐题证据](evals/reports/4b-baseline/README.md) |
| 2026-09-30 | GLM 两道 Pylint 题诊断 | 区分依赖导入与候选行为差异，人工诊断不补分；[诊断报告](evals/results/v30-glm/diagnosis.md) |
| 2026-09-30 | 剩余轮数提醒冒烟 | 检查提醒事件与完整判卷流程；[smoke5 报告](evals/results/smoke5-budget-deepseek/README.md) |
| 2026-10-01 | 4C 剩余轮数提醒对照 | 同一 30 题三次运行，记录收尾行为和解决率波动；[实验报告](evals/reports/4c-budget-reminders/README.md) |
| 2026-10-02～10-03 | 显式 `finish` 对照 | 开发集验证与三次 30 题对照；保留可选、默认关闭；[实验报告](evals/reports/4c-explicit-finish/README.md) |
| 2026-10-13～14 | xarray 三题隔离复验 | DeepSeek 基线 4/9、提醒 6/9、提醒 + finish 6/9；GLM 基线 1/3。29 份补丁实际测试，1 份空补丁保留分母；[报告与官方证据](evals/reports/xarray3-isolation/README.md) |

完整结果表及统计口径统一维护在[评测说明](docs/evaluation.md#完整结果)，逐题记录以各报告引用的原始数据为准。

## 命令策略离线检查

2026-10-19 使用已保存的十遍三题轨迹，对 985 条 bash 请求做只读分类：689 允许、95 拟拦截、201 未知。逐条复核拟拦截后，29 次有限 Git 查询存在范围过严的风险，先处理规则边界再安排新题对照。方法、复核记录和复算脚本见[离线报告](evals/reports/command-policy-offline-v1/README.md)。本次没有执行轨迹命令或调用模型，不增加修复次数，也不推算新的解决率、轮数或费用收益。

## 修复与官方判卷集成

| 日期 | 任务 | 结论与证据 |
| --- | --- | --- |
| 2026-10-05 | pytest-7205 修复 | 可见验证通过，官方判为未解决；[报告](evals/reports/repair-swebench-pytest7205-v3/README.md) |
| 2026-10-07 | pytest-7205 已有补丁重判 | 验证 `repair-judge` 的结果保存与哈希核对，未新增模型求解；[集成验收](evals/reports/repair-swebench-pytest7205-v3/README.md#官方判卷集成验收2026-10-07) |
| 2026-10-07 | pytest-7432、Flask-5014 | 两道已有开发题通过可见验证与官方判卷；[两仓库报告](evals/reports/repair-real-task-smoke2/README.md) |

可见验证、官方判卷及其失败分类分别保存。环境错误、测试未收集和补丁未解决不能仅凭日志中的一个标签互相替代。

## 产品流程验收

| 日期 | 阶段 | 验收范围 | 报告 |
| --- | --- | --- | --- |
| 2026-10-07 | API 与 worker | 接收任务、Docker 修复、独立验证、结果持久化与清理 | [worker 验收](evals/reports/task-worker-toy-smoke/README.md) |
| 2026-10-07 | M1 网页工作台 | 网页提交、结果展示、刷新与重新打开 | [网页验收](evals/reports/task-ui-toy-smoke/README.md) |
| 2026-10-08 | M2 人工审查与本地交付 | 批准准确补丁，生成独立分支并核对提交 | [本地交付验收](evals/reports/patch-review-delivery-toy-smoke/README.md) |
| 2026-10-09 | M3 公开 Issue 输入 | Flask 文档任务的来源、固定基线、修复与本地交付 | [Issue 任务验收](evals/reports/github-issue-flask6146-smoke/README.md) |
| 2026-10-09 | M4 GitHub PR 交付 | 自有示例仓库的草稿 PR、认证缺陷修复与原请求恢复 | [PR 交付验收](evals/reports/github-pr-publish-demo-smoke/README.md) |

这些验收证明对应任务的工程流程，不能代替复杂仓库的修复评测。M1 报告中的测试覆盖缺口、M4 报告中的事后版本记录和恢复证据限制继续保留。

## 验收配置与历史台账

| 资料 | 用途 |
| --- | --- |
| [xarray 三题隔离后复验配置](evals/profiles/xarray3-isolation/README.md) | 已完成的 10 遍配置、题单、启动校验和原计划；[归档报告](evals/reports/xarray3-isolation/README.md)另存完成状态 |
| [Flask #6146 配置](evals/profiles/github-flask-6146/README.md) | 当次固定基线、文档检查脚本及路径范围 |
| [GitHub PR 示例配置](evals/profiles/github-publish-demo/README.md) | 当次仓库、Issue、验证命令及发布操作 |
| [完整实验台账](docs/history/experiments-through-2026-10-09.md) | 原始时间顺序、长备注、环境观察和后续更正 |
| [开发历史](docs/history/development-through-2026-10-10.md) | 分阶段实现与当时的检查记录 |

配置和台账中的“下一步”“当前分支”、设备容量或耗时均属于当次记录。复现前需重新核对环境与仓库状态，不能将当时观察直接作为当前环境保证。

## 后续记录方式

新实验先固定目标、题单、实现版本、模型配置、预算和重试规则，完成后建立独立报告并在本文加入索引。报告保留成功与失败证据，不覆盖旧运行目录；只做文档整理或页面检查时不新增模型成绩。下一阶段计划统一维护在 [ROADMAP.md](ROADMAP.md)。
