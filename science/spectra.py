"""谱图与实验曲线解析：搜/读之外的「算」。

为什么需要它
------------
论文里最常见的图是谱图（XRD / Raman / IR / UV-Vis / 色谱）。LLM 不能"看图
读数"，但可以把数据点粘成两列数字。本模块负责把这些数字变成"几个峰、
多高、多宽、信噪比多少、主峰占比多少"这种可以直接写进结论的指标。

取舍
----
* Savitzky-Golay 用「窗口内最小二乘多项式拟合 + 取中心点」实现，而不是
  预先算卷积系数表：数学上等价，代码短得多，代价是慢一个常数倍（对几千点
  的谱图完全够用）。
* 峰检测只认严格的局部极大值，prominence 用"左右谷底中较高的那侧"（即相对
  较浅的谷），这样台阶上的肩峰不会被误判成独立峰。
* 噪声用一阶差分的 MAD 稳健估计，不被真峰带偏；信噪比用 P95-P5 作信号，
  避免单个离群点把 SNR 拉爆。
"""

from __future__ import annotations

import math

__all__ = [
    "smooth", "peaks", "snr", "integrate", "baseline_correct", "spectrum_summary",
]


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


def _as_list(values, label: str = "y") -> list:
    """把输入归一化为 float 列表（跳过 None / 非数字）。"""
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
        raise ValueError("%s 中没有可用的数值" % label)
    return out


def _x_of(x, n: int) -> list:
    """x 可省略：省略时按索引 0..n-1；长度不匹配时退化为索引。"""
    if x is None:
        return [float(i) for i in range(n)]
    xs = _as_list(x, "x")
    if len(xs) != n:
        return [float(i) for i in range(n)]
    return xs


def _solve(a: list, b: list) -> list:
    """高斯消元（列主元）解 n 元线性方程组，用于最小二乘正规方程。"""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-14:
            m[piv][col] = 1e-14  # 轻微正则化，避免完全奇异
        if piv != col:
            m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for r in range(col + 1, n):
            factor = m[r][col] / pv
            if factor == 0.0:
                continue
            for c in range(col, n + 1):
                m[r][c] -= factor * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))
        pv = m[r][r]
        x[r] = s / pv if abs(pv) > 1e-14 else 0.0
    return x


def _polyfit(xs: list, ys: list, deg: int):
    """最小二乘多项式拟合。

    返回 ``(coef, center, span)``：coef 是在缩放坐标 z=(x-center)/span 下的
    升幂系数。内部做缩放是为了改善正规方程的条件数（否则大 x 值会让
    Vandermonde 矩阵病态，高阶拟合直接崩）。
    """
    deg = max(0, int(deg))
    if len(xs) < deg + 1:
        deg = max(0, len(xs) - 1)
    if xs:
        center = (min(xs) + max(xs)) / 2.0
        span = max(1e-12, (max(xs) - min(xs)) / 2.0)
    else:
        center, span = 0.0, 1.0
    zs = [(v - center) / span for v in xs]
    m = deg + 1
    ata = [[0.0] * m for _ in range(m)]
    atb = [0.0] * m
    for z, y in zip(zs, ys):
        powers = [1.0] * m
        for k in range(1, m):
            powers[k] = powers[k - 1] * z
        for i in range(m):
            atb[i] += powers[i] * y
            for j in range(m):
                ata[i][j] += powers[i] * powers[j]
    return _solve(ata, atb), center, span


def _polyval(coef: list, z: float) -> float:
    acc = 0.0
    for c in reversed(coef):
        acc = acc * z + c
    return acc


def _median(values: list) -> float:
    sv = sorted(values)
    n = len(sv)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return float(sv[n // 2])
    return (sv[n // 2 - 1] + sv[n // 2]) / 2.0


def _percentile(sorted_v: list, q: float) -> float:
    n = len(sorted_v)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_v[0])
    pos = (n - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_v[lo]) * (1 - frac) + float(sorted_v[hi]) * frac


def _trapz(xs: list, ys: list) -> float:
    total = 0.0
    for i in range(1, min(len(xs), len(ys))):
        total += (xs[i] - xs[i - 1]) * (ys[i] + ys[i - 1]) / 2.0
    return total


# ---------------------------------------------------------------------------
# 1. Savitzky-Golay 平滑
# ---------------------------------------------------------------------------
def smooth(y, window: int = 5, poly: int = 2) -> dict:
    """Savitzky-Golay 平滑：窗口内拟合 poly 次多项式，取中心点作为平滑值。

    window 必须是大于 poly 的奇数；不满足时自动修正，并在 ``window_used``
    里回报实际使用的窗口，保证调用方知道发生了什么。
    """
    ys = _as_list(y, "y")
    n = len(ys)
    try:
        w = int(window)
    except (TypeError, ValueError):
        w = 5
    try:
        p = int(poly)
    except (TypeError, ValueError):
        p = 2
    if p < 0:
        p = 0

    note = ""
    # 至少要比阶数多 2 个点，才能让中心点的拟合是"超定"的
    min_w = p + 1 if (p + 1) % 2 == 1 else p + 2
    if w % 2 == 0:
        w += 1
        note = "window 已自动修正为奇数"
    if w <= p:
        w = min_w
        note = "window 太小，已自动放大到 %d" % w
    if w > n:
        w = n if n % 2 == 1 else n - 1
        if w <= p:
            # 点数太少，无法拟合多项式：原样返回（window_used 保持为奇数 1）
            return {"smoothed": [_r(v) for v in ys], "window_used": 1,
                    "poly_used": p, "note": "点数不足以做多项式拟合，已原样返回"}

    half = (w - 1) // 2
    out = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n - 1, i + half)
        xs = [float(v) for v in range(lo, hi + 1)]
        seg = ys[lo:hi + 1]
        coef, center, span = _polyfit(xs, seg, min(p, len(seg) - 1))
        # 中心点 i 在缩放坐标下的位置；窗口在边缘处不对称，所以不能直接用 z=0
        z = (i - center) / span
        out.append(_r(_polyval(coef, z), 9))

    return {
        "smoothed": out,
        "window_used": w,
        "poly_used": p,
        "note": note or "窗口合法，未做修正",
    }


# ---------------------------------------------------------------------------
# 2. 峰检测
# ---------------------------------------------------------------------------
def _prominence_of(ys: list, i: int):
    """返回 (prominence, left_base_index, right_base_index)。"""
    n = len(ys)
    h = ys[i]
    left_min = h
    lb = i
    j = i
    while j > 0:
        j -= 1
        if ys[j] > h:  # 遇到更高的峰即停在鞍点
            break
        if ys[j] < left_min:
            left_min = ys[j]
            lb = j
    right_min = h
    rb = i
    j = i
    while j < n - 1:
        j += 1
        if ys[j] > h:
            break
        if ys[j] < right_min:
            right_min = ys[j]
            rb = j
    prom = h - max(left_min, right_min)
    return max(0.0, prom), lb, rb


def _half_width(xs: list, ys: list, i: int, half_level: float, lb: int, rb: int):
    """在 [lb, rb] 内找 y 首次低于 half_level 的位置并线性插值，返回左/右边界 x。"""
    xl = xs[lb]
    j = i
    while j > lb:
        if ys[j - 1] < half_level:
            y0, y1 = ys[j - 1], ys[j]
            denom = (y1 - y0)
            frac = (half_level - y0) / denom if denom != 0 else 0.5
            frac = min(1.0, max(0.0, frac))
            xl = xs[j - 1] + frac * (xs[j] - xs[j - 1])
            break
        j -= 1
    xr = xs[rb]
    j = i
    while j < rb:
        if ys[j + 1] < half_level:
            y0, y1 = ys[j], ys[j + 1]
            denom = (y1 - y0)
            frac = (y0 - half_level) / (-denom) if denom != 0 else 0.5
            frac = min(1.0, max(0.0, frac))
            xr = xs[j] + frac * (xs[j + 1] - xs[j])
            break
        j += 1
    return xl, xr


def peaks(x, y, min_prominence=None, min_height=None, min_distance: int = 1) -> dict:
    """局部极大值峰检测，输出位置、突出度、半高宽、峰面积。

    min_prominence 缺省为 0.05*(max(y)-min(y))；min_distance 是相邻峰允许的
    最小索引间隔（保留更高的那个）。
    """
    ys = _as_list(y, "y")
    n = len(ys)
    if n < 3:
        return {"peaks": [], "count": 0, "baseline": _r(ys[0] if ys else 0.0),
                "noise": 0.0, "snr": 0.0, "min_prominence": 0.0,
                "note": "点数不足 3 个，无法找峰"}
    xs = _x_of(x, n)

    ymin, ymax = min(ys), max(ys)
    span = ymax - ymin
    if min_prominence is None:
        prom_thr = 0.05 * span
    else:
        try:
            prom_thr = float(min_prominence)
        except (TypeError, ValueError):
            raise ValueError("min_prominence 必须是数值")
        if prom_thr < 0:
            raise ValueError("min_prominence 不能为负数")
    if min_height is None:
        height_thr = ymin
    else:
        try:
            height_thr = float(min_height)
        except (TypeError, ValueError):
            raise ValueError("min_height 必须是数值")
    try:
        min_dist = max(1, int(min_distance))
    except (TypeError, ValueError):
        min_dist = 1

    # 1) 找严格局部极大值
    candidates = []
    for i in range(1, n - 1):
        if ys[i] > ys[i - 1] and ys[i] >= ys[i + 1]:
            candidates.append(i)

    # 2) 计算突出度并过滤
    raw = []
    for i in candidates:
        if ys[i] < height_thr:
            continue
        prom, lb, rb = _prominence_of(ys, i)
        if prom < prom_thr:
            continue
        raw.append((i, prom, lb, rb))

    # 3) 最小峰间距：冲突时保留突出度更大的
    raw.sort(key=lambda t: -t[1])
    kept = []
    for item in raw:
        if all(abs(item[0] - k[0]) >= min_dist for k in kept):
            kept.append(item)
    kept.sort(key=lambda t: t[0])

    noise, sig = _noise_signal(ys)
    denom = span if span > 0 else 1.0
    out = []
    for i, prom, lb, rb in kept:
        if prom > 0:
            half_level = ys[i] - prom / 2.0
        else:
            half_level = ys[i]
        xl, xr = _half_width(xs, ys, i, half_level, lb, rb)
        fwhm = abs(xr - xl)
        area = abs(_trapz(xs[lb:rb + 1], ys[lb:rb + 1]))
        out.append({
            "index": int(i),
            "x": _r(xs[i]),
            "y": _r(ys[i]),
            "prominence": _r(prom),
            "height_ratio": _r((ys[i] - ymin) / denom),
            "left_base_index": int(lb),
            "right_base_index": int(rb),
            "fwhm": _r(fwhm),
            "area": _r(area),
        })

    return {
        "peaks": out,
        "count": len(out),
        "baseline": _r(ymin),
        "noise": _r(noise),
        "snr": _r(sig / noise) if noise > 0 else 0.0,
        "min_prominence": _r(prom_thr),
        "min_distance": min_dist,
    }


# ---------------------------------------------------------------------------
# 3. 信噪比
# ---------------------------------------------------------------------------
def _noise_signal(ys: list):
    """返回 (noise, signal)。噪声用一阶差分 MAD 稳健估计，信号用 P95-P5。"""
    n = len(ys)
    if n < 3:
        return 0.0, (max(ys) - min(ys)) if ys else 0.0
    diffs = [ys[i] - ys[i - 1] for i in range(1, n)]
    med = _median(diffs)
    mad = _median([abs(d - med) for d in diffs])
    sigma_d = 1.4826 * mad
    noise = sigma_d / math.sqrt(2.0)
    sv = sorted(ys)
    signal = _percentile(sv, 0.95) - _percentile(sv, 0.05)
    return noise, signal


def snr(y) -> dict:
    """信噪比：噪声 = 一阶差分 MAD/sqrt(2)，信号 = P95-P5。"""
    ys = _as_list(y, "y")
    noise, signal = _noise_signal(ys)
    ratio = (signal / noise) if noise > 0 else 0.0
    return {
        "snr": _r(ratio),
        "noise": _r(noise, 9),
        "signal": _r(signal),
        "n": len(ys),
        "note": "噪声为 0（完全平滑数据）时 snr 记 0.0" if noise == 0 else "OK",
    }


# ---------------------------------------------------------------------------
# 4. 积分
# ---------------------------------------------------------------------------
def integrate(x, y, a=None, b=None) -> dict:
    """梯形法积分；a/b 可截取区间，端点处线性插值。"""
    ys = _as_list(y, "y")
    n = len(ys)
    xs = _x_of(x, n)
    if n < 2:
        raise ValueError("积分至少需要 2 个数据点")

    lo = min(xs) if a is None else float(a)
    hi = max(xs) if b is None else float(b)
    if lo > hi:
        lo, hi = hi, lo
    if lo < min(xs) or hi > max(xs):
        # 不报错，但提示区间被裁剪到数据范围内
        lo = max(lo, min(xs))
        hi = min(hi, max(xs))
    if lo == hi:
        return {"area": 0.0, "a": _r(lo), "b": _r(hi), "method": "trapezoid",
                "n_points": 0}

    # 区间内的线性插值：给定 x 求 y
    def _interp(target):
        for k in range(1, n):
            x0, x1 = xs[k - 1], xs[k]
            if (x0 - target) * (x1 - target) <= 0 and x0 != x1:
                t = (target - x0) / (x1 - x0)
                return ys[k - 1] + t * (ys[k] - ys[k - 1])
        return ys[-1]

    # 收集区间 [lo, hi] 内的点；端点若不在采样点上则线性插值补上
    sel_x = [xs[k] for k in range(n) if lo <= xs[k] <= hi]
    sel_y = [ys[k] for k in range(n) if lo <= xs[k] <= hi]

    if not sel_x:
        sel_x = [lo, hi]
        sel_y = [_interp(lo), _interp(hi)]
    else:
        if sel_x[0] > lo:
            sel_x.insert(0, lo)
            sel_y.insert(0, _interp(lo))
        if sel_x[-1] < hi:
            sel_x.append(hi)
            sel_y.append(_interp(hi))

    return {
        "area": _r(abs(_trapz(sel_x, sel_y))),
        "a": _r(lo),
        "b": _r(hi),
        "method": "trapezoid",
        "n_points": len(sel_x),
    }


# ---------------------------------------------------------------------------
# 5. 基线校正
# ---------------------------------------------------------------------------
def baseline_correct(x, y, method: str = "poly", degree: int = 3) -> dict:
    """迭代多项式基线校正：拟合 -> 剔除残差大于 1 倍标准差的点 -> 重复 3 次。

    思路是"不对称最小二乘"的简化版：峰是正向的离群点，把它们剔掉以后
    剩下的点才代表基线。
    """
    ys = _as_list(y, "y")
    n = len(ys)
    xs = _x_of(x, n)
    if n < 2:
        raise ValueError("基线校正至少需要 2 个数据点")
    m = str(method or "poly").lower()
    if m not in ("poly", "polynomial", "多项式"):
        raise ValueError("目前只支持 method='poly'（多项式基线），收到：%s" % method)
    try:
        deg = int(degree)
    except (TypeError, ValueError):
        raise ValueError("degree 必须是整数")
    if deg < 0:
        deg = 0
    deg = min(deg, n - 1)

    mask = [True] * n  # True 表示该点参与基线拟合
    coef, center, span = _polyfit(xs, ys, deg)
    for _ in range(3):
        base = [_polyval(coef, (xs[i] - center) / span) for i in range(n)]
        resid = [ys[i] - base[i] for i in range(n)]
        sel = [resid[i] for i in range(n) if mask[i]]
        if len(sel) < deg + 2:
            break
        mu = sum(sel) / len(sel)
        sd = math.sqrt(sum((v - mu) ** 2 for v in sel) / max(1, len(sel) - 1))
        if sd <= 0:
            break
        new_mask = [mask[i] and resid[i] <= mu + sd for i in range(n)]
        if sum(new_mask) < deg + 2:
            break
        mask = new_mask
        sub_x = [xs[i] for i in range(n) if mask[i]]
        sub_y = [ys[i] for i in range(n) if mask[i]]
        coef, center, span = _polyfit(sub_x, sub_y, min(deg, len(sub_x) - 1))

    baseline = [_r(_polyval(coef, (xs[i] - center) / span), 9) for i in range(n)]
    corrected = [_r(ys[i] - baseline[i], 9) for i in range(n)]
    return {
        "corrected": corrected,
        "baseline": baseline,
        "degree": deg,
        "n_used": sum(1 for v in mask if v),
        "method": "poly",
    }


# ---------------------------------------------------------------------------
# 6. 一步读懂谱图
# ---------------------------------------------------------------------------
def spectrum_summary(x, y) -> dict:
    """谱图一句话摘要：平滑 + 信噪比 + 找峰 + 主峰占比。"""
    ys = _as_list(y, "y")
    n = len(ys)
    xs = _x_of(x, n)
    sm = smooth(ys, 5, 2)
    smoothed = sm["smoothed"]
    s = snr(smoothed)
    pk = peaks(xs, smoothed)

    plist = pk["peaks"]
    main_peak = None
    top_peaks = []
    if plist:
        ordered = sorted(plist, key=lambda p: -p["y"])
        main_peak = ordered[0]
        top_peaks = ordered[:3]
        span = (max(smoothed) - min(smoothed)) or 1.0
        main_peak_ratio = (main_peak["y"] - min(smoothed)) / span
    else:
        main_peak_ratio = 0.0

    return {
        "n": n,
        "snr": s["snr"],
        "noise": s["noise"],
        "peak_count": pk["count"],
        "main_peak": main_peak,
        "main_peak_ratio": _r(main_peak_ratio),
        "top_peaks": top_peaks,
        "baseline": pk["baseline"],
        "x_min": _r(min(xs)),
        "x_max": _r(max(xs)),
        "window_used": sm["window_used"],
        "text": ("共 %d 个数据点，信噪比约 %.1f，检出 %d 个峰，主峰位于 x=%.4g"
                 "（占动态范围 %.0f%%）。" % (
                     n, s["snr"], pk["count"],
                     main_peak["x"] if main_peak else 0.0,
                     100.0 * main_peak_ratio)) if main_peak else
                ("共 %d 个数据点，信噪比约 %.1f，未检出显著峰。" % (n, s["snr"])),
    }
