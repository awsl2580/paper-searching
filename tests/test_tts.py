"""朗读功能：文本清洗、口播裁剪、请求归一、缓存键、引擎状态、接口接线。

这些检查都**不联网、不加载模型** —— "哪些该朗读、哪些不该、出错时说什么"
这些逻辑必须随时可验证。

音色只有两档在线语音（晓晓 / 云希）。曾经接过一档 RVC 音色（沃雅妮莎），
因为 CPU 上转换比合成还慢，已经整层删掉；下面留了几条检查防止它被手滑加回来。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import core.tts as tts  # noqa: E402

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


# ── 一、strip_markdown：把"给人看的"改成"给耳朵听的" ────────────
md = """## 一、结论

- **过拟合**的根因是模型把噪声也学进去了。
- 详见 [论文原文](https://example.com/paper.pdf)。

```python
print("这段代码不该被念出来")
```

| 组别 | 准确率 |
| --- | --- |
| 实验组 | 0.91 |
| 对照组 | 0.82 |

该结果显著（p值=0.03），参考 [1] 与文献[2,3]。
> 引用块也要念。✅ 完成
"""
spoken = tts.strip_markdown(md)
ok("代码块整段不念", "print(" not in spoken and "```" not in spoken)
ok("标题符号不念", "##" not in spoken and "一、结论" in spoken)
ok("粗体星号不念", "**" not in spoken and "过拟合" in spoken)
ok("链接只留文字", "example.com" not in spoken and "论文原文" in spoken)
ok("表格行变成顿号列表", "组别，准确率" in spoken and "实验组，0.91" in spoken, spoken)
ok("表格分隔行不念", "| --- |" not in spoken and "---" not in spoken)
ok("方括号引用号不念", "[1]" not in spoken and "[2,3]" not in spoken and "[2, 3]" not in spoken)
ok("带内容的方括号保留（p值=0.03 要念）", "0.03" in spoken, spoken)
ok("引用块符号不念", ">" not in spoken and "引用块也要念" in spoken)
ok("emoji 不念", "✅" not in spoken and "完成" in spoken)
ok("纯代码块文本清洗后为空", tts.strip_markdown("```\ncode\n```") .strip() == "",
   repr(tts.strip_markdown("```\ncode\n```")))


# ── 二、clip_for_speech：长文裁剪，别让浏览器读十分钟 ────────────
short = "这是一句很短的话。"
ok("短文原样返回", tts.clip_for_speech(short) == short)

long_text = "第一句话说完了。" * 200          # 1600 字
clipped = tts.clip_for_speech(long_text)
ok("长文被裁到上限内", len(clipped) <= tts.MAX_CHARS + 30, len(clipped))
ok("长文在句子边界收尾", clipped[:tts.MAX_CHARS].endswith("。"), clipped[-30:])
ok("长文给出提示语", "后面还有内容" in clipped)

# 3000 字但只有一个句号：不能为了"句子边界"把整段丢光
one_sentence = "啊" * 3000 + "。"
clipped2 = tts.clip_for_speech(one_sentence)
ok("没有句子边界时也能裁", 0 < len(clipped2) <= tts.MAX_CHARS + 30, len(clipped2))


# ── 三、normalize_request：脏输入不崩，落到默认值 ─────────────────
spoken, voice, rate = tts.normalize_request("  你好  ", "不存在的音色", "哇")
ok("空白被去掉", spoken == "你好", spoken)
ok("未知音色落回默认", voice == tts.DEFAULT_VOICE, voice)
ok("未知语速落回默认", rate == tts.DEFAULT_RATE, rate)
ok("合法语速保留", tts.normalize_request("x", "base-f", "+20%")[2] == "+20%")
ok("语速档位包含默认档", tts.DEFAULT_RATE in tts.RATES)

for bad in ("", "   \n ", "```code```", "```\nprint(1)\n```", "###", None):
    try:
        tts.normalize_request(bad, "base-f", "+0%")
        ok("空文本应报错 %r" % (bad,), False)
    except tts.TtsError as exc:
        ok("空文本报 TtsError %r" % (bad,), "可朗读" in str(exc), str(exc))


# ── 四、cache_key：同一句同一音色复用，换了音色不复用 ─────────────
k1 = tts.cache_key("文本", "base-f", "+0%")
k2 = tts.cache_key("文本", "base-f", "+0%")
k3 = tts.cache_key("文本", "base-m", "+0%")
k4 = tts.cache_key("文本", "base-f", "+10%")
ok("同样的输入哈希一致", k1 == k2)
ok("换音色哈希不同", k1 != k3)
ok("换语速哈希不同", k1 != k4)
ok("哈希是安全的文件名", len(k1) == 20 and all(c in "0123456789abcdef" for c in k1), k1)


# ── 五、options()：前端下拉/设置面板要的数据一次给全 ─────────────
opts = tts.options()
ok("返回 enabled 字段", "enabled" in opts)
ok("enabled 与 edge 可用性一致", opts["enabled"] == opts["engine_ready"]["edge"])
ok("只有两档音色（晓晓 / 云希）", [v["id"] for v in opts["voices"]] == ["base-f", "base-m"],
   [v["id"] for v in opts["voices"]])
ok("每档都有短名", all(v.get("short") for v in opts["voices"]))
ok("每档都有状态提示", all(v.get("hint") for v in opts["voices"]))
ok("带上语速档位", list(opts["rates"]) == list(tts.RATES), opts["rates"])
ok("带上朗读字数上限", opts["max_chars"] == tts.MAX_CHARS)
ok("带上默认音色/语速", opts["default_voice"] in tts.VOICES and opts["default_rate"] in tts.RATES)
ok("engine_ready 只有 edge 一项", list(opts["engine_ready"]) == ["edge"], opts["engine_ready"])


# ── 六、RVC 那一层应当是彻底不在了 ──────────────────────────────
ok("音色表里没有 rvc", "rvc" not in tts.VOICES)
ok("模块里没有 worker 相关符号",
   not any(hasattr(tts, n) for n in ("rvc_convert", "rvc_status", "RvcWorker",
                                    "WORKER_SCRIPT", "RVC_VENV")), dir(tts)[:5])
ok("没有 tts_rvc 目录了", not os.path.isdir(os.path.join(ROOT, "tts_rvc")))
ok("没有 setup_rvc 脚本了",
   not os.path.isfile(os.path.join(ROOT, "scripts", "setup_rvc.py")))


# ── 七、接口与前端接线（源码契约，防手滑改掉） ───────────────────
def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


app_src = read(os.path.join("web", "app.py"))
ok("@app.post(\"/api/tts\") 还在", '@app.post("/api/tts")' in app_src)
ok("音色选项接口还在", "/api/tts/options" in app_src)
ok("音频接口还在", "/api/tts/audio/" in app_src)
tts_route = app_src.split('@app.post("/api/tts")')[1][:600]
ok("合成接口要求登录", "login_required" in tts_route)
ok("音频接口做了文件名校验", 're.fullmatch(r"[0-9a-f]{20}' in app_src
   and "非法的音频名" in app_src)
ok("app.py 里不再有 worker 收尾", "tts_layer.shutdown" not in app_src)

html = read(os.path.join("static", "index.html"))
ok("前端有朗读按钮构造函数", "function speakBubble" in html)
ok("朗读请求打到 /api/tts", "fetch('/api/tts'" in html)
ok("朗读按钮挂在 .bubble-tools 里", ".bubble-tools" in html and "tts-btn" in html)
ok("设置里有朗读分组", "<h4>朗读</h4>" in html)
ok("切会话/重渲染时停止播放", "stopTts()" in html)
ok("不可用的音色按钮置灰", "voice.available" in html or "v.available" in html)
ok("前端不再特判 rvc 音色", "=== 'rvc'" not in html)

tts_src = read(os.path.join("core", "tts.py"))
ok("合成前先正规化参数", "normalize_request(strip_markdown" in tts_src
   or "normalize_request(text" in tts_src)
ok("写文件先落 .part 再改名", '".part"' in tts_src and "os.replace" in tts_src)


# ── 汇总 ──────────────────────────────────────────────────────
failed = 0
for name, good, got in R:
    print("%s %s%s" % ("[OK]  " if good else "[FAIL]", name,
                       "" if good else "  -> %r" % (got,)))
    failed += 0 if good else 1
print("%d/%d checks passed" % (len(R) - failed, len(R)))
sys.exit(1 if failed else 0)
