#Requires -Version 5.1
<#
.SYNOPSIS
    CUDA 监控的看门狗：发现主任务没在跑就把它拉起来。

.DESCRIPTION
    主任务以 SYSTEM 身份运行，普通用户本来就结束不了它；
    但管理员可以 Stop-ScheduledTask，或者直接把 pythonw 进程结束掉。
    这个看门狗每隔几分钟检查一次，发现主任务不在运行就重新启动，
    让"杀掉"这件事失去实际意义。

    看门狗自己也是一个 SYSTEM 身份的隐藏计划任务，因此受同等保护。

    判断逻辑刻意分两步，因为被强杀时经常出现
    "计划任务状态仍显示 Running，但进程实际已经消失"的假象：

      1. 任务状态是否 Running
      2. 进程列表里是否真有带本安装目录的 pythonw.exe

    两者都对才算健康。

    第三件事是"连续拉起检测"：看门狗每轮都写"已重新启动"，既可能是主任务
    真被杀了，也可能是主程序启动即崩 —— 两种情况在日志里长得一模一样。
    所以统计连续多次、且间隔都在一个巡检周期左右的拉起，到第 3 次就升级成
    带说明的告警行。这不是理论担忧：本机 2026-09-18 13:38–15:11 因
    config.json 带了 BOM 导致主程序启动即崩，看门狗空拉了 1.5 小时，
    日志里只有一排"已重新启动"，从外部看毫无异常。

.PARAMETER TaskName
    要守护的主任务名，默认 CUDA_Monitor。

.PARAMETER InstallDir
    安装目录。既用于日志落盘位置，也用于在进程命令行里辨认主进程。

.PARAMETER LogFile
    看门狗日志。默认 <InstallDir>\watchdog.log，只记"拉起过"这类事件。

.EXAMPLE
    .\watchdog.ps1 -TaskName CUDA_Monitor
#>
[CmdletBinding()]
param(
    [string]$TaskName = "CUDA_Monitor",
    [string]$InstallDir = "C:\ProgramData\CudaMonitor",
    [string]$LogFile = ""
)

$ErrorActionPreference = "Stop"

if (-not $LogFile) {
    $LogFile = Join-Path $InstallDir "watchdog.log"
}

function Write-WatchdogLog {
    param([string]$Text)
    try {
        $dir = Split-Path -Parent $LogFile
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        # 不用 Add-Content -Encoding UTF8：PS 5.1 会写入 UTF-8 BOM，
        # 日志被追加/拼接时会多出不可见的 \ufeff，grep 和 tail 都会别扭。
        $line = "[$stamp] $Text" + [Environment]::NewLine
        [System.IO.File]::AppendAllText(
            $LogFile,
            $line,
            (New-Object System.Text.UTF8Encoding($false))
        )
    }
    catch {
        # 看门狗不能因为写不了日志就把自己搞挂。
    }
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-WatchdogLog "主任务 $TaskName 不存在，看门狗不动作（可能已卸载）。"
    exit 0
}

# 管理员有意禁用（Disable-ScheduledTask）时，看门狗不擅自启用 ——
# 这是留给你的"正规停止"开关，看门狗尊重它。
if ($task.State -eq "Disabled") {
    Write-WatchdogLog "主任务处于 Disabled 状态（应为有意禁用），看门狗不插手。"
    exit 0
}

$alive = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$InstallDir*" }

if ($task.State -eq "Running" -and $alive) {
    exit 0
}

if ($task.State -eq "Running" -and -not $alive) {
    # 僵尸态：状态还挂在 Running，进程却没了。先清状态再重启。
    Write-WatchdogLog "任务状态为 Running 但进程已消失（疑似被强杀），先复位任务。"
    try {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    catch {
        Write-WatchdogLog "复位时出错（继续尝试启动）：$($_.Exception.Message)"
    }
    Start-Sleep -Seconds 2
}

# 统计"连续被拉起"的次数：从日志末尾往前看，只认相邻间隔 ≤ 一个巡检周期 × 1.5
# 的那些"检测到主任务未运行"行。间隔一旦变大，说明中间稳定运行过，链就断了。
function Get-RestartStreak {
    if (-not (Test-Path -LiteralPath $LogFile)) { return 0 }
    $lines = @(Get-Content -LiteralPath $LogFile -Encoding UTF8 -Tail 20 -ErrorAction SilentlyContinue)
    $streak = 0
    $hasPrev = $false
    $prev = [datetime]::MinValue
    for ($i = $lines.Count - 1; $i -ge 0; $i--) {
        $m = [regex]::Match($lines[$i], '^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] 检测到主任务未运行')
        if (-not $m.Success) { break }
        $ts = [datetime]::ParseExact(
            $m.Groups[1].Value,
            'yyyy-MM-dd HH:mm:ss',
            [System.Globalization.CultureInfo]::InvariantCulture
        )
        if ($hasPrev -and ($prev - $ts).TotalMinutes -gt 4.5) { break }
        $prev = $ts
        $hasPrev = $true
        $streak++
    }
    return $streak
}

$streak = Get-RestartStreak

try {
    Start-ScheduledTask -TaskName $TaskName
    if (($streak + 1) -ge 3) {
        # 前缀与普通行保持一致，这样连续计数不会因为升级成告警而断链。
        Write-WatchdogLog ("检测到主任务未运行，已重新启动（第 $($streak + 1) 次连续拉起，" +
            "疑似启动即崩 —— 请查 monitor.log 与 config.json）")
    }
    else {
        Write-WatchdogLog "检测到主任务未运行，已重新启动。"
    }
}
catch {
    Write-WatchdogLog "尝试启动主任务失败：$($_.Exception.Message)"
}

exit 0
