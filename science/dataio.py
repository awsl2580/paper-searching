"""数据 I/O 层：把「论文里粘出来的一段表格」变成可计算的列。

为什么需要它
------------
科研智能体拿到的表格常常是三种来源混在一起：Excel 复制出来的制表符文本、
CSV 文件、以及 Markdown 表格。如果每个计算工具都自己写一遍解析，就会出现
同一张表在不同工具里列名/缺值语义不一致的问题。所以这里统一成一个入口
``parse_table``，之后的统计、谱图、拟合工具全部消费同一种结构。

取舍
----
* 只用标准库（csv + re），不引入 numpy/pandas：MCP 工具要能在评委的裸环境里跑。
* 不做类型推断的"魔法"：能转 float 就转，转不了就原样保留字符串，最终由
  ``numeric`` 字段告诉调用方这一列能不能当数值用。
* 空单元格一律映射为 ``None``（而不是 0），因为"缺失"和"零"在科研数据里
  含义完全不同，统计工具会根据 ``None`` 主动剔除。
"""

from __future__ import annotations

import csv
import io
import re

__all__ = ["parse_table", "to_columns", "numbers"]

# 科学计数法也要认：1e-3 / -2.5E+4
_NUM_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")

# Markdown 分隔行只由 | - : 和空白组成，且至少含一个 '-'
_MD_SEP_CHARS = set("|-: \t")
# 多空格 / 制表符分隔
_WS_SPLIT_RE = re.compile(r"\t+|\s{2,}")


def _parse_cell(cell):
    """把单个单元格归一化：空 -> None，能转数字就转 float，否则保留字符串。"""
    if cell is None:
        return None
    s = cell.strip()
    if s == "":
        return None
    # 去掉 Markdown 里常见的粗体 /**/ 与行内代码包裹
    if len(s) >= 2 and ((s[0] == "*" and s[-1] == "*") or (s[0] == "`" and s[-1] == "`")):
        s = s[1:-1].strip()
        if s == "":
            return None
    if _NUM_RE.match(s):
        try:
            return float(s)
        except ValueError:  # 理论上不会发生，防御性兜底
            return s
    return s


def _is_md_separator(line: str) -> bool:
    """判断一行是不是 Markdown 表格的 |---|---| 分隔行。"""
    s = line.strip()
    if "-" not in s or "|" not in s:
        return False
    if any(ch not in _MD_SEP_CHARS for ch in s):
        return False
    # 必须真的像分隔行：每一格至少两个连字符
    for seg in s.strip("|").split("|"):
        seg = seg.strip()
        if seg and "-" not in seg:
            return False
    return True


def _split_markdown_row(line: str) -> list:
    """拆 Markdown 管道行：去掉首尾竖线后再按 | 切分。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _split_plain_row(line: str, use_csv: bool) -> list:
    """拆普通行：优先按 CSV 引号规则，否则按制表符/多空格。"""
    if use_csv:
        try:
            return next(csv.reader(io.StringIO(line)))
        except (csv.Error, StopIteration):
            pass
    if "\t" in line:
        return [c.strip() for c in re.split(r"\t+", line.strip())]
    return [c.strip() for c in _WS_SPLIT_RE.split(line.strip())]


def _dedup_columns(columns: list) -> list:
    """列名去重：同名列依次加 _2 / _3 后缀，避免按列取数时互相覆盖。"""
    seen: dict = {}
    out = []
    for i, raw in enumerate(columns, start=1):
        name = raw if raw not in (None, "") else "c%d" % i
        name = str(name).strip()
        if name == "":
            name = "c%d" % i
        if name in seen:
            seen[name] += 1
            name = "%s_%d" % (name, seen[name])
        else:
            seen[name] = 1
        out.append(name)
    return out


def parse_table(text: str) -> dict:
    """解析文本表格，返回统一的列式 + 行式结构。

    支持三种输入格式：
      1. 逗号分隔 CSV（含引号包裹的字段）
      2. 制表符或多空格分隔的文本（Excel 直接粘贴的形态）
      3. Markdown 管道表格（第一行或第二行是 |---|---| 分隔行）

    表头规则：第一行默认当表头；若第一行所有单元格都是数字，则自动生成
    c1..cn 作为列名，并把第一行也当作数据行。

    返回::

        {"columns": [...], "rows": [[float|str|None, ...], ...],
         "n_rows": int, "n_cols": int, "numeric": {列名: bool}}

    空单元格 -> None；行长度不足补 None，超出截断。
    """
    if text is None or (isinstance(text, str) and text.strip() == ""):
        raise ValueError("表格内容为空")
    if not isinstance(text, str):
        raise ValueError("表格内容必须是一段文本")

    # 丢掉纯空行：Markdown 表格前后常有空行，不能让它变成一行 None
    raw_lines = [ln for ln in text.splitlines() if ln.strip() != ""]
    if not raw_lines:
        raise ValueError("表格内容为空")

    # ---- 1. 判定格式 ----
    # Markdown：第一行是表头、第二行是 |---|---| 分隔行
    is_markdown = len(raw_lines) >= 2 and _is_md_separator(raw_lines[1])
    use_csv = (not is_markdown) and ("," in raw_lines[0]) and ("\t" not in raw_lines[0])

    header_cells = None
    if is_markdown:
        header_cells = _split_markdown_row(raw_lines[0])
        data_lines = raw_lines[2:]  # 跳过表头行和分隔行
    else:
        data_lines = raw_lines[1:]

    # ---- 2. 判定首行是不是表头（全是数字则当数据行，自动命名 c1..cn）----
    if header_cells is None:
        first = [_parse_cell(c) for c in _split_plain_row(raw_lines[0], use_csv)]
        first_has_data = any(c is not None for c in first)
        all_numeric = first_has_data and all(
            c is None or isinstance(c, float) for c in first)
        if all_numeric:
            header_cells = ["c%d" % i for i in range(1, len(first) + 1)]
            data_lines = raw_lines
        else:
            header_cells = _split_plain_row(raw_lines[0], use_csv)

    columns = _dedup_columns(header_cells)
    n_cols = len(columns)
    if n_cols == 0:
        raise ValueError("表格内容为空")

    # ---- 3. 逐行归一化，补齐/截断到 n_cols ----
    rows = []
    for line in data_lines:
        if is_markdown:
            cells = _split_markdown_row(line)
        else:
            cells = _split_plain_row(line, use_csv)
        if not cells or all(c.strip() == "" for c in cells):
            continue
        row = [_parse_cell(c) for c in cells[:n_cols]]
        if len(row) < n_cols:
            row.extend([None] * (n_cols - len(row)))
        rows.append(row)

    # ---- 4. 判定每列是否可以当数值列 ----
    numeric = {}
    for j, name in enumerate(columns):
        col = [r[j] for r in rows]
        has_num = any(isinstance(v, float) for v in col)
        only_num_or_none = all(v is None or isinstance(v, float) for v in col)
        numeric[name] = bool(has_num and only_num_or_none)

    return {
        "columns": columns,
        "rows": rows,
        "n_rows": len(rows),
        "n_cols": n_cols,
        "numeric": numeric,
    }


def to_columns(table) -> dict:
    """把 ``parse_table`` 的结果转成 {列名: [值...]}，便于按列取数。

    也接受原始文本（内部先调 ``parse_table``）。
    """
    if isinstance(table, str):
        table = parse_table(table)
    if not isinstance(table, dict):
        raise ValueError("table 必须是 parse_table 的返回值或表格文本")
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    out = {name: [] for name in columns}
    for row in rows:
        for j, name in enumerate(columns):
            out[name].append(row[j] if j < len(row) else None)
    return out


def numbers(values) -> list:
    """过滤 None / 空串 / 非数字，返回纯 float 列表。

    接受 list、tuple，也接受 ``"1,2,3"`` 这类逗号串。
    """
    if values is None:
        return []
    if isinstance(values, str):
        parts = [p for p in re.split(r"[,\s;]+", values.strip()) if p != ""]
        items = parts
    elif isinstance(values, (list, tuple)):
        items = list(values)
    else:
        items = [values]

    out = []
    for v in items:
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out.append(float(v))
            continue
        try:
            out.append(float(str(v).strip()))
        except (TypeError, ValueError):
            continue
    return out
