# 命令策略设计与开发样例

[发展路线](../ROADMAP.md) · [执行架构](architecture.md) · [三题复验](../evals/reports/xarray3-isolation/README.md)

日期：2026-10-19。分支：`feat/command-policy`。状态：**纯判定器已实现，尚未接入执行流程**。`PolicyContext`、`PolicyDecision` 和 `assess_command` 可独立调用；下文的 CLI 开关、执行拦截和轨迹事件仍为拟定契约。本阶段不修改工具执行行为、不调用模型。

## 目标与范围

减少求解过程中全盘搜索、获取其他版本源码、查询额外历史等无效操作，保留当前仓库阅读、编辑、复现和测试能力。三题复验中 DeepSeek 在 xarray-4687 的九次尝试均达到 40 轮上限，其中一次没有补丁；这一观察用于选择改进方向，不等于所有搜索都无用或拦截一定提分。

首版只接入 SWE-bench 批量评测的 Agent 模式，由实验开关显式启用。暂不改变网页、worker、通用 `agent run`、`agent repair`、`repair-swebench`、单次直出及官方判卷。策略只检查模型发出的 `bash` 请求，不检查宿主机归档、环境预检、补丁收集或官方验证命令。

这不是安全沙箱。模型可以在 Python、嵌套 shell 或已有仓库脚本中执行静态检查无法理解的操作；Docker 无网络、缓存遮蔽及 v2 环境预检继续提供现有隔离。首版不做任意程序的副作用分析，不以“命令允许”表示“程序安全”。

结构化读取与编辑工具另做后续阶段，先完成命令策略的单独对照，避免同时改变工具接口、提示词和检索方式。

## 判定与执行模式

拟定评测选项为 `--command-policy off|audit|enforce`，默认 `off`。

| 判定 | 含义 | off | audit | enforce |
| --- | --- | --- | --- | --- |
| `allow` | 在支持的语法范围内，没有命中禁止规则 | 原样执行 | 原样执行并记录判定 | 原样执行并记录判定 |
| `block` | 能静态确定包含禁止操作 | 原样执行，不做策略判定 | 原样执行，记录 `would_block` | 整条 bash 请求不执行，返回原因 |
| `unknown` | 脚本、动态路径或语法超出判定范围 | 原样执行 | 原样执行并记录未知原因 | 原样执行并记录未知原因 |

`unknown` 仍执行是明确的首版边界，不是放行证明。不能把未知命令计为“已覆盖”，也不能为了降低未知率把 Python 一律拒绝。未知率过高或复杂命令误判时，先保留 audit，不开展 enforce 效果实验。

能可靠拆分的同一调用中，存在 `block` 则整条拒绝；否则存在 `unknown` 则记为未知，其余为允许。无法可靠解析时不猜测子命令。拒绝只针对这一条工具调用，不回滚同一模型回复中已执行的其他工具。

## 判定规则与支持范围

路径以容器内仓库根、当前工作目录、被测模块名、依赖目录和临时目录为上下文。不写死 xarray 题号、函数名或官方断言；xarray 仅作为样例上下文。

| 规则 ID | 拟定行为 | 边界 |
| --- | --- | --- |
| `repository_operation` | 允许当前仓库的读取、搜索、简单编辑和测试 | 不因为路径包含 `tests`、`gold` 等词自动拒绝；补丁路径限制仍由原机制处理 |
| `baseline_git` | 允许 `status`、`diff`、当前 HEAD 元数据、`show HEAD:<path>`、常用 stash 对照 | 不扩展为所有 Git 命令都允许；已有单提交环境仍保留 |
| `dependency_read` | 允许明确依赖包目录内的读取与有界检索 | 如 numpy；被测项目的额外安装源码优先命中拒绝规则，不允许递归搜索整个 site-packages |
| `environment_metadata` | 允许 `pwd`、版本查询、`pip show/list`、简单目录列表 | 安装信息不等于源码内容暴露，不将所有 `/opt` 访问都归为违规 |
| `scratch_operation` | 允许明确临时文件的读取、写入和清理 | 不将任意 `rm` 或临时目录全盘搜索归为允许 |
| `literal_data` | 允许引用参数、注释和已正确解析的纯文本 heredoc | 字符串里的 `pip download` 不是正在执行下载 |
| `network_or_install` | 拒绝明确的网络请求、包下载和安装 | 覆盖直接 `curl/wget`、`pip download/install`、`python -m pip`、`conda install` 等；不判断下载内容是否“像答案” |
| `filesystem_search` | 拒绝从 `/`、整个 `/opt`、`/root`、整个 site-packages 等位置递归搜索 | 静态 `cd / && find .` 也应识别；指定正常依赖包的局部搜索允许 |
| `extra_project_source` | 拒绝明确读取或搜索被测模块的缓存源码、额外安装源码及测试 | 已遮蔽缓存仍保留规则反馈以减少重复尝试；不能据路径断言某次已经成功读取 |
| `evaluation_material` | 拒绝明确读取仓库外的判卷材料路径 | 如 `/tmp/gold.patch`；普通复现脚本不受影响，仓库内未跟踪材料由预检检查 |
| `history_search` | 拒绝查找基线外提交、全历史搜索、reflog、fsck 恢复和批量枚举历史对象 | `log -1 HEAD` 等只查当前元数据的形式不拒绝；自定义 Git 配置或别名不能简单当作安全操作 |

`opaque_script`、`dynamic_shell`、`unresolved_path`、`unparsed_shell` 是未知原因。Python `-c`、Python heredoc、动态变量、命令替换、嵌套 shell、无法确定的符号链接和解析失败均需保留这种区分。`python -m pytest` 是明确测试入口，不因 Python 可执行程序名而判为未知；测试本身的运行行为仍不由静态策略证明。

## 解析要求

1. 先解析 shell 结构，再检查实际命令、参数和路径；不能只搜索关键词，也不能按 `;`、`|` 或空格直接切字符串。
2. 首版支持范围至少覆盖简单命令、常用管道、`&&`、`||`、`;`、重定向、静态 `cd` 和已声明的环境变量赋值。quoted heredoc 的纯文本输入与 Python 程序体分别处理。
3. `cd` 后的相对路径按语义解析；`..` 做词法归一化，不使用宿主机 `Path.resolve()` 判断容器路径。遇到不确定的分支目录或动态展开返回 `unknown`。
4. 不为判定执行 `$(...)`、`eval`、Python 代码或任何来自模型的内容。嵌套 shell、未知 shell 语法和自定义命令包装先返回 `unknown`，不宣称抗绕过。
5. 路径与字符串比较不能误匹配代码片段、注释或日志中的命令文本；项目路径和依赖读取优先级必须有对照样例。
6. `grep ... || pip download ...` 即使下载分支未必执行，也按包含禁止分支拒绝；轨迹要记录静态判定，而不是声称已经执行下载。
7. 包含多个子命令的单条 bash 请求必须在执行任何部分前完成判定。不得先运行前半段再拦截后半段，也不自动删掉被拒绝部分后执行剩余命令。

当前使用 [Tree-sitter Python bindings](https://github.com/tree-sitter/py-tree-sitter) 0.26.0 和 [Bash grammar](https://github.com/tree-sitter/tree-sitter-bash) 0.25.1，版本锁定在 `pyproject.toml` 与 `uv.lock`。解析器只生成语法树，不调用 Bash。实现不依赖关键词分割，也不使用宿主机路径解析容器路径。

当前上限为 32,768 UTF-8 字节、4,096 个语法树节点及 64 层深度；超限返回 `unknown/input_limit`。语法错误、NUL 或无法编码的输入返回 `unknown/unparsed_shell`。这些是输入及遍历限制，不是硬实时保证；原有工具超时也不会限制尚未接入的纯判定器。

支持简单命令、管道、重定向、`&&`／`||`／顺序语句和静态目录流转。目录可能因分支或 `cd` 失败而不确定时，相对路径返回未知；管道里的 `cd` 不改变后续父 shell 的目录。quoted heredoc 内容视为数据；未加引号的 heredoc、循环、函数、后台执行和嵌套 shell 保守返回未知。

命令参数只解释已支持的选项：如 grep 的模式与文件参数、head/tail 数量参数、find 的常用查找条件、sed 的数字行范围读取。sed 程序、`find -exec`、未支持的包装及选项返回 `unknown`，不执行它们来判断行为。`unsupported_command` 区分合法但未支持的命令形式。显式未知目录中的可执行文件不因叫 `python`、`pip` 等名字就被当成系统程序；已知路径仍只是词法识别，不保证不存在符号链接、shell 配置或程序副作用。

读取 `/tmp/test.patch` 与创建、清理自己的同名临时补丁分别处理：前者命中明确命名的外部材料规则，后者不因文件名触发读取规则。判定器没有文件内容或来源证明，已存在同名自建文件的读取仍可能误拦，需在接入前的离线分析中统计。

## 已实现的独立接口

实现位于 [command_policy.py](../src/patchloop/command_policy.py)，只提供分类结果：

```python
from patchloop.command_policy import PolicyContext, assess_command

context = PolicyContext(repository_root="/testbed", cwd="/testbed", target_modules=("xarray",))
decision = assess_command("pip download xarray", context)
assert decision.classification == "block"
assert decision.rule_ids == ("network_or_install",)
```

`PolicyContext` 保存不可变的容器路径配置；`PolicyDecision` 包含 `classification`、排序后的 `rule_ids` 和说明 `reason`，本身不选择执行动作。判定采用 `block > unknown > allow`；有阻断或未知原因时不把普通读取类标签混入最终规则集合。`POLICY_VERSION` 和 `PARSER_VERSION` 可供后续配置与轨迹使用，当前没有自动写入实验记录。

## 拟定接入方式

当前调用链为 `loop._execute → tools.bash → runner.run`。在工具参数完成校验、兼容 `cmd/command` 之后、`runner.run` 之前做策略判定，保证两个参数名不会绕过检查。

已实现的纯判定函数 `assess_command(command, context)` 不访问网络、启动子进程或修改工作区。下一步才接入模式映射和工具反馈，不把日志写入与纯分类函数耦合。目前 `loop`、`tools`、runner 和 CLI 均未接入策略，保持原行为。

拟新增 `command_policy` 轨迹事件：模式、策略版本、turn、tool_call_id、判定、规则 ID、未知原因，以及执行层选择的 `execute/block` 动作。事件必须能与原工具调用、返回结果对应。audit 命中记录为 would-block，不计入实际拦截次数；工具执行是否成功仍看原工具返回。

示例反馈（拟定，非真实运行输出）：

```text
Command blocked [filesystem_search]. The command was not executed.
Search within /testbed or a specific dependency directory, then implement and test the fix.
```

拒绝是正常工具反馈，不将任务直接标成 API 错误；不重试原命令、不退还模型轮次、不更改工具调用 ID。多工具调用仍遵守原有轮次统计。命令长度和错误输出按原有轨迹规则处理，不在反馈中读取环境变量或凭证。

策略模式、版本、配置摘要、样例版本及解析器版本需要纳入新评测配置；只改变开关的两个实验使用相同代码与 v2 镜像。原来的冻结 v1 计划继续保持原样。

## 开发样例

[cases.json](../evals/profiles/command-policy/cases.json) 包含 **56 条判定开发样例**：10 条从已保存轨迹提取，46 条构造对照；预期为 23 条允许、23 条拦截、10 条未知。命令及来源哈希保持原样，状态更新为纯判定器已实现。

- 原始命令保存 run-id、题号、turn、轨迹行号、tool_call_id 和哈希；未运行这些命令，也未改写旧轨迹。
- 构造样例包含正常依赖读取、关键词纯文本、Python 编辑、网络获取、全盘搜索、路径归一化、动态包装和“前半条有副作用、后半条应拒绝”。
- `source.kind` 区分真实轨迹与构造数据。`expected.rule_ids` 按集合比较，多个禁止规则可以同时命中。
- 样例是可调整的开发集，不是不可改动的实验计划，更不是策略有效性的评测结果。
- 原始命令只是数据，包含有副作用的写文件、下载和清理操作。数据校验与离线分类只读命令字符串，不得交给 shell 重放。

这些数据已接入 [test_command_policy.py](../tests/test_command_policy.py) 的参数化测试；共 100 项纯判定及边界测试通过。额外用例覆盖条件目录、系统命令名伪装、Git 外部仓库、引用与转义、临时补丁写入、输入上限、无 I/O 及并发确定性。验证命令：`uv run pytest -q tests/test_command_policy.py`。测试不重放轨迹命令，纯判定通过不代表执行层已经拦截。

本阶段检查：判定器、工具、循环、评测、配置与 CLI 的针对性回归 171 passed；后端全量 `uv run pytest -q` 为 1182 passed。另以固定种子构造 2,000 条语法输入进行仅分类检查，未出现异常；该检查不证明语义判定全部正确。锁文件只新增两项解析依赖，既有包版本未升级；旧模型运行目录和报告未修改。没有运行真实模型或执行样例中的命令。

## 验收与对照

| 检查层次 | 要证明什么 |
| --- | --- |
| 纯判定测试 | 支持范围内分类正确，字面文本不误拦，不支持语法确实返回未知 |
| 执行层测试 | off 完全沿用原行为；audit 原样执行；enforce 的 block 不调用 runner，unknown 执行原串 |
| 原子性测试 | `printf touched > /tmp/policy-marker; pip download xarray` 被拒后 marker 不存在；由 FakeRunner／一次性测试目录验证，不实际下载 |
| 工具循环测试 | `cmd/command` 均走同一检查，反馈与调用 ID 配对，拒绝计入原轮次，无自动重试；finish 不受影响 |
| 配置测试 | 非 Agent 模式不允许显式启用策略，默认关闭；模式或版本改变不能混入旧 run-id |
| 真实 Docker 冒烟 | 正常代码阅读、编辑和测试仍可完成；阻断样例仅在无网络的一次性容器中验证，不调用真实模型 |

通过开发测试后，可在保留的轨迹上只做离线分类，逐条复核误拦截与未知命令。离线分析只说明哪些历史命令会被策略识别，不能模拟模型收到拒绝后的行为，也不能直接预测成功率变化。

真实模型 A/B 另建计划：两组都使用 v2 环境、相同实现、相同模型、题单、预算、并发、提醒开启且 finish 关闭；A 为 off，B 为 enforce。不在这次同时修改系统提示或加入结构化工具。分别记录官方解决率、空补丁、模型轮数、费用、命令判定分布、实际拦截、误拦截及未知率。

首次编辑轮次应按工作区变更或补丁证据确认，不能只看命令包含 `sed` 或 `python`。被拒绝次数不是收益指标，搜索次数下降也不自动代表修复能力提高。现有三道 xarray 题只作开发回归；新题效果评估先固定范围和验收规则，模型预算另行确认。

## 下一步

1. 已完成纯判定器与参数化测试；支持的 shell 结构与未知降级以测试和上述范围为准。
2. 接入 off／audit／enforce 的工具执行与轨迹契约，默认关闭，通过 FakeRunner 和 Docker 冒烟。
3. 离线核对覆盖范围、误拦截和未知命令，再决定是否进入新题模型对照。

本分支不创建 GitHub Issue、不更改发布权限、不合并旧实验成绩。每个实现阶段通过检查后提交；是否启用新策略以对照结果决定。
