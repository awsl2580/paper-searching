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

# ── 2. 接口层 ────────────────────────────────────────────────
os.path.exists(PROV + ".broken") and os.remove(PROV + ".broken")
provider_store.upsert({"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
                       "api_key": "sk-abcdefghijklmnop", "model": "deepseek-chat"})

c = appmod.app.test_client()
r = c.get("/api/providers")
data = r.get_json() or {}
ok("GET /api/providers 成功", r.status_code == 200 and data.get("success"), r.status_code)
ok("接口返回提供商列表", len(data.get("providers") or []) == 1)
ok("接口不回传完整 Key",
   "sk-abcdefghijklmnop" not in json.dumps(data, ensure_ascii=False))
ok("本机请求允许修改", data.get("editable") is True, data.get("editable"))

r2 = c.post("/api/providers", json={"name": "本地 vLLM",
                                    "base_url": "http://127.0.0.1:8001/v1",
                                    "api_key": "sk-local-1234567890",
                                    "model": "qwen"})
d2 = r2.get_json() or {}
ok("POST 新增成功", r2.status_code == 200 and d2.get("success"), d2.get("error"))
ids = [p["id"] for p in (d2.get("list") or {}).get("providers", [])]
ok("新增后列表里有两家", len(ids) == 2, ids)

r3 = c.post("/api/providers", json={"name": ""})
ok("空名称返回 400", r3.status_code == 400, r3.status_code)

r4 = c.post(f"/api/providers/{ids[1]}/activate")
ok("切换接口成功", r4.status_code == 200 and (r4.get_json() or {}).get("success"))
ok("切换后 llm 用的是新家", llm.api_key() == "sk-local-1234567890", llm.api_key())

r5 = c.delete(f"/api/providers/{ids[1]}")
ok("删除接口成功", r5.status_code == 200 and (r5.get_json() or {}).get("success"))
ok("删除不存在的返回 404", c.delete("/api/providers/nope").status_code == 404)

# 公网（非回环）且未登录：只能看，不能改
c2 = appmod.app.test_client()
r6 = c2.post("/api/providers", json={"name": "hack", "api_key": "sk-xxxxxxxxxxxx"},
             environ_base={"REMOTE_ADDR": "203.0.113.9"})
ok("公网未登录不能新增（403）", r6.status_code == 403, r6.status_code)
r7 = c2.delete("/api/providers/deepseek", environ_base={"REMOTE_ADDR": "203.0.113.9"})
ok("公网未登录不能删除（403）", r7.status_code == 403, r7.status_code)

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
ok("编辑已有提供商时不会被预设覆盖（传 fill=false）",
   "onProviderPresetChange(false)" in html_src)

n = sum(1 for _, c_, _ in R if c_)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n}/{len(R)} checks passed")
sys.exit(0 if n == len(R) else 1)
