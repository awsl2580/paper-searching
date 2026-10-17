"""Run every test suite and print one summary line each.

用法（在项目根目录执行）:
    python tests/run_all.py

设计说明：这些用例**不需要 pytest**，故意只用标准库。
每个文件都是独立脚本，跑完打印 "N/M checks passed" 并以退出码表示成败，
这样在没装任何测试框架的机器上（比如评委的环境）也能直接验证。
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 自己的输出也统一成 UTF-8：Windows 上重定向到文件时默认是 GBK，
# 打印 "▶" 这类符号会直接抛 UnicodeEncodeError，整个 runner 挂掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PY_TESTS = [
    ("编排器（规划/五步/GB-T 7714/中断/恢复）", "test_workflow.py"),
    ("工作流 API（SSE/落库/续跑/鉴权）", "test_workflow_api.py"),
    ("中断与断点续跑（轮询起点语义）", "test_interrupt_resume.py"),
    ("对话 -> 工作流任务移交工具", "test_agent_handoff.py"),
    ("聊天接口（SSE/论文卡片/任务移交/回答非空）", "test_chat_route.py"),
    ("新数据源（Crossref/Europe PMC/DOAJ 字段归一化）", "test_new_sources.py"),
    ("工作流流式推送与抽取并发", "test_streaming.py"),
    ("端到端冒烟（静态白名单/鉴权/跨用户隔离）", "test_smoke.py"),
    ("升级检查（给老库补新表）", "test_migration.py"),
    ("公网暴露前安全检查（源码不外泄/密钥/口令门禁）", "test_exposure.py"),
    ("图片与 PDF 附件（校验/渲染/多模态装载/不落盘）", "test_attachments.py"),
    ("账号持久化（注册一次，以后直接登录）", "test_accounts.py"),
]
JS_TESTS = [
    ("前端参考文献作者格式（与后端规则一致）", "test_frontend_authors.js"),
    ("搜索结果折叠 + 翻页（分页数学与折叠状态）", "test_pagination.js"),
    ("弹窗拖动 + 缩放（指针移出也跟随 / 尺寸夹取）", "test_drag.js"),
    ("未登录提示 + 去登录 + 登录后自动打开面板", "test_auth_ui.js"),
    ("对话记录立即可见（不等回答结束）", "test_conversation_sidebar.js"),
    ("前端附件（选择/校验/预览/属性转义）", "test_attachments_frontend.js"),
]

# 子进程统一给一份"够用的"环境：
#   * 没配 .env 的机器上，app.py 缺 FLASK_SECRET_KEY / DEEPSEEK_API_KEY 会直接报错；
#   * Windows 上子进程的 stdout 默认是 GBK，打印带 emoji 的启动信息会抛
#     UnicodeEncodeError —— 这里用 PYTHONIOENCODING 统一成 utf-8。
CHILD_ENV = dict(os.environ)
CHILD_ENV.setdefault("DEEPSEEK_API_KEY", "sk-dummy")
CHILD_ENV.setdefault("FLASK_SECRET_KEY", "test-secret-key-" + "0" * 40)
CHILD_ENV["PYTHONIOENCODING"] = "utf-8"

failures = []
print("=" * 68)
for title, fname in PY_TESTS:
    print(f"\n▶ {title}\n  {fname}")
    r = subprocess.run([sys.executable, os.path.join(HERE, fname)],
                       cwd=ROOT, capture_output=True, text=True, env=CHILD_ENV,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    tail = [ln for ln in out.splitlines() if ln.startswith("FAIL") or "checks passed" in ln]
    for ln in tail:
        print("  " + ln)
    if r.returncode != 0:
        failures.append(fname)
        if not tail:
            print("  " + out.strip()[-800:])

for title, fname in JS_TESTS:
    print(f"\n▶ {title}\n  {fname}")
    try:
        r = subprocess.run(["node", os.path.join(HERE, fname)],
                           cwd=ROOT, capture_output=True, text=True, env=CHILD_ENV,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print("  SKIP  未安装 node")
        continue
    out = (r.stdout or "") + (r.stderr or "")
    tail = [ln for ln in out.splitlines() if ln.startswith("FAIL") or "checks passed" in ln]
    for ln in tail:
        print("  " + ln)
    if r.returncode != 0:
        failures.append(fname)
        if not tail:
            print("  " + out.strip()[-800:])

print("\n" + "=" * 68)

# 测试用的临时库现在统一放在"确认可写"的目录里（见 tests/_writable_temp.py），
# 不再落在 tests/ 下。这里把两处都清一遍，保证工作区不留产物。
for d in {HERE, os.path.dirname(os.path.abspath(__file__))}:
    try:
        names = os.listdir(d)
    except OSError:
        continue
    for f in names:
        if f.startswith("_tmp_") and f.endswith(".db"):
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass

if failures:
    print("失败:", ", ".join(failures))
    sys.exit(1)
print("全部通过 ✅")
