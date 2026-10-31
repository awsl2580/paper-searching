"""分子结构（SMILES）解析：不联网也能算分子式、分子量和类药性。

为什么需要它
------------
化学类科研任务的第一道坎是"这个分子到底多大、几个环、能不能过类药五规则"。
这些信息在 SMILES 字符串里是隐含的（氢原子不用写、环闭合靠数字配对），
必须真的解析一遍才能得到，LLM 手算必错。

取舍
----
* 只手写有机子集解析器，不做完整的 SMILES 标准（不处理同位素、手性语义、
  互变异构）。手性标记 @/@@ 直接跳过（只影响立体化学，不影响分子式）。
* 氢原子数用"标准价 - 键级之和"估算；芳香键按 1.5 级处理，这样苯环每个碳
  自动得到 1 个 H，吡啶氮得到 0 个 H，结果与教科书一致。
* HBD/HBA 用最简单的近似：HBD = 带氢的 O/N 原子数，HBA = 全部 N+O 原子数。
  这个近似会把酰胺氮也算成受体（严格说不是），文档里已说明，够用于初筛。
* 环数用"环闭合键数"统计（欧拉公式的简化版）：每条闭合键计 1 个环，
  对稠环体系（如咖啡因 2 个环）结果正确。
"""

from __future__ import annotations

import math
import re

__all__ = ["parse", "formula_of", "descriptors", "batch", "ATOMIC_MASS"]

# 标准原子量表（单位 g/mol，IUPAC 常用值）
ATOMIC_MASS = {
    "H": 1.008, "B": 10.81, "C": 12.011, "N": 14.007, "O": 15.999,
    "F": 18.998, "Na": 22.99, "Mg": 24.305, "Si": 28.085, "P": 30.974,
    "S": 32.06, "Cl": 35.45, "K": 39.098, "Ca": 40.078, "Fe": 55.845,
    "Zn": 65.38, "Br": 79.904, "I": 126.904,
}

# 常见价态，用于估算隐含氢
_VALENCE = {
    "H": 1, "B": 3, "C": 4, "N": 3, "O": 2, "F": 1, "Si": 4, "P": 3,
    "S": 2, "Cl": 1, "Br": 1, "I": 1, "Na": 1, "K": 1, "Ca": 2,
    "Mg": 2, "Fe": 2, "Zn": 2,
}

# 小写芳香原子 -> 元素符号
_AROMATIC = {"b": "B", "c": "C", "n": "N", "o": "O", "p": "P", "s": "S"}
_AROMATIC_SET = set(_AROMATIC.keys())

# 芳香原子在"芳香价"意义下的参考价（苯碳 4、吡啶氮 3）
_AROM_VALENCE = {"B": 3, "C": 4, "N": 3, "O": 2, "P": 3, "S": 2}

_BOND_SYMBOLS = {"-": 1.0, "=": 2.0, "#": 3.0, "/": 1.0, "\\": 1.0, ":": 1.5}


class _Atom(object):
    """解析过程中的原子记录（轻量结构体）。"""

    __slots__ = ("symbol", "aromatic", "bracket", "h", "charge")

    def __init__(self, symbol, aromatic=False, bracket=False, h=0, charge=0):
        self.symbol = symbol
        self.aromatic = aromatic
        self.bracket = bracket
        self.h = h          # 括号原子里的显式氢数；非括号原子由价态推算
        self.charge = charge


def _r(x, nd: int = 6) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    v = round(v, nd)
    return 0.0 if v == 0.0 else v


def _read_element(s: str, i: int):
    """从位置 i 读元素符号，返回 (symbol, 新位置)；读不到返回 (None, i)。"""
    if i >= len(s):
        return None, i
    ch = s[i]
    if ch.isupper():
        if i + 1 < len(s) and s[i + 1].islower() and (ch + s[i + 1]) in ATOMIC_MASS:
            return ch + s[i + 1], i + 2
        return ch, i + 1
    return None, i


def _parse_bracket(content: str, warnings: list):
    """解析方括号原子内容，如 "nH" / "NH4+" / "C@@H" / "O-" / "13CH4"。"""
    s = content
    i = 0
    # 跳过同位素数字前缀
    while i < len(s) and s[i].isdigit():
        i += 1
    if i >= len(s):
        warnings.append("方括号原子为空：[%s]" % content)
        return None, i

    aromatic = False
    if s[i] in _AROMATIC_SET:
        symbol = _AROMATIC[s[i]]
        aromatic = True
        i += 1
    else:
        symbol, i = _read_element(s, i)
        if symbol is None or symbol not in ATOMIC_MASS:
            warnings.append("无法识别的元素：[%s]" % content)
            return None, i

    atom = _Atom(symbol, aromatic=aromatic, bracket=True)

    # 跳过手性标记与后续的 @/@@
    while i < len(s):
        ch = s[i]
        if ch == "@":
            i += 1
            continue
        if ch == "H":
            # 氢原子计数：H 后面可跟数字，默认 1
            i += 1
            num = ""
            while i < len(s) and s[i].isdigit():
                num += s[i]
                i += 1
            atom.h = int(num) if num else 1
            continue
        if ch in "+-":
            sign = 1 if ch == "+" else -1
            j = i + 1
            num = ""
            while j < len(s) and s[j].isdigit():
                num += s[j]
                j += 1
            # 兼容 "++" / "--" 记法
            if num == "" and j < len(s) and s[j] == ch:
                atom.charge = sign * 2
                i = j + 1
                continue
            atom.charge = sign * (int(num) if num else 1)
            i = j
            continue
        if ch == ":":
            i += 1  # 方括号内的环闭合冒号，忽略
            continue
        if ch.isdigit():
            i += 1  # 方括号内的环闭合编号，忽略
            continue
        warnings.append("方括号原子中出现未识别字符 %r：[%s]" % (ch, content))
        i += 1
    return atom, i


def _tokenize(smiles: str, warnings: list):
    """把 SMILES 切成 token 序列：('atom', _Atom) / ('bond', 级数) / ('open'|'close')
    / ('ring', 编号) / ('dot',)。
    """
    tokens = []
    s = smiles.strip()
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "[":
            j = s.find("]", i + 1)
            if j < 0:
                warnings.append("方括号未闭合，已忽略：%s" % s[i:])
                break
            atom, _ = _parse_bracket(s[i + 1:j], warnings)
            if atom is not None:
                tokens.append(("atom", atom))
            i = j + 1
            continue
        if ch == "(":
            tokens.append(("open", None))
            i += 1
            continue
        if ch == ")":
            tokens.append(("close", None))
            i += 1
            continue
        if ch in _BOND_SYMBOLS:
            tokens.append(("bond", _BOND_SYMBOLS[ch]))
            i += 1
            continue
        if ch == ".":
            tokens.append(("dot", None))
            i += 1
            continue
        if ch == "%":
            # %nn 两位数环闭合
            num = s[i + 1:i + 3]
            if len(num) == 2 and num.isdigit():
                tokens.append(("ring", int(num)))
                i += 3
            else:
                warnings.append("%% 后应跟两位数字，已跳过：%s" % s[i:i + 4])
                i += 1
            continue
        if ch.isdigit():
            tokens.append(("ring", int(ch)))
            i += 1
            continue
        if ch in _AROMATIC_SET:
            tokens.append(("atom", _Atom(_AROMATIC[ch], aromatic=True)))
            i += 1
            continue
        if ch.isupper():
            symbol, j = _read_element(s, i)
            if symbol is not None and symbol in ATOMIC_MASS:
                tokens.append(("atom", _Atom(symbol)))
                i = j
                continue
            warnings.append("未识别的元素符号 %r，已忽略" % ch)
            i += 1
            continue
        # 其余字符（如多余的 *、空白、+、-）直接忽略并记录
        if not ch.isspace():
            warnings.append("未识别字符 %r，已忽略" % ch)
        i += 1
    return tokens


class _Molecule(object):
    """解析结果：原子表 + 键表。"""

    def __init__(self):
        self.atoms = []            # list[_Atom]
        self.bonds = []            # list[{"a","b","order","ring"}]
        self.rings = 0
        self.ring_open = {}
        self.valid = True
        self.warnings = []

    def add_bond(self, a: int, b: int, order: float, ring: bool = False):
        self.bonds.append({"a": a, "b": b, "order": order, "ring": ring})
        if ring:
            self.rings += 1

    def degree(self, idx: int) -> int:
        return sum(1 for bd in self.bonds if bd["a"] == idx or bd["b"] == idx)

    def bond_sum(self, idx: int) -> float:
        total = 0.0
        for bd in self.bonds:
            if bd["a"] == idx or bd["b"] == idx:
                total += bd["order"]
        return total


def _build(smiles: str):
    """核心解析：token 流 -> 原子/键图。"""
    mol = _Molecule()
    tokens = _tokenize(smiles, mol.warnings)
    prev = None
    pending = None            # 待用的键级
    branch_stack = []         # 分支起点
    open_brackets = 0

    for kind, val in tokens:
        if kind == "atom":
            idx = len(mol.atoms)
            mol.atoms.append(val)
            if prev is not None:
                if pending is not None:
                    order = pending
                elif mol.atoms[prev].aromatic and val.aromatic:
                    order = 1.5  # 两个芳香原子之间默认芳香键
                else:
                    order = 1.0
                mol.add_bond(prev, idx, order)
            prev = idx
            pending = None
        elif kind == "bond":
            pending = val
        elif kind == "open":
            open_brackets += 1
            if prev is None:
                mol.warnings.append("分支起点缺少前一个原子")
                mol.valid = False
            branch_stack.append(prev)
        elif kind == "close":
            if not branch_stack:
                mol.warnings.append("多余的右括号")
                mol.valid = False
            else:
                open_brackets -= 1
                prev = branch_stack.pop()
        elif kind == "dot":
            prev = None
            pending = None
        elif kind == "ring":
            num = val
            if num in mol.ring_open:
                other, other_order = mol.ring_open.pop(num)
                if other is None or other >= len(mol.atoms):
                    mol.warnings.append("环闭合 %d 找不到起点" % num)
                    mol.valid = False
                    continue
                if prev is None:
                    mol.warnings.append("环闭合 %d 缺少当前原子" % num)
                    mol.valid = False
                    continue
                if pending is not None:
                    order = pending
                elif other_order is not None:
                    order = other_order
                elif mol.atoms[other].aromatic and mol.atoms[prev].aromatic:
                    order = 1.5
                else:
                    order = 1.0
                mol.add_bond(other, prev, order, ring=True)
            else:
                mol.ring_open[num] = (prev, pending)
            pending = None

    if open_brackets > 0:
        mol.warnings.append("括号不平衡（缺 %d 个右括号）" % open_brackets)
        mol.valid = False
    if mol.ring_open:
        mol.warnings.append("环闭合编号未配对：%s" % sorted(mol.ring_open.keys()))
        mol.valid = False
    # 出现了完全不认识的字符：说明这串东西不是合法 SMILES，不能算 valid
    if any("未识别" in w for w in mol.warnings):
        mol.valid = False
    if not mol.atoms:
        mol.valid = False
        mol.warnings.append("未解析出任何原子")
    return mol


def _counts(mol) -> dict:
    """统计元素 -> 个数，氢原子由价态推算后再并入。"""
    counts = {}
    for i, atom in enumerate(mol.atoms):
        counts[atom.symbol] = counts.get(atom.symbol, 0) + 1
        if atom.bracket:
            h = atom.h
        else:
            if atom.aromatic:
                base = _AROM_VALENCE.get(atom.symbol, _VALENCE.get(atom.symbol, 0))
            else:
                base = _VALENCE.get(atom.symbol, 0)
            h = int(round(base - mol.bond_sum(i)))
            if h < 0:
                h = 0
        if h > 0:
            counts["H"] = counts.get("H", 0) + h
    return counts


def _hill(counts: dict) -> str:
    """Hill 排序：C 优先，H 其次，其余按字母序。"""
    parts = []
    if "C" in counts:
        parts.append("C%s" % (counts["C"] if counts["C"] > 1 else ""))
        if "H" in counts:
            parts.append("H%s" % (counts["H"] if counts["H"] > 1 else ""))
        rest = sorted(k for k in counts if k not in ("C", "H"))
    else:
        rest = sorted(counts.keys())
    for k in rest:
        if k in ("C", "H") and "C" in counts:
            continue
        v = counts[k]
        parts.append("%s%s" % (k, v if v > 1 else ""))
    return "".join(parts)


def _molecular_weight(counts: dict) -> float:
    total = 0.0
    for symbol, num in counts.items():
        total += ATOMIC_MASS.get(symbol, 0.0) * num
    return total


# 纯分子式形态：只由「大写开头元素 + 可选数字」组成
_FORMULA_RE = re.compile(r"^(?:[A-Z][a-z]?\d*)+$")


def _as_formula_counts(text: str):
    """把 "C6H6" / "C8H10N4O2" 这类**分子式**直接转成元素计数。

    注意这不是 SMILES：SMILES 里氢必须写在方括号内。因此当输入长得像分子式
    且含有方括号外的 H 时（这在 SMILES 里是非法的），按分子式理解更合理。
    返回 None 表示不像分子式。
    """
    s = text.strip()
    if not s or not _FORMULA_RE.match(s):
        return None
    if "H" not in s:
        # 没有氢的纯元素串更可能是 SMILES（如 CCO / C1CCCCC1），不抢它的解释权
        return None
    counts = {}
    i = 0
    while i < len(s):
        m = re.match(r"([A-Z][a-z]?)(\d*)", s[i:])
        if not m:
            return None
        symbol = m.group(1)
        num = m.group(2)
        if symbol not in ATOMIC_MASS:
            return None
        count = int(num) if num else 1
        if count <= 0:
            return None
        counts[symbol] = counts.get(symbol, 0) + count
        i += len(m.group(0))
    return counts or None


def _ring_bond_flags(mol) -> list:
    """判断每条键是否属于环：删掉该键后两端是否仍连通。"""
    n = len(mol.atoms)
    adj = [[] for _ in range(n)]
    for bi, bd in enumerate(mol.bonds):
        adj[bd["a"]].append((bd["b"], bi))
        adj[bd["b"]].append((bd["a"], bi))
    flags = []
    for bi, bd in enumerate(mol.bonds):
        if bd["ring"]:
            flags.append(True)
            continue
        src, dst = bd["a"], bd["b"]
        seen = {src}
        stack = [src]
        found = False
        while stack and not found:
            cur = stack.pop()
            for nxt, nbi in adj[cur]:
                if nbi == bi or nxt in seen:
                    continue
                if nxt == dst:
                    found = True
                    break
                seen.add(nxt)
                stack.append(nxt)
        flags.append(found)
    return flags


def parse(smiles: str) -> dict:
    """解析 SMILES，返回分子式、分子量与基础拓扑描述符。

    返回::

        {"formula": "C6H6", "mw": 78.114, "heavy_atoms": 6, "rings": 1,
         "aromatic_atoms": 6, "rotatable_bonds": 0, "hbd": 0, "hba": 0,
         "charge": 0, "valid": True, "warnings": []}
    """
    if smiles is None or (isinstance(smiles, str) and smiles.strip() == ""):
        raise ValueError("SMILES 不能为空")
    if not isinstance(smiles, str):
        raise ValueError("SMILES 必须是字符串")

    # 先看是不是直接给了分子式（如 "C6H6"），这种输入没有拓扑信息
    direct = _as_formula_counts(smiles)
    if direct is not None:
        heavy = sum(v for k, v in direct.items() if k != "H")
        return {
            "smiles": smiles.strip(),
            "input_kind": "formula",
            "formula": _hill(direct),
            "mw": _r(_molecular_weight(direct), 3),
            "heavy_atoms": heavy,
            "rings": 0,
            "aromatic_atoms": 0,
            "rotatable_bonds": 0,
            "hbd": 0,
            "hba": direct.get("N", 0) + direct.get("O", 0),
            "charge": 0,
            "n_bonds": 0,
            "valid": True,
            "warnings": ["输入被识别为分子式（非 SMILES），环数/芳香原子等拓扑描述符不可用"],
        }

    mol = _build(smiles)
    counts = _counts(mol)
    mw = _molecular_weight(counts)

    hbd = 0
    hba = 0
    for i, atom in enumerate(mol.atoms):
        if atom.symbol in ("N", "O"):
            hba += 1
            if atom.bracket:
                h = atom.h
            else:
                if atom.aromatic:
                    base = _AROM_VALENCE.get(atom.symbol, 0)
                else:
                    base = _VALENCE.get(atom.symbol, 0)
                h = max(0, int(round(base - mol.bond_sum(i))))
            if h >= 1:
                hbd += 1

    # 可旋转键：单键 + 不在环上 + 两端都不是末端重原子
    ring_flags = _ring_bond_flags(mol)
    rotatable = 0
    for bi, bd in enumerate(mol.bonds):
        if ring_flags[bi] or bd["order"] != 1.0:
            continue
        if mol.degree(bd["a"]) >= 2 and mol.degree(bd["b"]) >= 2:
            rotatable += 1

    charge = sum(a.charge for a in mol.atoms)
    aromatic_atoms = sum(1 for a in mol.atoms if a.aromatic)

    return {
        "smiles": smiles.strip(),
        "input_kind": "smiles",
        "formula": _hill(counts) if counts else "",
        "mw": _r(mw, 3),
        "heavy_atoms": len(mol.atoms),
        "rings": mol.rings,
        "aromatic_atoms": aromatic_atoms,
        "rotatable_bonds": rotatable,
        "hbd": hbd,
        "hba": hba,
        "charge": charge,
        "n_bonds": len(mol.bonds),
        "valid": bool(mol.valid),
        "warnings": list(mol.warnings),
    }


def formula_of(smiles: str) -> dict:
    """只取分子式（Hill 排序），如苯 -> "C6H6"、咖啡因 -> "C8H10N4O2"。"""
    info = parse(smiles)
    if not info["formula"]:
        raise ValueError("SMILES 无法解析出分子式：%s" % smiles)
    return {"smiles": info["smiles"], "formula": info["formula"],
            "valid": info["valid"]}


def descriptors(smiles: str) -> dict:
    """类药性描述符：在 parse 的基础上加 Lipinski 五规则判定。

    规则：MW <= 500、HBD <= 5、HBA <= 10；logP 与 TPSA 这里不做估算，直接跳过。
    """
    info = parse(smiles)
    violations = []
    if info["mw"] > 500:
        violations.append("分子量 %.1f > 500" % info["mw"])
    if info["hbd"] > 5:
        violations.append("氢键供体 %d > 5" % info["hbd"])
    if info["hba"] > 10:
        violations.append("氢键受体 %d > 10" % info["hba"])
    info["lipinski_violations"] = violations
    info["lipinski_pass"] = not violations
    info["skipped_rules"] = ["logP（未估算）", "TPSA（未估算）"]
    info["note"] = "HBD=带氢的 O/N 原子数，HBA=全部 N+O 原子数，均为简化近似"
    return info


def batch(smiles_list) -> dict:
    """批量解析：一次给出多个分子的描述符，坏输入不中断整批。"""
    if smiles_list is None:
        raise ValueError("smiles 列表不能为空")
    if isinstance(smiles_list, str):
        items = [p.strip() for p in smiles_list.replace("\n", ",").split(",") if p.strip()]
    elif isinstance(smiles_list, (list, tuple)):
        items = list(smiles_list)
    else:
        raise ValueError("smiles 必须是字符串列表，例如 [\"CCO\", \"c1ccccc1\"]")

    out = []
    invalid = []
    valid_count = 0
    for raw in items:
        try:
            info = descriptors(raw)
        except (ValueError, TypeError) as exc:
            invalid.append({"smiles": raw, "reason": str(exc)})
            continue
        except Exception as exc:  # 解析器内部异常也不让整批失败
            invalid.append({"smiles": raw, "reason": "解析异常：%s" % exc})
            continue
        if not info["valid"]:
            invalid.append({"smiles": raw,
                            "reason": "；".join(info["warnings"]) or "结构不完整"})
        else:
            valid_count += 1
        out.append(info)
    return {
        "items": out,
        "n": len(out),
        "valid_count": valid_count,
        "invalid": invalid,
    }
