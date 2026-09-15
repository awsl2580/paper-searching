"""多源文献检索 —— provider 公共约定。

统一论文结构（所有 provider 的 search()/detail() 都必须返回这个形状的 dict）：

    id              带来源前缀的全局唯一 id，形如 "s2:abc123"、"openalex:W123"。
                    收藏、查详情、跨源去重都以它为准；旧数据里不带前缀的裸 id
                    也仍然能被解析（见 split_id）。
    paperId         provider 的原始 id（保留，便于回查原始接口或做引用导出）
    source          来源标识：semantic_scholar / openalex
    sourceLabel     给人看的来源名
    language        "zh" / "en" / ""（未知）
    title           标题
    authors         [str]
    year            年份（int 或 "未知"）
    abstract        摘要
    tldr            一句话摘要（来源没有则为 ""）
    url             落地页
    venue           发表期刊/会议
    journal         期刊名
    doi             DOI（用于跨源去重）
    citationCount   被引次数
    referenceCount  参考文献数（来源没有则为 None）
    fieldsOfStudy   [str] 学科/主题
    openAccessPdf   全文 PDF 直链（有就是"能直接读到完整论文"）
    isOa            是否开放获取（决定前端要不要提示"只有摘要页"）
    oaStatus        开放获取类型：gold/green/diamond/bronze/closed（来源没给则空）
    volume          卷（GB/T 7714 引用格式要用）
    issue           期
    pages           起止页码，如 "7-13"；来源没给则为 ""
    docType         文献类型（journal/conference/thesis/book/preprint），
                    决定引用里的 [J]/[C]/[D]/[M]/[EB/OL] 标识

为什么要有这一层：数据源会变（Semantic Scholar 公共配额时好时坏、中文权威源
如知网/万方都需要机构授权），而工具层和前端只应该认识上面这一种结构。
换源 = 新增一个 provider 文件，不动 app.py / Agent.py / index.html。
"""

from __future__ import annotations

import re

# 统一结构里所有键，provider 返回前可以少给，但聚合层会补齐
PAPER_KEYS = (
    "id", "paperId", "source", "sourceLabel", "language",
    "title", "authors", "year", "abstract", "tldr", "url",
    "venue", "journal", "doi", "citationCount", "referenceCount",
    "fieldsOfStudy", "openAccessPdf", "isOa", "oaStatus",
    "volume", "issue", "pages", "docType",
)


# ── 作者清洗：两个实测踩到的坑 ─────────────────────────────
#
# 1. OpenAlex 会把作者单位当成作者塞进 authorships。
#    实测：一篇论文的作者列表是
#    ["湖南工商大学财政金融学院", "文婷 唐", "绪螺 殷", "湖南工商大学财政金融学院"]
#    —— 机构名直接出现在作者位，会污染展示和参考文献格式。
# 2. 中文姓名被存成"名 姓"且带空格（"文婷 唐"、"力根 李"），
#    而 GB/T 7714 要求姓前名后（"唐文婷"、"李力根"）。
#    但顺序并非总是一致（也见过 "李 勇" 这种姓在前的），
#    所以用常见姓氏表来判断，判断不了就保持原样。

# 常见中文姓氏，**按常见度从高到低排列**：位置越靠前越可能是姓。
#
# 为什么需要顺序而不是一个集合：像 "文婷 唐" 这种两段首字都可能是姓氏
# （文、唐都是姓）时，只有比较常见度才能选对——唐远比文常见，所以姓是唐。
# 一开始我用的是"在不在集合里"的二元判断，"文"一进表就把这条判错了。
_SURNAME_ORDER = (
    "王李张刘陈杨黄赵周吴徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
    "温康施牛樊葛邢安齐易乔伍庞颜倪庄聂章鲁岳翟殷詹申欧耿关兰焦俞左柳甘祝包宁尚符舒阮柯纪梅童凌毕单季裴霍涂成苗谷盛曲翁冉骆蓝路游辛靳管柴蒙鲍华喻祁蒲房滕屈饶解牟艾尤阳时穆农司卓文"
)
_CJK_SURNAME_RANK = {}
for _i, _ch in enumerate(_SURNAME_ORDER):
    _CJK_SURNAME_RANK.setdefault(_ch, _i)
# 机构名常见后缀：命中就认为是单位而不是人
_ORG_MARKERS = (
    "大学", "学院", "研究院", "研究所", "科学院", "医院", "公司", "集团",
    "中心", "实验室", "重点实验室", "附属", "系", "部", "局", "委", "协会",
    "university", "institute", "college", "hospital", "laborator",
)


def _looks_like_org(name):
    low = name.lower()
    return any(marker in low for marker in _ORG_MARKERS)


def normalize_chinese_name(name):
    """把"名 姓"形式的中文姓名还原成"姓名"；判断不了就只去掉空格。

    只在两段都是中日韩汉字时才处理，避免误伤英文名（英文名的
    姓在前/名在后的转换是另一套规则，见前端的引用格式化）。
    """
    if not name or " " not in name:
        return name

    parts = [p for p in name.split(" ") if p]
    if len(parts) != 2:
        return name.replace(" ", "")

    given, family = parts[0], parts[1]
    if not all("\u4e00" <= ch <= "\u9fff" for ch in given + family):
        return name          # 不是纯中文，交给别的规则

    # 按常见度排名判断谁是姓：排名更靠前的那个当姓
    given_rank = _CJK_SURNAME_RANK.get(given[0])
    family_rank = _CJK_SURNAME_RANK.get(family[0])

    if family_rank is not None and (given_rank is None or family_rank < given_rank):
        return family + given        # 名 姓 -> 姓名（"文婷 唐" -> "唐文婷"）
    if given_rank is not None and (family_rank is None or given_rank < family_rank):
        return given + family        # 本来就是姓在前（"李 勇" -> "李勇"）
    # 判断不了：按原顺序去掉空格，不瞎猜
    return given + family


def _split_merged_cjk_names(name):
    """拆开被数据源粘在一起/重复拼接的中文姓名。

    实测遇到过两种：
      * "赵天凤白祥" —— 其实是 赵天凤 + 白祥 两个人
      * "杨莉杨莉"   —— 同一个名字被重复拼了两遍
    中文姓名通常 2-4 字，所以只在很确定的形态下才拆，
    其余一律原样返回 —— 宁可偶尔少拆，也不要把正常姓名拆坏。
    """
    compact = name.replace(" ", "")
    if len(compact) < 4 or not all("\u4e00" <= ch <= "\u9fff" for ch in compact):
        return [name]

    # 前后两半完全相同的重复拼接（"杨莉杨莉"）
    half = len(compact) // 2
    if len(compact) % 2 == 0 and compact[:half] == compact[half:]:
        return [compact[:half]]

    # 3+2 形式的两个人名（"赵天凤白祥"）：第 4 个字得是常见姓氏
    if len(compact) >= 5 and compact[3] in _CJK_SURNAME_RANK:
        return [compact[:3], compact[3:]]
    return [name]


def clean_authors(raw_authors):
    """过滤机构、规整中文姓名、拆开粘连的多个姓名、去掉重复项，保持原顺序。"""
    out = []
    seen = set()
    for name in raw_authors or []:
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name or _looks_like_org(name):
            continue
        for single in _split_merged_cjk_names(name):
            single = normalize_chinese_name(single)
            key = single.replace(" ", "").lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(single)
    return out


class ProviderError(Exception):
    """provider 检索/取详情失败。

    kind 取值（聚合层靠它决定怎么向用户解释）：
        rate_limited  被限流（可重试，值得告诉用户"稍后再试"）
        timeout       超时
        connection    网络不通
        http_error    接口返回了明确的错误状态
        bad_json      返回内容无法解析
        unavailable   这个源没配置好（例如缺 API Key），本轮没参与检索
        exception     其它意外
    """

    def __init__(self, message, kind="exception", retryable=False, source=None, status=None):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.retryable = retryable
        self.source = source
        self.status = status


class ProviderUnavailable(ProviderError):
    """源未配置或不可用（不算错误，只是这个源这次没参与检索）。"""

    def __init__(self, message, source=None):
        super().__init__(message, kind="unavailable", retryable=False, source=source)


class Provider:
    """数据源接口。子类至少要给出 name / label，并实现 search()。"""

    name = "base"
    label = "Base"
    # 这个源擅长的语种：用于 source="cn" / "en" 时挑选参与检索的源
    languages = ()
    # 是否实现了真正的"按标题精确匹配"端点（S2 有 /paper/search/match）。
    # 没有的源只能退化成"检索第一条当命中"。
    supports_title_match = False

    def available(self):
        """当前配置下这个源能不能用（例如需要 API Key 的源没配 key）。"""
        return True

    def unavailable_reason(self):
        """不可用的原因，用于给用户提示。"""
        return ""

    def search(self, query, limit=5, language=None):
        """返回 [paper dict]，失败时抛 ProviderError。"""
        raise NotImplementedError

    def detail(self, raw_id):
        """按 provider 原始 id 取详情，返回 paper dict 或 None（找不到）。"""
        raise NotImplementedError

    def detail_by_title(self, title):
        """按标题找一篇（默认不支持，返回 None）。"""
        return None


def qualify(source, raw_id):
    """把 provider 原始 id 变成全局唯一 id。"""
    return f"{source}:{raw_id}"


def split_id(paper_id):
    """拆解 id -> (source, raw_id)。

    兼容三种历史/新格式：
        "s2:abc" / "openalex:W1"                         带前缀
        "abc123...(40位十六进制)"                        旧的裸 Semantic Scholar id
        "title_xxx"                                     旧的按标题生成的假 id
    识别不出来时返回 (None, 原样)，交给上层按标题检索。
    """
    pid = (paper_id or "").strip()
    if not pid:
        return None, ""

    # wanfang 留在列表里是为了向后兼容：万一以后重新接入带授权的中文源，
    # 历史收藏里已经存下的这类 id 仍然能路由回去
    for known in ("semantic_scholar", "openalex", "wanfang"):
        prefix = known + ":"
        if pid.startswith(prefix):
            return known, pid[len(prefix):]

    # 旧格式兼容
    if pid.startswith("title_"):
        return None, pid[len("title_"):]
    if len(pid) == 40 and all(c in "0123456789abcdef" for c in pid.lower()):
        return "semantic_scholar", pid

    return None, pid


def guess_language(*texts):
    """粗判语种：中日韩字符占比高就当中文，否则英文。

    为什么需要它：Semantic Scholar 不返回 language 字段，但它确实收录了
    一部分中文期刊（标题直接就是中文），一律标成 en 会误导用户。
    这里只用于展示标注，不参与 source="cn" 的选源决策（那个由 GROUPS 决定）。
    """
    sample = " ".join(t for t in texts if isinstance(t, str) and t)[:400]
    if not sample:
        return ""
    cjk = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
    letters = sum(1 for ch in sample if ch.isalpha())
    if letters and cjk / letters > 0.3:
        return "zh"
    return "en"


def _clean_venue(value):
    """清洗期刊/会议名。

    实测 OpenAlex 会给出 "She hui qi ye jing ji fa zhan =" 这种带尾随等号的
    拼音刊名，直接写进参考文献很难看，所以去掉尾部孤立的 = 和多余空白。
    """
    text = str(value or "").strip()
    text = re.sub(r"[\s=]+$", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def normalize_paper(raw, *, source, source_label, language=""):
    """把 provider 拼好的 dict 补齐成统一结构，缺的字段给安全默认值。"""
    p = dict(raw or {})
    raw_id = p.get("paperId") or p.get("id") or ""
    paper = {
        "id": p.get("id") or qualify(source, raw_id),
        "paperId": raw_id,
        "source": source,
        "sourceLabel": source_label,
        "language": p.get("language") or language
                    or guess_language(p.get("title"), p.get("abstract")),
        "title": (p.get("title") or "未知标题").strip(),
        "authors": clean_authors(p.get("authors")),
        "year": p.get("year") if p.get("year") not in (None, "") else "未知",
        "abstract": p.get("abstract") or "暂无摘要",
        "tldr": p.get("tldr") or "",
        "url": p.get("url") or "",
        "venue": _clean_venue(p.get("venue")),
        "journal": _clean_venue(p.get("journal")),
        "doi": (p.get("doi") or "").strip(),
        "citationCount": p.get("citationCount") or 0,
        "referenceCount": p.get("referenceCount"),
        "fieldsOfStudy": [f for f in (p.get("fieldsOfStudy") or []) if f],
        "openAccessPdf": p.get("openAccessPdf") or "",
        # 有 PDF 直链就算有免费全文；isOa 由各 provider 按自己的字段判断
        "isOa": bool(p.get("isOa") or p.get("openAccessPdf")),
        "oaStatus": p.get("oaStatus") or "",
        "volume": str(p.get("volume") or ""),
        "issue": str(p.get("issue") or ""),
        "pages": p.get("pages") or "",
        "docType": p.get("docType") or "journal",
    }
    return paper


def dedup_key(paper):
    """跨源去重键：优先 DOI，其次规范化标题。

    中文标题里全角/半角空格、书名号、《》差异很常见，所以只保留
    中英文数字，其它一律丢掉再比较。
    """
    doi = (paper.get("doi") or "").lower().replace("https://doi.org/", "").strip()
    if doi:
        return "doi:" + doi
    title = (paper.get("title") or "").lower()
    cleaned = "".join(ch for ch in title if ch.isalnum())
    return "title:" + cleaned
