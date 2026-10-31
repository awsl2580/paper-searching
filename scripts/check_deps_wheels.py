"""检查本项目依赖在 Python 3.14 上有没有现成 wheel（跑完即删）。

原理：查 PyPI 的 JSON 接口，看每个包最新版本的文件里有没有
  * `cp314-...win_amd64.whl`（3.14 已编译的 wheel）
  * `py3-none-any.whl`（纯 Python，任何版本都能装）
两者都没有 -> 3.14 上要现场编译（很可能失败，比如需要 Rust/C 工具链）。
"""
import json
import sys
import urllib.request

PKGS = [
    # requirements.txt 里的直接依赖
    "flask", "langchain", "langchain-core", "langchain-openai",
    "requests", "python-dotenv", "bcrypt", "pymupdf",
    "edge-tts", "aiohttp",
    # 间接但很容易卡住编译的
    "pydantic", "pydantic-core", "openai", "httpx", "anyio", "orjson",
    "tiktoken", "pyyaml", "regex", "uuid-utils", "zstandard", "jiter",
    "distro", "tenacity", "jsonpatch", "langgraph", "langsmith", "xxhash",
    "ormsgpack", "requests-toolbelt", "langchain-protocol", "langgraph-checkpoint",
    "langgraph-prebuilt", "langgraph-sdk", "annotated-types", "typing-extensions",
    "packaging", "certifi", "charset-normalizer", "idna", "urllib3",
]


def check(pkg):
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=25) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return pkg, "查询失败", f"{type(e).__name__}: {e}", ""
    version = data["info"]["version"]
    files = data.get("urls") or []
    names = [f["filename"] for f in files]
    pure = any(n.endswith("-py3-none-any.whl") or n.endswith("-py2.py3-none-any.whl")
               for n in names)
    cp314 = [n for n in names if "cp314" in n and "win_amd64" in n]
    if pure:
        return pkg, "纯 Python", version, ""
    if cp314:
        return pkg, "有 cp314 wheel", version, cp314[0]
    # 没有 win_amd64 的 cp314，看看有没有 abi3（cp39-abi3 之类，3.14 也能装）
    abi3 = [n for n in names if "abi3" in n and "win_amd64" in n]
    if abi3:
        return pkg, "有 abi3 wheel", version, abi3[0]
    return pkg, "⚠️ 无 3.14 wheel", version, names[:2]


def main():
    rows = [check(p) for p in PKGS]
    width = max(len(r[0]) for r in rows) + 2
    bad = []
    for pkg, status, version, extra in rows:
        flag = "❌" if status.startswith("⚠️") else ("?" if status == "查询失败" else "✅")
        print(f"{flag} {pkg:<{width}} {status:<14} 最新 {version:<12} {extra[:60]}")
        if status.startswith("⚠️"):
            bad.append(pkg)
    print()
    if bad:
        print("这些包在 3.14 上没有现成 wheel，需要源码编译：", ", ".join(bad))
        return 1
    print("全部依赖在 Python 3.14 上都有可用 wheel（纯 Python / cp314 / abi3）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
