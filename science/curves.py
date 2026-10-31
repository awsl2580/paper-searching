"""实验曲线拟合与外推：把「散点」变成「公式 + 参数 + 可信度」。

为什么需要它
------------
科研结论常常需要一句"符合指数增长模型，增长率 0.5±0.01，R²=0.999"。
本模块提供 5 个常用模型（线性/指数/逻辑斯蒂/幂律/高斯），用统一的
"初值 + 高斯-牛顿迭代"流程拟合，并支持拿拟合参数做正向预测与区间外推
（供 Agent 的"仿真"步骤使用）。

取舍
----
* 不引 scipy.optimize：自己写 Levenberg-Marquardt 阻尼的高斯-牛顿，
  正规方程用高斯消元解。优点是零依赖、结果完全确定；缺点是对极端病态
  数据可能不收敛，所以失败时退回"线性化初值"并显式标 ``converged=False``，
  绝不假装成功。
* logistic 是非凸的，单靠一个初值极易收敛到错解，因此先做多初值网格搜索
  再精修；这是本模块唯一"多花时间换稳定"的地方。
* 所有系数都做数值缩放（z = (x-center)/span）后再解方程，避免 x 值很大时
  正规方程病态。
"""

from __future__ import annotations

import math

__all__ = ["model_names", "fit", "predict", "extrapolate"]

_MODELS = ("linear", "exponential", "logistic", "power", "gaussian")

# 每个模型的参数名顺序（同时也是雅可比列的次序）
_KEYS = {
    "linear": ["a", "b"],
    "exponential": ["a", "b"],
    "logistic": ["L", "k", "x0"],
    "power": ["a", "b"],
    "gaussian": ["a", "mu", "sigma"],
}

# 参数下/上界，防止 exp 溢出与 sigma=0
_BOUNDS = {
    "linear": [(-1e15, 1e15), (-1e15, 1e15)],
    "exponential": [(-1e15, 1e15), (-60.0, 60.0)],
    "logistic": [(-1e15, 1e15), (-60.0, 60.0), (-1e15, 1e15)],
    "power": [(-1e15, 1e15), (-30.0, 30.0)],
    "gaussian": [(-1e15, 1e15), (-1e15, 1e15), (1e-9, 1e15)],
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _r(x, nd: int = 6) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    v = round(v, nd)
    return 0.0 if v == 0.0 else v


def _floats(values, label: str) -> list:
    if values is None:
        raise ValueError("%s 不能为空，请提供数值数组" % label)
    if isinstance(values, str):
        items = [p for p in values.replace(",", " ").split() if p]
    elif isinstance(values, (list, tuple)):
        items = list(values)
    else:
        items = [values]
    out = []
    for v in items:
        if v is None or v == "":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    if not out:
        raise ValueError("%s 中没有可用数值" % label)
    return out


def _check_model(model: str) -> str:
    m = str(model or "linear").strip().lower()
    alias = {"线": "linear", "linear_regression": "linear", "指数": "exponential",
             "exp": "exponential", "sigmoid": "logistic", "逻辑": "logistic",
             "幂": "power", "幂律": "power", "高斯": "gaussian"}
    m = alias.get(m, m)
    if m not in _MODELS:
        raise ValueError("未知模型：%s（可选：%s）" % (model, ", ".join(_MODELS)))
    return m


def _solve(a: list, b: list) -> list:
    """列主元高斯消元，解 n 元线性方程组（正规方程专用）。"""
    n = len(b)
    m = [list(a[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-14:
            m[piv][col] = 1e-14
        if piv != col:
            m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for r in range(col + 1, n):
            f = m[r][col] / pv
            if f == 0.0:
                continue
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))
        pv = m[r][r]
        x[r] = s / pv if abs(pv) > 1e-14 else 0.0
    return x


def _eval(model: str, p: dict, x: float) -> float:
    """按模型正向计算一个点；任何越界都收敛成有限值，绝不返回 inf/nan。"""
    try:
        if model == "linear":
            return p["a"] * x + p["b"]
        if model == "exponential":
            e = p["b"] * x
            e = max(-700.0, min(700.0, e))
            return p["a"] * math.exp(e)
        if model == "logistic":
            z = -p["k"] * (x - p["x0"])
            z = max(-700.0, min(700.0, z))
            return p["L"] / (1.0 + math.exp(z))
        if model == "power":
            if x <= 0.0:
                return 0.0
            try:
                return p["a"] * (x ** p["b"])
            except (OverflowError, ValueError):
                return 0.0
        if model == "gaussian":
            s = p["sigma"] if p["sigma"] != 0 else 1e-9
            e = -((x - p["mu"]) ** 2) / (2.0 * s * s)
            e = max(-700.0, min(700.0, e))
            return p["a"] * math.exp(e)
    except (OverflowError, ValueError, ZeroDivisionError):
        return 0.0
    return 0.0


def _jac(model: str, p: dict, x: float) -> list:
    """对参数求偏导，返回与 _KEYS[model] 同序的列表。"""
    if model == "linear":
        return [x, 1.0]
    if model == "exponential":
        e = p["b"] * x
        e = max(-700.0, min(700.0, e))
        v = math.exp(e)
        return [v, p["a"] * x * v]
    if model == "logistic":
        z = -p["k"] * (x - p["x0"])
        z = max(-700.0, min(700.0, z))
        s = 1.0 / (1.0 + math.exp(z))
        y = p["L"] * s
        g = s * (1.0 - s)
        return [s, y * g * (x - p["x0"]) * 1.0, -p["L"] * g * p["k"]]
    if model == "power":
        if x <= 0.0:
            return [0.0, 0.0]
        xb = x ** p["b"]
        return [xb, p["a"] * xb * math.log(x)]
    if model == "gaussian":
        s = p["sigma"] if p["sigma"] != 0 else 1e-9
        d = x - p["mu"]
        e = -(d * d) / (2.0 * s * s)
        e = max(-700.0, min(700.0, e))
        v = math.exp(e)
        return [v, p["a"] * v * d / (s * s), p["a"] * v * d * d / (s ** 3)]
    return []


def _sse(xs: list, ys: list, model: str, p: dict) -> float:
    total = 0.0
    for x, y in zip(xs, ys):
        d = y - _eval(model, p, x)
        if not math.isfinite(d):
            return float("inf")
        total += d * d
    return total


def _clamp(model: str, p: dict) -> dict:
    keys = _KEYS[model]
    out = {}
    for k, (lo, hi) in zip(keys, _BOUNDS[model]):
        v = p.get(k, 0.0)
        if not math.isfinite(v):
            v = 0.0
        out[k] = min(hi, max(lo, v))
    return out


# ---------------------------------------------------------------------------
# 初值：线性化 / 网格
# ---------------------------------------------------------------------------
def _ols(xs: list, ys: list):
    """普通最小二乘 y = a*x + b，返回 (a, b, r2)。"""
    n = min(len(xs), len(ys))
    if n == 0:
        return 0.0, 0.0, 0.0
    mx = sum(xs[:n]) / n
    my = sum(ys[:n]) / n
    sxx = sum((v - mx) ** 2 for v in xs[:n])
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    if sxx <= 0:
        return 0.0, my, 0.0
    a = sxy / sxx
    b = my - a * mx
    ss_res = sum((ys[i] - (a * xs[i] + b)) ** 2 for i in range(n))
    my_ss = sum((v - my) ** 2 for v in ys[:n])
    r2 = 1.0 - ss_res / my_ss if my_ss > 0 else 0.0
    return a, b, r2


def _initial(model: str, xs: list, ys: list) -> dict:
    """按模型挑选初值：能线性化的就线性化，非凸的做网格搜索。"""
    n = len(xs)
    ymax = max(ys)
    ymin = min(ys)
    xmin, xmax = min(xs), max(xs)
    span = (xmax - xmin) or 1.0
    imax = max(range(n), key=lambda i: ys[i])

    if model == "linear":
        a, b, _ = _ols(xs, ys)
        return {"a": a, "b": b}

    if model == "exponential":
        # 先试 log|y| 线性化
        if all(v > 0 for v in ys):
            ln = [math.log(v) for v in ys]
            b, ln_a, r2 = _ols(xs, ln)
            return {"a": math.exp(ln_a) if abs(ln_a) < 700 else 1.0, "b": clampb(b)}
        # 有非正值：退化为"符号 + 起点"启发式
        return {"a": ymax if ymax != 0 else 1.0, "b": 0.1}

    if model == "power":
        if all(v > 0 for v in ys) and all(v > 0 for v in xs):
            lx = [math.log(v) for v in xs]
            ly = [math.log(v) for v in ys]
            b, ln_a, _ = _ols(lx, ly)
            return {"a": math.exp(ln_a) if abs(ln_a) < 700 else 1.0,
                    "b": clampb(b)}
        return {"a": ymax if ymax != 0 else 1.0, "b": 1.0}

    if model == "logistic":
        # 多初值网格：L 取 max(y) 的若干倍数，k 正负若干档，x0 在 x 范围上取分位
        best_p = {"L": ymax * 1.1 if ymax > 0 else 1.0, "k": 1.0,
                  "x0": xs[imax]}
        best = _sse(xs, ys, model, _clamp(model, best_p))
        base_L = abs(ymax) if ymax != 0 else 1.0
        L_list = [base_L * f for f in (1.1, 1.3, 1.6, 2.0, 3.0, 5.0)]
        cands = []
        for f in (0.6, 0.8, 1.0):
            cands.append(xmin + span * f / 2.0)
        cands.extend([xs[imax], xmin + span / 2.0, xmin + span / 4.0,
                      xmin + 3 * span / 4.0])
        for L in L_list:
            for kk in (-4.0, -2.0, -1.0, -0.5, -0.2, 0.2, 0.5, 1.0, 2.0, 4.0):
                for x0 in cands:
                    p = _clamp(model, {"L": L, "k": kk, "x0": x0})
                    s = _sse(xs, ys, model, p)
                    if s < best:
                        best = s
                        best_p = p
        return best_p

    if model == "gaussian":
        return {"a": ymax if ymax != 0 else 1.0, "mu": xs[imax],
                "sigma": span / 6.0 if span > 0 else 1.0}

    return _clamp(model, {k: 0.0 for k in _KEYS[model]})


def clampb(b: float) -> float:
    return max(-30.0, min(30.0, b))


# ---------------------------------------------------------------------------
# 高斯-牛顿（带 LM 阻尼）
# ---------------------------------------------------------------------------
def _gauss_newton(xs: list, ys: list, model: str, p0: dict, max_iter: int = 200):
    """返回 (params, converged, sse)。用 Levenberg 阻尼保证单调下降。"""
    keys = _KEYS[model]
    m = len(keys)
    p = dict(p0)
    sse = _sse(xs, ys, model, p)
    lam = 1e-3
    converged = False
    last_rel = 1.0
    stalled = False

    for _ in range(max_iter):
        # 组装 J^T J 与 J^T r
        jtj = [[0.0] * m for _ in range(m)]
        jtr = [0.0] * m
        for x, y in zip(xs, ys):
            jac = _jac(model, p, x)
            r = y - _eval(model, p, x)
            for i in range(m):
                ji = jac[i]
                if ji == 0.0:
                    continue
                jtr[i] += ji * r
                row = jtj[i]
                for j in range(i, m):
                    row[j] += ji * jac[j]
        for i in range(m):
            for j in range(i):
                jtj[i][j] = jtj[j][i]

        improved = False
        for _try in range(12):
            # LM 阻尼：在对角线上加 lam，病态时自动退化为梯度下降
            a = [row[:] for row in jtj]
            for i in range(m):
                a[i][i] *= (1.0 + lam)
                if abs(a[i][i]) < 1e-16:
                    a[i][i] = 1e-16
            delta = _solve(a, jtr)
            if not all(math.isfinite(d) for d in delta):
                lam *= 10.0
                continue
            # 步长已经小到不影响结果：视为收敛
            step = max(abs(p.get(k, 0.0)) for k in keys) or 1.0
            if max(abs(d) for d in delta) <= 1e-12 * step:
                converged = True
                break
            cand = {k: p.get(k, 0.0) + d for k, d in zip(keys, delta)}
            cand = _clamp(model, cand)
            s_new = _sse(xs, ys, model, cand)
            if s_new < sse:
                last_rel = (sse - s_new) / max(1e-300, sse if sse > 0 else 1.0)
                p = cand
                sse = s_new
                lam = max(1e-12, lam / 3.0)
                improved = True
                if last_rel < 1e-14:
                    converged = True
                break
            lam *= 10.0
            if lam > 1e12:
                break
        if converged:
            break
        if not improved:
            # 阻尼加到极限仍无法下降：已到局部最优（或数值极限）
            stalled = True
            break

    if not converged and sse < float("inf"):
        converged = bool(stalled or last_rel < 1e-6)
    return p, converged, sse


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def model_names() -> list:
    """返回内置模型名（顺序固定）。"""
    return list(_MODELS)


def _formula(model: str, p: dict) -> str:
    if model == "linear":
        return "y = %.6g*x + %.6g" % (p["a"], p["b"])
    if model == "exponential":
        return "y = %.6g*exp(%.6g*x)" % (p["a"], p["b"])
    if model == "logistic":
        return "y = %.6g/(1+exp(-%.6g*(x-%.6g)))" % (p["L"], p["k"], p["x0"])
    if model == "power":
        return "y = %.6g*x^%.6g" % (p["a"], p["b"])
    if model == "gaussian":
        return "y = %.6g*exp(-(x-%.6g)^2/(2*%.6g^2))" % (p["a"], p["mu"], p["sigma"])
    return ""


def fit(x, y, model: str = "linear") -> dict:
    """曲线拟合：先算初值，再高斯-牛顿精修，最多 200 次迭代。

    返回 ``{"model","params","r2","rmse","n","converged","formula",...}``。
    不收敛时退回初值并置 ``converged=False``，同时 ``note`` 说明原因。
    """
    m = _check_model(model)
    xs = _floats(x, "x")
    ys = _floats(y, "y")
    n = min(len(xs), len(ys))
    if n < 2:
        raise ValueError("拟合需要至少 2 对有效数据")
    xs, ys = xs[:n], ys[:n]

    if m == "power" and any(v <= 0 for v in xs):
        raise ValueError("power 模型要求所有 x > 0（幂函数在非正数上无定义）")

    p0 = _initial(m, xs, ys)
    p, converged, sse = _gauss_newton(xs, ys, m, p0)
    if not math.isfinite(sse):
        # 迭代把参数带飞了：退回初值
        p = _clamp(m, p0)
        sse = _sse(xs, ys, m, p)
        converged = False

    my = sum(ys) / n
    ss_tot = sum((v - my) ** 2 for v in ys)
    if ss_tot > 0:
        r2 = 1.0 - sse / ss_tot
    else:
        r2 = 0.0
    rmse = math.sqrt(max(0.0, sse) / n)
    degenerate = ss_tot <= 0
    preds = [_r(_eval(m, p, v), 9) for v in xs]

    return {
        "model": m,
        "params": {k: _r(p[k], 9) for k in _KEYS[m]},
        "r2": _r(r2),
        "rmse": _r(rmse),
        "n": n,
        "converged": bool(converged),
        "degenerate": bool(degenerate),
        "ss_res": _r(sse),
        "formula": _formula(m, p),
        "predict": {"xs": [_r(v) for v in xs], "ys": preds},
        "note": "拟合收敛" if converged else "迭代未完全收敛，参数为当前最优解",
    }


def predict(model: str, params, xs) -> dict:
    """用给定参数正向计算：params 必须包含该模型的全部参数名。"""
    m = _check_model(model)
    if not isinstance(params, dict):
        raise ValueError("params 必须是对象，例如 {\"a\": 3, \"b\": 0.5}")
    keys = _KEYS[m]
    p = {}
    for k in keys:
        if k not in params:
            raise ValueError("模型 %s 缺少参数：%s（需要 %s）"
                             % (m, k, ", ".join(keys)))
        try:
            p[k] = float(params[k])
        except (TypeError, ValueError):
            raise ValueError("参数 %s 必须是数值" % k)
        if not math.isfinite(p[k]):
            raise ValueError("参数 %s 不是有限数值" % k)
    p = _clamp(m, p)
    xv = _floats(xs, "xs")
    ys = [_r(_eval(m, p, v), 9) for v in xv]
    return {"model": m, "params": {k: _r(p[k], 9) for k in keys},
            "xs": [_r(v) for v in xv], "ys": ys}


def extrapolate(x, y, model: str, xs) -> dict:
    """拟合 + 预测合并；outside_range 标记预测点是否越出原始 x 范围。"""
    m = _check_model(model)
    res = fit(x, y, m)
    xv = _floats(xs, "xs")
    ys = [_r(_eval(m, res["params"], v), 9) for v in xv]
    xs_fit = _floats(x, "x")
    lo, hi = min(xs_fit), max(xs_fit)
    outside = bool(any(v < lo - 1e-12 or v > hi + 1e-12 for v in xv))
    return {
        "model": m,
        "params": res["params"],
        "r2": res["r2"],
        "rmse": res["rmse"],
        "converged": res["converged"],
        "n": res["n"],
        "formula": res["formula"],
        "xs": [_r(v) for v in xv],
        "ys": ys,
        "outside_range": outside,
        "x_range": [_r(lo), _r(hi)],
        "note": ("预测点越出原始数据范围，属外推，需谨慎解释" if outside
                 else "预测点在原始数据范围内（内插）"),
    }
