#Requires -Version 5.1
<#
.SYNOPSIS
    卸载 CUDA 显存监控服务并清理安装目录。

.DESCRIPTION
    做的事情：
      1. 先删除看门狗任务（必须最先，否则它会把主任务又拉起来）
      2. 停止并删除主计划任务
      3. 结束可能残留的 pythonw 进程
      4. 接管安装目录权限并删除整个目录
      5. 可选：删除机器级环境变量里的 AppSecret

.EXAMPLE
    .\uninstall.ps1

.EXAMPLE
    .\uninstall.ps1 -InstallDir "D:\CudaMonitor" -KeepFiles
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "C:\ProgramData\CudaMonitor",
    [string]$TaskName = "CUDA_Monitor",
    [switch]$KeepFiles,
    [switch]$KeepSecret
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Text)
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Text)
    Write-Host "    [OK] $Text" -ForegroundColor Green
}

Write-Step "检查管理员权限"
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "本脚本必须以管理员身份运行。"
}
Write-Ok "已是管理员"

# 顺序很重要：先干掉看门狗。否则主任务删掉后，看门狗下一轮又把它拉起来。
Write-Step "删除看门狗任务"
$watchdogName = "${TaskName}_Watchdog"
$watchdog = Get-ScheduledTask -TaskName $watchdogName -ErrorAction SilentlyContinue
if ($watchdog) {
    Unregister-ScheduledTask -TaskName $watchdogName -Confirm:$false
    Write-Ok "看门狗任务 $watchdogName 已删除"
}
else {
    Write-Ok "未找到看门狗任务，跳过"
}

Write-Step "停止并删除主任务"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    if ($task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Write-Ok "已停止任务"
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Ok "任务 $TaskName 已删除"
}
else {
    Write-Ok "未找到任务 $TaskName，跳过"
}

Write-Step "结束残留进程"
$stale = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$InstallDir*" }
if ($stale) {
    foreach ($proc in $stale) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Ok "已结束 PID $($proc.ProcessId)"
    }
}
else {
    Write-Ok "没有匹配的残留进程"
}

if ($KeepFiles) {
    Write-Step "按 -KeepFiles 保留目录"
    Write-Ok "目录未删除：$InstallDir"
}
elseif (Test-Path -LiteralPath $InstallDir) {
    Write-Step "删除安装目录"
    # ACL 已被收紧，先接管所有权并放开权限，否则删不掉。
    takeown /F $InstallDir /R /D Y 2>&1 | Out-Null
    icacls $InstallDir /grant:r "BUILTIN\Administrators:(OI)(CI)F" /T /C /Q | Out-Null
    Remove-Item -LiteralPath $InstallDir -Recurse -Force
    Write-Ok "已删除 $InstallDir"
}
else {
    Write-Ok "目录不存在，跳过"
}

if ($KeepSecret) {
    Write-Step "按 -KeepSecret 保留环境变量"
}
else {
    Write-Step "清理机器级环境变量"
    if ([Environment]::GetEnvironmentVariable("CUDA_MONITOR_WECHAT_SECRET", "Machine")) {
        [Environment]::SetEnvironmentVariable("CUDA_MONITOR_WECHAT_SECRET", $null, "Machine")
        Write-Ok "已删除 CUDA_MONITOR_WECHAT_SECRET"
    }
    else {
        Write-Ok "环境变量不存在，跳过"
    }
}

Write-Step "卸载完成"
