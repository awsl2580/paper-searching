"""朗读（TTS）：把一段回答文本变成能播放的音频。

用的是微软 Edge 的在线神经语音（`edge-tts`）：中文自然、不用下模型，
一句话通常 1~3 秒合成完。代价是要联网，而且声音来自微软的接口。

> 曾经还有一档 RVC 音色（沃雅妮莎）：先合成基础音频，再用 torch + faiss 换成
> 目标音色。实测 CPU 上 6 秒的音频要转 5.7 秒、首次还要多等 4 秒加载模型，
> 为了"稍微个性化一点"付这个代价不划算，已经整档删掉（`tts_rvc/`、
> `scripts/setup_rvc.py` 一起移除了）。留在这里是为了以后别有人再踩一遍。

## 缓存

按 `(音色, 语速, 文本)` 的 sha1 落盘到 `tts_cache/`。同一段回答再点一次朗读是
直接读文件（毫秒级），也不会把微软的接口刷爆。目录大小有上限，超了按最久未用删。

## 这一层对外只暴露一个函数

    synthesize(text, voice, rate) -> {ok, path, mime, engine, cached, ms, voice}

出错一律抛 `TtsError`（带一句人话），由 web 层转成 JSON。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import threading
import time

try:  # 项目根的 paths.py 提供 ROOT；单独 import 本模块时（测试）也兜得住
    import paths  # type: ignore
    ROOT = paths.ROOT
except Exception:  # noqa: BLE001
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CACHE_DIR = os.path.join(ROOT, "tts_cache")
CACHE_LIMIT_MB = 300          # 超过就按最久未用删到 80%

#: 单次朗读的文本上限。不设的话，用户点一篇长报告会读出十分钟，
#: 期间既费流量又占住合成进程。截断在句号/换行处，不切断句子。
MAX_CHARS = 1000

#: 可用音色，都是 edge-tts 的官方中文神经语音。
VOICES = {
    "base-f": {
        "id": "base-f",
        "label": "晓晓（女声）",
        "short": "晓晓",
        "engine": "edge",
        "edge": "zh-CN-XiaoxiaoNeural",
        "hint": "在线合成，1~3 秒出音",
    },
    "base-m": {
        "id": "base-m",
        "label": "云希（男声）",
        "short": "云希",
        "engine": "edge",
        "edge": "zh-CN-YunxiNeural",
        "hint": "在线合成，1~3 秒出音",
    },
}
DEFAULT_VOICE = "base-f"

#: 语速档位（edge-tts 的 rate 参数是相对百分比）。
RATES = ("-20%", "-10%", "+0%", "+10%", "+20%")
DEFAULT_RATE = "+0%"


class TtsError(RuntimeError):
    """合成失败。消息是给用户看的一句话，不要塞栈。"""


# ═══════════════════════════════════════════════════════════════
#  文本清洗：屏幕上能看 ≠ 能读
# ═══════════════════════════════════════════════════════════════
_EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U0001F1E6-\U0001F1FF" "\u2600-\u27BF" "\u2B00-\u2BFF" "\uFE0F" "]"
)
_CITE_RE = re.compile(r"\[(\d{1,3}(?:\s*[,，]\s*\d{1,3})*)\]")   # [1] [2,3] 这类引用角标
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")             # [文字](url) → 文字
_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")             # ![alt](url) → 丢掉
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]{3,}\|?\s*$")
_HR_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_HEAD_RE = re.compile(r"^\s{0,3}#{1,6}\s*")
_BULLET_RE = re.compile(r"^\s*(?:[-*+·•]|\d{1,2}[.)、])\s+")
_QUOTE_RE = re.compile(r"^\s*>\s?")


def strip_markdown(text: str) -> str:
    """把 Markdown 洗成"念出来不奇怪"的纯文本。

    取舍（每条都是因为念出来难听才删的）：
      * 代码块整段跳过 —— 念 `def foo(x):` 没有意义；
      * 表格竖线换成顿号 —— 否则会念出"竖线"；
      * 分隔线 / 标题井号 / 列表符号 / 引用箭头：视觉元素，念出来是噪音；
      * 引用角标 `[1]` 去掉，但 `[p值 = 0.03]` 这种带内容的方括号要留下；
      * emoji 去掉：多数语音会把 😊 念成"微笑"。
    """
    if not text:
        return ""
    src = str(text).replace("\r\n", "\n").replace("\r", "\n")

    # 代码块（``` 包裹）整段拿掉：宁可少读一段，也不要念一堆符号
    src = re.sub(r"```.*?```", "\n", src, flags=re.S)
    src = re.sub(r"`([^`]*)`", r"\1", src)      # 行内代码保留内容

    out_lines = []
    for raw in src.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            out_lines.append("")
            continue
        if _TABLE_SEP_RE.match(line) or _HR_RE.match(line):
            continue                              # 表格分隔行 / 分隔线
        line = _MD_IMG_RE.sub("", line)
        line = _MD_LINK_RE.sub(r"\1", line)
        line = _HEAD_RE.sub("", line)
        line = _QUOTE_RE.sub("", line)
        line = _BULLET_RE.sub("", line)
        if line.lstrip().startswith("|"):
            # 表格内容行：| 单元格 | 单元格 | → 单元格，单元格。
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            line = "，".join([c for c in cells if c])
        line = _CITE_RE.sub("", line)
        line = line.replace("**", "").replace("__", "")
        line = re.sub(r"(?<!\w)[*_](\S(?:.*?\S)?)[*_](?!\w)", r"\1", line)
        out_lines.append(line)

    text = "\n".join(out_lines)
    text = _EMOJI_RE.sub("", text)
    # 空白收敛：连续空行压成一个，行首行尾多余空格去掉
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def clip_for_speech(text: str, limit: int = MAX_CHARS) -> str:
    """按上限截断，尽量断在句末，避免"半句话"。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind("。"), head.rfind("！"), head.rfind("？"),
              head.rfind("\n"), head.rfind("."))
    if cut >= limit * 0.5:
        return head[:cut + 1].strip() + "……（后面还有内容，先读到这里）"
    return head.rstrip() + "……（后面还有内容，先读到这里）"


# ═══════════════════════════════════════════════════════════════
#  基础音色：edge-tts
# ═══════════════════════════════════════════════════════════════
def edge_available() -> bool:
    """edge-tts 装没装。没装时朗读入口整块隐藏 —— 不要给用户一个点了必失败的按钮。"""
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


async def _edge_render(text: str, voice: str, rate: str, out_path: str) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, rate=rate)
    buf = bytearray()
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio" and chunk.get("data"):
            buf += chunk["data"]
    if not buf:
        raise TtsError("语音服务没有返回音频（可能是网络被拦或文本为空）")
    tmp = out_path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(bytes(buf))
    os.replace(tmp, out_path)      # 先写 .part 再改名：不给播放器半个文件


def synth_base(text: str, edge_voice: str, rate: str, out_path: str,
               timeout: float = 120.0) -> None:
    """合成基础音频（mp3）。失败抛 TtsError。"""
    if not edge_available():
        raise TtsError("没装 edge-tts，基础音色不可用（pip install edge-tts）")

    async def _run():
        await asyncio.wait_for(
            _edge_render(text, edge_voice, rate, out_path), timeout=timeout)

    try:
        asyncio.run(_run())
    except TtsError:
        raise
    except (asyncio.TimeoutError, TimeoutError):
        raise TtsError("合成超时（%d 秒），网络可能不通" % int(timeout))
    except Exception as e:  # noqa: BLE001
        raise TtsError("合成失败：%s: %s" % (type(e).__name__, e))


# ═══════════════════════════════════════════════════════════════
#  缓存
# ═══════════════════════════════════════════════════════════════
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


def cache_key(text: str, voice: str, rate: str) -> str:
    raw = "\x1f".join([voice, rate, text]).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:20]


def _sweep() -> None:
    """缓存超过上限时，按最久未访问删到 80%。"""
    if not os.path.isdir(CACHE_DIR):
        return
    items = []
    total = 0
    for name in os.listdir(CACHE_DIR):
        path = os.path.join(CACHE_DIR, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        items.append((st.st_atime, st.st_size, path))
        total += st.st_size
    if total <= CACHE_LIMIT_MB * 1024 * 1024:
        return
    items.sort()
    target = CACHE_LIMIT_MB * 0.8 * 1024 * 1024
    for _atime, size, path in items:
        if total <= target:
            break
        try:
            os.remove(path)
            total -= size
        except OSError:
            pass


def cache_stats() -> dict:
    count = 0
    size = 0
    if os.path.isdir(CACHE_DIR):
        for name in os.listdir(CACHE_DIR):
            try:
                size += os.path.getsize(os.path.join(CACHE_DIR, name))
                count += 1
            except OSError:
                pass
    return {"files": count, "bytes": size}


# ═══════════════════════════════════════════════════════════════
#  对外主函数
# ═══════════════════════════════════════════════════════════════
def normalize_request(text: str, voice: str, rate: str) -> tuple[str, str, str]:
    """校验并规整参数。前端传什么都别信。"""
    if voice not in VOICES:
        voice = DEFAULT_VOICE
    if rate not in RATES:
        rate = DEFAULT_RATE
    spoken = clip_for_speech(strip_markdown(text))
    if not spoken.strip():
        raise TtsError("这段内容没有可朗读的文字（可能只有代码块或图片）")
    return spoken, voice, rate


def synthesize(text: str, voice: str = DEFAULT_VOICE,
               rate: str = DEFAULT_RATE) -> dict:
    """合成并返回可播放文件的信息。命中缓存时 `cached=True`、`ms` 很小。"""
    spoken, voice, rate = normalize_request(text, voice, rate)
    conf = VOICES[voice]
    key = cache_key(spoken, voice, rate)
    os.makedirs(CACHE_DIR, exist_ok=True)
    out_path = os.path.join(CACHE_DIR, key + ".mp3")

    with _lock_for(key):
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            os.utime(out_path, None)          # 记一次"最近用过"，给 LRU 排序
            return {"ok": True, "path": out_path, "mime": _mime(".mp3"),
                    "engine": conf["engine"], "voice": voice, "rate": rate,
                    "cached": True, "ms": 0, "chars": len(spoken)}

        started = time.time()
        synth_base(spoken, conf["edge"], rate, out_path)   # 内部先写 .part 再改名
        _sweep()
        return {"ok": True, "path": out_path, "mime": _mime(".mp3"),
                "engine": conf["engine"], "voice": voice, "rate": rate,
                "cached": False, "ms": int((time.time() - started) * 1000),
                "chars": len(spoken)}


def _mime(ext: str) -> str:
    return "audio/mpeg" if ext == ".mp3" else "audio/wav"


def options() -> dict:
    """给前端渲染设置项用的音色清单（带可用性）。"""
    edge_ok = edge_available()
    voices = []
    for vid, conf in VOICES.items():
        voices.append({"id": vid, "label": conf["label"],
                       "short": conf.get("short", conf["label"]),
                       "hint": conf["hint"], "engine": conf["engine"],
                       "available": edge_ok})
    return {
        "success": True,
        "enabled": edge_ok,
        "engine_ready": {"edge": edge_ok},
        "default_voice": DEFAULT_VOICE,
        "default_rate": DEFAULT_RATE,
        "rates": list(RATES),
        "voices": voices,
        "max_chars": MAX_CHARS,
        "cache": cache_stats(),
        "reasons": ([] if edge_ok else ["没装 edge-tts（pip install edge-tts），朗读不可用"]),
    }
