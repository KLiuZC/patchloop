# patchloop

一个从零写的最小 coding agent：给它一个仓库和一个任务，它通过 bash 自己定位、修改、跑测试，直到完成。目标是在公开 benchmark 上跑出可复现的数字，并把评测和轨迹分析做成配套工具。

当前状态：第一版，只有一个 `bash` 工具，无沙箱，无 context 压缩。

## 运行

```bash
cp .env.example .env        # 填入 API key
uv sync
uv run agent run --workdir examples/toy_repo --provider glm \
  --task "运行 pytest，修复失败的测试。不要改动 tests/ 目录。"
```

默认用两家的 flash 模型（`glm-5.3-flash`、`deepseek-flash`），便宜、够开发用；正式评测时用 `-m glm-5.3` / `-m deepseek-v4-pro` 切到旗舰。跑完后用 `git checkout -- examples/toy_repo` 还原玩具仓库。每次运行的完整轨迹写在 `traj/<run_id>.jsonl`。

## 结构

```
src/patchloop/
  llm.py         OpenAI-compatible 客户端与 provider 表
  tools.py       bash 工具：工作目录、超时、输出截断
  prompts.py     system prompt
  loop.py        agent 循环：调模型 → 执行工具 → 结果回填
  trajectory.py  JSONL 轨迹
  cli.py         命令行入口
examples/toy_repo/   带一个已知 bug 的玩具仓库，用于冒烟
tests/               项目自身的单元测试：uv run pytest
```

## 路线

- [x] 最小 loop + bash 工具
- [ ] Docker 沙箱
- [ ] SWE-bench Lite 子集评测
- [ ] context 管理与消融
