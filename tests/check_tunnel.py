"""一键体检：本地服务 + 公网隧道，看到底卡在哪一环。

用法：
    python check_tunnel.py                    # 用默认的本地端口与域名
    python check_tunnel.py 8080               # 指定本地端口
    python check_tunnel.py 8080 你的域名       # 域名也自己给

它按链路顺序逐段检查，第一个断掉的环节就是问题所在：
    ① 本地端口有没有服务        -> 没有就先把 python app.py 跑起来
    ② 本机有没有 natapp 客户端   -> 没有就双击 内网穿透\\run_natapp.bat
    ③ 公网域名能不能通          -> 502 说明隧道没连到本机这个端口
"""
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "8080"
DOMAIN = sys.argv[2] if len(sys.argv) > 2 else "nd4c2f66.natappfree.cc"
BASE = f"http://127.0.0.1:{PORT}"
PUBLIC = f"http://{DOMAIN}"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

OK = "  [OK]  "
BAD = "  [!!]  "


def check_local():
    try:
        with opener.open(BASE + "/api/health", timeout=6) as r:
            print(OK + f"本地 {BASE} 有服务（HTTP {r.status}）")
            return True
    except Exception as e:
        print(BAD + f"本地 {BASE} 没有服务：{type(e).__name__}")
        print("        解决：cd AIagent2\\AIagent1 然后  set APP_PORT=" + PORT + " && python app.py")
        return False


def check_client():
    """本机有没有 natapp 进程。"""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq natapp.exe"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=20).stdout or ""
        if "natapp.exe" in out.lower():
            print(OK + "本机 natapp 客户端在运行")
            return True
        print(BAD + "本机没有 natapp 客户端在跑")
        print("        解决：双击 C:\\code\\论文助手\\内网穿透\\run_natapp.bat")
        print("        看到 Tunnel Status  Online  才算成功")
        return False
    except Exception as e:
        print("  [??]  查不到 natapp 进程：" + str(e)[:60])
        return None


def check_public():
    print(f"  [..]  正在访问 {PUBLIC} …")
    try:
        with opener.open(PUBLIC + "/api/health", timeout=25) as r:
            body = r.read().decode("utf-8", "replace")
            print(OK + f"公网可用！HTTP {r.status} -> {body.strip()[:60]}")
            print(f"        现在可以在浏览器打开：{PUBLIC}")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if e.code == 502:
            print(BAD + "公网 502：natapp 收到了请求，但连不到本机的后端端口")
            print("        最常见原因，按顺序排查：")
            print(f"          a) 隧道里配的“本地端口”和 {PORT} 不一致 -> 改成一致，")
            print(f"             或者用 set APP_PORT=<隧道里那个数字> 重启服务")
            print("          b) 这条隧道正被**另一台机器**上的 natapp 客户端占用。")
            print("             症状：natapp 启动时报「已有相同隧道正在运行」。")
            print("             先在那台机器上关掉它，本机才能接管这条隧道")
            print("          c) 隧道类型不是 Web（TCP 隧道不会转发 HTTP 请求头）")
        elif e.code == 404:
            print(OK + "公网通了（404 是正常的：这个路径本来就不存在）")
        else:
            print(BAD + f"公网 HTTP {e.code}")
        if "Web服务错误" in body or "服务错误" in body:
            print("        （这是 natapp 自己的错误页，说明请求到得了 natapp、到不了本机）")
        return False
    except Exception as e:
        print(BAD + f"公网连不上：{type(e).__name__}: {str(e)[:80]}")
        return False


def check_dns():
    try:
        ips = sorted({i[4][0] for i in socket.getaddrinfo(DOMAIN, 80, socket.AF_INET)})
        print(OK + f"域名解析正常：{DOMAIN} -> {ips}")
        return True
    except Exception as e:
        print(BAD + f"域名解析失败：{e}")
        return False


print("=" * 62)
print(f"公网链路体检   本地端口={PORT}   域名={DOMAIN}")
print("=" * 62)
print("\n① 本地服务")
local = check_local()
print("\n② natapp 客户端")
client = check_client()
print("\n③ 域名解析")
check_dns()
print("\n④ 公网访问")
public = check_public()

print("\n" + "=" * 62)
if public:
    print("结论：链路已通 ✅")
elif not local:
    print("结论：先把本地服务跑起来（见上面 ①）")
elif client is False:
    print("结论：本机没跑 natapp 客户端（见上面 ②）")
else:
    print("结论：客户端在跑但公网 502 —— 看 ④ 里的 a/b/c 三条")
print("=" * 62)
