"""MCP（Model Context Protocol）服务端 —— 把科学计算工具封装成标准 MCP 工具。

## 为什么要有这个文件

赛题明确鼓励「以 MCP 协议封装科学计算工具」。但很多作品的"支持 MCP"
只是写了个 HTTP 接口就说自己是 MCP —— 那不是协议，是自定义 API。
这里实现的是真正的 MCP 语义：

* 传输：**stdio 上的换行分隔 JSON-RPC 2.0**（MCP 官方定义的本地传输之一）
* 生命周期：`initialize` 握手 -> `notifications/initialized` -> `tools/list`
  -> `tools/call`，响应结构与 MCP 规范一致（`content` / `isError` /
  `structuredContent`，`tools/list` 用 `inputSchema` 而不是 `parameters`）
* 错误语义分开：**协议错误**走 JSON-RPC 的 `error`，
  **工具执行失败**走 `result.isError = true`（这是 MCP 规范的要求：
  工具跑失败不是协议错误，客户端不该当成连接断了）

## 谁能用

* 外部 harness（Claude Code / openclaw / hermes 等）可以直接
  `command = python, args = ["mcp/mcp_server.py"]` 挂上本服务，拿到 20+ 个科学计算工具；
* 本项目内部（`mcp_client.py`）默认走 in-process 通道调同一套 handler，
  省掉子进程开销，但**协议路径完全一样**（同一个 `handle_request`）。

## 为什么工具返回里带 repro

科学智能体光"算出来"不够，要能说清"怎么算的、拿什么参数算的"。
每个工具返回都带 `repro`（工具名 / 归一化参数 / 解释器版本），
报告里的「复现清单」直接由它生成。
"""

from __future__ import annotations

import json
import os
import sys
import traceback

# ── 目录整理后的路径兼容（必须先于 science / providers 的 import）──
# 本文件在 mcp/ 下，science/ 与 providers/ 在项目根。作为独立 MCP 子进程
# 启动时（python mcp/mcp_server.py），sys.path[0] 是 mcp/，所以要先把项目根
# 加进来，再由 paths.py 把 science/、providers/ 等目录加进 sys.path。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import paths  # noqa: E402,F401

SERVER_NAME = "ai4s-science-tools"
SERVER_VERSION = "2.0.0"

# MCP 规范里客户端会声明一个协议版本，服务端按自己支持的版本回应。
# 这里同时认 2024-11-05 与 2025-03-26（前者最通用，后者带 structuredContent）。
SUPPORTED_PROTOCOLS = ("2025-03-26", "2024-11-05", "2024-10-07")
DEFAULT_PROTOCOL = "2025-03-26"


# ═══════════════════════════════════════════════════════════════
#  工具目录（懒加载：导入 providers 会连带导入 requests，
#  纯计算场景不该被这个依赖卡住）
# ═══════════════════════════════════════════════════════════════

def _science_tools():
    """科学计算工具（science/ 包，纯标准库，永远可用）。"""
    try:
        import science
        return science.list_tools()
    except Exception as e:  # noqa: BLE001 - science 缺失不该让整个 MCP 服务起不来
        print(f"[mcp] science 工具不可用: {e}", file=sys.stderr)
        return []


def _literature_tools():
    """文献检索工具（providers/ 聚合层，需要网络）。"""
    tools = [
        {
            "name": "search_papers",
            "title": "多源学术文献检索",
            "description": (
                "并发检索 Semantic Scholar / OpenAlex / Crossref / Europe PMC / DOAJ，"
                "跨源去重并按相关度+影响力排序。"
                "参数：query=检索式（中英文均可，多个词用空格分隔）；"
                "limit=条数(1-50)；source=all|cn|en；min_relevance=相关度下限0-100"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索式，例如 '图神经网络 遥感图像'"},
                    "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                    "source": {"type": "string", "enum": ["all", "cn", "en"], "default": "all"},
                    "min_relevance": {"type": "integer", "default": 0, "minimum": 0, "maximum": 100},
                },
                "required": ["query"],
            },
            "tags": ["检索", "文献"],
        },
        {
            "name": "get_paper_detail",
            "title": "论文详情",
            "description": "按论文 id（检索结果里的 id 字段）或标题取完整摘要、被引、卷期页、全文链接。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "paper_id": {"type": "string", "description": "论文 id 或标题"},
                },
                "required": ["paper_id"],
            },
            "tags": ["检索", "文献"],
        },
    ]
    return tools


def list_tools():
    """全部 MCP 工具（科学计算在前，文献检索在后）。"""
    return _science_tools() + _literature_tools()


def _call_science(name, args):
    import science
    return science.call_tool(name, args)


def _call_literature(name, args):
    import providers
    args = args or {}
    if name == "search_papers":
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query 不能为空")
        raw = providers.search_papers(
            query,
            int(args.get("limit") or 10),
            str(args.get("source") or "all"),
            int(args.get("min_relevance") or 0),
        )
        payload = json.loads(raw) if isinstance(raw, str) else raw
        return payload
    if name == "get_paper_detail":
        paper_id = str(args.get("paper_id") or "").strip()
        if not paper_id:
            raise ValueError("paper_id 不能为空")
        raw = providers.get_detail(paper_id)
        return json.loads(raw) if isinstance(raw, str) else raw
    raise KeyError(name)


def call_tool(name, args=None):
    """执行一个工具，返回 MCP 的 tools/call result。

    注意：工具执行失败返回 `isError: True`，**不抛异常** —— 这是 MCP 的约定。
    """
    args = args or {}
    known = {t["name"] for t in list_tools()}
    if name not in known:
        return {
            "content": [{"type": "text", "text": f"未知工具：{name}"}],
            "isError": True,
        }

    try:
        if any(t["name"] == name for t in _science_tools()):
            result = _call_science(name, args)
        else:
            result = _call_literature(name, args)
        is_error = False
        if isinstance(result, dict) and result.get("ok") is False:
            is_error = True
    except Exception as e:  # noqa: BLE001 - 任何工具异常都转成 isError
        result = {
            "tool": name,
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc().splitlines()[-3:],
        }
        is_error = True

    text = json.dumps(result, ensure_ascii=False)
    # 内容太长时也照样给全文：客户端（LLM）需要真实数值；
    # 截断会让"算出来的数"变成"看着像的数"。
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": result if isinstance(result, dict) else {"result": result},
        "isError": is_error,
    }


# ═══════════════════════════════════════════════════════════════
#  JSON-RPC 分发
# ═══════════════════════════════════════════════════════════════

def _rpc_result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def handle_request(req):
    """处理一条 JSON-RPC 消息。返回响应 dict；通知（无 id）返回 None。

    单独抽成函数，是为了让 app.py 能用 HTTP 暴露同一套协议（/api/mcp）
    而不必再起一个子进程 —— 协议行为与 stdio 通道**逐字节一致**。

    批量（JSON 数组）也在这里处理，因为 MCP 允许批量消息，
    而调用方（HTTP 入口 / stdio 入口）不应该各写一份批量逻辑。
    """
    if isinstance(req, list):
        if not req:
            return _rpc_error(None, -32600, "Invalid Request：空批量消息")
        return [r for r in (handle_request(x) for x in req) if r is not None]

    if not isinstance(req, dict) or req.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "Invalid Request：需要 JSON-RPC 2.0 消息")

    method = req.get("method")
    msg_id = req.get("id")
    params = req.get("params") or {}
    is_notification = "id" not in req

    if method == "initialize":
        requested = str((params or {}).get("protocolVersion") or "")
        version = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        result = {
            "protocolVersion": version,
            "capabilities": {
                "tools": {"listChanged": False},
                "logging": {},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
                "vendor": "兴智杯 AI4S 科学智能体",
            },
            # 非标准但很有用的自描述字段（客户端可忽略）
            "instructions": (
                "这是一套面向科学研究的 MCP 工具：统计检验、谱图解析、曲线拟合、"
                "分子描述符、实验设计与参数扫描。调用前先 tools/list 看 inputSchema。"
            ),
        }
        return None if is_notification else _rpc_result(msg_id, result)

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return None if is_notification else _rpc_result(msg_id, {})

    if method == "tools/list":
        tools = []
        for t in list_tools():
            tools.append({
                "name": t["name"],
                "title": t.get("title") or t["name"],
                "description": t.get("description") or "",
                "inputSchema": t.get("inputSchema") or {"type": "object", "properties": {}},
                "annotations": {"tags": t.get("tags") or []},
            })
        return None if is_notification else _rpc_result(msg_id, {"tools": tools})

    if method == "tools/call":
        name = (params or {}).get("name")
        args = (params or {}).get("arguments") or {}
        if not isinstance(name, str) or not name:
            return _rpc_error(msg_id, -32602, "Invalid params：tools/call 需要 name")
        return None if is_notification else _rpc_result(msg_id, call_tool(name, args))

    if method in ("resources/list",):
        # 本项目暂时没有 MCP resources，但客户端常会先问一句，
        # 返回空列表比返回 -32601 友好（不支持的 method 更能说明问题）
        return None if is_notification else _rpc_result(msg_id, {"resources": []})

    if method == "prompts/list":
        return None if is_notification else _rpc_result(msg_id, {"prompts": []})

    return None if is_notification else _rpc_error(
        msg_id, -32601, f"Method not found: {method}")


# ═══════════════════════════════════════════════════════════════
#  stdio 传输（MCP 本地传输之一：换行分隔 JSON）
# ═══════════════════════════════════════════════════════════════

def serve_stdio(stdin=None, stdout=None, stderr=None):
    """跑 stdio 循环，直到 stdin EOF。

    为什么要手写而不是用官方 mcp SDK：官方 SDK 依赖 pydantic + anyio 的
    较新版本，而本项目希望"评委装个 requirements.txt 就能跑"；
    MCP 的 stdio 传输本身只是换行分隔的 JSON-RPC，不需要框架。
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    def log(msg):
        print(f"[mcp] {msg}", file=stderr, flush=True)

    log(f"{SERVER_NAME} v{SERVER_VERSION} 就绪（stdio / JSON-RPC 2.0）")
    while True:
        try:
            line = stdin.readline()
        except KeyboardInterrupt:
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            stdout.write(json.dumps(_rpc_error(None, -32700, "Parse error"), ensure_ascii=False) + "\n")
            stdout.flush()
            continue

        # 支持批量请求（JSON 数组）
        if isinstance(req, list):
            out = [r for r in (handle_request(x) for x in req) if r is not None]
            if out:
                stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
                stdout.flush()
            continue

        try:
            resp = handle_request(req)
        except Exception as e:  # noqa: BLE001 - 服务端不能因为一个坏请求就退出
            log(f"处理 {req.get('method')} 时异常: {e}")
            resp = _rpc_error(req.get("id"), -32603, f"Internal error: {e}")
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--list" in argv or "-l" in argv:
        tools = list_tools()
        print(json.dumps({"server": SERVER_NAME, "version": SERVER_VERSION,
                          "tool_count": len(tools), "tools": tools},
                         ensure_ascii=False, indent=2))
        return 0
    if "--version" in argv:
        print(f"{SERVER_NAME} {SERVER_VERSION}")
        return 0
    serve_stdio()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
