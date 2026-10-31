"""MCP 客户端 —— 让项目内部也走 MCP 协议调工具，而不是直接 import。

## 为什么内部调用还要绕一层协议

直接把 `science.call_tool()` 写进工作流会更省事，但那样"我们支持 MCP"
就只是一句装饰。这里让**执行层真的走协议**：

    workflow.step_compute()
        -> mcp_client.get_client().call_tool("stats_ttest", {...})
            -> 传输层（in-process 或 stdio 子进程）
                -> mcp_server.handle_request()  # 与外部 harness 完全同一条路径

好处有三个，都是实际的：

1. **同一套协议既能被外部 harness 挂载，也能被自己用** —— 不存在
   "对外的 MCP 服务"和"内部用的 Python 函数"两套实现互相漂移；
2. 走子进程时（`MCP_TRANSPORT=stdio`）就算某个工具把解释器搞崩了，
   主进程也只是收到一个超时/断开，而不是整站 500；
3. `tools/call` 的 `isError` 语义强制我们把"工具失败"当数据往下传，
   而不是让它变成异常把整条工作流打断。

## 两种传输

* `inproc`（默认）：同进程直接调 `handle_request`。零开销，便于测试。
* `stdio`：真的起一个 `python mcp_server.py` 子进程，换行分隔 JSON-RPC。
  这是 MCP 的本地传输方式，也是外部 harness（Claude Code 等）用的那条路。
* `auto`：先试 stdio，握手失败自动退回 inproc，并把原因记在 `status()` 里
  —— 有些受限环境禁子进程，这时候不该让整个应用起不来。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "ai4s-agent", "version": "2.0.0"}
DEFAULT_TIMEOUT = float(os.getenv("MCP_TIMEOUT", "60") or 60)

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_SCRIPT = os.path.join(HERE, "mcp_server.py")


class MCPError(RuntimeError):
    """协议层错误（不是工具执行失败）。"""


def _transport_mode():
    return (os.getenv("MCP_TRANSPORT") or "inproc").strip().lower()


# ═══════════════════════════════════════════════════════════════
#  in-process 传输
# ═══════════════════════════════════════════════════════════════

class InProcessTransport:
    """同进程传输：直接用服务端的 `handle_request`。"""

    kind = "inproc"

    def __init__(self):
        import mcp_server
        self._server = mcp_server
        self._id = 0
        self._lock = threading.Lock()
        self._closed = False

    def request(self, method, params=None, timeout=DEFAULT_TIMEOUT):
        with self._lock:
            self._id += 1
            msg_id = self._id
        resp = self._server.handle_request({
            "jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {},
        })
        if resp is None:
            raise MCPError(f"方法 {method} 没有响应（可能是通知类方法）")
        if "error" in resp:
            err = resp["error"] or {}
            raise MCPError(f"{err.get('message')}（code={err.get('code')}）")
        return resp.get("result") or {}

    def notify(self, method, params=None):
        self._server.handle_request({
            "jsonrpc": "2.0", "method": method, "params": params or {},
        })

    def close(self):
        self._closed = True


# ═══════════════════════════════════════════════════════════════
#  stdio 子进程传输
# ═══════════════════════════════════════════════════════════════

class StdioTransport:
    """真·子进程传输：换行分隔 JSON-RPC，stdout 只跑协议、日志走 stderr。"""

    kind = "stdio"

    def __init__(self, script=SERVER_SCRIPT, python=None, timeout=DEFAULT_TIMEOUT):
        self.script = script
        self.python = python or sys.executable
        self.timeout = timeout
        self._id = 0
        self._id_lock = threading.Lock()
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._closed = False
        self.proc = None
        self._reader = None
        self._stderr_tail = []
        self._start()

    # ── 生命周期 ──────────────────────────────────────────
    def _start(self):
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUNBUFFERED", "1")
        self.proc = subprocess.Popen(
            [self.python, "-u", self.script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, cwd=HERE, env=env,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        threading.Thread(target=self._err_loop, daemon=True).start()

    def _err_loop(self):
        try:
            for line in self.proc.stderr:
                line = (line or "").rstrip()
                if not line:
                    continue
                self._stderr_tail.append(line)
                del self._stderr_tail[:-40]
        except Exception:  # noqa: BLE001 - 子进程退出时读管道会报错，忽略
            pass

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = (line or "").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if isinstance(msg, list):
                    for one in msg:
                        self._dispatch(one)
                    continue
                self._dispatch(msg)
        except Exception:  # noqa: BLE001
            pass
        finally:
            # 进程没了：把所有等待中的请求一次性失败掉，别让调用方干等
            with self._pending_lock:
                for box in self._pending.values():
                    box["error"] = MCPError("MCP 子进程已退出")
                    box["event"].set()
                self._pending.clear()

    def _dispatch(self, msg):
        if not isinstance(msg, dict):
            return
        msg_id = msg.get("id")
        if msg_id is None:
            return
        with self._pending_lock:
            box = self._pending.pop(msg_id, None)
        if box is None:
            return
        box["msg"] = msg
        box["event"].set()

    # ── 协议 ──────────────────────────────────────────────
    def request(self, method, params=None, timeout=None):
        if self._closed or self.proc is None or self.proc.poll() is not None:
            raise MCPError("MCP 子进程不可用")
        with self._id_lock:
            self._id += 1
            msg_id = self._id
        box = {"event": threading.Event(), "msg": None, "error": None}
        with self._pending_lock:
            self._pending[msg_id] = box
        payload = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        try:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except Exception as e:  # noqa: BLE001
            with self._pending_lock:
                self._pending.pop(msg_id, None)
            raise MCPError(f"写入 MCP 子进程失败：{e}") from e

        if not box["event"].wait(timeout or self.timeout):
            with self._pending_lock:
                self._pending.pop(msg_id, None)
            raise MCPError(f"MCP 请求超时（{method}，{timeout or self.timeout:.0f}s）。"
                           f"子进程日志：{self.stderr_tail()[-1:] or '（无）'}")
        if box["error"]:
            raise box["error"]
        msg = box["msg"] or {}
        if "error" in msg:
            err = msg["error"] or {}
            raise MCPError(f"{err.get('message')}（code={err.get('code')}）")
        return msg.get("result") or {}

    def notify(self, method, params=None):
        if self._closed or self.proc is None or self.proc.poll() is not None:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        try:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except Exception:  # noqa: BLE001
            pass

    def stderr_tail(self):
        return list(self._stderr_tail)

    def close(self):
        self._closed = True
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:  # noqa: BLE001
            pass


# ═══════════════════════════════════════════════════════════════
#  客户端
# ═══════════════════════════════════════════════════════════════

class MCPClient:
    """按 MCP 生命周期使用的客户端：握手 -> tools/list -> tools/call。"""

    def __init__(self, transport=None, mode=None):
        self.mode = (mode or _transport_mode()).lower()
        self.transport = transport
        self.server = {}
        self.connected = False
        self.fallback_reason = ""
        self._tools = None
        self._lock = threading.Lock()

    # ── 连接 ──────────────────────────────────────────────
    def connect(self):
        if self.connected:
            return self
        order = {"inproc": ["inproc"], "stdio": ["stdio"], "auto": ["stdio", "inproc"]}
        for kind in order.get(self.mode, ["inproc"]):
            try:
                tr = InProcessTransport() if kind == "inproc" else StdioTransport()
                self.transport = tr
                init = tr.request("initialize", {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"roots": {"listChanged": False}},
                    "clientInfo": CLIENT_INFO,
                })
                tr.notify("notifications/initialized")
                self.server = init.get("serverInfo") or {}
                self.server["protocolVersion"] = init.get("protocolVersion")
                self.connected = True
                self.mode = kind
                return self
            except Exception as e:  # noqa: BLE001 - 换下一种传输
                self.fallback_reason = f"{kind} 握手失败：{type(e).__name__}: {e}"
                try:
                    if self.transport:
                        self.transport.close()
                except Exception:  # noqa: BLE001
                    pass
                self.transport = None
                continue
        raise MCPError("MCP 传输全部不可用。" + (self.fallback_reason or ""))

    # ── 能力 ──────────────────────────────────────────────
    def list_tools(self, refresh=False):
        self.connect()
        with self._lock:
            if self._tools is None or refresh:
                result = self.transport.request("tools/list", {})
                self._tools = result.get("tools") or []
            return list(self._tools)

    def call_tool(self, name, arguments=None, timeout=DEFAULT_TIMEOUT):
        """调用工具，返回**统一形状**：

        {"tool","ok","result","error","is_error","raw"}
        `ok` 为 False 时 `error` 一定有中文原因。
        """
        self.connect()
        try:
            result = self.transport.request(
                "tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)
        except MCPError as e:
            return {"tool": name, "ok": False, "result": None, "error": str(e),
                    "is_error": True, "raw": None}

        is_error = bool(result.get("isError"))
        structured = result.get("structuredContent")
        payload = structured
        if not isinstance(payload, dict):
            text = ""
            for part in (result.get("content") or []):
                if isinstance(part, dict) and part.get("type") == "text":
                    text += part.get("text") or ""
            try:
                payload = json.loads(text) if text.strip() else {}
            except Exception:  # noqa: BLE001
                payload = {"text": text}

        ok = (not is_error) and (not isinstance(payload, dict) or payload.get("ok") is not False)
        error = None
        if isinstance(payload, dict):
            error = payload.get("error")
        if ok is False and not error:
            error = "工具执行失败（未返回原因）"
        return {"tool": name, "ok": bool(ok), "result": payload,
                "error": error, "is_error": is_error, "raw": result}

    def status(self):
        info = {
            "connected": self.connected,
            "mode": self.mode,
            "transport": getattr(self.transport, "kind", None),
            "server": self.server,
            "fallback_reason": self.fallback_reason,
            "tool_count": len(self._tools or []),
        }
        if isinstance(self.transport, StdioTransport):
            info["stderr_tail"] = self.transport.stderr_tail()[-5:]
        return info

    def close(self):
        if self.transport:
            try:
                self.transport.close()
            except Exception:  # noqa: BLE001
                pass
        self.transport = None
        self.connected = False
        self._tools = None


# ═══════════════════════════════════════════════════════════════
#  进程级单例
# ═══════════════════════════════════════════════════════════════

_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def get_client():
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = MCPClient()
        return _CLIENT


def reset_client(client=None):
    """测试/热更换传输用。传 client 就直接换成它。"""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            try:
                _CLIENT.close()
            except Exception:  # noqa: BLE001
                pass
        _CLIENT = client


def call_tool(name, arguments=None, timeout=DEFAULT_TIMEOUT):
    """便捷入口：走单例客户端。"""
    return get_client().call_tool(name, arguments, timeout=timeout)


def list_tools(refresh=False):
    return get_client().list_tools(refresh=refresh)


def status():
    try:
        client = get_client()
        client.connect()
        return client.status()
    except Exception as e:  # noqa: BLE001
        return {"connected": False, "mode": _transport_mode(),
                "error": f"{type(e).__name__}: {e}"}


def warm_up():
    """启动时预热（可选）：握手 + 拉一次工具清单。

    失败**不抛**：MCP 起不来不代表整个站点不能用 —— 只是"科学计算"面板
    会显示不可用，聊天和工作流其余步骤照常。
    """
    t0 = time.time()
    try:
        client = get_client()
        client.connect()
        tools = client.list_tools(refresh=True)
        return {"ok": True, "tools": len(tools), "mode": client.mode,
                "elapsed_ms": round((time.time() - t0) * 1000, 1)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "elapsed_ms": round((time.time() - t0) * 1000, 1)}


if __name__ == "__main__":
    # 手动验证：python mcp_client.py [工具名] [JSON 参数]
    args = sys.argv[1:]
    if not args:
        print(json.dumps({"status": status(),
                          "tools": [t["name"] for t in list_tools(refresh=True)]},
                         ensure_ascii=False, indent=2))
    else:
        name = args[0]
        payload = json.loads(args[1]) if len(args) > 1 else {}
        print(json.dumps(call_tool(name, payload), ensure_ascii=False, indent=2))
