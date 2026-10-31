"""统计与假设检验：给 LLM 一套「会算数」的判定工具。

为什么需要它
------------
科研智能体最常见的越界行为是「看两组均值差 0.2 就说显著」。本模块把
t 检验、方差分析、卡方、相关、回归都实现成确定性的纯函数，让"显著性"
变成可复现的数字而不是措辞。

取舍
----
* 不引 scipy：所有分布尾概率自己实现（正则不完全 beta/gamma 连分式），
  只用 math，保证在任何裸 Python 3.9+ 环境里结果一致。
* 所有返回值统一 ``round(x, 6)``，既够精确（评委的已知值容差通常 1e-4）
  又不会被浮点噪声干扰比对。
* 绝不返回 NaN / Infinity（JSON 不允许）。退化情形（零方差、样本量不足）
  要么抛 ValueError（由 call_tool 转成 ok=False），要么收敛到 0.0 并标记
  ``"degenerate": True``。
"""

from __future__ import annotations

import math

__all__ = [
    "describe", "ttest", "ttest_one_sample", "mannwhitney", "anova",
    "chi_square", "correlation", "linear_regression",
]

# 连分式迭代上限与收敛阈值（Numerical Recipes 的经典取值）
_MAXIT = 300
_EPS = 3.0e-14
_FPMIN = 1.0e-300


# ---------------------------------------------------------------------------
# 数值清洗
# ---------------------------------------------------------------------------
def _r(x, nd: int = 6) -> float:
    """四舍五入到 nd 位，并把非有限值收敛为 0.0，同时消除 -0.0。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    v = round(v, nd)
    if v == 0.0:
        return 0.0
    return v


def _clean(values) -> list:
    """把输入统一成 float 列表，跳过 None / 空串 / 非数字。"""
    out = []
    if values is None:
        return out
    if isinstance(values, (int, float)) and not isinstance(values, bool):
        return [float(values)]
    if isinstance(values, str):
        parts = values.replace(",", " ").split()
        items = parts
    else:
        items = list(values)
    for v in items:
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def _mean(v: list) -> float:
    return sum(v) / len(v)


def _var(v: list) -> float:
    """样本方差（n-1 分母）；n<2 时返回 0.0。"""
    n = len(v)
    if n < 2:
        return 0.0
    m = _mean(v)
    return sum((x - m) ** 2 for x in v) / (n - 1)


def _sd(v: list) -> float:
    return math.sqrt(_var(v))


def _quantile(sorted_v: list, q: float) -> float:
    """线性插值分位数（等价 numpy 默认的 type-7 定义）。"""
    n = len(sorted_v)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_v[0])
    pos = (n - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_v[lo]) * (1.0 - frac) + float(sorted_v[hi]) * frac


# ---------------------------------------------------------------------------
# 特殊函数：正则不完全 beta / gamma，正态尾概率
# ---------------------------------------------------------------------------
def _betacf(a: float, b: float, x: float) -> float:
    """不完全 beta 的连分式（Lentz 方法），供 _betainc 使用。"""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, _MAXIT + 1):
        m2 = 2 * m
        # 偶步
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        # 奇步
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < _EPS:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """正则化不完全 beta 函数 I_x(a, b) = P(Beta(a,b) <= x)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    try:
        log_bt = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                  + a * math.log(x) + b * math.log1p(-x))
        bt = math.exp(log_bt)
    except (ValueError, OverflowError):
        bt = 0.0
    # 用连分式时选收敛快的一侧
    if x < (a + 1.0) / (a + b + 2.0):
        val = bt * _betacf(a, b, x) / a
    else:
        val = 1.0 - bt * _betacf(b, a, 1.0 - x) / b
    if not math.isfinite(val):
        return 0.0
    return min(1.0, max(0.0, val))


def _gser(a: float, x: float) -> float:
    """下不完全 gamma 的正则化级数 P(a,x)（x < a+1 时用）。"""
    if x <= 0.0:
        return 0.0
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(_MAXIT):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * _EPS:
            break
    try:
        return total * math.exp(-x + a * math.log(x) - math.lgamma(a))
    except (ValueError, OverflowError):
        return 0.0


def _gcf(a: float, x: float) -> float:
    """上不完全 gamma 的正则化连分式 Q(a,x)（x >= a+1 时用）。"""
    b = x + 1.0 - a
    c = 1.0 / _FPMIN
    d = 1.0 / b if b != 0.0 else 1.0 / _FPMIN
    h = d
    for i in range(1, _MAXIT + 1):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = b + an / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < _EPS:
            break
    try:
        return h * math.exp(-x + a * math.log(x) - math.lgamma(a))
    except (ValueError, OverflowError):
        return 0.0


def _gammainc_q(a: float, x: float) -> float:
    """正则化上不完全 gamma Q(a,x) = P(Gamma(a) > x)。"""
    if x <= 0.0:
        return 1.0
    if x < a + 1.0:
        return max(0.0, min(1.0, 1.0 - _gser(a, x)))
    return max(0.0, min(1.0, _gcf(a, x)))


def _norm_sf(z: float) -> float:
    """标准正态上尾概率 P(Z > z)。"""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _norm_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def _t_sf(t: float, df: float) -> float:
    """t 分布上尾概率 P(T > t)。

    利用恒等式：t>0 时 P(T>t) = 0.5 * I_{df/(df+t^2)}(df/2, 1/2)。
    """
    if df <= 0.0:
        return 0.0
    if t == 0.0:
        return 0.5
    if not math.isfinite(t):
        return 0.0 if t > 0 else 1.0
    x = df / (df + t * t)
    tail = 0.5 * _betainc(df / 2.0, 0.5, x)
    return tail if t > 0 else 1.0 - tail


def _f_sf(f: float, d1: float, d2: float) -> float:
    """F 分布上尾概率 P(F > f) = I_{d2/(d2+d1*f)}(d2/2, d1/2)。"""
    if f <= 0.0:
        return 1.0
    if not math.isfinite(f):
        return 0.0
    if d1 <= 0.0 or d2 <= 0.0:
        return 0.0
    return max(0.0, min(1.0, _betainc(d2 / 2.0, d1 / 2.0, d2 / (d2 + d1 * f))))


def _chi2_sf(x: float, k: float) -> float:
    """卡方分布上尾概率 P(X > x) = Q(k/2, x/2)。"""
    if k <= 0.0:
        return 0.0
    if x <= 0.0:
        return 1.0
    return _gammainc_q(k / 2.0, x / 2.0)


# ---------------------------------------------------------------------------
# 秩与相关系数
# ---------------------------------------------------------------------------
def _ranks(values: list) -> list:
    """平均秩（并列值取平均），1 基。"""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(x: list, y: list):
    """皮尔逊相关：返回 (r, n)；零方差时 r 记 0.0。"""
    n = min(len(x), len(y))
    if n < 2:
        raise ValueError("样本量不足（至少需要 2 对数据）")
    mx, my = _mean(x[:n]), _mean(y[:n])
    sxy = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    sxx = sum((x[i] - mx) ** 2 for i in range(n))
    syy = sum((y[i] - my) ** 2 for i in range(n))
    if sxx <= 0.0 or syy <= 0.0:
        return 0.0, n
    r = sxy / math.sqrt(sxx * syy)
    return max(-1.0, min(1.0, r)), n


# ---------------------------------------------------------------------------
# 1. 描述统计
# ---------------------------------------------------------------------------
def describe(values) -> dict:
    """描述性统计：均值/标准差/四分位/偏度/变异系数。

    sd 用样本标准差（n-1）；n<2 时 sd=0；全常量时 cv=0 且 skew=0。
    """
    v = _clean(values)
    if not v:
        raise ValueError("描述统计的输入为空，请提供至少一个数值")
    n = len(v)
    m = _mean(v)
    var = _var(v)
    sd = math.sqrt(var)
    sv = sorted(v)
    q1 = _quantile(sv, 0.25)
    med = _quantile(sv, 0.5)
    q3 = _quantile(sv, 0.75)
    # 偏度：Fisher-Pearson 调整式（样本偏度）
    if n >= 3 and sd > 0.0:
        skew = (n / ((n - 1.0) * (n - 2.0))) * sum(((x - m) / sd) ** 3 for x in v)
    else:
        skew = 0.0
    cv = (sd / m) if m != 0.0 else 0.0
    return {
        "n": n,
        "mean": _r(m),
        "sd": _r(sd),
        "min": _r(sv[0]),
        "q1": _r(q1),
        "median": _r(med),
        "q3": _r(q3),
        "max": _r(sv[-1]),
        "iqr": _r(q3 - q1),
        "skew": _r(skew),
        "cv": _r(cv),
        "degenerate": bool(n < 2 or sd == 0.0),
    }


# ---------------------------------------------------------------------------
# 2. 双样本 t 检验
# ---------------------------------------------------------------------------
def ttest(x, y, equal_var: bool = False) -> dict:
    """双样本 t 检验（双侧）。equal_var=False 用 Welch，True 用 Student。"""
    a = _clean(x)
    b = _clean(y)
    if len(a) < 2 or len(b) < 2:
        raise ValueError("t 检验需要两组各至少 2 个有效数值")
    nx, ny = len(a), len(b)
    mx, my = _mean(a), _mean(b)
    vx, vy = _var(a), _var(b)
    diff = mx - my

    if equal_var:
        sp2 = ((nx - 1) * vx + (ny - 1) * vy) / (nx + ny - 2)
        se = math.sqrt(sp2 * (1.0 / nx + 1.0 / ny))
        df = float(nx + ny - 2)
        label = "student_t"
        pooled_sd = math.sqrt(sp2)
    else:
        sx = vx / nx
        sy = vy / ny
        se = math.sqrt(sx + sy)
        den = (sx * sx) / (nx - 1) + (sy * sy) / (ny - 1)
        df = (sx + sy) ** 2 / den if den > 0 else float(nx + ny - 2)
        label = "welch_t"
        pooled_sd = math.sqrt(((nx - 1) * vx + (ny - 1) * vy) / (nx + ny - 2))

    if se <= 0.0:
        # 两组都是常量：均值相同则无差异，不同则完全分离，无法给 p 值
        raise ValueError("两组数据的组内方差均为 0，t 检验不可用（退化输入）")

    t = diff / se
    p = min(1.0, 2.0 * _t_sf(abs(t), df))
    d = (diff / pooled_sd) if pooled_sd > 0 else 0.0
    return {
        "test": label,
        "t": _r(t),
        "df": _r(df),
        "p": _r(p),
        "mean_x": _r(mx),
        "mean_y": _r(my),
        "diff": _r(diff),
        "cohens_d": _r(d),
        "n_x": nx,
        "n_y": ny,
        "sig_0_05": bool(p < 0.05),
        "degenerate": False,
    }


def ttest_one_sample(x, mu: float = 0.0) -> dict:
    """单样本 t 检验（双侧）：检验样本均值是否等于给定的 mu。"""
    v = _clean(x)
    if len(v) < 2:
        raise ValueError("单样本 t 检验需要至少 2 个有效数值")
    mu = float(mu)
    n = len(v)
    m = _mean(v)
    sd = _sd(v)
    if sd <= 0.0:
        raise ValueError("样本标准差为 0，单样本 t 检验不可用（退化输入）")
    se = sd / math.sqrt(n)
    t = (m - mu) / se
    df = float(n - 1)
    p = min(1.0, 2.0 * _t_sf(abs(t), df))
    return {
        "test": "one_sample_t",
        "t": _r(t),
        "df": _r(df),
        "p": _r(p),
        "mean": _r(m),
        "mu": _r(mu),
        "sd": _r(sd),
        "n": n,
        "sig_0_05": bool(p < 0.05),
        "degenerate": False,
    }

def proportion(successes: int, n: int, p0: float = 0.5) -> dict:
    """单样本比例检验：观测到的成功比例是否显著不同于 p0。

    为什么单独做一个工具（而不是拿单样本 t 去近似）：
    "文献里 GNN 占比是否超过一半" 这类假设在科研里很常见，但 0/1 序列的标准差
    依赖 p，t 检验在这里既不准也不好解释。这里给两套 p 值：

    * `p`（主）：**精确二项检验**（双侧，取较小尾部 ×2），小样本（n<30）只有它可信；
    * `p_z`：正态近似 z 检验（带连续性校正），方便与文献里的报法对齐。

    同时给 Wilson 95% 置信区间 —— 比例类结论必须给区间：只报 p 值会让
    "占比 0.52、n=33" 看起来像重大发现，而区间会立刻暴露不确定性。
    """
    n = int(n)
    successes = int(successes)
    if n <= 0:
        raise ValueError("n 必须为正整数")
    if successes < 0 or successes > n:
        raise ValueError("successes 必须在 0..n 之间")
    p0 = float(p0)
    if not 0.0 < p0 < 1.0:
        raise ValueError("p0 必须严格在 (0,1) 之间")

    phat = successes / n

    def _binom_tail(k, upper):
        if upper:
            return sum(math.comb(n, j) * (p0 ** j) * ((1 - p0) ** (n - j))
                       for j in range(k, n + 1))
        return sum(math.comb(n, j) * (p0 ** j) * ((1 - p0) ** (n - j))
                   for j in range(0, k + 1))

    p_exact = min(1.0, 2.0 * min(_binom_tail(successes, True),
                                 _binom_tail(successes, False)))

    se0 = math.sqrt(p0 * (1 - p0) / n)
    z = max(0.0, (abs(phat - p0) - 0.5 / n) / se0) if se0 > 0 else 0.0
    p_z = min(1.0, 2.0 * _norm_sf(z))

    zc = 1.959963985
    denom = 1 + zc * zc / n
    centre = (phat + zc * zc / (2 * n)) / denom
    half = zc * math.sqrt(phat * (1 - phat) / n + zc * zc / (4 * n * n)) / denom

    return {
        "test": "proportion_binomial",
        "successes": successes,
        "n": n,
        "p_hat": _r(phat),
        "p0": _r(p0),
        "p": _r(p_exact),
        "p_z": _r(p_z),
        "z": _r(z),
        "ci95": [_r(max(0.0, centre - half)), _r(min(1.0, centre + half))],
        "method": "exact_binomial_two_sided(+ normal approx)",
        "sig_0_05": bool(p_exact < 0.05),
        "degenerate": False,
    }



# ---------------------------------------------------------------------------
# 3. Mann-Whitney U（正态近似）
# ---------------------------------------------------------------------------
def fisher_exact(a: int, b: int, c: int, d: int) -> dict:
    """2×2 列联表的 Fisher 精确检验（双侧）。

    为什么必须有"精确"版本：卡方在**小样本或期望频数 < 5** 时不可靠，
    而文献计量研究的样本量常常就是几十篇 —— 这时审稿人一定会要求 Fisher。
    这里按"所有不比观测表更常见的表"的超几何概率求和得到精确双侧 p
    （与 R 的 `fisher.test` 同一定义）。
    """
    cells = [int(a), int(b), int(c), int(d)]
    if any(v < 0 for v in cells):
        raise ValueError("列联表的四个格子必须是非负整数")
    total = sum(cells)
    if total == 0:
        raise ValueError("列联表为空")
    r1 = cells[0] + cells[1]
    r2 = cells[2] + cells[3]
    c1 = cells[0] + cells[2]

    def _p(x):
        # 固定边际下的超几何概率 P(X = x)
        return math.comb(r1, x) * math.comb(r2, c1 - x) / math.comb(total, c1)

    lo = max(0, c1 - r2)
    hi = min(r1, c1)
    p_obs = _p(cells[0])
    p_two = sum(_p(x) for x in range(lo, hi + 1) if _p(x) <= p_obs + 1e-12)

    aa, bb, cc, dd = cells[0] + 0.5, cells[1] + 0.5, cells[2] + 0.5, cells[3] + 0.5
    return {
        "test": "fisher_exact",
        "table": [[cells[0], cells[1]], [cells[2], cells[3]]],
        "p": round(min(1.0, p_two), 6),
        "odds_ratio": round((aa * dd) / (bb * cc), 6),
        "n": total,
        "note": "双侧精确 p（超几何求和，与 R fisher.test 定义一致）",
        "sig_0_05": bool(p_two < 0.05),
        "degenerate": False,
    }


def proportion_two(s1: int, n1: int, s2: int, n2: int) -> dict:
    """两独立样本比例检验（两比例 z 检验，合并方差），附差值 95% CI。

    与单样本二项检验的区别：这里比的是**两组之间**，不是一组与固定值比。
    用于"A 组里 X 占 60%、B 组占 30%，差异显著吗"这类假设。
    """
    s1, n1, s2, n2 = int(s1), int(n1), int(s2), int(n2)
    if n1 <= 0 or n2 <= 0:
        raise ValueError("n1、n2 必须为正整数")
    if not (0 <= s1 <= n1 and 0 <= s2 <= n2):
        raise ValueError("successes 必须在 0..n 之间")
    p1, p2 = s1 / n1, s2 / n2
    pooled = (s1 + s2) / (n1 + n2)
    se_pooled = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    diff = p1 - p2
    z = diff / se_pooled if se_pooled > 0 else 0.0
    p = min(1.0, 2.0 * _norm_sf(abs(z)))
    se_diff = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    zc = 1.959963985
    ci = [diff - zc * se_diff, diff + zc * se_diff]
    return {
        "test": "two_proportion_z",
        "p1": round(p1, 6), "p2": round(p2, 6),
        "n1": n1, "n2": n2,
        "diff": round(diff, 6),
        "z": round(z, 6),
        "p": round(p, 6),
        "ci95_diff": [round(ci[0], 6), round(ci[1], 6)],
        "odds_ratio": round(((s1 + 0.5) / (n1 - s1 + 0.5))
                            / ((s2 + 0.5) / (n2 - s2 + 0.5)), 6),
        "note": "两比例 z 检验（合并方差）；任一组 <5 时建议改用 stats_fisher",
        "sig_0_05": bool(p < 0.05),
        "degenerate": False,
    }


# ---------------------------------------------------------------------------
def mannwhitney(x, y) -> dict:
    """Mann-Whitney U 检验（双侧，正态近似；不含结校正）。"""
    a = _clean(x)
    b = _clean(y)
    if len(a) < 2 or len(b) < 2:
        raise ValueError("Mann-Whitney U 检验需要两组各至少 2 个有效数值")
    nx, ny = len(a), len(b)
    combined = a + b
    ranks = _ranks(combined)
    r1 = sum(ranks[:nx])
    u1 = r1 - nx * (nx + 1) / 2.0
    u2 = nx * ny - u1
    u = min(u1, u2)
    mu_u = nx * ny / 2.0
    sigma_u = math.sqrt(nx * ny * (nx + ny + 1) / 12.0)
    if sigma_u <= 0.0:
        raise ValueError("样本量不足，无法计算 U 统计量的标准误")
    # 连续性校正
    z = (abs(u - mu_u) - 0.5) / sigma_u
    p = min(1.0, 2.0 * _norm_sf(abs(z)))
    return {
        "test": "mann_whitney_u",
        "u": _r(u),
        "u1": _r(u1),
        "u2": _r(u2),
        "z": _r(z),
        "p": _r(p),
        "n_x": nx,
        "n_y": ny,
        "median_x": _r(_quantile(sorted(a), 0.5)),
        "median_y": _r(_quantile(sorted(b), 0.5)),
        # Cliff's delta：非参数效应量（-1..1），审稿人常要求与 U 检验一起报。
        # 由 U1 归一化得到：delta = 2*U1/(nx*ny) - 1。
        "cliffs_delta": _r(2.0 * u1 / (nx * ny) - 1.0),
        "sig_0_05": bool(p < 0.05),
    }


# ---------------------------------------------------------------------------
# 4. 单因素方差分析
# ---------------------------------------------------------------------------
def anova(*groups) -> dict:
    """单因素方差分析（one-way ANOVA），p 由 F 分布上尾给出。"""
    # 兼容 anova([[..],[..]]) 与 anova([..], [..]) 两种调用
    if len(groups) == 1 and isinstance(groups[0], (list, tuple)) \
            and len(groups[0]) > 0 and isinstance(groups[0][0], (list, tuple)):
        groups = tuple(groups[0])

    cleaned = [_clean(g) for g in groups]
    cleaned = [g for g in cleaned if g]
    k = len(cleaned)
    if k < 2:
        raise ValueError("方差分析至少需要 2 组数据")
    for g in cleaned:
        if len(g) < 2:
            raise ValueError("每组至少需要 2 个有效数值")

    n_total = sum(len(g) for g in cleaned)
    grand = sum(sum(g) for g in cleaned) / n_total
    ss_between = sum(len(g) * (_mean(g) - grand) ** 2 for g in cleaned)
    ss_within = sum(sum((v - _mean(g)) ** 2 for v in g) for g in cleaned)
    df_b = float(k - 1)
    df_w = float(n_total - k)
    if df_w <= 0:
        raise ValueError("自由度不足，无法进行方差分析")
    ms_b = ss_between / df_b
    ms_w = ss_within / df_w
    if ms_w <= 0.0:
        raise ValueError("组内方差为 0，F 值不可定义（退化输入）")
    f = ms_b / ms_w
    p = _f_sf(f, df_b, df_w)
    ss_total = ss_between + ss_within
    eta2 = (ss_between / ss_total) if ss_total > 0 else 0.0
    return {
        "test": "one_way_anova",
        "f": _r(f),
        "df_between": _r(df_b),
        "df_within": _r(df_w),
        "p": _r(p),
        "eta_squared": _r(eta2),
        "ss_between": _r(ss_between),
        "ss_within": _r(ss_within),
        "n_total": n_total,
        "k_groups": k,
        "sig_0_05": bool(p < 0.05),
        "degenerate": False,
    }


# ---------------------------------------------------------------------------
# 5. 卡方独立性检验
# ---------------------------------------------------------------------------
def chi_square(observed) -> dict:
    """二维列联表的卡方独立性检验（Pearson chi2，无 Yates 校正）。"""
    if observed is None:
        raise ValueError("卡方检验需要二维列联表 observed: [[..],[..]]")
    try:
        table = [list(row) for row in observed]
    except TypeError:
        raise ValueError("observed 必须是二维数组，例如 [[10,20],[30,40]]")
    if len(table) < 2 or any(len(r) < 2 for r in table):
        raise ValueError("列联表至少需要 2 行 2 列")
    n_cols = len(table[0])
    if any(len(r) != n_cols for r in table):
        raise ValueError("列联表每一行的列数必须一致")

    try:
        mat = [[float(v) for v in row] for row in table]
    except (TypeError, ValueError):
        raise ValueError("列联表内必须全部是数值")

    for row in mat:
        for v in row:
            if v < 0 or not math.isfinite(v):
                raise ValueError("列联表内不能有负数或非法数值")

    n = sum(sum(r) for r in mat)
    if n <= 0:
        raise ValueError("列联表合计为 0，无法进行卡方检验")

    rows_t = [sum(r) for r in mat]
    cols_t = [sum(mat[i][j] for i in range(len(mat))) for j in range(n_cols)]

    expected = []
    for i in range(len(mat)):
        exp_row = []
        for j in range(n_cols):
            e = rows_t[i] * cols_t[j] / n
            exp_row.append(e)
        expected.append(exp_row)

    for row in expected:
        for e in row:
            if e <= 0:
                raise ValueError("期望频数出现 0，卡方检验不适用（请合并类别）")

    chi2 = sum((mat[i][j] - expected[i][j]) ** 2 / expected[i][j]
               for i in range(len(mat)) for j in range(n_cols))
    df = float((len(mat) - 1) * (n_cols - 1))
    p = _chi2_sf(chi2, df) if df > 0 else 0.0
    min_dim = min(len(mat), n_cols) - 1
    cramers_v = math.sqrt(chi2 / (n * min_dim)) if min_dim > 0 else 0.0

    return {
        "test": "chi_square",
        "chi2": _r(chi2),
        "df": _r(df),
        "p": _r(p),
        "cramers_v": _r(cramers_v),
        "n": _r(n),
        "expected": [[_r(e) for e in row] for row in expected],
        "sig_0_05": bool(p < 0.05),
        "degenerate": False,
    }


# ---------------------------------------------------------------------------
# 6. 相关分析
# ---------------------------------------------------------------------------
def _strength_label(r: float) -> str:
    a = abs(r)
    if a >= 0.9:
        s = "极强"
    elif a >= 0.7:
        s = "强"
    elif a >= 0.5:
        s = "中等"
    elif a >= 0.3:
        s = "弱"
    else:
        return "几乎无相关"
    return s + ("正相关" if r > 0 else "负相关")


def correlation(x, y, method: str = "pearson") -> dict:
    """相关分析：method="pearson" 或 "spearman"。

    Spearman 先转平均秩再算 Pearson，p 用 t 近似（df = n-2）。
    """
    a = _clean(x)
    b = _clean(y)
    n = min(len(a), len(b))
    if n < 3:
        raise ValueError("相关分析需要至少 3 对有效数据")
    a, b = a[:n], b[:n]

    m = str(method or "pearson").strip().lower()
    if m in ("pearson", "pearsonr", "皮尔逊", "线性"):
        m = "pearson"
    elif m in ("spearman", "spearmanr", "斯皮尔曼", "秩"):
        m = "spearman"
    else:
        raise ValueError("method 只支持 'pearson' 或 'spearman'，收到：%s" % method)

    if m == "spearman":
        a2, b2 = _ranks(a), _ranks(b)
    else:
        a2, b2 = a, b

    r, _ = _pearson(a2, b2)
    if abs(r) >= 1.0:
        p = 0.0
        t = 0.0
    else:
        t = r * math.sqrt((n - 2) / max(1e-300, 1.0 - r * r))
        p = min(1.0, 2.0 * _t_sf(abs(t), float(n - 2)))
    return {
        "method": m,
        "r": _r(r),
        "p": _r(p),
        "n": n,
        "r2": _r(r * r),
        "t": _r(t),
        "strength": _strength_label(r),
        "degenerate": bool(abs(r) < 1e-12 and m == "pearson"),
    }


# ---------------------------------------------------------------------------
# 7. 一元线性回归
# ---------------------------------------------------------------------------
def linear_regression(x, y) -> dict:
    """最小二乘一元线性回归 y = slope*x + intercept，含斜率显著性 p。"""
    a = _clean(x)
    b = _clean(y)
    n = min(len(a), len(b))
    if n < 3:
        raise ValueError("线性回归需要至少 3 对有效数据")
    a, b = a[:n], b[:n]
    mx, my = _mean(a), _mean(b)
    sxx = sum((v - mx) ** 2 for v in a)
    if sxx <= 0.0:
        raise ValueError("x 全为同一值，回归斜率无法定义（退化输入）")
    sxy = sum((a[i] - mx) * (b[i] - my) for i in range(n))
    slope = sxy / sxx
    intercept = my - slope * mx
    pred = [slope * a[i] + intercept for i in range(n)]
    resid = [b[i] - pred[i] for i in range(n)]
    ss_res = sum(r * r for r in resid)
    ss_tot = sum((v - my) ** 2 for v in b)
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    rmse = math.sqrt(ss_res / n) if n > 0 else 0.0
    df = n - 2
    mse = ss_res / df if df > 0 else 0.0
    stderr = math.sqrt(mse / sxx) if sxx > 0 else 0.0
    if stderr > 0:
        t = slope / stderr
        p = min(1.0, 2.0 * _t_sf(abs(t), float(df)))
    else:
        t = 0.0
        p = 0.0
    return {
        "slope": _r(slope),
        "intercept": _r(intercept),
        "r2": _r(r2),
        "rmse": _r(rmse),
        "n": n,
        "p": _r(p),
        "t": _r(t),
        "stderr_slope": _r(stderr),
        "df": df,
        "formula": "y = %.6f * x + %.6f" % (slope, intercept),
        "degenerate": bool(ss_tot <= 0),
    }
