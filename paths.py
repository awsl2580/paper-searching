"""分类后的源码目录统一入口（保持原有的扁平 import）。

## 这个文件为什么存在

Python 的 `import` 是按 `sys.path` 找模块的。本项目历史上所有模块都堆在
根目录，彼此用 `import llm` / `import providers` 这种扁平写法。2026-10 做了
一次目录整理：源码按职责分到 `core/`、`web/`、`mcp/`、`cli/`、`scripts/`、
`tools/`，科学计算与文献源仍在 `science/`、`providers/`。

**没有改任何一条内部 import** —— 由这个文件把这些目录加进 `sys.path`，
扁平导入继续有效。好处是改动面最小、所有老命令/测试的写法都能延续；
代价是入口脚本必须先把这个文件 import 进来（见下）。

## 谁能用

* 入口脚本：`web/app.py`、`cli/Agent.py`、`mcp/mcp_server.py`、
  `scripts/launch.py`、`scripts/check_env.py`、`tools/view.py`
  —— 它们先把项目根插入 `sys.path`，再 `import paths`；
* 测试：`tests/*.py` 在插入 `ROOT` 后 `import paths`；
* 外部一行命令也照旧可用：

      python -c "import paths, llm, json; print(json.dumps(llm.model_info(), ensure_ascii=False))"

这个文件只依赖标准库，重复 import 无副作用。
"""

from __future__ import annotations

import os
import sys

# 项目根目录 = 本文件所在目录（数据文件、.env、.venv 都在这里）
ROOT = os.path.dirname(os.path.abspath(__file__))

# 分类后的源码目录。顺序不影响结果（模块名互不冲突），
# 但都插到 sys.path 前面，确保优先于同名的第三方包。
#
# ⚠️ 不要把 science/ 和 providers/ 本身放进来：
#    它们是**包**，项目根在 sys.path 里就能 `import providers`；
#    把包目录本身加进 sys.path 会让 `providers/http.py` 抢在标准库
#    `http` 前面被 import，进而搞坏 requests/urllib3（实测踩过）。
SOURCE_DIRS = ("core", "mcp", "web", "cli", "tools", "scripts")

for _name in SOURCE_DIRS:
    _p = os.path.join(ROOT, _name)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# 根目录也要在 sys.path 里：paths.py 本身、数据文件、以及
# 以后如果再加根级模块都要能 import 到。
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

__all__ = ["ROOT", "SOURCE_DIRS"]
