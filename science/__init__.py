"""科学计算工具层（science）：给科研智能体一套真能算、可复现的标准库工具。

为什么需要它
------------
把科学计算交给 LLM"心算"是不可接受的：p 值、分子量、峰位必须来自确定性
代码。本包用**纯标准库**实现表格解析、统计检验、谱图解析、曲线拟合、分子
描述符、实验设计六类能力，并通过 ``TOOLS`` 注册成 MCP 形状的工具清单，
交给 Agent 工作流按名字调用。

设计要点
--------
* ``ToolSpec.schema`` 是标准 JSON Schema，键名与 MCP ``tools/list`` 对齐
  （``inputSchema``），方便直接挂到 MCP server 上。
* ``call_tool`` 是唯一的执行入口：它不抛异常，把所有失败折叠成
  ``{"ok": False, "error": "中文可读信息"}``，这样 Agent 循环不会因为一次
  坏参数而中断。
* ``repro`` 是"可复现性凭据"：工具名 + 归一化参数 + 解释器版本 + 模块路径，
  报告附录里的复现清单直接由它生成。
* 参数做"宽松归一化"：既接受 ``[1,2,3]``，也接受 ``"1,2,3"`` 这种从表格里
  粘出来的逗号串——LLM 经常这么传。

只依赖标准库；不含随机性（唯一例外是 ``lhs_design``，必须显式传 seed）。
"""

from __future__ import annotations

import platform
import time

from science import curves, dataio, design, molecules, spectra, stats

__all__ = [
    "curves", "dataio", "design", "molecules", "spectra", "stats",
    "ToolSpec", "TOOLS", "list_tools", "tool_spec", "call_tool", "describe",
    "names", "__version__",
]

__version__ = "1.0.0"


# ---------------------------------------------------------------------------
# 工具描述对象
# ---------------------------------------------------------------------------
class ToolSpec(object):
    """一个可被 LLM 调用的科学计算工具。

    属性
    ----
    name        工具名（稳定标识，如 "stats_ttest"）
    title       中文短标题
    description 中文说明：用途 + 输入格式（直接喂给 LLM 当工具描述）
    schema      JSON Schema（type="object"），描述参数
    tags        中文标签，便于分类检索
    handler     callable(args: dict) -> dict，自己负责参数校验
    case        复现路径（如 "science.stats.ttest"）
    """

    __slots__ = ("name", "title", "description", "schema", "tags", "handler", "case")

    def __init__(self, name, title, description, schema, tags, handler, case=None):
        self.name = name
        self.title = title
        self.description = description
        self.schema = schema
        self.tags = list(tags or [])
        self.handler = handler
        self.case = case or "%s.%s" % (getattr(handler, "__module__", "science"),
                                       getattr(handler, "__name__", name))

    def to_mcp(self) -> dict:
        """转成 MCP ``tools/list`` 里的单个工具条目。"""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.schema,
            "tags": list(self.tags),
        }

    def __repr__(self):  # pragma: no cover - 仅调试用
        return "<ToolSpec %s %s>" % (self.name, self.title)


# ---------------------------------------------------------------------------
# 参数归一化：LLM 传的东西经常是"表格里粘出来的字符串"
# ---------------------------------------------------------------------------
def _floats(value, field: str = "数值"):
    """把 list / 逗号串 / 单个数字统一成 float 列表；非法就抛中文 ValueError。"""
    if value is None:
        raise ValueError("缺少参数 %s，请提供数值数组" % field)
    if isinstance(value, str):
        parts = [p for p in value.replace("，", ",").replace(",", " ").split() if p]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    elif isinstance(value, bool):
        raise ValueError("参数 %s 不能是布尔值" % field)
    elif isinstance(value, (int, float)):
        parts = [value]
    else:
        raise ValueError("参数 %s 必须是数值数组，例如 [1,2,3]" % field)

    out = []
    for p in parts:
        if p is None or p == "":
            continue
        if isinstance(p, bool):
            raise ValueError("参数 %s 中不能含布尔值" % field)
        try:
            f = float(p)
        except (TypeError, ValueError):
            raise ValueError("参数 %s 中含非法数值：%r" % (field, p))
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError("参数 %s 中含 NaN/Infinity" % field)
        out.append(f)
    if not out:
        raise ValueError("参数 %s 为空，请提供至少一个数值" % field)
    return out


def _matrix(value, field: str = "矩阵"):
    """二维数组归一化：接受 [[..],[..]]、逗号/分号/换行分隔的多行文本。"""
    if value is None:
        raise ValueError("缺少参数 %s，请提供二维数组" % field)
    if isinstance(value, str):
        rows = [r for r in value.replace(";", "\n").splitlines() if r.strip()]
        return [_floats(r, field) for r in rows]
    if isinstance(value, (list, tuple)):
        if not value:
            raise ValueError("参数 %s 不能为空" % field)
        if all(not isinstance(r, (list, tuple)) for r in value):
            return [_floats(value, field)]  # 一维：当成单行
        return [_floats(r, "%s 的第 %d 行" % (field, i + 1))
                for i, r in enumerate(value)]
    raise ValueError("参数 %s 必须是二维数组，例如 [[10,20],[30,40]]" % field)


def _need(args: dict, key: str):
    if key not in args or args[key] is None:
        raise ValueError("缺少必填参数 %s" % key)
    return args[key]


def _opt_int(args: dict, key: str, default):
    v = args.get(key)
    if v is None or v == "":
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        raise ValueError("参数 %s 必须是整数" % key)


def _opt_float(args: dict, key: str, default):
    v = args.get(key)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError("参数 %s 必须是数值" % key)


def _table_values(args: dict) -> list:
    """从 values / table(+/column) 里取出要统计的一列数值。"""
    if args.get("values") is not None:
        return _floats(args["values"], "values")

    tbl = args.get("table")
    if tbl is None:
        raise ValueError("需要 values（数值数组）或 table（表格文本）其中之一")

    if isinstance(tbl, str):
        table = dataio.parse_table(tbl)
    elif isinstance(tbl, dict):
        table = tbl
    elif isinstance(tbl, (list, tuple)):
        table = dataio.parse_table("\n".join(
            ",".join("" if c is None else str(c) for c in row) for row in tbl))
    else:
        raise ValueError("table 必须是表格文本或 parse_table 的返回结果")

    cols = dataio.to_columns(table)
    col = args.get("column")
    if col not in (None, ""):
        if col not in cols:
            raise ValueError("列 %s 不存在，可选列：%s" % (col, ", ".join(cols.keys())))
        vals = [v for v in cols[col] if isinstance(v, (int, float))
                and not isinstance(v, bool)]
        if not vals:
            raise ValueError("列 %s 中没有数值" % col)
        return vals

    numeric = table.get("numeric") or {}
    names = [n for n in table.get("columns", []) if numeric.get(n)]
    if not names:
        raise ValueError("表格中没有可识别的数值列，请指定 column 或改用 values")
    out = []
    for n in names:
        out.extend(v for v in cols[n] if isinstance(v, (int, float))
                   and not isinstance(v, bool))
    if not out:
        raise ValueError("表格中没有数值")
    return out


# ---------------------------------------------------------------------------
# 各工具的 handler
# ---------------------------------------------------------------------------
def _h_table_parse(args):
    text = _need(args, "text")
    if not isinstance(text, str):
        raise ValueError("参数 text 必须是表格文本")
    return dataio.parse_table(text)


def _h_stats_describe(args):
    return stats.describe(_table_values(args))


def _h_stats_ttest(args):
    return stats.ttest(_floats(_need(args, "x"), "x"),
                       _floats(_need(args, "y"), "y"),
                       equal_var=bool(args.get("equal_var", False)))


def _h_stats_ttest_one_sample(args):
    return stats.ttest_one_sample(_floats(_need(args, "x"), "x"),
                                  _opt_float(args, "mu", 0.0))


def _h_stats_proportion(args):
    def _count(name):
        raw = _need(args, name)
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"参数 {name} 必须是整数，收到 {raw!r}")
    return stats.proportion(_count("successes"), _count("n"), _opt_float(args, "p0", 0.5))


def _h_stats_fisher(args):
    def _int(name):
        raw = _need(args, name)
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"参数 {name} 必须是整数，收到 {raw!r}")
    return stats.fisher_exact(_int("a"), _int("b"), _int("c"), _int("d"))


def _h_stats_proportion_two(args):
    def _int(name):
        raw = _need(args, name)
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"参数 {name} 必须是整数，收到 {raw!r}")
    return stats.proportion_two(_int("s1"), _int("n1"), _int("s2"), _int("n2"))


def _h_stats_mannwhitney(args):
    return stats.mannwhitney(_floats(_need(args, "x"), "x"),
                            _floats(_need(args, "y"), "y"))


def _h_stats_anova(args):
    groups = _matrix(_need(args, "groups"), "groups")
    if len(groups) < 2:
        raise ValueError("groups 至少需要 2 组，例如 [[1,2,3],[4,5,6]]")
    return stats.anova(groups)


def _h_stats_chi_square(args):
    return stats.chi_square(_matrix(_need(args, "observed"), "observed"))


def _h_stats_correlation(args):
    method = args.get("method") or "pearson"
    return stats.correlation(_floats(_need(args, "x"), "x"),
                            _floats(_need(args, "y"), "y"), method=method)


def _h_stats_regression(args):
    return stats.linear_regression(_floats(_need(args, "x"), "x"),
                                   _floats(_need(args, "y"), "y"))


def _h_spectra_smooth(args):
    y = _floats(_need(args, "y"), "y")
    return spectra.smooth(y, window=_opt_int(args, "window", 5),
                          poly=_opt_int(args, "poly", 2))


def _h_spectra_peaks(args):
    y = _floats(_need(args, "y"), "y")
    x = args.get("x")
    xs = _floats(x, "x") if x not in (None, "") else None
    return spectra.peaks(xs, y,
                         min_prominence=_opt_float(args, "min_prominence", None),
                         min_height=_opt_float(args, "min_height", None),
                         min_distance=_opt_int(args, "min_distance", 1))


def _h_spectra_snr(args):
    return spectra.snr(_floats(_need(args, "y"), "y"))


def _h_spectra_integrate(args):
    y = _floats(_need(args, "y"), "y")
    x = args.get("x")
    xs = _floats(x, "x") if x not in (None, "") else None
    return spectra.integrate(xs, y,
                             a=_opt_float(args, "a", None),
                             b=_opt_float(args, "b", None))


def _h_spectra_baseline(args):
    y = _floats(_need(args, "y"), "y")
    x = args.get("x")
    xs = _floats(x, "x") if x not in (None, "") else None
    return spectra.baseline_correct(xs, y, method=args.get("method") or "poly",
                                   degree=_opt_int(args, "degree", 3))


def _h_spectra_summary(args):
    y = _floats(_need(args, "y"), "y")
    x = args.get("x")
    xs = _floats(x, "x") if x not in (None, "") else None
    return spectra.spectrum_summary(xs, y)


def _h_curves_fit(args):
    model = args.get("model") or "linear"
    return curves.fit(_floats(_need(args, "x"), "x"),
                      _floats(_need(args, "y"), "y"), model=model)


def _h_curves_predict(args):
    params = _need(args, "params")
    if not isinstance(params, dict):
        raise ValueError("参数 params 必须是对象，例如 {\"a\": 3, \"b\": 0.5}")
    model = args.get("model") or "linear"
    return curves.predict(model, params, _floats(_need(args, "xs"), "xs"))


def _h_curves_extrapolate(args):
    model = args.get("model") or "linear"
    return curves.extrapolate(_floats(_need(args, "x"), "x"),
                              _floats(_need(args, "y"), "y"),
                              model, _floats(_need(args, "xs"), "xs"))


def _h_molecule_parse(args):
    smiles = _need(args, "smiles")
    if not isinstance(smiles, str):
        raise ValueError("参数 smiles 必须是 SMILES 字符串")
    return molecules.parse(smiles)


def _h_molecule_descriptors(args):
    smiles = _need(args, "smiles")
    if not isinstance(smiles, str):
        raise ValueError("参数 smiles 必须是 SMILES 字符串")
    return molecules.descriptors(smiles)


def _h_molecule_batch(args):
    raw = _need(args, "smiles")
    if isinstance(raw, str):
        items = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise ValueError("参数 smiles 必须是字符串数组，例如 [\"CCO\", \"c1ccccc1\"]")
    if not items:
        raise ValueError("参数 smiles 为空")
    return molecules.batch(items)


def _h_design_factorial(args):
    factors = _need(args, "factors")
    if isinstance(factors, str):
        names = [p.strip() for p in factors.replace("，", ",").split(",") if p.strip()]
    elif isinstance(factors, (list, tuple)):
        names = [str(f).strip() for f in factors if str(f).strip()]
    else:
        raise ValueError("参数 factors 必须是因子名数组，例如 [\"温度\",\"pH\"]")
    return design.factorial_design(names, levels=_opt_int(args, "levels", 2))


def _h_design_lhs(args):
    if args.get("n_factors") is None:
        raise ValueError("缺少必填参数 n_factors（因子个数）")
    if args.get("n_runs") is None:
        raise ValueError("缺少必填参数 n_runs（实验次数）")
    return design.lhs_design(_opt_int(args, "n_factors", 1),
                             _opt_int(args, "n_runs", 2),
                             _opt_int(args, "seed", 42))


def _h_design_power(args):
    return design.power_analysis(_opt_float(args, "effect_size", 0.5),
                                 _opt_float(args, "alpha", 0.05),
                                 _opt_float(args, "power", 0.8))


def _h_design_sweep(args):
    params = _need(args, "params")
    if not isinstance(params, dict):
        raise ValueError("参数 params 必须是对象，例如 {\"a\":3,\"b\":0.5}")
    if args.get("x_start") is None:
        raise ValueError("缺少必填参数 x_start（扫描起点）")
    if args.get("x_stop") is None:
        raise ValueError("缺少必填参数 x_stop（扫描终点）")
    return design.sweep(args.get("model") or "linear", params,
                        _opt_float(args, "x_start", 0.0),
                        _opt_float(args, "x_stop", 1.0),
                        _opt_int(args, "steps", 21))


def _h_design_grid_search(args):
    grid = _need(args, "params_grid")
    if not isinstance(grid, dict):
        raise ValueError("参数 params_grid 必须是对象，例如 {\"a\":[1,2],\"b\":[0.1,0.5]}")
    return design.grid_search(args.get("model") or "linear", grid,
                              _floats(_need(args, "x"), "x"),
                              _floats(_need(args, "y"), "y"))


# ---------------------------------------------------------------------------
# Schema 片段
# ---------------------------------------------------------------------------
def _arr(desc: str) -> dict:
    return {"type": "array", "items": {"type": "number"}, "description": desc}


def _mat(desc: str) -> dict:
    return {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
            "description": desc}


_SCHEMA_TABLE = {
    "type": "object",
    "properties": {
        "text": {"type": "string",
                 "description": "整段表格文本：CSV（逗号）、制表符/多空格分隔、或 Markdown 管道表格"},
    },
    "required": ["text"],
}

_SCHEMA_DESCRIBE = {
    "type": "object",
    "properties": {
        "values": _arr("一列数值，例如 [1,2,3,4]；也可传 \"1,2,3,4\" 逗号串"),
        "table": {"type": "string",
                  "description": "表格文本（CSV/TSV/Markdown），与 values 二选一"},
        "column": {"type": "string", "description": "从表格里取哪一列；省略则合并所有数值列"},
    },
    "required": [],
}

_SCHEMA_XY = {
    "type": "object",
    "properties": {
        "x": _arr("第一组数值，例如 [5.1,4.9,5.0]；也可传 \"5.1,4.9,5.0\""),
        "y": _arr("第二组数值，例如 [4.9,4.8,5.1]；长度可与 x 不同"),
    },
    "required": ["x", "y"],
}

_SCHEMA_ONE_SAMPLE = {
    "type": "object",
    "properties": {
        "x": _arr("样本数值，例如 [1.2,1.4,1.1,1.3]"),
        "mu": {"type": "number", "description": "待检验的总体均值，默认 0"},
    },
    "required": ["x"],
}

_SCHEMA_ANOVA = {
    "type": "object",
    "properties": {
        "groups": _mat("多组数据，二维数组，例如 [[1,2,3],[4,5,6],[7,8,9]]"),
    },
    "required": ["groups"],
}

_SCHEMA_CHI = {
    "type": "object",
    "properties": {
        "observed": _mat("二维列联表（行=类别1，列=类别2），例如 [[10,20],[30,40]]"),
    },
    "required": ["observed"],
}

_SCHEMA_CORR = {
    "type": "object",
    "properties": {
        "x": _arr("第一个变量，例如 [1,2,3,4,5]"),
        "y": _arr("第二个变量，与 x 等长"),
        "method": {"type": "string", "enum": ["pearson", "spearman"],
                   "description": "相关方法，默认 pearson；单调非线性用 spearman"},
    },
    "required": ["x", "y"],
}

_SCHEMA_SMOOTH = {
    "type": "object",
    "properties": {
        "y": _arr("信号强度数组，例如 [0.1,0.5,1.0,0.4,0.2]"),
        "window": {"type": "integer", "description": "窗口点数，必须是大于 poly 的奇数，默认 5；偶数会自动 +1"},
        "poly": {"type": "integer", "description": "拟合多项式阶数，默认 2"},
    },
    "required": ["y"],
}

_SCHEMA_PEAKS = {
    "type": "object",
    "properties": {
        "x": _arr("自变量的数组（如 2θ、波数），省略则按索引 0..n-1"),
        "y": _arr("信号强度数组，长度与 x 一致"),
        "min_prominence": {"type": "number",
                           "description": "最小峰突出度，省略则取 0.05*(max-min)"},
        "min_height": {"type": "number", "description": "最小峰高，省略则不设限"},
        "min_distance": {"type": "integer", "description": "相邻峰的最小索引间隔，默认 1"},
    },
    "required": ["y"],
}

_SCHEMA_SNR = {
    "type": "object",
    "properties": {"y": _arr("信号强度数组")},
    "required": ["y"],
}

_SCHEMA_INTEGRATE = {
    "type": "object",
    "properties": {
        "y": _arr("信号强度数组"),
        "x": _arr("自变量数组，省略则按索引"),
        "a": {"type": "number", "description": "积分下限，省略取 x 最小值"},
        "b": {"type": "number", "description": "积分上限，省略取 x 最大值"},
    },
    "required": ["y"],
}

_SCHEMA_BASELINE = {
    "type": "object",
    "properties": {
        "y": _arr("信号强度数组"),
        "x": _arr("自变量数组，省略则按索引"),
        "method": {"type": "string", "enum": ["poly"], "description": "基线类型，目前只支持 poly"},
        "degree": {"type": "integer", "description": "多项式阶数，默认 3"},
    },
    "required": ["y"],
}

_SCHEMA_SUMMARY = {
    "type": "object",
    "properties": {
        "x": _arr("自变量数组，省略则按索引"),
        "y": _arr("信号强度数组"),
    },
    "required": ["y"],
}

_SCHEMA_FIT = {
    "type": "object",
    "properties": {
        "x": _arr("自变量数组，例如 [0,1,2,3,4]"),
        "y": _arr("因变量数组，长度与 x 一致"),
        "model": {"type": "string",
                  "enum": ["linear", "exponential", "logistic", "power", "gaussian"],
                  "description": "模型：linear/ exponential/ logistic/ power/ gaussian，默认 linear"},
    },
    "required": ["x", "y"],
}

_SCHEMA_PREDICT = {
    "type": "object",
    "properties": {
        "model": {"type": "string",
                  "enum": ["linear", "exponential", "logistic", "power", "gaussian"],
                  "description": "模型名"},
        "params": {"type": "object",
                   "description": "参数字典，如 {\"a\":3,\"b\":0.5} 或 {\"L\":10,\"k\":2,\"x0\":5}",
                   "additionalProperties": {"type": "number"}},
        "xs": _arr("要预测的自变量数组，例如 [0,0.5,1,1.5]"),
    },
    "required": ["model", "params", "xs"],
}

_SCHEMA_EXTRAPOLATE = {
    "type": "object",
    "properties": {
        "x": _arr("自变量数组"),
        "y": _arr("因变量数组"),
        "model": {"type": "string",
                  "enum": ["linear", "exponential", "logistic", "power", "gaussian"],
                  "description": "模型名"},
        "xs": _arr("要外推/预测的自变量数组"),
    },
    "required": ["x", "y", "model", "xs"],
}

_SCHEMA_MOL = {
    "type": "object",
    "properties": {
        "smiles": {"type": "string",
                   "description": "SMILES 字符串，如 \"c1ccccc1\"（苯）、\"CC(=O)O\"（乙酸）"},
    },
    "required": ["smiles"],
}

_SCHEMA_MOL_BATCH = {
    "type": "object",
    "properties": {
        "smiles": {"type": "array", "items": {"type": "string"},
                   "description": "SMILES 字符串数组，如 [\"CCO\",\"c1ccccc1\"]"},
    },
    "required": ["smiles"],
}

_SCHEMA_FACTORIAL = {
    "type": "object",
    "properties": {
        "factors": {"type": "array", "items": {"type": "string"},
                    "description": "因子名数组，如 [\"温度\",\"pH\",\"时间\"]"},
        "levels": {"type": "integer",
                   "description": "每个因子的水平数，默认 2（编码 -1/+1）；>2 时要求 k<=3"},
    },
    "required": ["factors"],
}

_SCHEMA_LHS = {
    "type": "object",
    "properties": {
        "n_factors": {"type": "integer", "description": "因子个数"},
        "n_runs": {"type": "integer", "description": "实验次数"},
        "seed": {"type": "integer", "description": "随机种子，默认 42（同种子结果完全一致）"},
    },
    "required": ["n_factors", "n_runs"],
}

_SCHEMA_POWER = {
    "type": "object",
    "properties": {
        "effect_size": {"type": "number", "description": "标准化效应量 Cohen's d，默认 0.5"},
        "alpha": {"type": "number", "description": "显著性水平，默认 0.05"},
        "power": {"type": "number", "description": "目标统计功效，默认 0.8"},
    },
    "required": [],
}

_SCHEMA_SWEEP = {
    "type": "object",
    "properties": {
        "model": {"type": "string",
                  "enum": ["linear", "exponential", "logistic", "power", "gaussian"],
                  "description": "模型名"},
        "params": {"type": "object",
                   "description": "模型参数，如 {\"a\":2,\"mu\":5,\"sigma\":1}",
                   "additionalProperties": {"type": "number"}},
        "x_start": {"type": "number", "description": "扫描起点"},
        "x_stop": {"type": "number", "description": "扫描终点"},
        "steps": {"type": "integer", "description": "扫描点数，默认 21"},
    },
    "required": ["model", "params", "x_start", "x_stop"],
}

_SCHEMA_GRID = {
    "type": "object",
    "properties": {
        "model": {"type": "string",
                  "enum": ["linear", "exponential", "logistic", "power", "gaussian"],
                  "description": "模型名"},
        "params_grid": {"type": "object",
                        "description": "每个参数的候选值列表，如 {\"a\":[1,2,3],\"b\":[0.3,0.5]}",
                        "additionalProperties": {"type": "array", "items": {"type": "number"}}},
        "x": _arr("自变量数组"),
        "y": _arr("因变量数组"),
    },
    "required": ["model", "params_grid", "x", "y"],
}


# 单样本比例检验：successes / n 是"计数"，不是数组 —— 故意让它比传数组更省心，
# 因为"33 篇里 21 篇是 GNN"这种数据本来就只有两个数。
_SCHEMA_PROPORTION = {
    "type": "object",
    "properties": {
        "successes": {"type": "integer", "description": "成功（命中）的样本数，如 21"},
        "n": {"type": "integer", "description": "总样本数，如 33"},
        "p0": {"type": "number", "description": "零假设下的比例，默认 0.5", "default": 0.5},
    },
    "required": ["successes", "n"],
}


_SCHEMA_FISHER = {
    "type": "object",
    "properties": {
        "a": {"type": "integer", "description": "第 1 行第 1 列频数"},
        "b": {"type": "integer", "description": "第 1 行第 2 列频数"},
        "c": {"type": "integer", "description": "第 2 行第 1 列频数"},
        "d": {"type": "integer", "description": "第 2 行第 2 列频数"},
    },
    "required": ["a", "b", "c", "d"],
}

_SCHEMA_PROP_TWO = {
    "type": "object",
    "properties": {
        "s1": {"type": "integer", "description": "组 1 的命中数"},
        "n1": {"type": "integer", "description": "组 1 的总数"},
        "s2": {"type": "integer", "description": "组 2 的命中数"},
        "n2": {"type": "integer", "description": "组 2 的总数"},
    },
    "required": ["s1", "n1", "s2", "n2"],
}


# ---------------------------------------------------------------------------
# 注册表（插入顺序即 list_tools 顺序）
# ---------------------------------------------------------------------------
_TOOL_DEFS = [
    (
        "table_parse", "表格解析",
        "把一段表格文本解析成列名+数值矩阵；输入 text 为整段表格字符串，支持 CSV（逗号分隔）、"
        "制表符/多空格分隔、Markdown 管道表格；返回 {columns, rows, n_rows, n_cols, numeric}，"
        "空单元格为 null，非数字保留字符串。",
        _SCHEMA_TABLE, ["数据", "表格"], "science.dataio.parse_table", _h_table_parse,
    ),
    (
        "stats_describe", "描述统计",
        "计算一列数值的 n/mean/sd/min/q1/median/q3/max/iqr/skew/cv；"
        "输入 values 为数值数组（如 [1,2,3,4] 或 \"1,2,3,4\"），或改用 table + column 从表格里取列。",
        _SCHEMA_DESCRIBE, ["统计", "描述统计"], "science.stats.describe", _h_stats_describe,
    ),
    (
        "stats_ttest", "双样本 t 检验（Welch）",
        "比较两组均值差异是否显著（双侧）；输入 x、y 为两个数值数组，返回 t/df/p/cohens_d/sig_0_05。",
        _SCHEMA_XY, ["统计", "假设检验"], "science.stats.ttest", _h_stats_ttest,
    ),
    (
        "stats_ttest_one_sample", "单样本 t 检验",
        "检验一个样本的均值是否等于给定值 mu（双侧）；输入 x 为数值数组、mu 为待检验均值，"
        "返回 t/df/p/sig_0_05。",
        _SCHEMA_ONE_SAMPLE, ["统计", "假设检验"],
        "science.stats.ttest_one_sample", _h_stats_ttest_one_sample,
    ),
    (
        "stats_proportion", "单样本比例检验（精确二项）",
        "检验观测比例是否显著不同于 p0：输入 successes（命中数）、n（总数）、p0（默认 0.5）；"
        "返回 p_hat/p（精确二项）/p_z（正态近似）/z/ci95/sig_0_05。"
        "用于\"某某占比是否超过一半\"这类假设；小样本（n<30）只有精确 p 可信。",
        _SCHEMA_PROPORTION, ["统计", "假设检验"],
        "science.stats.proportion", _h_stats_proportion,
    ),
    (
        "stats_fisher", "Fisher 精确检验（2×2）",
        "小样本（或卡方期望频数 <5）时比较两组比例差异的精确检验；"
        "输入 a/b/c/d 为 2×2 列联表四个格子频数；返回 p（精确双侧）/odds_ratio/sig_0_05。",
        _SCHEMA_FISHER, ["统计", "假设检验"],
        "science.stats.fisher_exact", _h_stats_fisher,
    ),
    (
        "stats_proportion_two", "两比例 z 检验",
        "比较两组比例是否有差异（组间比较）；输入 s1/n1/s2/n2（各自命中数与总数）；"
        "返回 p1/p2/diff/z/p/ci95_diff/odds_ratio。小样本请改用 stats_fisher。",
        _SCHEMA_PROP_TWO, ["统计", "假设检验"],
        "science.stats.proportion_two", _h_stats_proportion_two,
    ),
    (
        "stats_mannwhitney", "Mann-Whitney U 检验",
        "非参数的两组差异检验（双侧，正态近似）；输入 x、y 为两个数值数组，返回 U/z/p 中位数对比。",
        _SCHEMA_XY, ["统计", "假设检验"], "science.stats.mannwhitney", _h_stats_mannwhitney,
    ),
    (
        "stats_anova", "单因素方差分析",
        "比较多组均值（one-way ANOVA）；输入 groups 为二维数组，如 [[1,2,3],[4,5,6]]，"
        "返回 F/df/p/eta_squared/sig_0_05。",
        _SCHEMA_ANOVA, ["统计", "假设检验"], "science.stats.anova", _h_stats_anova,
    ),
    (
        "stats_chi_square", "卡方独立性检验",
        "检验二维列联表中两个分类变量是否独立；输入 observed 为二维频数表，如 [[10,20],[30,40]]，"
        "返回 chi2/df/p/cramers_v/expected。",
        _SCHEMA_CHI, ["统计", "假设检验"], "science.stats.chi_square", _h_stats_chi_square,
    ),
    (
        "stats_correlation", "相关分析",
        "计算两个变量的相关系数与显著性；输入 x、y 等长数值数组，method 取 pearson 或 spearman，"
        "返回 r/p/r2/strength。",
        _SCHEMA_CORR, ["统计", "相关"], "science.stats.correlation", _h_stats_correlation,
    ),
    (
        "stats_regression", "一元线性回归",
        "最小二乘拟合 y=slope*x+intercept；输入 x、y 等长数组，返回 slope/intercept/r2/rmse/"
        "stderr_slope/p/formula。",
        _SCHEMA_XY, ["统计", "回归"], "science.stats.linear_regression", _h_stats_regression,
    ),
    (
        "spectra_smooth", "Savitzky-Golay 平滑",
        "用最小二乘多项式平滑谱图去噪；输入 y 为强度数组，window 为奇数窗口（默认 5），"
        "poly 为多项式阶数（默认 2），返回 smoothed 与 window_used。",
        _SCHEMA_SMOOTH, ["谱图", "去噪"], "science.spectra.smooth", _h_spectra_smooth,
    ),
    (
        "spectra_peaks", "谱图找峰",
        "检测局部极大峰并给出位置/突出度/半高宽/峰面积；输入 y 为强度数组、x 为自变量数组（可省略），"
        "可选 min_prominence、min_height、min_distance。",
        _SCHEMA_PEAKS, ["谱图", "找峰"], "science.spectra.peaks", _h_spectra_peaks,
    ),
    (
        "spectra_snr", "信噪比估计",
        "用一阶差分 MAD 稳健估计噪声并给出信噪比；输入 y 为强度数组，返回 snr/noise/signal。",
        _SCHEMA_SNR, ["谱图", "噪声"], "science.spectra.snr", _h_spectra_snr,
    ),
    (
        "spectra_integrate", "梯形积分",
        "对谱图/曲线做梯形积分，可用 a、b 指定区间；输入 y 为强度数组、x 可省略，返回 area/a/b。",
        _SCHEMA_INTEGRATE, ["谱图", "积分"], "science.spectra.integrate", _h_spectra_integrate,
    ),
    (
        "spectra_baseline", "基线校正",
        "迭代多项式拟合估计基线并从信号中扣除；输入 y 为强度数组、x 可省略，degree 默认 3，"
        "返回 corrected 与 baseline。",
        _SCHEMA_BASELINE, ["谱图", "基线"], "science.spectra.baseline_correct",
        _h_spectra_baseline,
    ),
    (
        "spectra_summary", "谱图一句话摘要",
        "一步完成平滑+信噪比+找峰，返回 n/snr/peak_count/main_peak/top_peaks/baseline 以及中文概述；"
        "输入 y 为强度数组、x 可省略。",
        _SCHEMA_SUMMARY, ["谱图", "摘要"], "science.spectra.spectrum_summary",
        _h_spectra_summary,
    ),
    (
        "curves_fit", "曲线拟合",
        "用高斯-牛顿迭代拟合实验曲线；输入 x、y 等长数组，model 取 linear/exponential/logistic/"
        "power/gaussian，返回 params/r2/rmse/converged/formula。",
        _SCHEMA_FIT, ["曲线", "拟合"], "science.curves.fit", _h_curves_fit,
    ),
    (
        "curves_predict", "模型正向计算",
        "用给定参数按模型算出一组预测值（供参数扫描/实验仿真）；输入 model、params 对象、xs 数组，"
        "返回 {model, params, xs, ys}。",
        _SCHEMA_PREDICT, ["曲线", "仿真"], "science.curves.predict", _h_curves_predict,
    ),
    (
        "curves_extrapolate", "曲线拟合+外推",
        "先拟合再用拟合参数预测新点（可越出原始范围做外推）；输入 x、y、model、xs，"
        "返回 params/r2/xs/ys/outside_range。",
        _SCHEMA_EXTRAPOLATE, ["曲线", "外推"], "science.curves.extrapolate",
        _h_curves_extrapolate,
    ),
    (
        "molecule_parse", "SMILES 分子解析",
        "解析 SMILES 得到分子式、分子量与拓扑描述符；输入 smiles 字符串，返回 formula/mw/"
        "heavy_atoms/rings/aromatic_atoms/rotatable_bonds/hbd/hba/valid/warnings。",
        _SCHEMA_MOL, ["分子", "化学"], "science.molecules.parse", _h_molecule_parse,
    ),
    (
        "molecule_descriptors", "类药性描述符",
        "在分子解析结果上加 Lipinski 五规则判定；输入 smiles 字符串，返回 lipinski_violations/"
        "lipinski_pass 及基础描述符。",
        _SCHEMA_MOL, ["分子", "类药性"], "science.molecules.descriptors",
        _h_molecule_descriptors,
    ),
    (
        "molecule_batch", "SMILES 批量解析",
        "一次解析多个 SMILES；输入 smiles 为字符串数组，返回 items 列表、valid_count、invalid 明细。",
        _SCHEMA_MOL_BATCH, ["分子", "批量"], "science.molecules.batch", _h_molecule_batch,
    ),
    (
        "design_factorial", "全因子实验设计",
        "生成全因子设计矩阵（2 水平编码 -1/+1）；输入 factors 为因子名数组、levels 为水平数，"
        "返回 runs/n_runs。",
        _SCHEMA_FACTORIAL, ["设计", "DOE"], "science.design.factorial_design",
        _h_design_factorial,
    ),
    (
        "design_lhs", "拉丁超立方抽样",
        "生成拉丁超立方实验矩阵（0~1 归一化、每列分层均匀）；输入 n_factors、n_runs、seed，"
        "同 seed 结果完全一致。",
        _SCHEMA_LHS, ["设计", "抽样"], "science.design.lhs_design", _h_design_lhs,
    ),
    (
        "design_power", "功效分析与样本量",
        "估算双样本 t 检验所需样本量；输入 effect_size（Cohen's d）、alpha、power，"
        "返回 n_per_group/n_total/df。",
        _SCHEMA_POWER, ["设计", "功效"], "science.design.power_analysis", _h_design_power,
    ),
    (
        "design_sweep", "参数扫描仿真",
        "在 [x_start, x_stop] 上按模型扫描参数并给出极值与单调性；输入 model、params 对象、"
        "x_start、x_stop、steps，返回 xs/ys/argmax/argmin/monotonic。",
        _SCHEMA_SWEEP, ["设计", "仿真"], "science.design.sweep", _h_design_sweep,
    ),
    (
        "design_grid_search", "参数网格寻优",
        "在参数网格上穷举，按 R² 找最贴合数据的参数组合；输入 model、params_grid 对象、x、y，"
        "返回 best/ranking（前 5 名）/n_evaluated。",
        _SCHEMA_GRID, ["设计", "寻优"], "science.design.grid_search", _h_design_grid_search,
    ),
]

TOOLS = {}
for _name, _title, _desc, _schema, _tags, _case, _handler in _TOOL_DEFS:
    TOOLS[_name] = ToolSpec(_name, _title, _desc, _schema, _tags, _handler, case=_case)
del _name, _title, _desc, _schema, _tags, _case, _handler


# ---------------------------------------------------------------------------
# JSON 安全化：绝不把 NaN/Infinity 漏给调用方（JSON 不支持）
# ---------------------------------------------------------------------------
def _json_safe(obj, _depth: int = 0):
    """递归把结果转成纯 JSON 值：非有限浮点 -> 0.0，未知类型 -> str。"""
    if _depth > 20:  # 防御性：结构异常深时不再递归
        return str(obj)
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return 0.0
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_safe(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v, _depth + 1) for v in obj]
    if isinstance(obj, set):
        return [_json_safe(v, _depth + 1) for v in sorted(obj, key=repr)]
    return str(obj)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def names() -> list:
    """按注册顺序返回全部工具名。"""
    return list(TOOLS.keys())


def list_tools() -> list:
    """MCP ``tools/list`` 形状的工具清单。"""
    return [spec.to_mcp() for spec in TOOLS.values()]


def tool_spec(name: str):
    """单个工具的 MCP 形状；不存在返回 None。"""
    spec = TOOLS.get(name)
    return spec.to_mcp() if spec is not None else None


def _repro(name: str, case: str, args: dict) -> dict:
    """可复现性凭据：工具名 + 归一化参数 + 解释器版本 + 模块路径。"""
    return {
        "tool": name,
        "args": _json_safe(args),
        "python": platform.python_version(),
        "case": case,
    }


def call_tool(name: str, args: dict | None = None) -> dict:
    """统一执行入口：永不抛异常，失败折叠成 ok=False + 中文 error。"""
    t0 = time.perf_counter()
    raw_args = args if args is not None else {}
    if not isinstance(raw_args, dict):
        # 允许传字符串（按 JSON 解析）或直接当空参数
        try:
            import json as _json
            raw_args = _json.loads(raw_args) if isinstance(raw_args, str) else {}
        except (ValueError, TypeError):
            raw_args = {}
        if not isinstance(raw_args, dict):
            raw_args = {}

    spec = TOOLS.get(name)
    if spec is None:
        return {
            "tool": name,
            "ok": False,
            "result": None,
            "error": "未知工具：%s" % name,
            "elapsed_ms": 0.0,
            "repro": _repro(name, "science.<unknown>", raw_args),
        }

    try:
        result = spec.handler(dict(raw_args))
        elapsed = round((time.perf_counter() - t0) * 1000.0, 3)
        return {
            "tool": name,
            "ok": True,
            "result": _json_safe(result),
            "error": None,
            "elapsed_ms": elapsed,
            "repro": _repro(name, spec.case, raw_args),
        }
    except Exception as exc:  # noqa: BLE001 - 工具层必须兜住一切异常
        elapsed = round((time.perf_counter() - t0) * 1000.0, 3)
        msg = str(exc).strip()
        if not msg:
            msg = "计算失败（%s）" % exc.__class__.__name__
        elif isinstance(exc, (TypeError, KeyError, IndexError, ZeroDivisionError,
                              OverflowError, AttributeError)):
            msg = "计算异常：%s" % msg
        return {
            "tool": name,
            "ok": False,
            "result": None,
            "error": msg,
            "elapsed_ms": elapsed,
            "repro": _repro(name, spec.case, raw_args),
        }


def describe() -> str:
    """给 LLM 看的纯文本工具清单（每行一个工具）。"""
    lines = ["- %s：%s —— %s" % (spec.name, spec.title, spec.description)
             for spec in TOOLS.values()]
    lines.append("")
    lines.append("共 %d 个工具。调用方式：先选工具，再按 inputSchema 传 JSON 参数。"
                 % len(TOOLS))
    return "\n".join(lines)
