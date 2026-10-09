# 推送认证故障与原请求恢复

这是根据诊断时的检查结果、版本历史及最终远端结果整理的回顾记录，不是原始发布尝试日志。`github_publications` 表保存一个发布意图及最新检查点，恢复会更新同一条记录；初始失败状态没有另存成不可变文件。

## 观察与定位

诊断时，任务 `3659f4fc-1eea-48cf-a744-b2bbebee4654` 的验证已通过，人工审查为第 1 次 `accepted`，本地交付完整。发布记录 `bc40eddb-49f2-43de-b8e0-e425bd4bc23c` 为 `uncertain`，阶段 `push`，`push_attempted=true`、`branch_pushed=false`、`pr_attempted=false`。接口信息为：

```text
Git preparation or push failed; query the remote branch before retrying
```

当时的 GitHub 只读查询显示目标仓库只有 `main`，尚无 PR。本地临时副本准备与 tree 核对可通过；使用相同推送参数执行 `--dry-run` 时，Git 报告无法取得用户密码。对照仅切换 `credential.interactive`：为 `false` 时未调用认证助手并失败，为 `true` 时调用程序生成的用户名/密码助手且试运行通过。诊断中的 Token 没有打印、写入脚本或归档。

Git 2.54.0 的认证获取流程会在 `credential.interactive=false` 时跳过 askpass；可对照 [Git credential.c](https://github.com/git/git/blob/v2.54.0/credential.c)。原有本地裸仓库推送测试验证了提交、浅克隆和分支保护，却没有经过 HTTPS 认证，所以没有暴露该缺陷。

## 修复与检查范围

`src/patchloop/api/github_push.py` 改为仅对配置了程序生成 `GIT_ASKPASS` 的推送操作使用 `credential.interactive=true`，保留 `GIT_TERMINAL_PROMPT=0` 与空用户 helper。没有修改补丁、确认方案、目标分支、原请求编号或权限范围。

新增测试保留生产推送命令的 Git 配置，仅用真实 `git credential fill` 替代网络传输，验证 GitHub 凭证取得、其他主机被拒绝及临时助手清理。旧代码上可复现失败，修复后发布专项 41 项通过。另对原任务与原凭证执行一次成功的推送试运行，没有远端写入。

修复代码和测试随后保存于 `caf82d6`；该提交时间晚于实际成功恢复，不能称为运行前冻结版本。

## 实际恢复结果

用户重启新版 API 后在网页按原方案恢复。最终 [publication-state.json](publication-state.json) 保留原请求 `f2dd120a-0e63-41df-a7ae-4b09353a674c`，状态与阶段均为 `completed`，关联草稿 PR #2。远端 head SHA、tree、唯一父提交、标题、正文和修改文件与批准的本地交付一致。

最终仅有一条发布记录和一个相同 head/base 的 PR。这次没有“创建 PR 已成功但响应丢失”的真实故障；该类场景仍由此前模拟测试覆盖。没有逐次发布调用日志，归档不推断精确 HTTP 重试次数，也不把故障修复期间的总等待时间当作推送或 PR 创建耗时。
