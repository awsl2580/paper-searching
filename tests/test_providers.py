"""模型提供商（设置 → 模型）：一家一条记录、各自一把 Key。

对应需求：把"只能填一把 DeepSeek Key"改成"提供商列表"（类似常见客户端的
「模型 → 添加提供商」）。覆盖三层：

1. `core/provider_store.py` 的存储：增改删、激活、Key 只回显头尾、原子写
2. 和 `core/llm.py` 的接线：**提供商优先于 .env**（这条最容易写反）
3. `/api/providers` 的 CRUD 与权限（本机可改；公网未登录不可改）
"""
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("FLASK_SECRET_KEY", "0123456789abcdef" * 4)
os.environ.setdefault("DEEPSEEK_RPM", "6000")
os.environ.setdefault("MCP_TRANSPORT", "inproc")
os.environ.setdefault("AI4S_MAX_ITERATIONS", "1")
os.environ["DEEPSEEK_API_KEY"] = "sk-env-fallback-123456"
sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _writable_temp import temp_db_path, temp_file_path  # noqa: E402

import database as db
db.DB_PATH = temp_db_path("_tmp_providers.db")

import provider_store  # noqa: E402
PROV = temp_file_path("_tmp_providers.json")
provider_store.set_path(PROV)

import llm  # noqa: E402
import app as appmod  # noqa: E402

db.init_db()
R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


def expect_error(fn, label, needle=None):
    try:
        fn()
        ok(label, False, "没有报错")
    except ValueError as e:
        ok(label, (needle in str(e)) if needle else True, str(e)[:80])


# ── 1. 存储层 ────────────────────────────────────────────────
os.path.exists(PROV) and os.remove(PROV)

ok("没有文件时列表为空", provider_store.list_public()["providers"] == [])
r0 = provider_store.resolve()
ok("没有文件时回退到 .env 的 Key", r0["api_key"] == "sk-env-fallback-123456", r0["api_key"])
ok("回退时标记 source=env", r0["source"] == "env", r0["source"])

ok("地址没有版本号时补 /v1",
   provider_store._normalize_base("https://api.deepseek.com") == "https://api.deepseek.com/v1")
ok("已经带 /v4 的地址不再补 /v1（否则必然 404）",
   provider_store._normalize_base("https://api.z.ai/api/coding/paas/v4")
   == "https://api.z.ai/api/coding/paas/v4",
   provider_store._normalize_base("https://api.z.ai/api/coding/paas/v4"))
ok("已经带 /v1 的地址保持不变",
   provider_store._normalize_base("https://api.moonshot.cn/v1/") == "https://api.moonshot.cn/v1")

p1 = provider_store.upsert({"name": "DeepSeek", "base_url": "api.deepseek.com",
                            "api_key": "sk-abcdefghijklmnop", "model": "deepseek-chat"})
ok("新增后 id 由名称生成", p1["id"] == "deepseek", p1["id"])
ok("base_url 自动补 /v1", p1["base_url"] == "https://api.deepseek.com/v1", p1["base_url"])
ok("第一条自动成为当前使用", provider_store.list_public()["active_id"] == "deepseek")

pub = provider_store.list_public()
ok("列表里 Key 只回显头尾",
   pub["providers"][0]["key_hint"] == "sk-abc…mnop", pub["providers"][0]["key_hint"])
ok("列表里不出现完整 Key",
   "sk-abcdefghijklmnop" not in json.dumps(pub, ensure_ascii=False))

r1 = provider_store.resolve()
ok("resolve 拿到了存的那把 Key", r1["api_key"] == "sk-abcdefghijklmnop", r1["api_key"][:8])
ok("resolve 标记 source=store", r1["source"] == "store", r1["source"])

# 编辑时不重填 Key，原 Key 要保留（否则改个名字就把 Key 弄丢了）
provider_store.upsert({"id": "deepseek", "name": "DeepSeek 官方",
                       "base_url": "https://api.deepseek.com/v1", "api_key": ""})
ok("编辑不填 Key 时沿用原来的",
   provider_store.resolve()["api_key"] == "sk-abcdefghijklmnop")
ok("名字改掉了", provider_store.list_public()["providers"][0]["name"] == "DeepSeek 官方")

# 第二家
p2 = provider_store.upsert({"name": "中转站", "base_url": "https://relay.example.com/v1",
                            "api_key": "sk-relay-000011112222", "model": "deepseek-chat"})
ok("第二家有不同的 id", p2["id"] != p1["id"], p2["id"])
ok("新增不会抢占当前使用", provider_store.list_public()["active_id"] == "deepseek")

# 校验
expect_error(lambda: provider_store.upsert({"name": "  "}), "拒绝空名称", "名称")
expect_error(lambda: provider_store.upsert({"name": "x", "api_key": "sk-有 空格"}),
             "拒绝带空格的 Key", "Key")
expect_error(lambda: provider_store.upsert({"name": "x", "api_key": "short"}),
             "拒绝过短的 Key", "Key")

# 切换与 llm 接线
ok("切换到第二家", provider_store.set_active(p2["id"]))
ok("切换后 llm.api_key() 用新家的",
   llm.api_key() == "sk-relay-000011112222", llm.api_key())
ok("切换后 llm.base_url() 也跟着变",
   llm.base_url() == "https://relay.example.com/v1", llm.base_url())
ok("提供商优先于 .env（.env 里那把不该盖住它）",
   llm.api_key() != os.environ["DEEPSEEK_API_KEY"])

# 删除：当前使用被删后要自动落到剩下的那家
ok("删除当前使用那家", provider_store.delete(p2["id"]))
ok("删除后当前使用自动落到剩下那家",
   provider_store.list_public()["active_id"] == "deepseek")
ok("删除不存在的返回 False", provider_store.delete("nope") is False)

# 原子写：不该留下临时文件
# 只看本文件的临时副本：临时目录是共用的，别人留下的 .tmp 不算数
_base = os.path.basename(PROV)
leftovers = [f for f in os.listdir(os.path.dirname(PROV))
             if f.startswith(_base) and f.endswith(".tmp")]
ok("没有遗留 .tmp 临时文件", leftovers == [], leftovers)

# 坏文件容错
open(PROV, "w", encoding="utf-8").write("{ not json")
ok("坏文件按空处理", provider_store.list_public()["providers"] == [])
ok("坏文件被改名留证", os.path.exists(PROV + ".broken"))

# ── 2. 账号隔离（本次需求的核心）──────────────────────────────
# 换账号后不能用到上一个账号的 Key，也不能看到它。
provider_store.set_current_user("1")
provider_store.upsert({"name": "A家", "base_url": "https://a.example.com/v1",
                       "api_key": "sk-aaaaaaaaaaaaaaaa"})
ok("A 账号有自己的提供商", len(provider_store.list_public()["providers"]) == 1)
ok("A 账号拿到自己的 Key",
   provider_store.resolve()["api_key"] == "sk-aaaaaaaaaaaaaaaa",
   provider_store.resolve()["api_key"])

provider_store.set_current_user("2")
ok("B 账号看不到 A 的提供商（列表为空）",
   provider_store.list_public()["providers"] == [],
   provider_store.list_public()["providers"])
ok("B 账号用不到 A 的 Key（退回 .env）",
   provider_store.resolve()["api_key"] == os.environ["DEEPSEEK_API_KEY"],
   provider_store.resolve()["api_key"])

provider_store.set_current_user("1")
ok("切回 A 账号，Key 还在",
   provider_store.resolve()["api_key"] == "sk-aaaaaaaaaaaaaaaa")

provider_store.set_current_user(None)
ok("访客看不到账号的 Key（只退回 .env）",
   provider_store.resolve()["api_key"] == os.environ["DEEPSEEK_API_KEY"],
   provider_store.resolve()["api_key"])
ok("访客列表里没有账号的提供商",
   provider_store.list_public()["providers"] == [])

# 老版本（v1：整机一份）自动搬进访客桶，不丢配置。
# 用**独立的临时文件**做这件事：否则会把上面 user 1 的配置一起覆盖掉。
import json as _json  # noqa: E402
MIG = temp_file_path("_tmp_providers_v1.json")
provider_store.set_path(MIG)
io.open(MIG, "w", encoding="utf-8", newline="\n").write(_json.dumps({
    "version": 1, "active_id": "legacy",
    "providers": [{"id": "legacy", "name": "旧配置", "base_url": "https://old.example.com/v1",
                   "api_key": "sk-legacy-1234567890", "model": "deepseek-chat"}],
}, ensure_ascii=False))
provider_store.set_current_user(None)
ok("v1 配置迁移到访客桶，没丢",
   provider_store.resolve()["api_key"] == "sk-legacy-1234567890",
   provider_store.resolve()["api_key"])
provider_store.set_current_user("7")
ok("迁移后的旧配置不会串给登录账号",
   provider_store.resolve()["api_key"] != "sk-legacy-1234567890",
   provider_store.resolve()["api_key"])
provider_store.set_current_user(None)
provider_store.set_path(PROV)      # 切回主测试文件

# ── 3. 接口层（写操作要求登录）───────────────────────────────
os.remove(PROV)


def login_as(client, uid):
    """给 test client 塞一个登录态（Flask 的 session 是签名的，测试里可直接写）。"""
    with client.session_transaction() as sess:
        sess["user_id"] = uid


c = appmod.app.test_client()

# 未登录：能看（空列表 + 提示），不能改
r = c.get("/api/providers")
data = r.get_json() or {}
ok("未登录也能打开（返回空列表）", r.status_code == 200 and data.get("success"), r.status_code)
ok("未登录时标记 login_required", data.get("login_required") is True, data.get("login_required"))
ok("未登录时 editable=false", data.get("editable") is False, data.get("editable"))
r0 = c.post("/api/providers", json={"name": "x", "api_key": "sk-xxxxxxxxxxxx"})
ok("未登录不能新增（403）", r0.status_code == 403, r0.status_code)
ok("提示里说明 Key 绑定账号",
   "绑定账号" in ((r0.get_json() or {}).get("error") or ""),
   (r0.get_json() or {}).get("error"))

# 账号 1
c1 = appmod.app.test_client()
login_as(c1, 1)
r1 = c1.post("/api/providers", json={"name": "DeepSeek",
                                     "base_url": "https://api.deepseek.com/v1",
                                     "api_key": "sk-abcdefghijklmnop",
                                     "model": "deepseek-chat"})
d1 = r1.get_json() or {}
ok("登录后可以新增", r1.status_code == 200 and d1.get("success"), d1.get("error"))
ok("接口不回传完整 Key",
   "sk-abcdefghijklmnop" not in _json.dumps(d1, ensure_ascii=False))
ok("新增后列表有一家", len((d1.get("list") or {}).get("providers") or []) == 1)

# 账号 2：看不到账号 1 的配置
c2 = appmod.app.test_client()
login_as(c2, 2)
d2 = c2.get("/api/providers").get_json() or {}
ok("账号 2 看不到账号 1 的提供商",
   d2.get("providers") == [], d2.get("providers"))
ok("账号 2 的 editable 仍是 true（自己的可以改）", d2.get("editable") is True)

# 账号 2 加自己的，两家互不干扰
r2 = c2.post("/api/providers", json={"name": "中转", "base_url": "https://relay.example.com/v1",
                                     "api_key": "sk-2222222222222222"})
ok("账号 2 加自己的提供商", r2.status_code == 200, r2.status_code)
d1b = c1.get("/api/providers").get_json() or {}
ok("账号 1 的列表没被污染（仍 1 家）",
   len(d1b.get("providers") or []) == 1, d1b.get("providers"))

# 切换属主校验：账号 2 不能切/删账号 1 的 id
pid1 = (d1b.get("providers") or [{}])[0].get("id")
r3 = c2.post(f"/api/providers/{pid1}/activate")
ok("账号 2 不能切换账号 1 的提供商（404）", r3.status_code == 404, r3.status_code)
r4 = c2.delete(f"/api/providers/{pid1}")
ok("账号 2 不能删除账号 1 的提供商（404）", r4.status_code == 404, r4.status_code)

# 账号 1 自己可以
r5 = c1.post(f"/api/providers/{pid1}/activate")
ok("账号 1 可以切换自己的", r5.status_code == 200, r5.status_code)
r6 = c1.delete("/api/providers/nope")
ok("删除不存在的返回 404", r6.status_code == 404, r6.status_code)

# ── 3. 版本库边界 ────────────────────────────────────────────
import subprocess  # noqa: E402
out = subprocess.run(["git", "check-ignore", "providers.json"], cwd=ROOT,
                     capture_output=True, text=True).returncode
ok("providers.json 不进版本库（含真实 Key）", out == 0, out)

# ── 4. 界面结构（"提供商下拉 + 删掉说明文字"）──────────────────
html_src = io.open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
ok("弹窗里不再有那段说明文字",
   "providerHelp" not in html_src and "还没有 Key？" not in html_src)
ok("有提供商下拉（不用手背 Base URL）", 'id="pvPreset"' in html_src)
ok("下拉里含常见服务与「自定义」",
   all(k in html_src for k in ("value=\"deepseek\"", "value=\"openai\"",
                               "value=\"custom\"", "自定义")))
ok("Base URL 输入默认隐藏（选自定义才出现）",
   'id="pvBaseWrap"' in html_src and "pvBaseWrap\" style=\"display:none;\"" in html_src)
ok("选预设会自动带出名称/地址/模型",
   "PROVIDER_PRESETS" in html_src and "onProviderPresetChange" in html_src)
import re as _re  # noqa: E402
_catalog = _re.search(r"const PROVIDER_PRESETS = \{(.*?)\n        \};", html_src, _re.S)
_preset_count = len(_re.findall(r"^\s+'?[\w-]+'?:\s*\{ name:", (_catalog.group(1) if _catalog else ""), _re.M))
ok("预设已与 DSH 内置目录同步（≥20 家）", _preset_count >= 20, _preset_count)
ok("覆盖主流 OpenAI 兼容服务",
   all(k in html_src for k in ("openrouter", "groq", "nvidia", "together",
                               "fireworks", "cerebras", "huggingface", "baseten",
                               "xai", "zai", "xiaomi")))
ok("下拉按「常用 / 更多 / 本地」分组", html_src.count("<optgroup") >= 3,
   html_src.count("<optgroup"))
ok("编辑已有提供商时不会被预设覆盖（传 fill=false）",
   "onProviderPresetChange(false)" in html_src)

n = sum(1 for _, c_, _ in R if c_)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n}/{len(R)} checks passed")
sys.exit(0 if n == len(R) else 1)
