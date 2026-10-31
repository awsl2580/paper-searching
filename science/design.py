"""实验设计与仿真算子：让智能体从"读文献"走到"设计实验"。

为什么需要它
------------
AI4S 的闭环是「规划 -> 做实验 -> 看结果 -> 改方案」。这个模块提供闭环里
"做"的一半：怎么排实验点（全因子 / 拉丁超立方）、要做多少次才有统计功效、
以及给定模型后做参数扫描和网格寻优。

取舍
----
* 拉丁超立方用 ``random.Random(seed)``，同一 seed 必然复现同一矩阵；这是
  唯一用到随机数的地方，且必须显式可复现。
* 全因子在 levels>2 且因子数>3 时直接报错（3^4=81 组已经不适合手工实验），
  宁可让调用方改用 LHS，也不要吐出一张没人能执行的表。
* 功效分析先用正态近似给起点，再用 t 分位数迭代校正，保证不低估样本量
  （低估样本量比高估代价大得多）。
* sweep / grid_search 复用 curves 的正向计算与拟合口径，保证"仿真"和"拟合"
  的数字是同一套公式算出来的。
"""

from __future__ import annotations

import itertools
import math
import random

from . import curves

__all__ = ["factorial_design", "lhs_design", "power_analysis", "sweep",
           "grid_search"]


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


def _num(v, label: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError("%s 必须是数值，收到：%r" % (label, v))
    if not math.isfinite(f):
        raise ValueError("%s 必须是有限数值" % label)
    return f


def _norm_ppf(p: float) -> float:
    """标准正态分位数（二分法反解 erfc，精度 1e-12，够用且无依赖）。"""
    if not (0.0 < p < 1.0):
        raise ValueError("概率必须在 (0,1) 区间内，收到：%r" % p)
    if p == 0.5:
        return 0.0
    lo, hi = -12.0, 12.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        cdf = 0.5 * math.erfc(-mid / math.sqrt(2.0))
        if cdf < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12:
            break
    return (lo + hi) / 2.0


def _t_ppf(p: float, df: float) -> float:
    """t 分布分位数（二分法反解上尾概率），用于功效分析的迭代校正。"""
    from . import stats as _stats
    if not (0.0 < p < 1.0):
        raise ValueError("概率必须在 (0,1) 区间内")
    lo, hi = -60.0, 60.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        cdf = 1.0 - _stats._t_sf(mid, df)
        if cdf < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return (lo + hi) / 2.0


# ---------------------------------------------------------------------------
# 1. 全因子设计
# ---------------------------------------------------------------------------
def factorial_design(factors, levels: int = 2) -> dict:
    """全因子实验设计矩阵。

    levels=2 时编码为 -1/+1（两水平正交表），levels>2 时编码为 0..1 等分。
    因子数 k 与水平数 levels 满足 levels**k 组实验；levels>2 时要求 k<=3。
    """
    if factors is None:
        raise ValueError("factors 不能为空，请给出因子名列表，例如 [\"温度\",\"pH\"]")
    if isinstance(factors, str):
        names = [p.strip() for p in factors.replace("，", ",").split(",") if p.strip()]
    elif isinstance(factors, (list, tuple)):
        names = [str(f).strip() for f in factors]
    else:
        raise ValueError("factors 必须是字符串列表")
    names = [n for n in names if n]
    if not names:
        raise ValueError("factors 不能为空")
    if len(set(names)) != len(names):
        raise ValueError("因子名不能重复：%s" % names)

    try:
        lv = int(levels)
    except (TypeError, ValueError):
        raise ValueError("levels 必须是整数")
    if lv < 2:
        raise ValueError("levels 至少为 2")
    if lv > 2 and len(names) > 3:
        raise ValueError("levels>2 时只允许 k<=3 个因子（否则组合数爆炸，请改用 LHS）")
    if lv == 2:
        lv_values = [-1.0, 1.0]
    else:
        lv_values = [_r(i / (lv - 1.0)) for i in range(lv)]

    k = len(names)
    runs = []
    # 按字典序展开：第一个因子变化最慢，便于人工复现
    for idx, combo in enumerate(itertools.product(lv_values, repeat=k), start=1):
        run = {"run": idx}
        for name, value in zip(names, combo):
            run[name] = _r(value)
        runs.append(run)

    return {
        "factors": names,
        "levels": lv_values,
        "level_labels": ["水平%d" % (i + 1) for i in range(lv)],
        "runs": runs,
        "n_runs": len(runs),
        "n_factors": k,
        "design": "full_factorial",
        "note": ("二水平全因子编码为 -1/+1，共 %d 组；-1 为低水平、+1 为高水平"
                 % len(runs)) if lv == 2 else
                ("%d 水平全因子编码为 0~1 等分，共 %d 组" % (lv, len(runs))),
    }


# ---------------------------------------------------------------------------
# 2. 拉丁超立方
# ---------------------------------------------------------------------------
def lhs_design(n_factors: int, n_runs: int, seed: int = 42) -> dict:
    """拉丁超立方抽样（LHS）：每列把 [0,1] 等分成 n_runs 层，每层恰好一点。"""
    try:
        k = int(n_factors)
        n = int(n_runs)
        sd = int(seed)
    except (TypeError, ValueError):
        raise ValueError("n_factors / n_runs / seed 必须是整数")
    if k < 1:
        raise ValueError("n_factors 至少为 1")
    if n < 2:
        raise ValueError("n_runs 至少为 2")
    if n > 10000 or k > 100:
        raise ValueError("规模过大（n_runs<=10000, n_factors<=100）")

    rng = random.Random(sd)
    matrix = []
    for _col in range(k):
        strata = list(range(n))
        rng.shuffle(strata)  # 每列独立洗牌，保证列间不相关
        col = [round((s + rng.random()) / n, 6) for s in strata]
        matrix.append(col)
    # 转成"行优先"：matrix[i] 是第 i 次实验的 k 个因子值
    rows = [[matrix[j][i] for j in range(k)] for i in range(n)]

    # 自检：每列每个分层恰好被命中一次
    stratified = True
    for j in range(k):
        buckets = set(int(v * n) for v in matrix[j])
        if len(buckets) != n:
            stratified = False

    return {
        "n_factors": k,
        "n_runs": n,
        "matrix": rows,
        "seed": sd,
        "columns": ["x%d" % (j + 1) for j in range(k)],
        "stratified": stratified,
        "note": "%d 因子 × %d 次实验，取值 0~1；每列每个分层恰好命中一次，seed=%d 可复现"
                % (k, n, sd),
    }


# ---------------------------------------------------------------------------
# 3. 功效分析（样本量估算）
# ---------------------------------------------------------------------------
def power_analysis(effect_size: float = 0.5, alpha: float = 0.05,
                   power: float = 0.8) -> dict:
    """双样本 t 检验所需样本量（正态近似起步 + t 分位数迭代校正）。"""
    d = abs(_num(effect_size, "effect_size"))
    a = _num(alpha, "alpha")
    pw = _num(power, "power")
    if d <= 0:
        raise ValueError("effect_size 必须大于 0")
    if not (0.0 < a < 1.0):
        raise ValueError("alpha 必须在 (0,1) 之间")
    if not (0.0 < pw < 1.0):
        raise ValueError("power 必须在 (0,1) 之间")

    z_a = _norm_ppf(1.0 - a / 2.0)
    z_p = _norm_ppf(pw)
    # 正态近似公式：n = 2*(z_{1-alpha/2}+z_{power})^2 / d^2
    n_approx = 2.0 * (z_a + z_p) ** 2 / (d * d)
    n = int(math.ceil(n_approx))

    # 用 t 分位数迭代校正，保证不低估
    for _ in range(100):
        df = 2 * n - 2
        if df <= 0:
            n += 1
            continue
        t_a = _t_ppf(1.0 - a / 2.0, df)
        t_p = _t_ppf(pw, df)
        need = 2.0 * (t_a + t_p) ** 2 / (d * d)
        n_new = int(math.ceil(need))
        if n_new <= n:
            break
        n = n_new

    # 不低估：保证 n 至少覆盖正态近似值
    n = max(n, int(math.ceil(n_approx)), 2)

    return {
        "effect_size": _r(d),
        "alpha": _r(a),
        "power": _r(pw),
        "n_per_group": n,
        "n_total": 2 * n,
        "df": 2 * n - 2,
        "z_alpha": _r(z_a),
        "z_power": _r(z_p),
        "n_normal_approx": _r(n_approx),
        "method": "two_sample_t",
        "note": ("每组至少 %d 例（共 %d 例）才能以 %d%% 把握检出 d=%.3g 的差异"
                 % (n, 2 * n, round(pw * 100), d)),
    }


# ---------------------------------------------------------------------------
# 4. 参数扫描仿真
# ---------------------------------------------------------------------------
def sweep(model: str, params: dict, x_start: float, x_stop: float,
          steps: int = 21) -> dict:
    """参数扫描：在 [x_start, x_stop] 上按模型算出一条曲线并给出极值点。"""
    x0 = _num(x_start, "x_start")
    x1 = _num(x_stop, "x_stop")
    try:
        st = int(steps)
    except (TypeError, ValueError):
        raise ValueError("steps 必须是整数")
    if st < 2:
        raise ValueError("steps 至少为 2")
    if st > 200000:
        raise ValueError("steps 过大（上限 200000）")

    step = (x1 - x0) / (st - 1)
    xs = [round(x0 + i * step, 10) for i in range(st)]
    res = curves.predict(model, params, xs)
    ys = res["ys"]

    imax = max(range(len(ys)), key=lambda i: ys[i])
    imin = min(range(len(ys)), key=lambda i: ys[i])

    # 单调性判定：允许 1e-9 量级的数值抖动
    tol = 1e-9 * max(1.0, max(abs(v) for v in ys))
    inc = all(ys[i + 1] >= ys[i] - tol for i in range(len(ys) - 1))
    dec = all(ys[i + 1] <= ys[i] + tol for i in range(len(ys) - 1))
    if inc and dec:
        mono = "increasing"  # 常数曲线按不下降处理
    elif inc:
        mono = "increasing"
    elif dec:
        mono = "decreasing"
    else:
        mono = "non-monotonic"

    return {
        "model": res["model"],
        "params": res["params"],
        "xs": res["xs"],
        "ys": ys,
        "argmax": {"x": res["xs"][imax], "y": ys[imax], "index": imax},
        "argmin": {"x": res["xs"][imin], "y": ys[imin], "index": imin},
        "monotonic": mono,
        "steps": st,
        "range": [_r(x0), _r(x1)],
    }


# ---------------------------------------------------------------------------
# 5. 网格搜索
# ---------------------------------------------------------------------------
def grid_search(model: str, params_grid: dict, x, y) -> dict:
    """在参数网格上穷举，按 R² 排序找出最贴合数据的参数组合。"""
    m = curves._check_model(model)
    if not isinstance(params_grid, dict) or not params_grid:
        raise ValueError("params_grid 必须是非空对象，例如 {\"a\":[1,2],\"b\":[0.1,0.5]}")
    xs = curves._floats(x, "x")
    ys = curves._floats(y, "y")
    n = min(len(xs), len(ys))
    if n < 2:
        raise ValueError("网格搜索需要至少 2 对有效数据")
    xs, ys = xs[:n], ys[:n]

    keys = curves._KEYS[m]
    missing = [k for k in keys if k not in params_grid]
    if missing:
        raise ValueError("params_grid 缺少参数：%s（模型 %s 需要 %s）"
                         % (", ".join(missing), m, ", ".join(keys)))

    cand_lists = []
    for k in keys:
        vals = params_grid[k]
        if isinstance(vals, str):
            vals = [p for p in vals.replace(",", " ").split() if p]
        elif not isinstance(vals, (list, tuple)):
            vals = [vals]
        if not vals:
            raise ValueError("参数 %s 的候选值列表为空" % k)
        try:
            cand_lists.append([float(v) for v in vals])
        except (TypeError, ValueError):
            raise ValueError("参数 %s 的候选值必须都是数值" % k)

    my = sum(ys) / n
    ss_tot = sum((v - my) ** 2 for v in ys)

    ranking = []
    for combo in itertools.product(*cand_lists):
        params = dict(zip(keys, combo))
        preds = [curves._eval(m, params, xv) for xv in xs]
        ss_res = sum((ys[i] - preds[i]) ** 2 for i in range(n))
        r2 = (1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0
        rmse = math.sqrt(max(0.0, ss_res) / n)
        ranking.append({
            "params": {k: _r(v, 9) for k, v in params.items()},
            "r2": _r(r2),
            "rmse": _r(rmse),
            "ss_res": _r(ss_res),
        })

    ranking.sort(key=lambda it: (-it["r2"], it["rmse"]))
    best = ranking[0] if ranking else {"params": {}, "r2": 0.0, "rmse": 0.0}
    return {
        "model": m,
        "best": best,
        "ranking": ranking[:5],
        "n_evaluated": len(ranking),
        "grid_size": {k: len(v) for k, v in zip(keys, cand_lists)},
        "note": "穷举 %d 组参数，按 R² 从高到低排序，只回报前 5 名" % len(ranking),
    }
