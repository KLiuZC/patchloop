# 实验记录

| 日期 | 任务 | provider / model | 轮数 | tokens (prompt / completion) | 结果 | 备注 |
|---|---|---|---|---|---|---|
| 2026-09-26 | toy_repo 修 v 前缀 | deepseek / deepseek-flash | 4 | 4382 / 373 | 通过 | 先 ls+find 探索；heredoc 整文件重写；每条命令带 cd 绝对路径；每轮约 1.3s |
| 2026-09-27 | toy_repo 修 v 前缀 | glm / glm-5.3-flash | 4 | 2769 / 207 | 通过 | 第 1 轮直接 pytest；sed 单行替换（macOS 写法 sed -i ''）；每轮约 5.5s；回传 reasoning_content 无报错 |
| 2026-09-28 | gold-check：SWE-bench Verified 5 题（pytest-7205/7236/7324/7432 + requests-2317），标准答案作答 | 官方 harness / 无模型 | - | - | 4/5 | `-j 2` 时两个镜像在 conda env create 一步 OOM（Docker VM 8GB，x86 模拟），改 `-j 1` 全部构建成功；requests-2317 标准答案也不过：6 个失败测试全依赖外网（httpbin.org、不可达 IP 的 connect timeout），Docker Desktop 用户态网络下 connect 总是立即成功，判为环境性失败，排除 |
| 2026-09-28 | gold-check：换入 pallets__flask-5014，题单为 4 道 pytest + flask | 官方 harness / 无模型 | - | - | 5/5 | 构建约 1.5 min/题（基础层缓存后），评测约 25 s/题，镜像约 1.5 GB/题独立占用；磁盘 97G→73G |
| 2026-09-28 | toy_repo 修 v 前缀，Docker 沙箱（patchloop-toy 镜像，挂载 /work） | deepseek / deepseek-flash | 5 | 4810 / 396 | 通过 | 沙箱首次跑通；仍每条命令带 cd 前缀；heredoc 整文件重写；挂载目录不是 git 根，patch 为空 |
| 2026-09-28 | SWE-bench 镜像环境检查（pytest-7205，无挂载，/testbed） | deepseek / deepseek-flash | 2 | 1181 / 302 | 通过 | 登录 shell 激活 conda testbed：Python 3.9.20、pytest 在 env 内、cwd=/testbed、git 干净 |
| 2026-09-28 | smoke-deepseek：pytest-7205 单题，agent 模式，v1 提示词 | deepseek / deepseek-flash | 40 | 631k / 13k（缓存命中 96%） | 0/1 | 流水线首次跑通（起容器→答题→交卷→官方判卷→汇总），¥0.09、5 min。失败归因：前 11 轮定位并复现了 bug，第 12 轮起 ~25 轮在找“上游的修法”（翻 git 历史/标签/不可达对象、pip 缓存、changelog，甚至找 SWE-bench 元数据），一次也没改文件；4 轮把参数键写成 command 而非 cmd；一次全盘 grep 超时。上下文 2k→27k，不是溢出问题。修正：任务模板写明“修复不在仓库/历史里，自己写，不必匹配上游改法”，系统提示写明命令预算；loop 接受 command 别名 |
| 2026-09-28 | smoke-deepseek-v2：同一道题 pytest-7205，只改提示词（预算意识 + “自己写修复”） | deepseek / deepseek-flash | 22 | 234k / 11k（缓存命中 96%） | 1/1 | ¥0.06、84 s。官方判卷 FAIL_TO_PASS 10/10、PASS_TO_PASS 16/16，补丁与上游修法本质相同（saferepr）。第 7-15 轮仍在找上游修法/元数据（9 轮），但第 16 轮开始动手，复现→跑相关测试→清理临时目录→收尾。n=1，只说明提示词是杠杆，不能当结论 |
| 2026-09-28 | smoke5-deepseek：smoke5 子集 5 题（3 题 <15min、2 题 15min-1h），agent 模式，v2 提示词 | deepseek / deepseek-flash | 40/16/40/7/9 | 1666k / 55k（缓存命中 97%） | **5/5** | ¥0.30、10 min。官方判卷每题 F2P 全过、P2P 无回归。7205 与 7324 跑满 40 轮仍解决：7205 第 40 轮才首次改代码（前 39 轮又在找上游修法、翻 reflog/apt 缓存），改完未验证，属侥幸；7324 第 29 轮改完后 9 次跑测试、最后 6 轮反复自我怀疑不收尾。两种失败模式：答案搜寻（提示词只能缓解、方差大）、不知何时停。n=5 且偏简单，不可外推 |
| 2026-09-29 | smoke5-single-shot：同 5 题，无工具一次直出基线 | deepseek / deepseek-flash | 1 | 3k / 216k（几乎全是思考） | 1/5 | ¥0.87（比 agent 的 ¥0.30 贵近 3 倍）、13 min（比 agent 慢）。只有 flask-5014（题面 195 字、补丁 14 行）盲写正确；4 题无补丁，其中 2 题输出恰为 65536 token（API 输出上限，回复被截断）。此时代码未保存原始回复，无法区分“没写出 diff”与“被截断”，已补：记录 finish_reason 与回复原文 |
| 2026-09-29 | probe-single-shot：重跑 pytest-7236 单次直出，带原始回复记录 | deepseek / deepseek-flash | 1 | 1k / 44k | 0/1 | ¥0.17、160 s。finish_reason=stop（这次没截断，思考 16 万字符），写出了 diff，但 git apply 失败：凭记忆猜的上下文行与真实文件不符。单次直出的两种失败模式齐了：思考过长被输出上限截断（上一轮 2/5）、盲写的 diff 对不上文件（本次）。同一题两次思考长度 65k vs 43k token，方差很大 |
| 2026-09-29 | smoke5-glm：同 5 题，agent 模式，v2 提示词 | glm / glm-5.3-flash | 18/23/15/9/11 | 665k / 41k（缓存命中 90%） | 4/5 | ¥0.30、19 min（每轮 5-24 s，比 DeepSeek 慢 2-4 倍）。5 题全部自行收尾、首次改代码都在第 3-11 轮、找答案类命令 0-2 条——过程指标明显比 DeepSeek 稳。唯一失败 7324：补丁能打上、原有测试 58/58，但隐藏测试 1/3：它把 -k 表达式里的 True/False/None 编译成 Python 常量，消除了崩溃却改变了语义（这些名字应仍作为可匹配的标识符）；DeepSeek 用内部前缀 + MatcherAdapter 解包，语义正确。失败类型：修法貌似合理但语义错误，现有测试覆盖不到。另：GLM 主动新增了 changelog 条目（编号是编的） |
| 2026-09-29 | gold-verified30：报告集 30 题，标准答案作答 | 官方 harness / 无模型 | - | - | **30/30** | 构建 30 个镜像约 75 min（含一次中断：Docker Hub 在 16:34 断连半分钟，排在后面的 11 个镜像核对基础镜像时连续失败，判卷程序随后在拉兜底镜像处崩溃；原命令重跑跳过已建成的 19 个，重建 11 个后判卷）；判卷 30 题 16 min，中位数约 30 s；镜像总量约 50GB。无需剔除，报告集 = 30 题全集 |

## 环境备忘

- Apple Silicon 上镜像由 `--task-repo` 本地构建，产物是 x86_64 镜像，运行靠 Rosetta 模拟；构建失败后的官方镜像兜底在 ARM 上不可用（拉取未指定平台），所以构建必须成功。
- Docker VM 8GB 时构建只能 `-j 1`，并行 conda 求解会 OOM。
- 依赖外网的测试在本机 Docker 里不可判（用户态网络 + 公共服务变化），老版本 requests 是重灾区。正式子集选定后先整体跑一遍 gold 校验，标准答案不过的题一律剔除，再让 agent 上。
- 每题镜像约 1.5GB，100 题约 150GB：内置盘装不下，需分批评测后删镜像，或把 Docker 数据盘迁到外置 APFS 卷 / 用云机。

## 子集约定

- `smoke5`（4 道 pytest v5.4 + flask-5014）：开发集。提示词、工具、循环机制的调整都先在它上面看效果。
- `verified30`（2026-09-29 生成，seed=20260929，6 个仓库各 5 道，难度 12/15/3）：报告集。只用来出数字；任何在它上面观察到的失败，改法先回到 smoke5 验证，再整体重跑报告集，不做单题调优。
- 报告集 gold 校验：30/30 通过（2026-09-29），无剔除。
