"""新接入的三个源：字段归一化的离线测试（不联网、不慢、不 flaky）。

为什么要专门测这些：多源最容易被"某个源某条记录少个字段/类型不一样"打崩，
而且崩的是整轮检索，不是那一条。开发中真实踩到过一次 ——
Europe PMC 的 pubTypeList.pubType 在某些记录里是 [{"code": ...}]，
直接进 fieldsOfStudy 后 `" ".join(...)` 抛
`TypeError: expected str instance, dict found`，整次检索 500。

所以这里喂的是**真实抓下来的报文形状**（含各种缺失/异形），
验证 normalize 之后一定是干净的统一结构。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-dummy")

from providers import crossref, doaj, europepmc  # noqa: E402
from providers.base import PAPER_KEYS, _str_list, clean_authors, dedup_key  # noqa: E402

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


# ══════════════════════════════════════════════════════════════
#  Crossref
# ══════════════════════════════════════════════════════════════

CROSSREF_ITEM = {
    "DOI": "10.3390/rs13010119",
    "title": ["Combining Deep Semantic Segmentation Network and Graph Convolutional"],
    "author": [
        {"given": "Song", "family": "Ouyang"},
        {"given": "Yansheng", "family": "Li"},
        # 中文作者：Crossref 常给 family=张 given=三
        {"given": "三", "family": "张"},
        # 只有 name 的情况（团体作者/中文）
        {"name": "中国科学院遥感所"},
    ],
    "issued": {"date-parts": [[2020, 12, 29]]},
    "container-title": ["Remote Sensing"],
    "abstract": "<jats:title>Abstract</jats:title><jats:p>We propose a &amp; novel method.</jats:p>",
    "type": "journal-article",
    "volume": "13",
    "issue": "1",
    "page": "119",
    "publisher": "MDPI AG",
    "URL": "https://doi.org/10.3390/rs13010119",
    "subject": ["General Earth and Planetary Sciences"],
    "is-referenced-by-count": 73,
    "link": [{"content-type": "application/pdf", "URL": "https://example.org/a.pdf"}],
}

p = crossref.normalize_crossref_paper(CROSSREF_ITEM)
ok("Crossref 字段齐全", all(k in p for k in PAPER_KEYS), [k for k in PAPER_KEYS if k not in p])
ok("Crossref 摘要剥掉 JATS 标签", "<jats:" not in p["abstract"], p["abstract"][:60])
ok("Crossref 摘要解开 &amp;", "&amp;" not in p["abstract"], p["abstract"][:60])
ok("Crossref 摘要去掉了 Abstract 小标题",
   not p["abstract"].startswith("Abstract"), p["abstract"][:40])
ok("Crossref 卷期页完整", (p["volume"], p["issue"], p["pages"]) == ("13", "1", "119"),
   (p["volume"], p["issue"], p["pages"]))
ok("Crossref 年份取自 issued", p["year"] == 2020, p["year"])
ok("Crossref 中文作者还原成姓名在前", "张三" in p["authors"], p["authors"])
ok("Crossref 英文作者是 名 姓", "Song Ouyang" in p["authors"], p["authors"])
ok("Crossref PDF 链接被识别", p["openAccessPdf"] == "https://example.org/a.pdf", p["openAccessPdf"])
ok("Crossref id 带前缀", p["id"] == "crossref:10.3390/rs13010119", p["id"])
ok("Crossref 不谎报被引为 0", p["citationCount"] == 73, p["citationCount"])

# 异形/缺失字段不能崩
for label, item in [
    ("空对象", {}),
    ("title 是字符串", {"title": "Plain string title", "DOI": "10.1/x"}),
    ("author 里有 None", {"author": [None, {"given": "A", "family": "B"}]}),
    ("date-parts 为空", {"issued": {"date-parts": [[]]}, "DOI": "10.1/y"}),
    ("subject 是字符串", {"subject": "single subject", "DOI": "10.1/z"}),
    ("link 缺 content-type", {"link": [{"URL": "https://x/y"}], "DOI": "10.1/w"}),
    ("page 缺失", {"DOI": "10.1/v", "volume": "1"}),
]:
    try:
        q = crossref.normalize_crossref_paper(item)
        good = all(k in q for k in PAPER_KEYS) and isinstance(q["fieldsOfStudy"], list)
        ok(f"Crossref 异形输入不崩: {label}", good, q.get("title"))
    except Exception as e:
        ok(f"Crossref 异形输入不崩: {label}", False, f"{type(e).__name__}: {e}")

# ══════════════════════════════════════════════════════════════
#  DOAJ
# ══════════════════════════════════════════════════════════════

DOAJ_ITEM = {
    "id": "abc123",
    "bibjson": {
        "title": "Long-Tailed Graph Representation Learning",
        "author": [{"name": "Yijun Duan"}, {"name": "Xin Liu"}],
        "year": "2022",
        "abstract": "An abstract.",
        "identifier": [{"type": "doi", "id": "10.3390/rs14143295"},
                       {"type": "issn", "id": "2072-4292"}],
        "journal": {"title": "Remote Sensing", "volume": "14", "number": "14"},
        "link": [{"type": "fulltext", "url": "https://www.mdpi.com/2072-4292/14/14/3295/pdf"},
                 {"type": "homepage", "url": "https://www.mdpi.com/journal/remotesensing"}],
        "start_page": "3295",
        "end_page": "3295",
        "subject": [{"term": "Geodesy"}],
        "language": ["EN"],
    },
}

d = doaj.normalize_doaj_paper(DOAJ_ITEM)
ok("DOAJ 字段齐全", all(k in d for k in PAPER_KEYS), [k for k in PAPER_KEYS if k not in d])
ok("DOAJ 取到 DOI", d["doi"] == "10.3390/rs14143295", d["doi"])
ok("DOAJ 卷期取自 journal", (d["volume"], d["issue"]) == ("14", "14"),
   (d["volume"], d["issue"]))
ok("DOAJ 单页不写成 3295-3295", d["pages"] == "3295", d["pages"])
ok("DOAJ 视为开放获取", d["isOa"] is True, d["isOa"])
ok("DOAJ 不谎报被引", d["citationCount"] == 0, d["citationCount"])
ok("DOAJ 命中的是 OA 源（有 fulltext 链接）", bool(d["url"]), d["url"])
ok("DOAJ id 带前缀", d["id"].startswith("doaj:"), d["id"])

# subject 是 list[dict] 也不能崩
ok("DOAJ subject 是 [{term}] 不崩", isinstance(d["fieldsOfStudy"], list), d["fieldsOfStudy"])
ok("DOAJ subject 提取出可读文本",
   all(isinstance(x, str) for x in d["fieldsOfStudy"]), d["fieldsOfStudy"])

for label, item in [("空 bibjson", {}), ("bibjson 为空", {"bibjson": {}}),
                    ("author 有 dict 缺 name", {"bibjson": {"author": [{}]}}),
                    ("link 为空", {"bibjson": {"link": []}}),
                    ("language 为空", {"bibjson": {"language": []}})]:
    try:
        q = doaj.normalize_doaj_paper(item)
        ok(f"DOAJ 异形输入不崩: {label}", all(k in q for k in PAPER_KEYS), q.get("title"))
    except Exception as e:
        ok(f"DOAJ 异形输入不崩: {label}", False, f"{type(e).__name__}: {e}")

# ══════════════════════════════════════════════════════════════
#  Europe PMC
# ══════════════════════════════════════════════════════════════

EPMC_REC = {
    "id": "3690544", "source": "MED", "pmid": "40000001",
    "doi": "10.1109/tpami.2026.3690544",
    "title": "Fractal-Domain Vision Graph Neural Network.",
    "authorString": "Yin J, Zhen T.",
    "authorList": {"author": [{"fullName": "Yin J", "lastName": "Yin", "initials": "J"},
                              {"lastName": "Zhen", "initials": "T"}]},
    "journalInfo": {"volume": "48", "issue": "9", "pageInfo": "10585-10597",
                    "journal": {"title": "IEEE Trans Pattern Anal Mach Intell"}},
    "pubYear": "2026",
    "abstractText": "A real abstract.",
    "citedByCount": 12,
    "isOpenAccess": "Y",
    "pubTypeList": {"pubType": ["Journal Article"]},
    "fullTextUrlList": {"fullTextUrl": [
        {"availabilityCode": "S", "documentStyle": "doi", "url": "https://doi.org/10.1/x"},
        {"availabilityCode": "OA", "documentStyle": "pdf", "url": "https://x/y.pdf"},
    ]},
    "language": "eng",
}

e = europepmc.normalize_epmc_paper(EPMC_REC)
ok("EPMC 字段齐全", all(k in e for k in PAPER_KEYS), [k for k in PAPER_KEYS if k not in e])
ok("EPMC 取到被引次数（比 PubMed 强的地方）", e["citationCount"] == 12, e["citationCount"])
ok("EPMC 卷期页完整", (e["volume"], e["issue"], e["pages"]) == ("48", "9", "10585-10597"),
   (e["volume"], e["issue"], e["pages"]))
ok("EPMC 只认 OA/F 的全文链接（排除 Subscription required）",
   e["openAccessPdf"] == "https://x/y.pdf", e["openAccessPdf"])
ok("EPMC 标题去掉句尾句点", not e["title"].endswith("."), e["title"])
ok("EPMC 作者来自 authorList", e["authors"][:2] == ["Yin J", "Zhen T"], e["authors"])
ok("EPMC id 带前缀", e["id"].startswith("europepmc:"), e["id"])

# 关键回归：pubType 是 list[dict] 时，fieldsOfStudy 必须是纯字符串
WEIRD = dict(EPMC_REC)
WEIRD["pubTypeList"] = {"pubType": [{"code": "IM", "name": "Index Medicus"},
                                    "Journal Article"]}
w = europepmc.normalize_epmc_paper(WEIRD)
ok("EPMC pubType 混入 dict 时仍是纯字符串列表",
   all(isinstance(x, str) for x in w["fieldsOfStudy"]), w["fieldsOfStudy"])
ok("EPMC dict pubType 提取出可读文本", "Index Medicus" in w["fieldsOfStudy"],
   w["fieldsOfStudy"])


def _join_like_matcher(paper):
    """复现打崩过的那一行：相关度打分里会对 fieldsOfStudy 做 join。"""
    return " ".join(paper.get("fieldsOfStudy") or []).lower()


try:
    _join_like_matcher(w)
    ok("回归：字段可被 join（曾经在这里 TypeError）", True)
except TypeError as exc:
    ok("回归：字段可被 join（曾经在这里 TypeError）", False, str(exc))

for label, rec in [
    ("空记录", {}),
    ("无 authorList 用 authorString", {"authorString": "A B, C D."}),
    ("authorString 为空", {"authorString": ""}),
    ("journalInfo 缺失", {"title": "T"}),
    ("pubYear 缺失", {"title": "T", "pmid": "1"}),
    ("fullTextUrlList 空", {"fullTextUrlList": {"fullTextUrl": []}}),
    ("pubTypeList 为 None", {"pubTypeList": None}),
]:
    try:
        q = europepmc.normalize_epmc_paper(rec)
        ok(f"EPMC 异形输入不崩: {label}", all(k in q for k in PAPER_KEYS), q.get("title"))
    except Exception as e:
        ok(f"EPMC 异形输入不崩: {label}", False, f"{type(e).__name__}: {e}")

# ══════════════════════════════════════════════════════════════
#  跨源去重与工具函数
# ══════════════════════════════════════════════════════════════

ok("同 DOI 跨源判为同一篇",
   dedup_key({"doi": "10.1/X", "title": "a"}) == dedup_key({"doi": "10.1/x", "title": "b"}))
ok("DOI 带 URL 前缀也归一",
   dedup_key({"doi": "https://doi.org/10.1/x"}) == dedup_key({"doi": "10.1/x"}))
ok("无 DOI 时按标题去噪（大小写/标点/空格）",
   dedup_key({"title": "A Study: of Things!"}) == dedup_key({"title": "a study of things"}))

ok("_str_list 过滤 None/数字", _str_list([None, 1, "ok", ""]) == ["ok"], _str_list([None, 1, "ok"]))
ok("_str_list 从 dict 取名字", _str_list([{"name": "N"}]) == ["N"], _str_list([{"name": "N"}]))
ok("_str_list 去重保序", _str_list(["a", "b", "a"]) == ["a", "b"], _str_list(["a", "b", "a"]))
ok("clean_authors 丢弃非字符串", clean_authors([None, 5, "张三"]) == ["张三"],
   clean_authors([None, 5, "张三"]))

# ══════════════════════════════════════════════════════════════
#  相关度：中文长词的部分命中必须命中词首
# ══════════════════════════════════════════════════════════════
# 接上 Crossref 后暴露的问题：只按 2-gram 覆盖率给部分命中，
# 查「图神经网络」时"神经网络"就够 75%，于是这些跑题论文拿到 0.5 分混进结果：
#   《基于U-Net 神经网络的禽翅图像分割方法》
#   《基于细胞神经网络的图像分割及其在医学图像中的应用》
#   《一种基于脉冲耦合神经网络的植物胚性细胞图像分割》
# 修法是要求词首二字（图神）也命中。

from providers import _query_words, _word_hit, score_relevance  # noqa: E402

Q = "图神经网络 遥感图像 语义分割"
qw = _query_words(Q)


def word_score(title, abstract=""):
    t, b = title.lower(), abstract.lower()
    return round(sum(_word_hit(w, t, b) for w in qw), 2)


ok("整词命中标题得满分", word_score("多层次分支跨尺度融合的遥感图像语义分割网络") == 2.0,
   word_score("多层次分支跨尺度融合的遥感图像语义分割网络"))

# 回归：这三篇必须降到 0（此前是 0.5，会混进结果）
for off_topic in ("基于U-Net 神经网络的禽翅图像分割方法",
                  "基于细胞神经网络的图像分割及其在医学图像中的应用",
                  "一种基于脉冲耦合神经网络的植物胚性细胞图像分割"):
    ok(f"跑题论文词分为 0: {off_topic[:16]}…", word_score(off_topic) == 0.0,
       word_score(off_topic))

# 不能误伤：真正提到该词的仍然给分
# 注意「图卷积神经网络」里没有「图神」这个连续二字，所以不算"图神经网络"的部分命中 ——
# 这是刻意的：它们是不同的模型族，不能互相顶替。
ok("图卷积神经网络 ≠ 图神经网络（不顶替）",
   _word_hit("图神经网络", "基于图卷积神经网络的遥感分割", "") == 0.0,
   _word_hit("图神经网络", "基于图卷积神经网络的遥感分割", ""))
ok("标题里出现词首二字给部分分（遥感图像处理 ⊃ 遥感图像）",
   _word_hit("遥感图像", "遥感图像处理与识别", "") == 1.0,
   _word_hit("遥感图像", "遥感图像处理与识别", ""))
ok("近义替换不算标题命中（遥感影像 ≠ 遥感图像）",
   _word_hit("遥感图像", "高分遥感影像分类方法", "") == 0.0,
   _word_hit("遥感图像", "高分遥感影像分类方法", ""))
ok("近义替换只在摘要命中时给 0.2",
   _word_hit("遥感图像", "高分遥感影像分类方法", "面向遥感图像的分割") == 0.2,
   _word_hit("遥感图像", "高分遥感影像分类方法", "面向遥感图像的分割"))
ok("只命中词尾不给分（神经网络 ≠ 图神经网络）",
   _word_hit("图神经网络", "基于神经网络的图像分类", "") == 0.0,
   _word_hit("图神经网络", "基于神经网络的图像分类", ""))
ok("摘要里命中给 0.2", _word_hit("遥感图像", "", "本文研究遥感图像分割") == 0.2,
   _word_hit("遥感图像", "", "本文研究遥感图像分割"))
ok("标题命中优先于摘要（1.0 而非 0.2）",
   _word_hit("遥感图像", "遥感图像分割", "遥感图像分割") == 1.0,
   _word_hit("遥感图像", "遥感图像分割", "遥感图像分割"))
ok("英文词不受中文规则影响", _word_hit("graph", "a graph neural network", "") == 1.0,
   _word_hit("graph", "a graph neural network", ""))
ok("英文词只在摘要也给 0.2", _word_hit("graph", "unrelated title", "a graph model") == 0.2,
   _word_hit("graph", "unrelated title", "a graph model"))

# 端到端：那三篇跑题论文不能再进入保留集
TERMS = None
from providers import _apply_relevance  # noqa: E402

mix = [{"title": t, "abstract": "", "fieldsOfStudy": []} for t in (
    "多层次分支跨尺度融合的遥感图像语义分割网络",
    "基于自适应多尺度与轮廓梯度的遥感图像分割网络",
    "基于U-Net 神经网络的禽翅图像分割方法",
    "基于细胞神经网络的图像分割及其在医学图像中的应用",
    "一种基于脉冲耦合神经网络的植物胚性细胞图像分割",
)]
kept, dropped, _th = _apply_relevance(mix, Q, 0)
kept_titles = {p["title"] for p in kept}
ok("端到端：3 篇跑题论文被剔除", dropped == 3, dropped)
ok("端到端：对题的 2 篇保留",
   "多层次分支跨尺度融合的遥感图像语义分割网络" in kept_titles
   and "基于自适应多尺度与轮廓梯度的遥感图像分割网络" in kept_titles, kept_titles)
ok("端到端：不会把结果清空", len(kept) > 0, len(kept))

# 每个源都必须能被聚合层认出来（前缀 = provider.name）
from providers import SOURCES  # noqa: E402
for name in ("crossref", "europepmc", "doaj"):
    ok(f"{name} 已注册进 SOURCES", name in SOURCES, sorted(SOURCES))
    ok(f"{name} 的 id 前缀可被 split_id 解析",
       __import__("providers.base", fromlist=["split_id"]).split_id(f"{name}:X")[0] == name)

n_pass = sum(1 for _, c, _ in R if c)
for name, cond, got in R:
    if not cond:
        print(f"FAIL  {name}  -> got: {got!r}")
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
