"""MCP 协议层测试 —— 服务端（mcp_server.py）与客户端（mcp_client.py）。

赛题鼓励"以 MCP 协议封装科学计算工具"。所以这里测的不是"有没有一个 HTTP 接口"，
而是**协议语义本身**：

* JSON-RPC 2.0 的握手 / 方法分发 / 错误码（-32600 / -32601 / -32700）；
* `tools/list` 用 `inputSchema`（MCP 规范字段名），不是 `parameters`；
* `tools/call` 的**工具失败**走 `isError=true`，不是 JSON-RPC error
  （这条是 MCP 与普通 RPC 最容易搞混的地方）；
* 两条传输（同进程 / stdio 子进程）行为一致 —— 外部 harness 挂的是后者；
* 通知类消息（无 id）不返回响应。

第 8 段会真的起一个 `python mcp_server.py` 子进程，所以它同时验证了
"外部 harness 能挂上"这件事。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 测试环境自洽：不依赖 .env，也不要有网络/限流副作用
os.environ.setdefault("FLASK_SECRET_KEY", "0123456789abcdef" * 4)   # 用例里不要因为限流而 sleep
os.environ.setdefault("MCP_TRANSPORT", "inproc")
os.environ.setdefault("AI4S_MAX_ITERATIONS", "1")
sys.path.insert(0, ROOT)
import paths  # noqa: E402,F401  （分类后的源码目录，见项目根 paths.py）

import mcp_client  # noqa: E402
import mcp_server  # noqa: E402

R = []


def ok(name, cond, got=None):
    R.append((name, bool(cond), got))


def rpc(method, params=None, msg_id=1, notify=False):
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notify:
        msg["id"] = msg_id
    return mcp_server.handle_request(msg)


# ── 1. 握手 ────────────────────────────────────────────────────────
init = rpc("initialize", {"protocolVersion": "2025-03-26",
                          "capabilities": {}, "clientInfo": {"name": "test"}})
ok("initialize 返回 result", "result" in init, list(init))
res = init["result"]
ok("握手回报协议版本", res.get("protocolVersion") == "2025-03-26", res.get("protocolVersion"))
ok("声明 tools 能力", "tools" in (res.get("capabilities") or {}), res.get("capabilities"))
ok("serverInfo 带 name/version",
   bool((res.get("serverInfo") or {}).get("name")) and bool(res["serverInfo"].get("version")),
   res.get("serverInfo"))
ok("服务端自述用途（instructions）", "科学" in (res.get("instructions") or ""),
   (res.get("instructions") or "")[:40])

# 客户端声明一个我们不认识的版本时，服务端回自己支持的版本（而不是报错）
init2 = rpc("initialize", {"protocolVersion": "1999-01-01"})
ok("未知协议版本回落到服务端支持的版本",
   init2["result"]["protocolVersion"] in mcp_server.SUPPORTED_PROTOCOLS,
   init2["result"]["protocolVersion"])

# 通知类消息：没有 id -> 不返回任何东西
ok("notifications/initialized 不返回响应",
   rpc("notifications/initialized", notify=True) is None)
ok("ping 返回空 result", rpc("ping")["result"] == {})

# ── 2. tools/list ─────────────────────────────────────────────────
tools_msg = rpc("tools/list")
tools = tools_msg["result"]["tools"]
ok("tools/list 有工具", len(tools) >= 28, len(tools))
names = [t["name"] for t in tools]
ok("每个工具都带 name/title/description/inputSchema",
   all(t.get("name") and t.get("title") and t.get("description") is not None
       and isinstance(t.get("inputSchema"), dict) for t in tools))
ok("字段名是 MCP 规范的 inputSchema（不是 parameters）",
   all("inputSchema" in t and "parameters" not in t for t in tools))
ok("工具名唯一", len(names) == len(set(names)), len(names) - len(set(names)))
for must in ("stats_ttest", "spectra_peaks", "curves_fit", "molecule_parse",
             "design_power", "table_parse", "search_papers", "get_paper_detail"):
    ok(f"注册了 {must}", must in names)
ok("科学计算工具占多数", sum(1 for n in names if not n.startswith(("search_", "get_"))) >= 26,
   len(names))

# ── 3. tools/call 正常路径 ────────────────────────────────────────
call = rpc("tools/call", {"name": "stats_ttest",
                          "arguments": {"x": [5.1, 4.9, 5.0, 5.2, 5.3],
                                        "y": [4.9, 4.8, 5.1, 4.7]}})
ok("tools/call 返回 content 数组", isinstance(call["result"].get("content"), list))
ok("content 是 text 类型", call["result"]["content"][0]["type"] == "text")
ok("正常计算 isError=false", call["result"]["isError"] is False, call["result"].get("isError"))
payload = json.loads(call["result"]["content"][0]["text"])
ok("工具返回里带真实数值", payload["result"]["p"] == 0.086464, payload["result"].get("p"))
ok("工具返回里带 repro（复现凭据）",
   payload["repro"]["tool"] == "stats_ttest" and "args" in payload["repro"], payload.get("repro"))
ok("同时提供 structuredContent（MCP 2025 规范）",
   isinstance(call["result"].get("structuredContent"), dict))

# 分子量这种"可核对"的数值也验一下（C6H6 = 78.114）
mol = rpc("tools/call", {"name": "molecule_parse", "arguments": {"smiles": "c1ccccc1"}})
mol_payload = json.loads(mol["result"]["content"][0]["text"])
ok("SMILES 解析出苯的分子式", mol_payload["result"]["formula"] == "C6H6",
   mol_payload["result"].get("formula"))
ok("分子量接近 78.11", abs(mol_payload["result"]["mw"] - 78.114) < 0.01,
   mol_payload["result"].get("mw"))

# ── 4. tools/call 失败路径：isError，不是 JSON-RPC error ──────────
bad = rpc("tools/call", {"name": "stats_ttest", "arguments": {"x": [1], "y": []}})
ok("参数不合法时 isError=true", bad["result"].get("isError") is True, bad["result"])
ok("参数不合法时不返回 JSON-RPC error", "error" not in bad, list(bad))
ok("失败原因可读（中文）",
   "error" in json.loads(bad["result"]["content"][0]["text"]),
   bad["result"]["content"][0]["text"][:80])

unknown = rpc("tools/call", {"name": "not_a_tool", "arguments": {}})
ok("未知工具也是 isError=true（不是协议错误）", unknown["result"].get("isError") is True)
ok("未知工具提示里点名了工具", "not_a_tool" in unknown["result"]["content"][0]["text"],
   unknown["result"]["content"][0]["text"])

# ── 5. 协议级错误码 ───────────────────────────────────────────────
ok("不支持的 method -> -32601",
   rpc("resources/subscribe")["error"]["code"] == -32601)
ok("缺少 method 也不该崩", rpc("nope")["error"]["code"] == -32601)
ok("非 JSON-RPC 2.0 -> -32600",
   mcp_server.handle_request({"id": 1, "method": "tools/list"})["error"]["code"] == -32600)
ok("tools/call 缺 name -> -32602",
   rpc("tools/call", {"arguments": {}})["error"]["code"] == -32602)
ok("resources/list 返回空列表（客户端常先问一句）",
   rpc("resources/list")["result"] == {"resources": []})
ok("prompts/list 返回空列表", rpc("prompts/list")["result"] == {"prompts": []})

# 批量请求
batch = mcp_server.handle_request([
    {"jsonrpc": "2.0", "id": 1, "method": "ping"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
])
ok("批量请求逐条响应（通知被跳过）",
   isinstance(batch, list) and len(batch) == 2, len(batch) if isinstance(batch, list) else batch)

# ── 6. 客户端（in-process 传输）──────────────────────────────────
cli = mcp_client.MCPClient(mode="inproc").connect()
ok("客户端握手成功", cli.connected is True)
ok("客户端拿到 serverInfo", cli.server.get("name") == mcp_server.SERVER_NAME, cli.server)
ok("客户端 tools/list 与协议层一致", len(cli.list_tools()) == len(names))
res = cli.call_tool("stats_describe", {"values": [1, 2, 3, 4]})
ok("客户端调用成功", res["ok"] is True, res.get("error"))
ok("客户端把 result 解包出来", res["result"]["result"]["mean"] == 2.5,
   res["result"].get("result"))
ok("客户端保留 is_error 字段", res.get("is_error") is False)
ok("status 报告传输类型", cli.status().get("transport") == "inproc", cli.status())
cli.close()

# ── 7. 客户端（stdio 子进程传输）——外部 harness 走的就是这条路 ────
try:
    stdio_cli = mcp_client.MCPClient(mode="stdio").connect()
    ok("stdio 子进程握手成功", stdio_cli.connected is True, stdio_cli.fallback_reason)
    ok("stdio 传输类型正确", stdio_cli.status().get("transport") == "stdio",
       stdio_cli.status())
    stdio_tools = stdio_cli.list_tools()
    ok("stdio 与 inproc 工具数一致", len(stdio_tools) == len(names),
       (len(stdio_tools), len(names)))
    r = stdio_cli.call_tool("curves_fit", {"x": [1, 2, 3, 4], "y": [3, 5, 7, 9],
                                           "model": "linear"})
    ok("stdio 通道能真算（线性拟合斜率≈2）",
       r["ok"] and abs(r["result"]["result"]["params"]["a"] - 2.0) < 1e-6,
       r.get("result"))
    stdio_cli.close()
except Exception as e:  # noqa: BLE001 - 受限环境不允许起子进程时明确失败（不静默跳过）
    ok("stdio 子进程传输可用", False, f"{type(e).__name__}: {e}")

# ── 8. 单例入口 ──────────────────────────────────────────────────
st = mcp_client.status()
ok("单例 status 可用", st.get("connected") is True, st)
ok("单例工具数与协议层一致", len(mcp_client.list_tools()) == len(names))
one = mcp_client.call_tool("stats_correlation", {"x": [1, 2, 3, 4], "y": [2, 4, 6, 8]})
ok("单例调用可用且 r=1", one["ok"] and abs(one["result"]["result"]["r"] - 1.0) < 1e-9,
   one.get("result"))
mcp_client.reset_client()

n_pass = sum(1 for _, c, _ in R if c)
for name, cond, got in R:
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  -> got: {got!r}"))
print(f"\n{n_pass}/{len(R)} checks passed")
sys.exit(0 if n_pass == len(R) else 1)
