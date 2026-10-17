"""图片 / PDF 附件处理：校验、PDF 渲染成图、拼装成模型能读的多模态消息。

为什么单独一个模块：`app.py` 已经 1300 多行了，附件校验和 PDF 渲染
是被测试重点盯着的一块纯逻辑（给定输入 -> 得到什么），单独放更好测。

## 关于"图片不落盘"

产品要求图片**不保存**。所以这里的处理全在内存里：
前端把文件读成 base64 随 JSON 发过来，我们解码成字节 -> 需要就渲染成图
-> 拼进消息 -> 发完就丢。**不写临时文件、不存数据库、不进日志**；
写日志时也刻意不打印 base64 内容，只打印体积和格式。

## 为什么 PDF 要自己渲染

DeepSeek 的多模态接口实测**只接受图片**（webp/png/jpeg/gif）。
直接把它不认识的格式塞进 `file` 变体会被明确拒绝：

    You have uploaded an unsupported file. Please make sure your file is valid
    and has one of the following formats: webp, png, jpeg, and gif.

所以 PDF 在服务端渲染成 PNG 再送进去。用 PyMuPDF（import 名 pymupdf），
它自带渲染器、不需要额外装 poppler 之类的系统组件。

## 为什么默认 300 DPI

实测（同样的问题、同样的 max_tokens=300、各跑 3 次）：

    DPI 100（25KB）：成功 1/3   推理 820-834 字，正文经常为空
    DPI 150（41KB）：成功 1/3   推理 633-842 字
    DPI 300（89KB）：成功 3/3   推理 241-426 字

原因不是"越清晰越好"，而是**图越难认、模型思考越久**，
而思考内容（reasoning_content）会占用同一个 max_tokens 预算——
认不清时它把额度全花在推理上，正文就空了，看起来像"模型没回答"。
所以宁可多花点带宽把图渲染清楚，也不要让它去猜。
"""

from __future__ import annotations

import base64
import binascii
import re

# ── 限制 ────────────────────────────────────────────────────
# 为什么要有硬上限（而不是"不压缩就随便传"）：
# 实测 14.7 MB 的图片送进去**不报错、但返回空内容**（静默失败），
# 8.2 MB 时已经开始认不准字。所以体积上限是"可用性"问题，不只是成本问题。
MAX_FILES = 5                      # 单次最多几个附件
MAX_FILE_BYTES = 12 * 1024 * 1024  # 单个文件 12 MB（实测 14.7MB 会静默失败）
MAX_TOTAL_BYTES = 24 * 1024 * 1024 # 单次合计 24 MB
MAX_PDF_PAGES = 8                  # PDF 最多取前几页（整本论文没必要、也读不完）

# 接口真正接受的图片格式（来自它的报错信息）
IMAGE_MIMES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
PDF_MIME = "application/pdf"
PDF_DPI = 300     # 见模块开头实测数据

# 视觉请求的 max_tokens 下限。
# 思考模型读图时推理内容很长（实测 241-842 字），额度给小了正文就没了。
VISION_MIN_MAX_TOKENS = 4000


class AttachmentError(ValueError):
    """附件不合法（格式/体积/数量）。调用方应把 message 直接回给用户。"""


_DATA_URL_RE = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?;?base64,(?P<data>.*)$", re.S)


def _decode_payload(raw: str) -> bytes:
    """把前端传来的 base64（允许带 data URL 前缀）解成字节。"""
    if not isinstance(raw, str) or not raw.strip():
        raise AttachmentError("附件内容为空。")
    m = _DATA_URL_RE.match(raw.strip())
    b64 = m.group("data") if m else raw.strip()
    # 前端 btoa / FileReader 都可能带换行
    b64 = re.sub(r"\s+", "", b64)
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise AttachmentError("附件不是合法的 base64 数据。")


def _sniff_mime(blob: bytes, declared: str) -> str:
    """按魔术字节判断真实类型，不信任前端声明的 mime。"""
    if blob[:4] == b"%PDF":
        return PDF_MIME
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return (declared or "").strip().lower()


def render_pdf_to_pngs(blob: bytes, max_pages: int = MAX_PDF_PAGES,
                       dpi: int = PDF_DPI) -> tuple[list[bytes], int]:
    """把 PDF 前几页渲染成 PNG。返回 (png列表, 总页数)。

    为什么不送整本：一页 300DPI 约 89KB，一本 30 页的论文光图就 2.7MB，
    而且模型一次读几十页效果很差。默认取前 8 页（封面/摘要/引言基本都在）。
    """
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - 依赖缺失时给出可执行的提示
        raise AttachmentError(
            "服务端缺少 PDF 渲染组件（pymupdf），暂时无法处理 PDF。"
            "请改为上传图片，或安装依赖：pip install pymupdf"
        )
    try:
        doc = pymupdf.open(stream=blob, filetype="pdf")
    except Exception:
        raise AttachmentError("这个 PDF 无法解析，文件可能已损坏或被加密。")

    try:
        total = doc.page_count
        if total == 0:
            raise AttachmentError("这个 PDF 没有任何页面。")
        pages = []
        for i in range(min(total, max_pages)):
            try:
                pix = doc[i].get_pixmap(dpi=dpi)
                pages.append(pix.tobytes("png"))
            except Exception:
                # 单页渲染失败不影响其它页；全失败时下面会报错
                continue
        if not pages:
            raise AttachmentError("这个 PDF 的页面无法渲染成图片。")
        return pages, total
    finally:
        doc.close()


def build_image_block(png_or_any: bytes, mime: str = "image/png") -> dict:
    """拼成一个模型能读的图片内容块（OpenAI 风格 image_url + data URL）。"""
    b64 = base64.b64encode(png_or_any).decode("ascii")
    return {"type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"}}


def prepare_attachments(files, max_files: int = MAX_FILES) -> tuple[list[dict], list[str]]:
    """校验附件并转成内容块。

    参数 files: [{"name":..., "mime":..., "data": <base64 字符串>}, ...]
    返回 (内容块列表, 给用户看的说明列表)

    说明列表会拼进用户消息里，告诉模型"这几张是 PDF 的第几页"——
    否则模型不知道多张图之间的关系（一个 8 页 PDF 会变成 8 张孤立的图）。
    """
    if not files:
        return [], []
    if not isinstance(files, list):
        raise AttachmentError("附件格式不正确。")
    if len(files) > max_files:
        raise AttachmentError(f"一次最多上传 {max_files} 个文件。")

    blocks: list[dict] = []
    notes: list[str] = []
    total = 0

    for idx, f in enumerate(files, 1):
        if not isinstance(f, dict):
            raise AttachmentError("附件格式不正确。")
        name = str(f.get("name") or f"文件{idx}")[:120]
        declared = str(f.get("mime") or "")
        blob = _decode_payload(f.get("data"))

        if len(blob) > MAX_FILE_BYTES:
            raise AttachmentError(
                f"「{name}」{len(blob) / 1024 / 1024:.1f} MB，超过单个文件 "
                f"{MAX_FILE_BYTES // 1024 // 1024} MB 上限。"
                "实测更大的图会静默失败（返回空内容），所以这里直接拦住。"
            )
        total += len(blob)
        if total > MAX_TOTAL_BYTES:
            raise AttachmentError(
                f"附件合计超过 {MAX_TOTAL_BYTES // 1024 // 1024} MB，请分几次上传。")

        mime = _sniff_mime(blob, declared)

        if mime == PDF_MIME:
            pages, total_pages = render_pdf_to_pngs(blob)
            for pno, png in enumerate(pages, 1):
                blocks.append(build_image_block(png, "image/png"))
            if total_pages > len(pages):
                notes.append(f"「{name}」共 {total_pages} 页，这里只读了前 {len(pages)} 页")
            else:
                notes.append(f"「{name}」共 {total_pages} 页，已全部读入")
        elif mime in IMAGE_MIMES:
            blocks.append(build_image_block(blob, mime))
        else:
            raise AttachmentError(
                f"「{name}」的格式（{mime or '未知'}）不支持。"
                "支持 PNG / JPEG / WebP / GIF 图片，以及 PDF。"
            )

    return blocks, notes


def build_user_content(text: str, blocks: list[dict], notes: list[str]):
    """把文字、图片、附件说明拼成一条用户消息的 content。

    没有图片时**返回纯字符串**（而不是单元素列表）：
    纯文本走原来的路径，行为完全不变，也更容易和旧记录兼容。
    """
    if not blocks:
        return text

    parts: list[dict] = []
    if notes:
        parts.append({"type": "text",
                      "text": "（附件信息：" + "；".join(notes) + "）"})
    parts.append({"type": "text",
                  "text": text or "请看这些图片，按你的判断开始工作。"})
    parts.extend(blocks)
    return parts
