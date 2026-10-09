# patchloop-publish-demo

用于检验 Issue 修复、独立验证、人工审查和 PR 交付的小型 Python 仓库。

`calc.py` 中的 `parse_version` 有一个有意保留的问题：普通版本号可以解析，但 `v1.2.3` 会抛出 `ValueError`。期望支持一个小写 `v` 前缀，并保留普通输入和非法输入的原有行为。

测试环境为 Python 3.12。安装测试依赖并运行：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

初始结果应为 **1 failed, 10 passed**；正确修复后应为 **11 passed**。测试还检查大写 `V`、重复 `v`、空字符串等输入继续抛出 `ValueError`。

修复范围只包含 `calc.py`。保留测试文件不变，最终通过独立分支上的草稿 PR 展示修改。验收期间保持 `main` 停留在初始提交，避免目标基线在修复与发布之间改变。
