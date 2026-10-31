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

PY_TESTS = [
    ("AI4S 闭环（九步 / 假设—实验—算—验—迭代 / 篡改检测 / 降级）", "test_ai4s_loop.py"),
    ("科学计算工具（统计检验 / 谱图 / 拟合 / 分子 / 实验设计）", "test_science_tools.py"),
    ("MCP 协议（握手 / tools / 两种传输 / 错误语义）", "test_mcp.py"),
    ("长周期记忆（可解释打分 / 预算 / 归档 / 记忆≠证据）", "test_memory.py"),
    ("DeepSeek 模型层（两个模型 / 思考切换 / JSON 容错 / 自动退回）", "test_llm.py"),
    ("编排器（规划/九步/GB-T 7714/中断/恢复）", "test_workflow.py"),
    ("工作流 API（SSE/落库/续跑/鉴权）", "test_workflow_api.py"),
    ("中断与断点续跑（轮询起点语义）", "test_interrupt_resume.py"),
    ("对话 -> 工作流任务移交工具", "test_agent_handoff.py"),
    ("聊天接口（SSE/论文卡片/任务移交/回答非空）", "test_chat_route.py"),
    ("账号持久化（注册即登录/重启后仍可登录/记住我/本机账号列表）", "test_local_accounts.py"),
    ("账号管理（改用户名/改密码/改头像/未登录拦截）", "test_account_settings.py"),
    ("账号文件存储（accounts.json：落盘/哈希/迁移/原子写）", "test_accounts_file.py"),
    ("新数据源（Crossref/Europe PMC/DOAJ 字段归一化）", "test_new_sources.py"),
    ("工作流流式推送与抽取并发", "test_streaming.py"),
    ("端到端冒烟（静态白名单/鉴权/跨用户隔离）", "test_smoke.py"),
    ("升级检查（给老库补新表）", "test_migration.py"),
    ("启动引导（venv 跟随本机 Python：重定向/重建）", "test_bootstrap_env.py"),
    ("启动体验（缺依赖可见/镜像重试/重定向不崩编码）", "test_startup_deps.py"),
    ("公网暴露前安全检查（源码不外泄/密钥/口令门禁）", "test_exposure.py"),
    ("管理员页面（隐藏路由/登录/用户列表/普通用户拦截）", "test_admin.py"),
    ("图片与 PDF 附件（校验/渲染/多模态装载/不落盘）", "test_attachments.py"),
    ("历史对话论文卡片（papers 落库/坏数据/老库补列/API 带出）", "test_history_papers.py"),
    ("会话删除幂等（思考中删会话不再堆出「❌ 会话不存在」）", "test_conversation_delete.py"),
    ("运行日志（写入/过滤/分页/保留上限/请求埋点/不记内容/管理员接口）", "test_runlog.py"),
    ("朗读（Markdown 清洗/长文裁剪/参数归一/缓存键/引擎状态）", "test_tts.py"),
    ("名词解释接口（校验/结构归一化/模型失败降级）", "test_term_api.py"),
    ("摘要润色（必须重写/相似度重试/改动说明）", "test_polish.py"),
]
JS_TESTS = [
    ("前端参考文献作者格式（与后端规则一致）", "test_frontend_authors.js"),
    ("面板可缩放 + 未登录提示跳转登录 + 账号记住我", "test_panel_resize.js"),
    ("模型下拉列表（清单/默认/持久化/请求带 model/收起）", "test_model_picker.js"),
    ("功能下拉 + 小窗/内嵌显示方式 + 设置", "test_feature_menu.js"),
    ("一键生成账号（随机名/密码规则/撞名重试/凭证卡片）", "test_quick_account.js"),
    ("设置弹窗（主题颜色/深浅/账号管理）+ 主题落盘", "test_settings_ui.js"),
    ("搜索结果折叠 + 翻页（分页数学与折叠状态）", "test_pagination.js"),
    ("弹窗拖动（事件委托 + 视口边界 + 五个面板都能拖）", "test_drag.js"),
    ("对话记录立即可见（不等回答结束）", "test_conversation_sidebar.js"),
    ("前端附件（选择/校验/预览/属性转义）", "test_attachments_frontend.js"),
    ("历史对话重建论文卡片（papers 往返/每条消息独立分页）", "test_history_cards.js"),
    ("聊天流收尾（done / 「思考中」归属 / 切分支不串味 / 删会话）", "test_chat_stream_state.js"),
    ("卡片划词解释（选区判定/浮层按钮/右键/上下文截取）", "test_term_selection.js"),
]

failures = []
print("=" * 68)
for title, fname in PY_TESTS:
    print(f"\n▶ {title}\n  {fname}")
    r = subprocess.run([sys.executable, os.path.join(HERE, fname)],
                       cwd=ROOT, capture_output=True, text=True,
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
                           cwd=ROOT, capture_output=True, text=True,
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

# 测试用的临时库/账号文件统一放在"确认可写"的目录里（见 tests/_writable_temp.py）。
# 但有些环境（例如本机的 TEMP 不可写）会退回到项目根，所以两处都清：
# HERE = tests/，ROOT = 项目根。以前这里写的是 {HERE, dirname(__file__)}，
# 两个表达式其实是同一个 tests/ 目录 —— 根目录的 _tmp_*.db 一直清不掉。
for d in {HERE, ROOT}:
    try:
        names = os.listdir(d)
    except OSError:
        continue
    for f in names:
        if f.startswith("_tmp_") or f == "_test_accounts.json":
            p = os.path.join(d, f)
            if os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

if failures:
    print("失败:", ", ".join(failures))
    sys.exit(1)
print("全部通过 ✅")
