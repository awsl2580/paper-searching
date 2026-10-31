"""科学计算工具层（science）的验收测试。

沿用仓库既有风格：不依赖 pytest，纯标准库脚本，末尾打印 "N/M checks passed"
并以退出码表示成败；在评委那种"没装任何测试框架"的机器上也能直接跑。

覆盖重点：
  * 每个工具函数返回的**数值**是否符合手算/已知值（容差 1e-3 ~ 1e-4）
  * 输入可以是 list、逗号串、表格文本等不同形态
  * 非法输入不会抛异常到调用方，而是 ok=False + 中文 error
  * 全部输出必须是 JSON 可序列化的（不允许 NaN / Infinity）

运行：
    .venv\\Scripts\\python.exe tests\\test_science_tools.py
"""
import json
import math
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 测试环境自洽：不依赖 .env，也不要有网络/限流副作用
os.environ.setdefault("FLASK_SECRET_KEY", "0123456789abcdef" * 4)
os.environ.setdefault("DEEPSEEK_RPM", "6000")   # 用例里不要因为限流而 sleep
os.environ.setdefault("MCP_TRANSPORT", "inproc")
os.environ.setdefault("AI4S_MAX_ITERATIONS", "1")
sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401  （分类后的源码目录，见项目根 paths.py）

# 控制台可能是 GBK，先切到 utf-8 避免中文输出乱码/报错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import science  # noqa: E402
from science import curves, dataio, design, molecules, spectra, stats  # noqa: E402

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


def near(a, b, tol=1e-6):
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return False
    except Exception:
        return True


# ===========================================================================
# 一、dataio：表格三格式解析
# ===========================================================================
csv_text = "sample,value,note\nA,1.5,ok\nB,2.5,\nC,3.5,缺\n"
t = dataio.parse_table(csv_text)
ok("CSV 列名", t["columns"] == ["sample", "value", "note"], t["columns"])
ok("CSV 行数", t["n_rows"] == 3, t["n_rows"])
ok("CSV 列数", t["n_cols"] == 3, t["n_cols"])
ok("CSV 单元格类型", t["rows"][0] == ["A", 1.5, "ok"], t["rows"][0])
ok("CSV numeric 判定", t["numeric"] == {"sample": False, "value": True, "note": False},
   t["numeric"])
ok("CSV 空单元格 -> None", t["rows"][1][2] is None, t["rows"][1])

tsv_text = "x\ty\n1\t10\n2\t20\n3\t30"
t2 = dataio.parse_table(tsv_text)
ok("TSV 列名", t2["columns"] == ["x", "y"], t2["columns"])
ok("TSV 数值", [r[1] for r in t2["rows"]] == [10.0, 20.0, 30.0], t2["rows"])

sp_text = "x    y\n1    2\n3    4"
t3 = dataio.parse_table(sp_text)
ok("多空格分隔列名", t3["columns"] == ["x", "y"], t3["columns"])
ok("多空格分隔数值", t3["rows"][1] == [3.0, 4.0], t3["rows"])

md_text = """| 温度 | 产率 | 备注 |
|------|------|------|
| 300  | 0.45 | 初测 |
| 350  | 0.62 | 复测 |
| 400  | 0.71 |      |"""
t4 = dataio.parse_table(md_text)
ok("Markdown 列名", t4["columns"] == ["温度", "产率", "备注"], t4["columns"])
ok("Markdown 行数", t4["n_rows"] == 3, t4["n_rows"])
ok("Markdown 数值解析", t4["rows"][1][1] == 0.62, t4["rows"][1])
ok("Markdown 空单元 None", t4["rows"][2][2] is None, t4["rows"][2])

# 首行全是数字 -> 自动生成 c1..cn，且首行当数据
t5 = dataio.parse_table("1,2,3\n4,5,6")
ok("无表头自动命名 c1..cn", t5["columns"] == ["c1", "c2", "c3"], t5["columns"])
ok("无表头首行计入数据", t5["n_rows"] == 2, t5["n_rows"])

# 行长度不足补 None / 超出截断
t6 = dataio.parse_table("a,b,c\n1,2\n3,4,5,6")
ok("缺列补 None", t6["rows"][0] == [1.0, 2.0, None], t6["rows"][0])
ok("超长截断到 n_cols", t6["rows"][1] == [3.0, 4.0, 5.0], t6["rows"][1])

ok("空表格抛 ValueError", raises(dataio.parse_table, "   "))

cols = dataio.to_columns(t)
ok("to_columns 结构", cols["value"] == [1.5, 2.5, 3.5], cols["value"])
nums = dataio.numbers([1, None, "2.5", "", "abc", 3])
ok("numbers 过滤非数值", nums == [1.0, 2.5, 3.0], nums)
ok("numbers 接受逗号串", dataio.numbers("1,2,3") == [1.0, 2.0, 3.0])

# ===========================================================================
# 二、stats：描述统计
# ===========================================================================
d = stats.describe([1, 2, 3, 4])
ok("describe n", d["n"] == 4, d["n"])
ok("describe mean=2.5", near(d["mean"], 2.5), d["mean"])
ok("describe sd=1.290994", near(d["sd"], 1.290994, 1e-6), d["sd"])
ok("describe median=2.5", near(d["median"], 2.5), d["median"])
ok("describe q1=1.75", near(d["q1"], 1.75), d["q1"])
ok("describe q3=3.25", near(d["q3"], 3.25), d["q3"])
ok("describe iqr=1.5", near(d["iqr"], 1.5), d["iqr"])
ok("describe min/max", near(d["min"], 1.0) and near(d["max"], 4.0), (d["min"], d["max"]))
ok("describe 对称数据 skew=0", near(d["skew"], 0.0, 1e-9), d["skew"])
ok("describe cv=sd/mean", near(d["cv"], 1.290994 / 2.5, 1e-5), d["cv"])
d1 = stats.describe([7])
ok("describe n=1 时 sd=0", d1["sd"] == 0.0, d1["sd"])
dc = stats.describe([5, 5, 5, 5])
ok("describe 常量 cv=0", dc["cv"] == 0.0, dc["cv"])
ok("describe 空输入抛错", raises(stats.describe, []))

# 分布式：t / F / 卡方 / 正态
ok("_t_sf(2,10)≈0.036694", near(stats._t_sf(2.0, 10), 0.036694, 1e-6),
   stats._t_sf(2.0, 10))
ok("t=2,df=10 双侧 p≈0.073388",
   near(2 * stats._t_sf(2.0, 10), 0.073388, 1e-4), 2 * stats._t_sf(2.0, 10))
ok("_t_sf(0,5)=0.5", near(stats._t_sf(0.0, 5), 0.5), stats._t_sf(0.0, 5))
ok("_f_sf(3,2,10)≈0.0953674", near(stats._f_sf(3.0, 2, 10), 0.0953674, 1e-5),
   stats._f_sf(3.0, 2, 10))
ok("_chi2_sf(3.841,1)≈0.05", near(stats._chi2_sf(3.8414588, 1), 0.05, 1e-4),
   stats._chi2_sf(3.8414588, 1))
ok("_chi2_sf(5.991,2)≈0.05", near(stats._chi2_sf(5.9914645, 2), 0.05, 1e-4),
   stats._chi2_sf(5.9914645, 2))
ok("_norm_sf(1.96)≈0.025", near(stats._norm_sf(1.96), 0.025, 1e-3),
   stats._norm_sf(1.96))

# ===========================================================================
# 三、stats：t 检验
# ===========================================================================
XW = [5.1, 4.9, 5.0, 5.2, 5.3]
YW = [4.9, 4.8, 5.1, 4.7]
w = stats.ttest(XW, YW)
ok("Welch 检验标签", w["test"] == "welch_t", w["test"])
ok("Welch t≈2.0294", near(w["t"], 2.029444, 1e-4), w["t"])
ok("Welch df≈6.3024", near(w["df"], 6.302354, 1e-4), w["df"])
ok("Welch p 落在合理区间(0.05,0.15)", 0.05 < w["p"] < 0.15, w["p"])
ok("Welch p≈0.086464", near(w["p"], 0.086464, 1e-4), w["p"])
ok("Welch 不显著 sig_0_05=False", w["sig_0_05"] is False, w["sig_0_05"])
ok("Welch 均值差=0.225", near(w["diff"], 0.225, 1e-9), w["diff"])
ok("Welch n_x/n_y", (w["n_x"], w["n_y"]) == (5, 4), (w["n_x"], w["n_y"]))
st = stats.ttest(XW, YW, equal_var=True)
ok("Student 检验标签", st["test"] == "student_t", st["test"])
ok("Student t≈2.04939", near(st["t"], 2.04939, 1e-4), st["t"])
ok("Student df=7", near(st["df"], 7.0), st["df"])
ok("Student p≈0.079602", near(st["p"], 0.079602, 1e-4), st["p"])
ok("t 检验单组样本量不足抛错", raises(stats.ttest, [1.0], [2.0, 3.0]))
ok("t 检验常数组抛错", raises(stats.ttest, [5, 5, 5], [5, 5, 5]))

o1 = stats.ttest_one_sample([1, 2, 3, 4, 5], 3.0)
ok("单样本 t=0（均值等于 mu）", near(o1["t"], 0.0), o1["t"])
ok("单样本 p=1", near(o1["p"], 1.0), o1["p"])
o2 = stats.ttest_one_sample([2, 3, 4, 5, 6], 0.0)
ok("单样本 t≈5.656854", near(o2["t"], 5.656854, 1e-5), o2["t"])
ok("单样本 df=4", near(o2["df"], 4.0), o2["df"])
ok("单样本 p≈0.004813", near(o2["p"], 0.004813, 1e-5), o2["p"])

mw = stats.mannwhitney([1, 2, 3, 4], [5, 6, 7, 8])
ok("Mann-Whitney U=0", near(mw["u"], 0.0), mw["u"])
ok("Mann-Whitney z≈2.165064", near(mw["z"], 2.165064, 1e-5), mw["z"])
ok("Mann-Whitney p≈0.030383", near(mw["p"], 0.030383, 1e-5), mw["p"])
ok("Mann-Whitney 中位数", near(mw["median_x"], 2.5) and near(mw["median_y"], 6.5),
   (mw["median_x"], mw["median_y"]))

# ===========================================================================
# 四、stats：方差分析与卡方
# ===========================================================================
# 三组 [1,2,3] [4,5,6] [7,8,10] 手算：
#   grand=46/9, SSB=60.2222, df_b=2, SSW=8.6667, df_w=6 -> F=20.8462
a = stats.anova([1, 2, 3], [4, 5, 6], [7, 8, 10])
ok("ANOVA F≈20.846154", near(a["f"], 20.846154, 1e-5), a["f"])
ok("ANOVA df_between=2", near(a["df_between"], 2.0), a["df_between"])
ok("ANOVA df_within=6", near(a["df_within"], 6.0), a["df_within"])
ok("ANOVA SSB≈60.222222", near(a["ss_between"], 60.222222, 1e-5), a["ss_between"])
ok("ANOVA SSW≈8.666667", near(a["ss_within"], 8.666667, 1e-5), a["ss_within"])
ok("ANOVA p≈0.001991", near(a["p"], 0.001991, 1e-5), a["p"])
ok("ANOVA eta_squared≈0.874194", near(a["eta_squared"], 0.874194, 1e-5),
   a["eta_squared"])
ok("ANOVA 显著", a["sig_0_05"] is True, a["sig_0_05"])
ok("ANOVA 接受 [[..],[..]] 形式",
   near(stats.anova([[1, 2, 3], [4, 5, 6], [7, 8, 10]])["f"], 20.846154, 1e-5))
ok("ANOVA 只给一组抛错", raises(stats.anova, [1, 2, 3]))

# 2x2 表 [[10,20],[30,40]]：期望 [[12,18],[28,42]]，chi2=0.793651, p≈0.373
c2 = stats.chi_square([[10, 20], [30, 40]])
ok("卡方 chi2≈0.793651", near(c2["chi2"], 0.793651, 1e-6), c2["chi2"])
ok("卡方 df=1", near(c2["df"], 1.0), c2["df"])
ok("卡方 p≈0.372998", near(c2["p"], 0.372998, 1e-4), c2["p"])
ok("卡方期望频数", c2["expected"] == [[12.0, 18.0], [28.0, 42.0]], c2["expected"])
ok("卡方 Cramers V≈0.089087", near(c2["cramers_v"], 0.089087, 1e-5),
   c2["cramers_v"])
ok("卡方不显著", c2["sig_0_05"] is False, c2["sig_0_05"])
ok("卡方非 2x2 表可算", stats.chi_square([[10, 20, 30], [30, 40, 50]])["df"] == 2.0)
ok("卡方拒绝一维输入", raises(stats.chi_square, [1, 2, 3]))

# ===========================================================================
# 五、stats：相关与回归
# ===========================================================================
cr = stats.correlation([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
ok("Pearson 完美线性 r=1", near(cr["r"], 1.0, 1e-9), cr["r"])
ok("Pearson 完美线性 p=0", near(cr["p"], 0.0, 1e-9), cr["p"])
ok("Pearson r2=1", near(cr["r2"], 1.0, 1e-9), cr["r2"])
cr2 = stats.correlation([1, 2, 3, 4, 5], [2, 4, 5, 4, 5])
ok("Pearson 已知 r≈0.774597", near(cr2["r"], 0.774597, 1e-5), cr2["r"])
ok("Pearson 已知 p≈0.124027", near(cr2["p"], 0.124027, 1e-5), cr2["p"])
sp = stats.correlation([1, 2, 3, 4, 5], [1, 4, 9, 16, 25], method="spearman")
ok("Spearman 单调非线性 r=1", near(sp["r"], 1.0, 1e-9), sp["r"])
ok("Spearman method 字段", sp["method"] == "spearman", sp["method"])
ok("Spearman p 很小", sp["p"] < 0.05, sp["p"])
ok("相关 method 非法抛错",
   raises(stats.correlation, [1, 2, 3], [1, 2, 3], "kendallx"))
ok("相关样本量不足抛错", raises(stats.correlation, [1, 2], [1, 2]))

reg = stats.linear_regression([1, 2, 3, 4], [3, 5, 7, 9])
ok("回归 slope=2", near(reg["slope"], 2.0), reg["slope"])
ok("回归 intercept=1", near(reg["intercept"], 1.0), reg["intercept"])
ok("回归 r2=1", near(reg["r2"], 1.0), reg["r2"])
ok("回归 rmse=0", near(reg["rmse"], 0.0, 1e-9), reg["rmse"])
reg2 = stats.linear_regression([1, 2, 3, 4, 5], [2, 4, 5, 4, 5])
ok("回归手算 slope=0.6", near(reg2["slope"], 0.6, 1e-9), reg2["slope"])
ok("回归手算 intercept=2.2", near(reg2["intercept"], 2.2, 1e-9), reg2["intercept"])
ok("回归手算 r2=0.6", near(reg2["r2"], 0.6, 1e-9), reg2["r2"])
ok("回归手算 rmse≈0.69282", near(reg2["rmse"], 0.69282, 1e-5), reg2["rmse"])
ok("回归 stderr_slope≈0.282843", near(reg2["stderr_slope"], 0.282843, 1e-5),
   reg2["stderr_slope"])
ok("回归 formula 含数值", "0.6" in reg2["formula"], reg2["formula"])

# 退化输入绝不返回 NaN/Inf
for label, obj in [("describe", d), ("ttest", w), ("anova", a), ("chi2", c2),
                   ("corr", cr2), ("reg", reg2), ("mw", mw)]:
    ok("JSON 严格可序列化：%s" % label,
       bool(json.dumps(obj, allow_nan=False, ensure_ascii=False)))

# ===========================================================================
# 六、spectra：平滑 / 找峰 / 信噪比 / 积分 / 基线
# ===========================================================================
# 纯二次曲线（窗口内 poly=2 应完全复现）
xs_q = [i * 0.2 for i in range(51)]
ys_q = [2.0 * x * x - 3.0 * x + 1.0 for x in xs_q]
sm_q = spectra.smooth(ys_q, 5, 2)
ok("SG 平滑重建二次曲线",
   max(abs(sm_q["smoothed"][i] - ys_q[i]) for i in range(len(ys_q))) < 1e-6,
   max(abs(sm_q["smoothed"][i] - ys_q[i]) for i in range(len(ys_q))))
ok("SG window_used 为奇数", sm_q["window_used"] % 2 == 1, sm_q["window_used"])
sm_even = spectra.smooth(ys_q, 4, 2)
ok("SG 偶数窗口自动修正为奇数", sm_even["window_used"] == 5, sm_even["window_used"])
sm_small = spectra.smooth(ys_q, 3, 4)
ok("SG 窗口不大于阶数时自动放大",
   sm_small["window_used"] > 4 and sm_small["window_used"] % 2 == 1,
   sm_small["window_used"])

# 双高斯峰 + 固定种子噪声
rnd = random.Random(7)
xs_p = [i * 0.05 for i in range(201)]


def _g(xv, amp, mu, sigma):
    return amp * math.exp(-((xv - mu) ** 2) / (2.0 * sigma * sigma))


truth_p = [_g(x, 1.0, 3.0, 0.4) + _g(x, 0.7, 6.5, 0.5) for x in xs_p]
ys_p = [truth_p[i] + rnd.gauss(0.0, 0.003) for i in range(len(xs_p))]

pk = spectra.peaks(xs_p, ys_p)
ok("找峰数量=2", pk["count"] == 2, [(p["x"], p["y"]) for p in pk["peaks"]])
if pk["count"] == 2:
    p1, p2 = pk["peaks"]
    ok("峰1 位置误差<0.3", abs(p1["x"] - 3.0) < 0.3, p1["x"])
    ok("峰2 位置误差<0.3", abs(p2["x"] - 6.5) < 0.3, p2["x"])
    ok("峰1 FWHM 误差<25%", abs(p1["fwhm"] - 2.3548 * 0.4) / (2.3548 * 0.4) < 0.25,
       p1["fwhm"])
    ok("峰2 FWHM 误差<25%", abs(p2["fwhm"] - 2.3548 * 0.5) / (2.3548 * 0.5) < 0.25,
       p2["fwhm"])
    ok("峰1 突出度大于峰2", p1["prominence"] > p2["prominence"],
       (p1["prominence"], p2["prominence"]))
    ok("主峰 height_ratio=1", near(p1["height_ratio"], 1.0, 1e-3), p1["height_ratio"])
    ok("峰面积为正", p1["area"] > 0 and p2["area"] > 0, (p1["area"], p2["area"]))
else:
    for _n in ("峰1 位置误差<0.3", "峰2 位置误差<0.3", "峰1 FWHM 误差<25%",
               "峰2 FWHM 误差<25%", "峰1 突出度大于峰2", "主峰 height_ratio=1",
               "峰面积为正"):
        ok(_n, False, pk["count"])

# SG 平滑应压低噪声（对比与真值的 RMSE）
rmse_raw = math.sqrt(sum((ys_p[i] - truth_p[i]) ** 2 for i in range(len(ys_p)))
                     / len(ys_p))
sm_p = spectra.smooth(ys_p, 5, 2)["smoothed"]
rmse_sm = math.sqrt(sum((sm_p[i] - truth_p[i]) ** 2 for i in range(len(ys_p)))
                    / len(ys_p))
ok("SG 平滑后噪声下降", rmse_sm < rmse_raw, (rmse_raw, rmse_sm))

# 信噪比：纯噪声 << 强信号
noise_only = [rnd.gauss(0.0, 1.0) for _ in range(600)]
strong = [10.0 * math.sin(i / 5.0) + rnd.gauss(0.0, 0.01) for i in range(600)]
s_noise = spectra.snr(noise_only)
s_strong = spectra.snr(strong)
ok("SNR 纯噪声<强信号", s_noise["snr"] < s_strong["snr"],
   (s_noise["snr"], s_strong["snr"]))
ok("SNR 噪声估计≈1（白噪声 sigma=1）", near(s_noise["noise"], 1.0, 0.2),
   s_noise["noise"])
ok("SNR 信号为 P95-P5", near(s_noise["signal"], 3.29, 0.5), s_noise["signal"])

it = spectra.integrate([i / 100.0 for i in range(101)],
                       [i / 100.0 for i in range(101)])
ok("梯形积分 ∫x dx(0..1)=0.5", near(it["area"], 0.5, 1e-9), it["area"])
it2 = spectra.integrate([i / 100.0 for i in range(101)],
                        [i / 100.0 for i in range(101)], a=0.2, b=0.6)
ok("区间积分 ∫x dx(0.2..0.6)=0.16", near(it2["area"], 0.16, 1e-9), it2["area"])

# 基线校正：4 + 0.3x 的线性基线（3 次多项式足以完全拟合）
xs_b = [i * 0.1 for i in range(101)]
ys_b = [4.0 + 0.3 * x for x in xs_b]
bc = spectra.baseline_correct(xs_b, ys_b, degree=3)
ok("基线校正后均值≈0",
   abs(sum(bc["corrected"]) / len(bc["corrected"])) < 1e-6,
   sum(bc["corrected"]) / len(bc["corrected"]))
ok("基线等于原始线性趋势", near(bc["baseline"][0], 4.0, 1e-6), bc["baseline"][0])

summ = spectra.spectrum_summary(xs_p, ys_p)
ok("谱图摘要 峰数=2", summ["peak_count"] == 2, summ["peak_count"])
ok("谱图摘要 snr>10", summ["snr"] > 10, summ["snr"])
ok("谱图摘要 主峰占比=1", near(summ["main_peak_ratio"], 1.0, 1e-3),
   summ["main_peak_ratio"])
ok("谱图摘要 top_peaks<=3", len(summ["top_peaks"]) <= 3, len(summ["top_peaks"]))
ok("谱图摘要含中文结论", isinstance(summ["text"], str) and "峰" in summ["text"],
   summ["text"])

# ===========================================================================
# 七、curves：拟合 / 预测 / 外推
# ===========================================================================
ok("模型名清单", curves.model_names() ==
   ["linear", "exponential", "logistic", "power", "gaussian"], curves.model_names())

xs_e = [i * 0.2 for i in range(26)]
ys_e = [3.0 * math.exp(0.5 * x) for x in xs_e]
fe = curves.fit(xs_e, ys_e, "exponential")
ok("指数拟合 a 误差<5%", abs(fe["params"]["a"] - 3.0) / 3.0 < 0.05, fe["params"]["a"])
ok("指数拟合 b 误差<5%", abs(fe["params"]["b"] - 0.5) / 0.5 < 0.05, fe["params"]["b"])
ok("指数拟合 r2>0.999", fe["r2"] > 0.999, fe["r2"])
ok("指数拟合 converged", fe["converged"] is True, fe["converged"])

fl = curves.fit(xs_e, [1.5 * x - 2.0 for x in xs_e], "linear")
ok("线性拟合 a=1.5", near(fl["params"]["a"], 1.5, 1e-6), fl["params"]["a"])
ok("线性拟合 b=-2", near(fl["params"]["b"], -2.0, 1e-6), fl["params"]["b"])
ok("线性拟合 r2=1", near(fl["r2"], 1.0, 1e-9), fl["r2"])

xs_l = [i * 0.2 for i in range(51)]  # 0..10
ys_l = [10.0 / (1.0 + math.exp(-2.0 * (x - 2.5))) for x in xs_l]
fg = curves.fit(xs_l, ys_l, "logistic")
ok("逻辑斯蒂拟合 r2>0.99", fg["r2"] > 0.99, fg["r2"])
ok("逻辑斯蒂 L 误差<5%", abs(fg["params"]["L"] - 10.0) / 10.0 < 0.05,
   fg["params"]["L"])
ok("逻辑斯蒂 k 误差<10%", abs(fg["params"]["k"] - 2.0) / 2.0 < 0.1,
   fg["params"]["k"])
ok("逻辑斯蒂 x0 误差<0.2", abs(fg["params"]["x0"] - 2.5) < 0.2, fg["params"]["x0"])

xs_w = [i * 0.1 + 0.1 for i in range(50)]
fw = curves.fit(xs_w, [2.0 * (x ** 1.5) for x in xs_w], "power")
ok("幂律拟合 a≈2", near(fw["params"]["a"], 2.0, 1e-3), fw["params"]["a"])
ok("幂律拟合 b≈1.5", near(fw["params"]["b"], 1.5, 1e-3), fw["params"]["b"])

xs_g = [i * 0.2 for i in range(51)]
fg2 = curves.fit(xs_g, [2.0 * math.exp(-((x - 2.5) ** 2) / (2 * 0.6 ** 2))
                        for x in xs_g], "gaussian")
ok("高斯拟合 a≈2", near(fg2["params"]["a"], 2.0, 1e-3), fg2["params"]["a"])
ok("高斯拟合 mu≈2.5", near(fg2["params"]["mu"], 2.5, 1e-3), fg2["params"]["mu"])
ok("高斯拟合 sigma≈0.6", near(fg2["params"]["sigma"], 0.6, 1e-3),
   fg2["params"]["sigma"])

pr = curves.predict("linear", {"a": 2, "b": 1}, [0, 1, 2, 3])
ok("predict 线性数值", pr["ys"] == [1.0, 3.0, 5.0, 7.0], pr["ys"])
ok("predict 缺参数抛错", raises(curves.predict, "linear", {"a": 1}, [0, 1]))
ok("fit 未知模型抛错", raises(curves.fit, [1, 2, 3], [1, 2, 3], "fourier"))

ex = curves.extrapolate([0, 1, 2, 3], [1, 3, 5, 7], "linear", [10, 20])
ok("extrapolate outside_range=True", ex["outside_range"] is True, ex["outside_range"])
ok("extrapolate 外推数值", ex["ys"] == [21.0, 41.0], ex["ys"])
ex2 = curves.extrapolate([0, 1, 2, 3], [1, 3, 5, 7], "linear", [0.5, 1.5])
ok("extrapolate outside_range=False", ex2["outside_range"] is False,
   ex2["outside_range"])

# ===========================================================================
# 八、molecules：SMILES 解析
# ===========================================================================
bz = molecules.parse("c1ccccc1")
ok("苯 分子式 C6H6", bz["formula"] == "C6H6", bz["formula"])
ok("苯 分子量≈78.11", near(bz["mw"], 78.11, 0.02), bz["mw"])
ok("苯 芳香原子=6", bz["aromatic_atoms"] == 6, bz["aromatic_atoms"])
ok("苯 环数=1", bz["rings"] == 1, bz["rings"])
ok("苯 heavy_atoms=6", bz["heavy_atoms"] == 6, bz["heavy_atoms"])
ok("苯 valid=True", bz["valid"] is True, bz["valid"])

caf = molecules.parse("Cn1cnc2c1c(=O)n(C)c(=O)n2C")
ok("咖啡因 分子式 C8H10N4O2", caf["formula"] == "C8H10N4O2", caf["formula"])
ok("咖啡因 分子量≈194.19", near(caf["mw"], 194.19, 0.02), caf["mw"])
ok("咖啡因 环数=2", caf["rings"] == 2, caf["rings"])

ac = molecules.parse("CC(=O)O")
ok("乙酸 分子式 C2H4O2", ac["formula"] == "C2H4O2", ac["formula"])
ok("乙酸 分子量≈60.05", near(ac["mw"], 60.05, 0.02), ac["mw"])
ok("乙酸 HBD=1", ac["hbd"] == 1, ac["hbd"])
ok("乙酸 HBA=2", ac["hba"] == 2, ac["hba"])

et = molecules.parse("CCO")
ok("乙醇 分子式 C2H6O", et["formula"] == "C2H6O", et["formula"])
ok("乙醇 分子量≈46.07", near(et["mw"], 46.07, 0.02), et["mw"])
ok("乙醇 环数=0", et["rings"] == 0, et["rings"])
ok("乙醇 HBD=1 HBA=1", (et["hbd"], et["hba"]) == (1, 1), (et["hbd"], et["hba"]))

ok("formula_of 苯", molecules.formula_of("c1ccccc1")["formula"] == "C6H6")
ok("分子式输入 C6H6 也能算 MW",
   near(molecules.parse("C6H6")["mw"], 78.11, 0.02),
   molecules.parse("C6H6")["mw"])
ok("环己烷 环数=1 且无可旋转键",
   molecules.parse("C1CCCCC1")["rings"] == 1
   and molecules.parse("C1CCCCC1")["rotatable_bonds"] == 0,
   molecules.parse("C1CCCCC1")["rotatable_bonds"])
ok("丁烷 可旋转键=1", molecules.parse("CCCC")["rotatable_bonds"] == 1,
   molecules.parse("CCCC")["rotatable_bonds"])
ok("空 SMILES 抛错", raises(molecules.parse, "  "))

desc = molecules.descriptors("CCO")
ok("descriptors 含 lipinski 字段",
   "lipinski_pass" in desc and "lipinski_violations" in desc)
ok("乙醇通过类药五规则", desc["lipinski_pass"] is True, desc["lipinski_violations"])

bt = molecules.batch(["CCO", "c1ccccc1", "C1CC"])
ok("batch items 数量", len(bt["items"]) == 3, len(bt["items"]))
ok("batch valid_count=2", bt["valid_count"] == 2, bt["valid_count"])
ok("batch invalid 记录坏结构", len(bt["invalid"]) == 1, bt["invalid"])

# ===========================================================================
# 九、design：实验设计 / 功效 / 仿真 / 寻优
# ===========================================================================
fd = design.factorial_design(["A", "B", "C"])
ok("2^3 全因子 8 次", fd["n_runs"] == 8, fd["n_runs"])
ok("2 水平编码 -1/+1", fd["levels"] == [-1.0, 1.0], fd["levels"])
ok("首个 run 全 -1", fd["runs"][0]["A"] == -1.0 and fd["runs"][0]["C"] == -1.0,
   fd["runs"][0])
ok("末个 run 全 +1", fd["runs"][-1]["A"] == 1.0 and fd["runs"][-1]["C"] == 1.0,
   fd["runs"][-1])
ok("2^4 全因子 16 次",
   design.factorial_design(["A", "B", "C", "D"])["n_runs"] == 16)
ok("3 水平 2 因子 = 9 次",
   design.factorial_design(["A", "B"], levels=3)["n_runs"] == 9)
ok("3 水平 5 因子报错",
   raises(design.factorial_design, ["A", "B", "C", "D", "E"], 3))
ok("因子名重复报错", raises(design.factorial_design, ["A", "A"], 2))

l1 = design.lhs_design(3, 10, 42)
l2 = design.lhs_design(3, 10, 42)
l3 = design.lhs_design(3, 10, 7)
ok("LHS 形状", l1["n_factors"] == 3 and l1["n_runs"] == 10
   and len(l1["matrix"]) == 10 and len(l1["matrix"][0]) == 3, l1["n_runs"])
ok("LHS 同 seed 完全复现", l1["matrix"] == l2["matrix"], "seed=42")
ok("LHS 不同 seed 结果不同", l1["matrix"] != l3["matrix"], "seed=7")
ok("LHS 取值范围 0~1",
   all(0.0 <= v <= 1.0 for row in l1["matrix"] for v in row), "range")
ok("LHS 每列分层各命中一次",
   all(len({int(row[j] * 10) for row in l1["matrix"]}) == 10 for j in range(3)),
   [sorted(int(row[j] * 10) for row in l1["matrix"]) for j in range(3)])

pw = design.power_analysis(0.5, 0.05, 0.8)
ok("功效分析 n_per_group=64", pw["n_per_group"] == 64, pw["n_per_group"])
ok("功效分析 n_total=2n", pw["n_total"] == 2 * pw["n_per_group"],
   (pw["n_total"], pw["n_per_group"]))
ok("功效分析 df=2n-2", pw["df"] == 2 * pw["n_per_group"] - 2, pw["df"])
small = design.power_analysis(0.2, 0.05, 0.8)
big = design.power_analysis(0.8, 0.05, 0.8)
ok("功效分析：效应量越小样本量越大",
   small["n_per_group"] > pw["n_per_group"] > big["n_per_group"],
   (small["n_per_group"], pw["n_per_group"], big["n_per_group"]))
ok("功效分析：d=0.2 约 394/组", near(small["n_per_group"], 394, 1), small["n_per_group"])

sw = design.sweep("gaussian", {"a": 2, "mu": 5, "sigma": 1}, 0, 10, 21)
ok("sweep 找到 argmax x=5", near(sw["argmax"]["x"], 5.0), sw["argmax"])
ok("sweep argmax y≈2", near(sw["argmax"]["y"], 2.0, 1e-6), sw["argmax"]["y"])
ok("sweep 高斯是非单调", sw["monotonic"] == "non-monotonic", sw["monotonic"])
ok("sweep 点数=steps", len(sw["xs"]) == 21 and len(sw["ys"]) == 21, len(sw["xs"]))
sw2 = design.sweep("linear", {"a": 2, "b": 1}, 0, 10, 11)
ok("sweep 线性判为递增", sw2["monotonic"] == "increasing", sw2["monotonic"])
ok("sweep 线性 argmin 在起点", near(sw2["argmin"]["x"], 0.0), sw2["argmin"])

xs_gs = [0, 1, 2, 3]
ys_gs = [3.0 * math.exp(0.5 * x) for x in xs_gs]
gs = design.grid_search("exponential", {"a": [1, 2, 3, 4], "b": [0.3, 0.5, 0.7]},
                        xs_gs, ys_gs)
ok("grid_search 命中真参数",
   near(gs["best"]["params"]["a"], 3.0) and near(gs["best"]["params"]["b"], 0.5),
   gs["best"]["params"])
ok("grid_search 最优 r2=1", near(gs["best"]["r2"], 1.0, 1e-9), gs["best"]["r2"])
ok("grid_search 穷举 12 组", gs["n_evaluated"] == 12, gs["n_evaluated"])
ok("grid_search ranking 最多 5 条", len(gs["ranking"]) <= 5, len(gs["ranking"]))
ok("grid_search 缺参数报错",
   raises(design.grid_search, "exponential", {"a": [1]}, [0, 1], [1, 2]))

# ===========================================================================
# 十、注册表：MCP 形状 / 调用约定 / 错误兜底
# ===========================================================================
funcs = science.names()
ok("注册工具数 >= 21", len(funcs) >= 21, len(funcs))
REQUIRED = ["table_parse", "stats_describe", "stats_ttest", "stats_mannwhitney",
            "stats_anova", "stats_chi_square", "stats_correlation",
            "stats_regression", "spectra_smooth", "spectra_peaks", "spectra_snr",
            "spectra_summary", "curves_fit", "curves_extrapolate",
            "molecule_parse", "molecule_batch", "design_factorial",
            "design_lhs", "design_power", "design_sweep", "design_grid_search"]
missing = [n for n in REQUIRED if n not in funcs]
ok("契约要求的 21 个工具全部注册", not missing, missing)

specs = science.list_tools()
ok("list_tools 数量一致", len(specs) == len(funcs), len(specs))
ok("list_tools 键名对齐 MCP",
   all(set(s.keys()) == {"name", "title", "description", "inputSchema", "tags"}
       for s in specs))
ok("每个工具都有中文 description",
   all(len(s["description"]) > 10 for s in specs))
ok("每个工具都有 inputSchema(object)",
   all(s["inputSchema"].get("type") == "object" for s in specs))
ok("每个工具都有 properties",
   all(isinstance(s["inputSchema"].get("properties"), dict) for s in specs))
ok("每个工具都有 required 列表",
   all(isinstance(s["inputSchema"].get("required"), list) for s in specs))
ok("每个工具都有 tags", all(isinstance(s["tags"], list) and s["tags"] for s in specs))
ok("tool_spec 已知工具", science.tool_spec("stats_ttest")["name"] == "stats_ttest")
ok("tool_spec 未知工具返回 None", science.tool_spec("不存在") is None)

desc_txt = science.describe()
ok("describe 含全部工具名", all(n in desc_txt for n in funcs), "describe")
ok("describe 末尾给出调用方式", "inputSchema" in desc_txt.splitlines()[-1],
   desc_txt.splitlines()[-1])

r_ok = science.call_tool("stats_describe", {"values": [1, 2, 3, 4]})
ok("call_tool 正常返回 ok=True", r_ok["ok"] is True and r_ok["error"] is None,
   r_ok.get("error"))
ok("call_tool result 正确", near(r_ok["result"]["mean"], 2.5), r_ok["result"]["mean"])
ok("call_tool 返回 tool 名", r_ok["tool"] == "stats_describe", r_ok["tool"])
ok("call_tool repro 四字段",
   set(r_ok["repro"].keys()) == {"tool", "args", "python", "case"}, r_ok["repro"])
ok("call_tool repro.case 是模块路径",
   r_ok["repro"]["case"] == "science.stats.describe", r_ok["repro"]["case"])
ok("call_tool elapsed_ms >= 0", r_ok["elapsed_ms"] >= 0.0, r_ok["elapsed_ms"])

r_bad = science.call_tool("不存在的工具", {})
ok("未知工具 ok=False", r_bad["ok"] is False, r_bad["ok"])
ok("未知工具 error 含『未知工具』", "未知工具" in (r_bad["error"] or ""), r_bad["error"])
ok("未知工具 result=None", r_bad["result"] is None, r_bad["result"])
ok("未知工具 elapsed_ms=0", r_bad["elapsed_ms"] == 0.0, r_bad["elapsed_ms"])

r_inv = science.call_tool("stats_ttest", {"x": "abc", "y": [1, 2]})
ok("非法参数 ok=False 不抛异常", r_inv["ok"] is False, r_inv["ok"])
ok("非法参数 error 非空", bool(r_inv["error"]), r_inv["error"])
ok("缺参数 ok=False", science.call_tool("stats_ttest", {})["ok"] is False)
ok("空 SMILES 走 call_tool ok=False",
   science.call_tool("molecule_parse", {"smiles": ""})["ok"] is False)
ok("args=None 不抛异常",
   science.call_tool("stats_describe")["ok"] is False)

# 宽松归一化：字符串数字 / 逗号串都能被接住
r_str = science.call_tool("stats_ttest", {"x": "5.1,4.9,5.0,5.2,5.3",
                                          "y": "4.9,4.8,5.1,4.7"})
ok("逗号串参数被接受", r_str["ok"] is True, r_str.get("error"))
ok("逗号串结果与 list 一致",
   near(r_str["result"]["t"], 2.029444, 1e-4), r_str["result"]["t"])

r_tbl = science.call_tool("stats_describe", {"table": csv_text, "column": "value"})
ok("stats_describe 支持 table+column",
   r_tbl["ok"] is True and near(r_tbl["result"]["mean"], 2.5), r_tbl.get("error"))

r_peak = science.call_tool("spectra_peaks", {"y": ys_p})
ok("spectra_peaks 可省略 x", r_peak["ok"] is True and r_peak["result"]["count"] == 2,
   r_peak.get("error"))

# 所有工具的关键路径返回都必须是严格 JSON
for _name in funcs:
    _spec = science.TOOLS[_name]
    ok("Schema 合规：%s" % _name,
       isinstance(_spec.schema, dict) and _spec.schema.get("type") == "object")

dumped = json.dumps([science.tool_spec(n) for n in funcs],
                    allow_nan=False, ensure_ascii=False)
ok("工具清单严格 JSON 可序列化", len(dumped) > 100, len(dumped))

# ===========================================================================
# 汇总
# ===========================================================================
n_pass = sum(1 for _, good, _ in R if good)
for name, good, got in R:
    if not good:
        print("FAIL  %s  -> got: %r" % (name, got))
print("\n%d/%d checks passed" % (n_pass, len(R)))
sys.exit(0 if n_pass == len(R) else 1)
