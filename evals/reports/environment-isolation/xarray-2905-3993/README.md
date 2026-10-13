# xarray-2905 与 xarray-3993 环境验收

日期：2026-10-13。实现版本：`164d24a`，隔离策略：`swebench-isolation-v1`。本次沿用已提交的求解隔离实现，扩展真实 Docker 回归测试到三道受影响题目；没有调用模型、生成新补丁或更改历史解决率。

## 验收结果

| 题目 | 原环境 | 隔离后 | 基线可见测试 | 原镜像 gold 判卷 |
| --- | --- | --- | --- | --- |
| xarray-2905 | 存在新版源码和 `.conda` 包 | 已声明扫描范围内无残留或扫描错误 | 2 passed，456 deselected | resolved；F2P 1/1，P2P 364/364 |
| xarray-3993 | 存在新版源码和 `.conda` 包 | 已声明扫描范围内无残留或扫描错误 | 2 passed，941 deselected | resolved；F2P 2/2，P2P 2398/2398 |

两题均只保留指定 base commit 的一个提交对象，无远端、reflog 和 alternate 对象库；隔离前后跟踪文件的 tree ID 一致。xarray 从各自 `/testbed/xarray/__init__.py` 导入，pytest 及可见测试正常执行。

真实容器回归测试确认：`cat` 和 Python 读取新版源码失败，缓存压缩包不可读，卸载遮蔽目录失败。相关测试共 **68 passed**，包含此前的 xarray-4687 用例；测试不会自动下载缺失镜像或调用模型。

## 可见检查的选择

命令来自题目指定基线中已有的测试，不使用官方新增测试或 gold 补丁。2905 检查变量赋值；3993 的既有积分测试位于 `test_dataset.py`，同时覆盖 Dataset 与 DataArray。

```bash
uv run agent preflight-swebench \
  --instance pydata__xarray-2905 \
  --verify 'python -m pytest -q xarray/tests/test_variable.py -k setitem' \
  --artifacts /tmp/patchloop-preflight-2905-new

uv run agent preflight-swebench \
  --instance pydata__xarray-3993 \
  --verify 'python -m pytest -q xarray/tests/test_dataset.py -k test_integrate' \
  --artifacts /tmp/patchloop-preflight-3993-new
```

已有可见测试通过只说明环境仍可运行，并不表示待修复问题已经解决。这些命令用于环境预检，不作为真实求解时证明修复有效的充分条件。

## 官方判卷预检

运行编号：`environment-isolation-gold-xarray2-20261013`。使用原镜像、官方标准答案和官方 harness，串行执行（`-j 1`），结果为 **2/2 resolved、0 errors、0 infrastructure failures**。该过程独立于求解容器，标准答案及官方新增测试没有进入 Agent 上下文。

```bash
swebench eval verified --gold \
  -i pydata__xarray-2905 -i pydata__xarray-3993 \
  --run-id environment-isolation-gold-xarray2-new -j 1
```

本次镜像来自重新下载，实际 image ID 与平台记录在预检文件中，不认定为历史本地构建镜像的逐字复刻。本次未修改隔离实现，前一阶段的报告和校验清单保持原样。

## 原始证据

| 文件 | 内容 |
| --- | --- |
| [2905 preflight](xarray-2905-preflight.json) / [environment](xarray-2905-environment.json) | 原环境与隔离后检查、镜像、源码导入位置、可见测试输出 |
| [3993 preflight](xarray-3993-preflight.json) / [environment](xarray-3993-environment.json) | 同上 |
| [gold 汇总](gold-results.json) | 两题官方预检结果 |
| [2905 gold 判卷](xarray-2905-gold-report.json) / [3993 gold 判卷](xarray-3993-gold-report.json) | 逐项测试结论 |
| [manifest](manifest.json) | 已提交实现版本、源码与产物哈希、检查结果 |

## 后续工作

三道已发现污染访问的 xarray 题现已完成环境预检和 gold 预检，可以准备新的模型补跑配置。该阶段仍属于环境验收；清洁环境复验成绩需等待重新求解。检查范围及历史污染口径沿用[上一阶段报告](../README.md)，其他镜像的审计不由这三题代替。
