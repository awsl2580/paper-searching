"""Agent Skills —— 把"怎么做科研"沉淀成可复用的技能文件。

## 为什么是文件，而不是又一段写死在代码里的提示词

赛题把「Agent Skills 技能化封装」列为前沿技术。它的核心不是"提示词工程"，
而是**能力可分发包、可按需加载**：一个技能 = 一份 `SKILL.md`（说明何时用、
怎么做、用什么工具、验收标准），运行时按任务匹配后**只把匹配到的技能正文
注入上下文**。

这样带来三个实际好处：

1. **上下文预算可控**。七个角色的完整方法论全塞进系统提示词会有几万字，
   而一次任务其实只需要 1-2 个技能。按需注入把这段从"常驻"变成"召回"。
2. **可复现**。技能正文是文件、可 diff、可评审；改提示词不再是改代码。
3. **可扩展**。想加"质谱解析"能力，加一个目录就行，不动任何 Python。

## 文件格式

    ---
    name: spectral-analysis
    title: 谱图与实验曲线解析
    description: 一句话写清什么时候用它
    keywords: 谱图, 曲线, 峰, 信噪比
    roles: computationalist, reader
    tools: spectra_peaks, spectra_smooth, spectra_summary
    ---
    （正文：方法步骤 / 注意事项 / 验收标准）

frontmatter 用 `key: value` 的极简格式手写解析，不引 PyYAML ——
这个仓库的原则是"评委装完 requirements.txt 就能跑"，不为 8 个字段加依赖。
"""

from __future__ import annotations

import os
import re

SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "skills")

# 缓存：文件系统的读取不该出现在每次模型调用里
_CACHE = {}


def _parse_frontmatter(text):
    """解析 `---` 包起来的 key: value。支持逗号分隔的多值。"""
    meta, body = {}, text
    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return meta, body
    raw, body = m.group(1), m.group(2)
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'")
        if key in ("keywords", "roles", "tools", "tags"):
            meta[key] = [v.strip() for v in re.split(r"[,，、]", value) if v.strip()]
        else:
            meta[key] = value
    return meta, body.strip()


def _read_skill(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    meta, body = _parse_frontmatter(text)
    name = meta.get("name") or os.path.splitext(os.path.basename(path))[0]
    return {
        "name": name,
        "title": meta.get("title") or name,
        "description": meta.get("description") or "",
        "keywords": meta.get("keywords") or [],
        "roles": meta.get("roles") or [],
        "tools": meta.get("tools") or [],
        "body": body,
        "path": path,
        "chars": len(body),
    }


def load_skills(refresh=False):
    """扫描 skills/ 目录，返回 {name: skill}。

    目录不存在时返回空字典而不是报错 —— 技能是**增强**，不是启动依赖。
    """
    global _CACHE
    if _CACHE and not refresh:
        return _CACHE
    found = {}
    if os.path.isdir(SKILLS_DIR):
        for entry in sorted(os.listdir(SKILLS_DIR)):
            sub = os.path.join(SKILLS_DIR, entry)
            if os.path.isdir(sub):
                path = os.path.join(sub, "SKILL.md")
                if not os.path.isfile(path):
                    continue
            elif entry.lower().endswith(".md") and entry != "README.md":
                path = sub
            else:
                continue
            skill = _read_skill(path)
            if skill:
                found[skill["name"]] = skill
    _CACHE = found
    return found


def list_skills():
    """给 API / 前端 / 提示词用的技能清单（不含正文，省流量）。"""
    return [{
        "name": s["name"], "title": s["title"], "description": s["description"],
        "keywords": s["keywords"], "roles": s["roles"], "tools": s["tools"],
        "chars": s["chars"],
    } for s in load_skills().values()]


def get_skill(name):
    return load_skills().get(name)


def _score(skill, goal, role=None):
    """技能匹配分：角色命中权重最高，其次关键词命中。"""
    score = 0.0
    if role and role in skill["roles"]:
        score += 2.0
    goal_l = (goal or "").lower()
    for kw in skill["keywords"]:
        if kw and kw.lower() in goal_l:
            score += 1.0
    return score


def match_skills(goal="", role=None, limit=2):
    """按研究目标 + 角色挑技能。没有命中也给角色默认技能（roles 里第一个）。"""
    ranked = []
    for skill in load_skills().values():
        sc = _score(skill, goal, role)
        if sc > 0:
            ranked.append((sc, skill))
    ranked.sort(key=lambda x: -x[0])

    if not ranked and role:
        # 兜底：给这个角色对应的第一个技能 —— 角色有方法论比没有好
        for skill in load_skills().values():
            if role in skill["roles"]:
                ranked.append((0.4, skill))
                break

    return [s for _, s in ranked[:max(0, int(limit))]]


def skill_context(goal="", role=None, limit=2, budget_chars=2600):
    """拼成可直接插进系统提示词的技能块（含预算控制）。"""
    skills = match_skills(goal, role, limit)
    if not skills:
        return ""
    parts = ["【可用技能（按需参考，不要照抄术语）】"]
    used = len(parts[0])
    for s in skills:
        head = f"### 技能：{s['title']}（{s['name']}）\n"
        body = s["body"]
        if used + len(head) + len(body) > budget_chars:
            body = body[:max(0, budget_chars - used - len(head))] + "…"
        parts.append(head + body)
        used += len(head) + len(body)
        if used >= budget_chars:
            break
    return "\n\n".join(parts)


def reload():
    """测试/热更新用。"""
    return load_skills(refresh=True)


__all__ = ["SKILLS_DIR", "get_skill", "list_skills", "load_skills", "match_skills",
           "reload", "skill_context"]
