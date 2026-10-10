# 第三方材料与许可范围

patchloop 原创代码和文档采用根目录的 [MIT License](LICENSE)。该授权适用于项目有权许可的内容，不替代第三方材料的版权、许可证或其他适用条款。

## 图标

[GitHubIcon.jsx](frontend/src/components/GitHubIcon.jsx) 使用 Primer Octicons 的 `mark-github-16` 图形。上游为 [primer/octicons](https://github.com/primer/octicons)，采用 MIT；现有版权声明与完整许可证保存在 [primer-octicons.txt](frontend/public/licenses/primer-octicons.txt)，并随前端静态资源分发。

GitHub 名称、标识及第三方商标不因本项目的 MIT 声明而转归项目作者，也不表示相关组织为本项目背书。

## 评测代码与上游片段

`evals/` 中的补丁上下文、测试输出和代码片段可能来自被评测项目；这些内容保留相应上游权利，不统一重新许可为 patchloop 的 MIT。以下提供已涉及项目的来源和许可证副本，副本按所列版本原样保存。

| 来源 | 所列版本的许可 | 上游文件与本地副本 |
| --- | --- | --- |
| SWE-bench harness | MIT | [上游 LICENSE](https://github.com/SWE-bench/SWE-bench/blob/main/LICENSE) · [副本](LICENSES/swebench.txt) |
| Django | BSD-3-Clause | [3.1 LICENSE](https://github.com/django/django/blob/3.1/LICENSE) · [副本](LICENSES/django.txt) |
| pytest | MIT | [5.4.3 LICENSE](https://github.com/pytest-dev/pytest/blob/5.4.3/LICENSE) · [副本](LICENSES/pytest.txt) |
| Flask | BSD-3-Clause | [2.3.0 LICENSE.rst](https://github.com/pallets/flask/blob/2.3.0/LICENSE.rst) · [副本](LICENSES/flask.txt) |
| xarray | Apache-2.0 | [v0.12.0 LICENSE](https://github.com/pydata/xarray/blob/v0.12.0/LICENSE) · [副本](LICENSES/xarray.txt) |
| Sphinx | BSD-2-Clause 及文件中列明的第三方条款 | [v3.2.0 LICENSE](https://github.com/sphinx-doc/sphinx/blob/v3.2.0/LICENSE) · [副本](LICENSES/sphinx.txt) |
| SymPy | BSD-3-Clause | [sympy-1.6 LICENSE](https://github.com/sympy/sympy/blob/sympy-1.6/LICENSE) · [副本](LICENSES/sympy.txt) |
| Pylint | GPL v2 许可证文本；具体文件可能允许后续版本 | [v2.14.0 LICENSE](https://github.com/pylint-dev/pylint/blob/v2.14.0/LICENSE) · [副本](LICENSES/pylint.txt) |

这些版本用于标明许可证副本来源，不代表所有历史实验都使用同一上游版本。复用某份补丁、测试或代码片段时，应按其任务记录中的仓库、instance ID、基线提交及对应文件声明核对许可；片段、生成修改和整个被评测项目不能仅凭根目录 MIT 推断为相同授权。

评测代码的调用与数据的使用分别对待。[SWE-bench Verified 数据集](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified)用于题目与环境信息；harness 的 MIT 许可不自动覆盖数据集中来自其他项目的所有代码、Issue 或附件。

## Issue、PR 与实验记录

GitHub API 快照中的 Issue／PR 正文、评论、账号信息及链接保留各自来源。公开可读取不等于项目作者有权将所有内容重新许可。根目录 MIT 不为其中的第三方内容授予额外权利。

项目编写的统计脚本、报告说明和示例代码适用 MIT；报告所引用的外部材料仍适用各自条款。原始补丁、日志、JSON 快照及归档 ZIP 保留原件，新增本文件和许可证副本不改写其内容或历史哈希。

## 安装依赖与分发

Python 与前端依赖保留各自许可证。具体版本由 `uv.lock` 和 `frontend/package-lock.json` 记录，依赖授权以对应发行包内的许可证与声明为准；本项目许可证不覆盖安装后的全部依赖。

Python 分发包通过 `license-files` 携带项目许可证、本文及上游许可证副本。前端静态资源中的 [patchloop.txt](frontend/public/licenses/patchloop.txt) 与根目录 LICENSE 一致，构建后位于 `/licenses/patchloop.txt`。

分发包含第三方代码的前端构建、容器或其他组合产物时，应同时保留相关组件要求的版权、许可证和 NOTICE。`frontend/package.json` 中的 `private: true` 仅防止 npm 意外发布，不影响源码采用 MIT。
