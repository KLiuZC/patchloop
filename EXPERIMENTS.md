# 实验记录

| 日期 | 任务 | provider / model | 轮数 | tokens (prompt / completion) | 结果 | 备注 |
|---|---|---|---|---|---|---|
| 2026-09-26 | toy_repo 修 v 前缀 | deepseek / deepseek-flash | 4 | 4382 / 373 | 通过 | 先 ls+find 探索；heredoc 整文件重写；每条命令带 cd 绝对路径；每轮约 1.3s |
| 2026-09-27 | toy_repo 修 v 前缀 | glm / glm-5.3-flash | 4 | 2769 / 207 | 通过 | 第 1 轮直接 pytest；sed 单行替换（macOS 写法 sed -i ''）；每轮约 5.5s；回传 reasoning_content 无报错 |
| 2026-09-28 | gold-check：SWE-bench Verified 5 题（pytest-7205/7236/7324/7432 + requests-2317），标准答案作答 | 官方 harness / 无模型 | - | - | 4/5 | `-j 2` 时两个镜像在 conda env create 一步 OOM（Docker VM 8GB，x86 模拟），改 `-j 1` 全部构建成功；requests-2317 标准答案也不过：6 个失败测试全依赖外网（httpbin.org、不可达 IP 的 connect timeout），Docker Desktop 用户态网络下 connect 总是立即成功，判为环境性失败，排除 |
| 2026-09-28 | gold-check：换入 pallets__flask-5014，题单为 4 道 pytest + flask | 官方 harness / 无模型 | - | - | 5/5 | 构建约 1.5 min/题（基础层缓存后），评测约 25 s/题，镜像约 1.5 GB/题独立占用；磁盘 97G→73G |
| 2026-09-28 | toy_repo 修 v 前缀，Docker 沙箱（patchloop-toy 镜像，挂载 /work） | deepseek / deepseek-flash | 5 | 4810 / 396 | 通过 | 沙箱首次跑通；仍每条命令带 cd 前缀；heredoc 整文件重写；挂载目录不是 git 根，patch 为空 |
| 2026-09-28 | SWE-bench 镜像环境检查（pytest-7205，无挂载，/testbed） | deepseek / deepseek-flash | 2 | 1181 / 302 | 通过 | 登录 shell 激活 conda testbed：Python 3.9.20、pytest 在 env 内、cwd=/testbed、git 干净 |

## 环境备忘

- Apple Silicon 上镜像由 `--task-repo` 本地构建，产物是 x86_64 镜像，运行靠 Rosetta 模拟；构建失败后的官方镜像兜底在 ARM 上不可用（拉取未指定平台），所以构建必须成功。
- Docker VM 8GB 时构建只能 `-j 1`，并行 conda 求解会 OOM。
- 依赖外网的测试在本机 Docker 里不可判（用户态网络 + 公共服务变化），老版本 requests 是重灾区。正式子集选定后先整体跑一遍 gold 校验，标准答案不过的题一律剔除，再让 agent 上。
- 每题镜像约 1.5GB，100 题约 150GB：内置盘装不下，需分批评测后删镜像，或把 Docker 数据盘迁到外置 APFS 卷 / 用云机。
