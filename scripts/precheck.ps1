#Requires -Version 5.1
<#
.SYNOPSIS
    CUDA 显存监控 · 目标机部署前环境体检（只读，不改动任何东西）。

.DESCRIPTION
    逐项检查部署所需的前置条件，没通过的会直接给出修法。
    在目标机上以【管理员】身份运行，把输出整段发回来即可。

    检查项：
      1 管理员权限          2 操作系统 / PowerShell 版本
      3 NVIDIA 驱动与显卡    4 Python / pythonw 的位置与版本
      5 系统时间             6 微信接口出站连通性
      7 项目文件是否拷全      8 安装目录与已有任务

.PARAMETER InstallDir
    计划安装目录，默认 C:\ProgramData\CudaMonitor。

.PARAMETER SkipProjectFiles
    项目还没拷到本机时加这个开关，跳过第 7 项。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\precheck.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\precheck.ps1 -SkipProjectFiles
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "C:\ProgramData\CudaMonitor",
    [switch]$SkipProjectFiles
)

$ErrorActionPreference = "Continue"

$script:Rows = New-Object System.Collections.ArrayList

function Add-Row {
    param(
        [string]$Item,
        [ValidateSet("OK", "注意", "失败")][string]$State,
        [string]$Detail = "",
        [string]$Fix = ""
    )
    [void]$script:Rows.Add([PSCustomObject]@{
            Item   = $Item
            State  = $State
            Detail = $Detail
            Fix    = $Fix
        })
}

function Write-Head {
    param([string]$Text)
    Write-Host ""
    Write-Host "== $Text ==" -ForegroundColor Cyan
}

Write-Host ("=" * 66) -ForegroundColor DarkGray
Write-Host "CUDA 显存监控 · 部署前环境体检" -ForegroundColor White
Write-Host ("=" * 66) -ForegroundColor DarkGray

# ---------------------------------------------------------------- 1 权限

Write-Head "1/8 权限"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    Add-Row "管理员权限" "OK" $identity.Name
}
else {
    Add-Row "管理员权限" "失败" $identity.Name "右键 PowerShell -> 以管理员身份运行，再跑一次"
}

# ---------------------------------------------------------------- 2 系统

Write-Head "2/8 操作系统"
try {
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
    Add-Row "操作系统" "OK" ("{0} (版本 {1})" -f $os.Caption, $os.Version)
}
catch {
    Add-Row "操作系统" "注意" "取不到版本信息"
}
Add-Row "PowerShell" "OK" $PSVersionTable.PSVersion.ToString()

# ---------------------------------------------------------------- 3 NVIDIA

Write-Head "3/8 NVIDIA 驱动与显卡"
$smi = (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue).Source
if (-not $smi) {
    $candidate = Join-Path $env:SystemRoot "System32\nvidia-smi.exe"
    if (Test-Path -LiteralPath $candidate) { $smi = $candidate }
}

if (-not $smi) {
    Add-Row "nvidia-smi" "失败" "PATH 与 System32 里都没找到" "安装/更新 NVIDIA 显卡驱动"
}
else {
    Add-Row "nvidia-smi 路径" "OK" $smi
    $gpuOut = & $smi --query-gpu=index,name,memory.total --format=csv,noheader 2>&1
    $gpuText = (@($gpuOut) | Out-String).Trim()
    if ($LASTEXITCODE -eq 0 -and $gpuText -and $gpuText -notmatch "NVML") {
        $names = @($gpuOut) | Where-Object { $_ -and $_.Trim() }
        Add-Row "显卡识别" "OK" ("共 {0} 张：{1}" -f $names.Count, ($names -join " / "))
    }
    else {
        Add-Row "显卡识别" "失败" $gpuText "驱动没装好，或驱动版本与显卡不匹配"
    }
}

# ---------------------------------------------------------------- 4 Python

Write-Head "4/8 Python 运行时"
$pyPath = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
$pywPath = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source

$targets = @(
    [PSCustomObject]@{ Name = "python.exe"; Path = $pyPath },
    [PSCustomObject]@{ Name = "pythonw.exe"; Path = $pywPath }
)
foreach ($t in $targets) {
    if (-not $t.Path) {
        Add-Row $t.Name "失败" "PATH 里找不到" "装 Python 3.9+，安装时勾选 Add python.exe to PATH"
        continue
    }
    if ($t.Path -like "*\WindowsApps\*") {
        Add-Row $t.Name "失败" $t.Path "这是 Microsoft Store 版，SYSTEM 身份读不到。改装 python.org 的版本到 C:\Python3xx"
    }
    elseif ($t.Path -like "$env:USERPROFILE*") {
        Add-Row $t.Name "注意" $t.Path "装在用户目录下，SYSTEM 有概率读不到；建议改装到 C:\Python3xx 或 Program Files"
    }
    else {
        Add-Row $t.Name "OK" $t.Path
    }
}

if ($pyPath) {
    $verText = (@(& $pyPath --version 2>&1) | Out-String).Trim()
    if ($verText -match "(\d+)\.(\d+)\.(\d+)") {
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 9)) {
            Add-Row "Python 版本" "OK" $verText
        }
        else {
            Add-Row "Python 版本" "失败" $verText "需要 3.9 及以上"
        }
    }
    else {
        Add-Row "Python 版本" "注意" ("解析不了版本号：{0}" -f $verText)
    }
}

# ---------------------------------------------------------------- 5 时间

Write-Head "5/8 系统时间"
Add-Row "当前时间" "OK" (Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz")
$timeSvc = Get-Service w32time -ErrorAction SilentlyContinue
if ($timeSvc) {
    $state = if ($timeSvc.Status -eq "Running") { "OK" } else { "注意" }
    Add-Row "时间同步服务 w32time" $state $timeSvc.Status.ToString()
}
else {
    Add-Row "时间同步服务 w32time" "注意" "服务不存在" "确认系统时间准确：偏差过大会让 access_token 校验失败"
}

# ---------------------------------------------------------------- 6 网络

Write-Head "6/8 微信接口出站连通性"
try {
    $tcpOk = Test-NetConnection -ComputerName "api.weixin.qq.com" -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue
}
catch {
    $tcpOk = $false
}
if ($tcpOk) {
    Add-Row "TCP 443 -> api.weixin.qq.com" "OK" "可连通"
}
else {
    Add-Row "TCP 443 -> api.weixin.qq.com" "失败" "连不上" "放行出站 443，或在 config.json 的 wechat.proxy 里填代理"
}

# ---------------------------------------------------------------- 7 项目文件

Write-Head "7/8 项目文件完整性"
if ($SkipProjectFiles) {
    Add-Row "项目文件" "注意" "已按 -SkipProjectFiles 跳过"
}
else {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    Add-Row "项目根目录" "OK" $projectRoot
    $needed = @(
        "run_monitor.py",
        "config.example.json",
        "src\cuda_monitor\config.py",
        "src\cuda_monitor\collector.py",
        "src\cuda_monitor\judge.py",
        "src\cuda_monitor\notifier.py",
        "src\cuda_monitor\app.py",
        "scripts\deploy.ps1",
        "scripts\uninstall.ps1",
        "scripts\watchdog.ps1"
    )
    $missing = @()
    foreach ($rel in $needed) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $rel))) { $missing += $rel }
    }
    if ($missing.Count -eq 0) {
        Add-Row "必需文件" "OK" ("{0} 个文件齐全" -f $needed.Count)
    }
    else {
        Add-Row "必需文件" "失败" ("缺 {0} 个" -f $missing.Count) ("项目没拷全，缺：" + ($missing -join "、"))
    }
}

# ---------------------------------------------------------------- 8 安装目录

Write-Head "8/8 安装目录状态"
if (Test-Path -LiteralPath $InstallDir) {
    Add-Row "安装目录" "注意" ("{0} 已存在" -f $InstallDir) "重复部署会覆盖程序文件、保留 config.json"
    if (Get-ScheduledTask -TaskName "CUDA_Monitor" -ErrorAction SilentlyContinue) {
        Add-Row "已有计划任务" "注意" "CUDA_Monitor 已注册" "deploy.ps1 会用 -Force 覆盖"
    }
    if (Get-ScheduledTask -TaskName "CUDA_Monitor_Watchdog" -ErrorAction SilentlyContinue) {
        Add-Row "已有看门狗" "注意" "CUDA_Monitor_Watchdog 已注册" "deploy.ps1 会用 -Force 覆盖"
    }
}
else {
    Add-Row "安装目录" "OK" ("{0} 尚不存在，属首次部署" -f $InstallDir)
}

# ---------------------------------------------------------------- 汇总

Write-Host ""
Write-Host ("=" * 66) -ForegroundColor DarkGray
Write-Host "体检结果" -ForegroundColor White
Write-Host ("=" * 66) -ForegroundColor DarkGray

foreach ($row in $script:Rows) {
    $color = "Gray"
    if ($row.State -eq "OK") { $color = "Green" }
    elseif ($row.State -eq "注意") { $color = "Yellow" }
    elseif ($row.State -eq "失败") { $color = "Red" }

    if ($row.Detail) {
        Write-Host ("  [{0,-4}] {1,-28} {2}" -f $row.State, $row.Item, $row.Detail) -ForegroundColor $color
    }
    else {
        Write-Host ("  [{0,-4}] {1}" -f $row.State, $row.Item) -ForegroundColor $color
    }
    if ($row.Fix) {
        Write-Host ("            修法：{0}" -f $row.Fix) -ForegroundColor DarkGray
    }
}

$failCount = @($script:Rows | Where-Object { $_.State -eq "失败" }).Count
$warnCount = @($script:Rows | Where-Object { $_.State -eq "注意" }).Count

Write-Host ""
if ($failCount -gt 0) {
    Write-Host ("[结论] {0} 项未通过 —— 先修完这些，再进入下一步。" -f $failCount) -ForegroundColor Red
}
elseif ($warnCount -gt 0) {
    Write-Host ("[结论] 没有硬性失败，有 {0} 项提醒 —— 确认可接受就继续。" -f $warnCount) -ForegroundColor Yellow
}
else {
    Write-Host "[结论] 全部通过，可以进入下一步。" -ForegroundColor Green
}
Write-Host "把上面这段完整输出发回，我来判断怎么走。" -ForegroundColor Gray
Write-Host ""
