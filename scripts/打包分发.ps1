# 打包分发：生成一个"别人拿到就能跑"的干净压缩包
#
# 用法（在项目根目录）：
#     powershell -ExecutionPolicy Bypass -File .\scripts\打包分发.ps1
#
# 为什么需要它：直接把整个文件夹压缩发给别人，会带上一堆**不该传**的东西：
#   * .venv/        —— 虚拟环境里写死了本机的绝对路径和编译好的二进制，
#                      换台机器（尤其换 Python 版本）根本用不了，还占上百 MB；
#   * .env          —— 里面是**你自己的 DeepSeek API Key**，传出去等于泄露密钥；
#   * literature.db —— 你自己的账号（密码哈希）、收藏、对话记录、研究记忆；
#   * __pycache__ / .idea / *.log / 诊断结果.txt / tests/_tmp_*.db
#
# 打出来的包里只保留源码 + 必要资源 + .env.example，对方双击 一键启动.bat 即可。

param(
    [string]$OutDir = "$env:USERPROFILE\Desktop",
    [string]$Name = "DeepSeekAI4S科学智能体"
)

$ErrorActionPreference = "Stop"
# 本脚本在 scripts/ 下，项目根是上一级
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 需要排除的目录/文件（相对根目录的 glob）
$excludeDirs = @(".venv", ".venv.*", ".idea", "__pycache__", ".git", "node_modules",
                 ".pytest_cache", ".mypy_cache")
# 备份目录（_backup_*）：只有本机回滚用，不该发给别人
$excludeDirs += "_backup_*"
$excludeFiles = @(".env", "literature.db", "accounts.json", "accounts.json.*", "*.log", "*.zip", "*.pyc",
                  "诊断结果.txt", "_tmp_*.db", "_probe_*.json", ".DS_Store", "Thumbs.db")

# .env 模板必须保留（对方靠它生成自己的 .env）
$mustKeep = @(".env.example", "requirements.txt", "一键启动.bat", "paths.py",
              "scripts/launch.py", "scripts/bootstrap_env.py", "scripts/check_env.py",
              "web/app.py", "core/llm.py", "README.md")

# ── 1. 先把"必需文件"确认一遍，缺了就别打 ────────────────────────
$missing = @()
foreach ($f in $mustKeep) {
    if (-not (Test-Path (Join-Path $root $f))) { $missing += $f }
}
if ($missing.Count -gt 0) {
    Write-Host "❌ 缺少必需文件，打包中止：" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "     $_" }
    exit 1
}
Write-Host "✅ 必需文件齐全（$($mustKeep.Count) 项）" -ForegroundColor Green

# ── 2. 复制到临时目录，边复制边排除 ──────────────────────────────
$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$stage = Join-Path $env:TEMP "pkg_$stamp"
$dest = Join-Path $stage "$Name--main"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $dest -Force | Out-Null

$copied = 0
$skipped = 0
Get-ChildItem -Path $root -Recurse -Force | ForEach-Object {
    $rel = $_.FullName.Substring($root.Length).TrimStart('\')
    if (-not $rel) { return }

    # 目录：命中排除表就整枝跳过（用 -like 支持通配）
    $segments = $rel -split '\\'
    foreach ($seg in $segments) {
        foreach ($pat in $excludeDirs) {
            if ($seg -like $pat) { $skipped++; return }
        }
    }
    # 文件：命中排除表就跳过
    foreach ($pat in $excludeFiles) {
        if ($_.Name -like $pat) { $skipped++; return }
    }

    $target = Join-Path $dest $rel
    if ($_.PSIsContainer) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
    } else {
        $parent = Split-Path -Parent $target
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Copy-Item $_.FullName $target -Force
        $copied++
    }
}

# ── 3. 安全检查：确认包里没有 .env / 数据库 / venv ───────────────
$leak = @()
foreach ($bad in @(".env", "literature.db", "accounts.json", ".venv")) {
    if (Test-Path (Join-Path $dest $bad)) { $leak += $bad }
}
if ($leak.Count -gt 0) {
    Write-Host "❌ 安全检查未通过，包里仍有：$($leak -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "✅ 安全检查通过：不含 .env / literature.db / .venv" -ForegroundColor Green

# ── 4. 写一份"拿到先看这个"的说明 ───────────────────────────────
$readme = @'
# 拿到压缩包后怎么跑（Windows）

## 最省事：双击 `一键启动.bat`

它会自动：找 Python → 建虚拟环境 → 装依赖 → 检查环境 → 启动服务。
第一次会生成 `.env` 并用记事本打开，你只需要把**DeepSeek API Key** 粘进去：

    DEEPSEEK_API_KEY=你的key

保存关掉记事本，**再双击一次 `一键启动.bat`**，然后浏览器打开
<http://127.0.0.1:8000>。

API Key 申请：<https://platform.deepseek.com/api_keys>

## 前提

* Windows + Python **3.10 及以上**（推荐 3.12 / 3.13）。安装时勾选 "Add python.exe to PATH"。
* 能访问外网（要调 DeepSeek API 与几个学术数据源）。

## 跑不起来怎么办

1. 双击 `一键启动.bat`，把窗口里的红字按提示处理（它会给出具体命令）。
2. 仍然不行就跑一次环境自检，把 `诊断结果.txt` 发回给作者：

       .venv\Scripts\python.exe scripts\check_env.py

## 不要做的事

* **不要**把这个包里的 `.venv` 从别人那里拷来用 —— 虚拟环境写死了原机器的路径，
  换机器必然报错。删掉 `.venv` 重新双击 `一键启动.bat` 即可。
* **不要**在项目目录里放自己的 `.env` 之后再打包发出去（会泄露 token）。
* `literature.db` 是你的账号/收藏/对话记录，只在本机用，别传给别人。

## 手动启动（不想用 bat 的话）

在 PowerShell 里，**路径有括号或空格时一定要加引号**：

    cd "C:\放到哪里\兴智杯--main"
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    .\.venv\Scripts\python.exe web\app.py

> 常见报错：`无法将 "c:\...\兴智杯" 项识别为 cmdlet ...`
> 原因就是路径里的 `(1)` 被 PowerShell 当成语法了 —— 加引号，或者先 `cd` 进去再跑。
'@
$readme | Out-File -FilePath (Join-Path $dest "拿到先看这个.txt") -Encoding utf8

# ── 5. 压缩 ─────────────────────────────────────────────────────
$zip = Join-Path $OutDir "$Name--main_$stamp.zip"
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $dest -DestinationPath $zip -CompressionLevel Optimal

$sizeMB = [math]::Round((Get-Item $zip).Length / 1MB, 1)
$fileCount = (Get-ChildItem $dest -Recurse -File | Measure-Object).Count

Remove-Item $stage -Recurse -Force

Write-Host ""
Write-Host "==================== 打包完成 ====================" -ForegroundColor Cyan
Write-Host ("  压缩包：{0}" -f $zip)
Write-Host ("  大小：{0} MB    文件数：{1}    跳过：{2} 项" -f $sizeMB, $fileCount, $skipped)
Write-Host ""
Write-Host "  对方拿到后：解压 → 双击 一键启动.bat → 填 DEEPSEEK_API_KEY → 再双击一次" -ForegroundColor Yellow
Write-Host "==================================================" -ForegroundColor Cyan
