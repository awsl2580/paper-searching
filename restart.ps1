# 干净地重启本站服务（Windows / PowerShell）
#
# 为什么需要这个脚本：
# Flask 的 debug 模式带自动重载，杀掉父进程后**子进程会留下来继续占着端口**。
# 结果是新代码启动了、但请求仍由那个旧进程处理 —— 表现为
# "明明改了代码却没有任何变化"，而且几乎看不出来。
# 本项目真的踩过两次：一次是新加的图片功能完全不生效（旧进程还在跑没有该功能的版本），
# 一次是 /api/chat 全空（旧进程加载的是缺少常量的代码）。
#
# 所以每次重启前，先把所有 python 进程清掉，再确认端口确实没人监听。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\restart.ps1
#   powershell -ExecutionPolicy Bypass -File .\restart.ps1 -Port 8000

param(
    [int]$Port = 8080
)

$ErrorActionPreference = 'Continue'
Write-Host "=== 1. 停掉所有 python 进程（含重载器留下的子进程）===" -ForegroundColor Cyan
$procs = Get-Process python -ErrorAction SilentlyContinue
if ($procs) {
    $procs | ForEach-Object { Write-Host "  停止 PID $($_.Id)" }
    $procs | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
} else {
    Write-Host "  没有正在运行的 python 进程"
}

Write-Host "=== 2. 确认端口 $Port 已释放 ===" -ForegroundColor Cyan
$listeners = netstat -ano | Select-String ":$Port\s.*LISTENING"
if ($listeners) {
    Write-Host "  端口仍被占用，再等 3 秒重试..." -ForegroundColor Yellow
    Start-Sleep -Seconds 3
    $listeners = netstat -ano | Select-String ":$Port\s.*LISTENING"
    if ($listeners) {
        Write-Host "  !! 端口 $Port 仍被占用，请手动检查：" -ForegroundColor Red
        $listeners | ForEach-Object { Write-Host "    $_" }
        exit 1
    }
}
Write-Host "  已释放"

Write-Host "=== 3. 启动服务（端口 $Port）===" -ForegroundColor Cyan
$env:APP_PORT = "$Port"
$env:PYTHONIOENCODING = "utf-8"
$log = Join-Path $env:TEMP "paper_assistant_server.log"
Write-Host "  日志: $log"
# 不要用 -WindowStyle Hidden：它和 -RedirectStandardOutput 同时用时，
# Start-Process 在部分环境下会一直不返回（实测脚本就卡死在这一行）。
# -NoNewWindow 既能重定向输出，又不会阻塞。
$p = Start-Process -FilePath "python" -ArgumentList "app.py" `
     -WorkingDirectory $PSScriptRoot -PassThru -NoNewWindow `
     -RedirectStandardOutput $log -RedirectStandardError "$log.err"
Write-Host "  PID = $($p.Id)"

Write-Host "=== 4. 等待并验证 ===" -ForegroundColor Cyan
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}

if ($ok) {
    $count = (netstat -ano | Select-String ":$Port\s.*LISTENING" | Measure-Object).Count
    Write-Host "  ✅ 服务已就绪 http://127.0.0.1:$Port" -ForegroundColor Green
    Write-Host "     监听 $Port 的进程数: $count （应为 1；大于 1 说明又有残留进程）"
    Write-Host ""
    Write-Host "  下一步：另开一个窗口启动内网穿透 run_natapp.bat" -ForegroundColor Gray
} else {
    Write-Host "  !! 服务没起来，看日志：" -ForegroundColor Red
    if (Test-Path $log) { Get-Content $log -Tail 25 }
    if (Test-Path "$log.err") { Get-Content "$log.err" -Tail 25 }
    exit 1
}
