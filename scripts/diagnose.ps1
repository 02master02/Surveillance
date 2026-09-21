#Requires -Version 5.1
<#
.SYNOPSIS
    CUDA 显存监控 · 部署后诊断（只读为主，会尝试启动一次计划任务）。

.DESCRIPTION
    回答一个具体问题：**deploy.ps1 跑完了，任务却是 Ready 而不是 Running，为什么？**

    逐项查：
      1 安装目录与 ACL     —— SYSTEM 有没有完全控制（这是最常见的元凶）
      2 计划任务配置       —— 身份、命令行、ExecutionTimeLimit、重启策略
      3 上次运行结果       —— LastTaskResult 错误码 + 释义
      4 手动启动并观察     —— 6 秒后是 Running 还是又退回 Ready
      5 四个日志位置       —— monitor.log / watchdog.log / bootstrap 错误
      6 结论

    本脚本不修改任何配置。唯一的状态变化是第 4 步会尝试启动任务
    （它本来就应该在运行）。加 -NoStart 可跳过第 4 步，做到完全只读。

.PARAMETER InstallDir
    安装目录，默认 C:\ProgramData\CudaMonitor

.PARAMETER TaskName
    主任务名，默认 CUDA_Monitor

.PARAMETER NoStart
    跳过第 4 步的启动尝试，保持完全只读。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\diagnose.ps1
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "C:\ProgramData\CudaMonitor",
    [string]$TaskName = "CUDA_Monitor",
    [switch]$NoStart
)

$ErrorActionPreference = "Continue"
$script:Problems = New-Object System.Collections.ArrayList

function Write-Head {
    param([string]$Text)
    Write-Host ""
    Write-Host "== $Text ==" -ForegroundColor Cyan
}
function Write-Ok {
    param([string]$Text)
    Write-Host "  [OK  ] $Text" -ForegroundColor Green
}
function Write-Notice {
    param([string]$Text)
    Write-Host "  [注意] $Text" -ForegroundColor Yellow
    [void]$script:Problems.Add($Text)
}
function Write-Bad {
    param([string]$Text)
    Write-Host "  [失败] $Text" -ForegroundColor Red
    [void]$script:Problems.Add($Text)
}
function Write-Dim {
    param([string]$Text)
    Write-Host "    $Text" -ForegroundColor DarkGray
}

Write-Host ("=" * 68) -ForegroundColor DarkGray
Write-Host "CUDA 显存监控 · 部署后诊断（只读）" -ForegroundColor White
Write-Host ("=" * 68) -ForegroundColor DarkGray
Write-Host "  安装目录：$InstallDir"
Write-Host "  任务名称：$TaskName"

# ---------------------------------------------------------------- 1/6

Write-Head "1/6 安装目录与 ACL"

$dirExists = Test-Path -LiteralPath $InstallDir
if (-not $dirExists) {
    Write-Bad "安装目录不存在：$InstallDir"
}
else {
    Write-Ok "安装目录存在"

    foreach ($rel in @("run_monitor.py", "config.json", "src\cuda_monitor\app.py", "scripts\watchdog.ps1")) {
        $probe = Join-Path $InstallDir $rel
        if (Test-Path -LiteralPath $probe) { Write-Ok "存在 $rel" }
        else { Write-Bad "缺失 $rel" }
    }

    # config.json 的 BOM 检查。这是个会**伪装成权限问题**的隐蔽坑：
    # PowerShell 5.1 的 `Set-Content -Encoding UTF8` 写出的是「UTF-8 带 BOM」，
    # 而 Python 的 json 模块读到 BOM 直接抛 "Unexpected UTF-8 BOM"。
    # 后果：程序死在加载配置这一步 → 秒退 → 任务状态停在 Ready，
    # 而 LastTaskResult 只给一个含糊的 0x00000002。
    $cfgProbe = Join-Path $InstallDir "config.json"
    if (Test-Path -LiteralPath $cfgProbe) {
        $bytes = [System.IO.File]::ReadAllBytes($cfgProbe)
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
            Write-Bad "config.json 带 UTF-8 BOM —— Python 解析会失败，任务必定启动即退出"
            Write-Dim "修复：重跑修好的 deploy.ps1（改为无 BOM 写入），"
            Write-Dim "      或让程序侧用 utf-8-sig 读取（config.py 已改）"
        }
        else {
            Write-Ok "config.json 无 BOM（Python 可直接解析）"
        }
    }

    Write-Host "  icacls 原始输出：" -ForegroundColor Gray
    $rawAcl = & icacls $InstallDir 2>&1
    foreach ($line in $rawAcl) { Write-Host "    $line" -ForegroundColor DarkGray }

    $acl = $null
    try { $acl = Get-Acl -LiteralPath $InstallDir }
    catch { Write-Bad "读不到 ACL：$($_.Exception.Message)" }

    if ($acl) {
        Write-Dim "所有者：$($acl.Owner)"

        $sysFull = $false
        $admFull = $false
        $usersRule = ""
        foreach ($rule in $acl.Access) {
            if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
            $name = "$($rule.IdentityReference)"
            $rights = [int]$rule.FileSystemRights
            # FullControl = 0x1F01FF
            $isFull = (($rights -band 0x1F01FF) -eq 0x1F01FF)
            if ($name -match 'SYSTEM' -and $isFull) { $sysFull = $true }
            if ($name -match 'Administrators' -and $isFull) { $admFull = $true }
            if ($name -match 'Users') { $usersRule = "$name = $($rule.FileSystemRights)" }
        }

        if ($sysFull) {
            Write-Ok "SYSTEM 拥有完全控制"
        }
        else {
            Write-Bad "SYSTEM 没有完全控制 —— SYSTEM 身份的任务会启动即退出，状态停在 Ready 且不报错"
            Write-Dim "修复命令："
            Write-Dim "icacls `"$InstallDir`" /inheritance:r /grant:r `"SYSTEM:(OI)(CI)F`" `"BUILTIN\Administrators:(OI)(CI)F`" `"BUILTIN\Users:(OI)(CI)RX`" /C /Q"
        }

        if ($admFull) {
            Write-Ok "Administrators 拥有完全控制"
        }
        else {
            Write-Notice "Administrators 没有完全控制，后续维护会不便"
        }

        if ($usersRule) { Write-Dim "Users 规则：$usersRule" }
        else { Write-Notice "没有找到 Users 的规则" }
    }
}

# ---------------------------------------------------------------- 2/6

Write-Head "2/6 计划任务配置"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Bad "找不到计划任务 $TaskName"
}
else {
    Write-Ok "任务存在，当前状态：$($task.State)"
    Write-Dim "身份：$($task.Principal.UserId) / RunLevel=$($task.Principal.RunLevel) / LogonType=$($task.Principal.LogonType)"

    foreach ($act in $task.Actions) {
        Write-Dim "执行：$($act.Execute)"
        Write-Dim "参数：$($act.Arguments)"
        Write-Dim "工作目录：$($act.WorkingDirectory)"

        # 计划任务里的 exe 是否存在 —— 不存在时任务同样会秒退。
        $exePath = $act.Execute
        if ($exePath -and -not (Test-Path -LiteralPath $exePath)) {
            Write-Bad "任务指向的可执行文件不存在：$exePath"
        }
        elseif ($exePath) {
            Write-Ok "可执行文件存在：$exePath"
        }
    }

    Write-Dim "ExecutionTimeLimit：$($task.Settings.ExecutionTimeLimit)"
    Write-Dim "RestartCount：$($task.Settings.RestartCount) / RestartInterval：$($task.Settings.RestartInterval)"
    $mi = $task.Settings.MultipleInstances
    if (-not $mi) { $mi = $task.Settings.MultipleInstancesPolicy }
    Write-Dim "MultipleInstances：$mi / Hidden：$($task.Settings.Hidden)"
    Write-Dim "StartWhenAvailable：$($task.Settings.StartWhenAvailable)"

    # 常驻服务要的是 PT0S（无时间上限）。
    # 反过来才危险：如果这里是 PT72H 之类的有限值，任务跑满就被终止。
    $etl = "$($task.Settings.ExecutionTimeLimit)"
    if ($etl -eq "PT0S" -or [string]::IsNullOrWhiteSpace($etl)) {
        Write-Ok "ExecutionTimeLimit = '$etl'（无上限，符合常驻需求）"
    }
    else {
        Write-Notice "ExecutionTimeLimit = $etl —— 有限值，常驻服务会被定时终止，应设为 PT0S"
    }

    $wdName = "${TaskName}_Watchdog"
    $wd = Get-ScheduledTask -TaskName $wdName -ErrorAction SilentlyContinue
    if ($wd) {
        Write-Ok "看门狗任务存在，状态：$($wd.State)"
        $rep = $wd.Triggers | ForEach-Object { $_.Repetition }
        if ($rep -and $rep.Interval) {
            Write-Dim "看门狗间隔：$($rep.Interval) / 持续：'$($rep.Duration)'（空字符串才是无限重复）"
            if ($rep.Duration) {
                Write-Notice "看门狗 Repetition.Duration 非空，它只会运行有限次数后就停"
            }
        }
        else {
            Write-Notice "看门狗触发器里没有 Repetition 节点，它可能只跑一次"
        }
    }
    else {
        Write-Notice "没有看门狗任务 ${wdName}（用 -NoWatchdog 部署过？）"
    }
}

# ---------------------------------------------------------------- 3/6

Write-Head "3/6 上次运行结果"

$info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $info) {
    Write-Notice "取不到运行信息（任务可能不存在）"
}
else {
    Write-Dim "上次运行：$($info.LastRunTime)"
    Write-Dim "下次运行：$($info.NextRunTime)"
    Write-Dim "错过次数：$($info.NumberOfMissedRuns)"

    $hex = ('{0:X8}' -f [uint32]$info.LastTaskResult)
    Write-Dim "结果码：0x$hex"
    switch ($hex) {
        '00000000' { Write-Ok "释义：执行成功" }
        '00000001' { Write-Notice "释义：通用错误 —— 脚本抛异常或路径不对" }
        '00000002' { Write-Notice "释义：找不到文件" }
        '00041303' { Write-Notice "释义：任务尚未运行过" }
        '80070002' { Write-Bad "释义：0x80070002 找不到指定的文件 —— Execute 指向的 exe 不存在" }
        '80070005' { Write-Bad "释义：0x80070005 拒绝访问 —— 权限/ACL 问题，最常见元凶" }
        '800710E0' { Write-Notice "释义：0x800710E0 管理员拒绝了请求" }
        'C0000135' { Write-Bad "释义：0xC0000135 DLL 缺失 —— Python 运行时依赖不完整" }
        'C0000005' { Write-Bad "释义：0xC0000005 访问冲突 —— 进程崩溃" }
        default { Write-Dim "释义：未见过的错误码，可到任务计划程序 GUI 里查" }
    }
}

# ---------------------------------------------------------------- 4/6

Write-Head "4/6 手动启动并观察"

if ($NoStart) {
    Write-Notice "按 -NoStart 跳过启动尝试"
}
elseif (-not $task) {
    Write-Notice "任务不存在，无法启动"
}
elseif ($task.State -eq "Running") {
    Write-Ok "任务已在运行，无需启动"
}
else {
    Write-Dim "当前状态 $($task.State)，尝试 Start-ScheduledTask ..."
    try {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Start-Sleep -Seconds 6
        $after = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($after -and $after.State -eq "Running") {
            Write-Ok "6 秒后仍是 Running —— 进程活着，启动成功"
        }
        else {
            $stateNow = "未知"
            if ($after) { $stateNow = $after.State }
            Write-Bad "6 秒后状态变成 $stateNow —— 进程启动后立刻退出了"
            $info2 = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
            if ($info2) {
                Write-Dim "本次结果码：0x$('{0:X8}' -f [uint32]$info2.LastTaskResult)"
            }
        }
    }
    catch {
        Write-Bad "Start-ScheduledTask 抛异常：$($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------- 5/6

Write-Head "5/6 日志"

function Write-Tail {
    param([string]$Path, [string]$Label, [int]$Lines = 15)

    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Host "  --- $Label：不存在 ---" -ForegroundColor DarkGray
        return
    }
    $item = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($item) {
        Write-Host "  --- $Label（$($item.Length) 字节，最后写入 $($item.LastWriteTime)）---" -ForegroundColor Gray
    }
    else {
        Write-Host "  --- $Label ---" -ForegroundColor Gray
    }
    Get-Content -LiteralPath $Path -Tail $Lines -Encoding UTF8 -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
}

Write-Tail (Join-Path $InstallDir "monitor.log") "monitor.log"
Write-Tail (Join-Path $InstallDir "watchdog.log") "watchdog.log"
Write-Tail (Join-Path $InstallDir "cuda_monitor_bootstrap_error.log") "bootstrap 错误（安装目录）"
Write-Tail (Join-Path $env:ProgramData "cuda_monitor_bootstrap_error.log") "bootstrap 错误（ProgramData）"
Write-Tail "C:\Windows\system32\config\systemprofile\AppData\Local\Temp\cuda_monitor_bootstrap_error.log" "bootstrap 错误（SYSTEM 的 TEMP）"

$systemTemp = "C:\Windows\system32\config\systemprofile\AppData\Local\Temp"
if (Test-Path -LiteralPath $systemTemp) {
    Write-Ok "SYSTEM 的 TEMP 目录存在：$systemTemp"
}
else {
    # 不算问题 —— 程序已改成多位置落盘兜底。但知道这件事有助于理解
    # bootstrap 错误日志为什么会出现在安装目录 / ProgramData 下。
    Write-Dim "SYSTEM 的 TEMP 目录不存在：$systemTemp"
    Write-Dim "（正常：程序已做多位置兜底，bootstrap 错误会写到安装目录与 ProgramData）"
}

# ---------------------------------------------------------------- 6/6

Write-Head "6/6 结论"

if ($script:Problems.Count -eq 0) {
    Write-Host "  未发现异常。" -ForegroundColor Green
}
else {
    Write-Host "  共 $($script:Problems.Count) 项需要处理：" -ForegroundColor Yellow
    foreach ($p in $script:Problems) {
        Write-Host "    - $p" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host ("=" * 68) -ForegroundColor DarkGray
Write-Host "  把上面整段输出发回来即可。" -ForegroundColor White
Write-Host ("=" * 68) -ForegroundColor DarkGray
Write-Host ""
