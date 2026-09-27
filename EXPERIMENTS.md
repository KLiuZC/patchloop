# 实验记录

| 日期 | 任务 | provider / model | 轮数 | tokens (prompt / completion) | 结果 | 备注 |
|---|---|---|---|---|---|---|
| 2026-09-26 | toy_repo 修 v 前缀 | deepseek / deepseek-flash | 4 | 4382 / 373 | 通过 | 先 ls+find 探索；heredoc 整文件重写；每条命令带 cd 绝对路径；每轮约 1.3s |
| 2026-09-27 | toy_repo 修 v 前缀 | glm / glm-5.3-flash | 4 | 2769 / 207 | 通过 | 第 1 轮直接 pytest；sed 单行替换（macOS 写法 sed -i ''）；每轮约 5.5s；回传 reasoning_content 无报错 |
