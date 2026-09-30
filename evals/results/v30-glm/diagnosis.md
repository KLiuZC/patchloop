# GLM 评测中两道 pylint 题的失败诊断

日期：2026-09-30。运行：`v30-glm`；官方判卷：`v30-glm-0930-135232`。

正式成绩仍为 **20/30**。下面的补齐依赖实验只用于归因，不能计入模型成绩，也没有修改原始答卷、轨迹、`summary.json` 或官方判卷文件。

## 结论

| 题目 | 原始报错 | 对照实验 | 归因 |
| --- | --- | --- | --- |
| pylint-4604 | 隐藏测试无法导入 `pylint.constants.IS_PYPY`，21 个目标测试未执行 | 保持 GLM 修复不变，只补常量：21 passed；撤掉常量：再次导入失败 | 隐藏测试依赖标准答案新增的辅助常量；不能据此判断核心修法错误 |
| pylint-4661 | 隐藏测试无法导入 `appdirs`，目标测试未执行 | 只安装 appdirs 1.4.4：50 passed、1 failed；改用 gold patch 后：51 passed | 缺失测试所需依赖，同时 GLM 的目录选择与官方验收预期不同 |

两处原始导入错误都可在全新容器中重现，不需要 Apple Silicon 特有故障、磁盘不足或 Docker 网络错误来解释。

## pylint-4604

题目要求修复类型注释中的模块引用被误报为未使用导入。GLM 在 `pylint/checkers/variables.py` 中加入处理 `astroid.Attribute` 的四行代码；这一业务修改与 gold patch 相同。

官方隐藏测试还执行 `from pylint.constants import IS_PYPY`，用该常量跳过 PyPy 上不适用的测试。原始代码没有此常量，gold patch 另行添加了它，而 GLM 补丁没有。即使当前使用 CPython，测试模块也必须先成功导入常量才能进行跳过条件判断。

在新容器中应用原始 GLM 补丁和官方 test patch，重现导入错误。只在 `pylint/constants.py` 增加 `import platform` 和 `IS_PYPY = platform.python_implementation() == "PyPy"`，同一个测试模块 21/21 通过；恢复此文件后再次出现相同导入错误。另用 gold patch 对照，同样 21/21 通过。

因此应记录为“隐藏测试依赖特定辅助代码，原答卷缺少该代码”。官方原成绩仍为未解决；人工补常量后的通过不作为新的正式分数。原补丁另有无关的 `classes.dot` 文件，不是此次失败的原因。

## pylint-4661

题目讨论将 `.pylint.d` 移至符合 XDG 约定的目录，题面同时提到 data 与 cache 的目录示例。GLM 自行实现目录选择，默认使用 `~/.local/share/pylint`，没有引入第三方依赖。

官方隐藏测试却直接导入 `appdirs`，并以 `appdirs.user_cache_dir("pylint")` 为期望值。在该 Linux 容器中，这是 `/root/.cache/pylint`。gold patch 将 `appdirs>=1.4.0` 加入 `setup.cfg`；官方评测脚本安装项目时会随之安装该依赖。GLM 补丁没有声明此依赖，所以原判卷先被导入错误阻断。

诊断时仅安装与 gold 日志相同的 appdirs 1.4.4，GLM 版本随即得到 50 passed、1 failed，唯一断言差异是：

```text
actual:   /root/.local/share/pylint
expected: /root/.cache/pylint
```

同一已补依赖环境换成 gold patch 后，51/51 通过。因此补装依赖不会让原 GLM 答卷通过；它还存在与官方目标行为的分歧。这里的判断范围是官方验收预期，不能仅凭该结果声称题面没有歧义。

## 方法与证据

- 使用现有两道题镜像，新建临时容器；执行 Linux amd64 环境，内存限制 3GB。
- 从原判卷 `eval.sh` 提取官方 test patch，直接应用原始 GLM 补丁。
- 分别执行 `python -m pytest -q tests/checkers/unittest_variables.py` 和 `python -m pytest -q tests/lint/unittest_lint.py`。
- 未重跑模型，未修改正式预测。这里重放的是相关测试模块，不是另一次完整官方判卷。
- appdirs 仅安装于临时诊断容器。临时容器已删除，原镜像未改变。
- 原始官方证据位于 `~/code/swebench-work/logs/evaluation/v30-glm-0930-135232/v30-glm/<题号>/`；gold 对照位于 `~/code/swebench-work/logs/evaluation/gold-verified30/gold/<题号>/`。
- 本次复现输出保存在同目录的 `diagnostics/`，包括补齐前、补齐后、gold 对照和撤回常量的输出。

## 对之前归因的更正

DeepSeek r1/r2/r3 的 pylint-4604 判卷日志也都是同样的 `IS_PYPY` 导入错误；pylint-4661 在 r1/r3 是同样的 appdirs 导入错误，r2 是空补丁。本次没有重放这几份 DeepSeek 补丁，不能推断补齐依赖后的通过情况。

此前将这些失败概括为“模型理解有偏差”缺少依据，尤其不能把“未收集到测试”当成业务断言失败。gold 全过只能证明标准答案在当前环境中通过，不能保证隐藏测试与特定实现解耦。保持所有模型的原始统计口径，单独记录这些限制；若将来更改评测规则，应版本化并对所有对照组一致复评，不能只给 GLM 补分或事后选择性删题。
