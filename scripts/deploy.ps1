#Requires -Version 5.1
<#
.SYNOPSIS
    把 CUDA 显存监控服务安装到本机，并注册为开机自启的后台任务。

.DESCRIPTION
    做的事情：
      1. 把项目文件复制到安装目录（默认 C:\ProgramData\CudaMonitor）
      2. 生成 / 更新 config.json
      3. 注册计划任务：以 SYSTEM 身份、开机触发、无窗口、失败自动重启
      4. 注册看门狗任务：每 3 分钟检查一次，主任务被强杀后自动拉起
      5. 收紧目录 ACL：普通用户只读，且不授予删除与修改权限
      6. 可选：把微信 AppSecret 写入机器级环境变量，避免落盘

    为什么用 SYSTEM 而不是你自己的账号：
      以 SYSTEM 运行不需要保存账号密码，任务只在你登录时也不受影响，
      而且普通用户（包括你自己不带提权）无法结束它。

.PARAMETER InstallDir
    安装目录。默认 C:\ProgramData\CudaMonitor

.PARAMETER PythonExe
    pythonw.exe 的完整路径。不指定则自动探测。

.PARAMETER AppId
    微信测试号 appID。不填则沿用现有 config.json。

.PARAMETER AppSecret
    微信测试号 appSecret。填了会写入机器级环境变量，并清空 config.json 里的明文。

.PARAMETER TemplateId
    模板消息的 template_id。

.PARAMETER ToUsers
    接收人 OpenID 列表，可传多个。

.PARAMETER SkipAcl
    跳过 ACL 收紧。不建议 —— 除非你想之后手动调整权限。

.PARAMETER StartNow
    安装完成后立即启动任务（不等到下次开机）。

.EXAMPLE
    .\deploy.ps1 -AppId wx123 -AppSecret abc -TemplateId tpl -ToUsers oid1,oid2

.EXAMPLE
    .\deploy.ps1 -PythonExe "C:\Python311\pythonw.exe" -StartNow
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "C:\ProgramData\CudaMonitor",
    [string]$SourceDir = "",
    [string]$PythonExe = "",
    [string]$TaskName = "CUDA_Monitor",
    [string]$AppId = "",
    [string]$AppSecret = "",
    [string]$TemplateId = "",
    [string[]]$ToUsers = @(),
    [switch]$SkipAcl,
    [switch]$NoWatchdog,
    [switch]$StartNow
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

function Write-Warn {
    param([string]$Text)
    Write-Host "    [注意] $Text" -ForegroundColor Yellow
}

# ---------------------------------------------------------------- 前置检查

Write-Step "检查管理员权限"
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "本脚本必须以管理员身份运行。请右键 PowerShell -> 以管理员身份运行，再执行本脚本。"
}
Write-Ok "已是管理员"

if (-not $SourceDir) {
    $SourceDir = Split-Path -Parent $PSScriptRoot
}
if (-not (Test-Path -LiteralPath (Join-Path $SourceDir "run_monitor.py"))) {
    throw "在 $SourceDir 里找不到 run_monitor.py。请用 -SourceDir 指定项目根目录。"
}
Write-Ok "源目录：$SourceDir"

Write-Step "定位 pythonw.exe"
if ($PythonExe) {
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        throw "指定的 Python 路径不存在：$PythonExe"
    }
    $pythonw = (Resolve-Path -LiteralPath $PythonExe).Path
}
else {
    $found = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($found) {
        $pythonw = $found.Source
    }
    else {
        $candidates = @(
            "$env:LOCALAPPDATA\Programs\Python\Python313\pythonw.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python311\pythonw.exe",
            "C:\Python313\pythonw.exe",
            "C:\Python312\pythonw.exe",
            "C:\Python311\pythonw.exe",
            "C:\Program Files\Python313\pythonw.exe",
            "C:\Program Files\Python312\pythonw.exe",
            "C:\Program Files\Python311\pythonw.exe"
        )
        foreach ($candidate in $candidates) {
            if (Test-Path -LiteralPath $candidate) { $pythonw = $candidate; break }
        }
    }
    if (-not $pythonw) {
        throw "找不到 pythonw.exe。请先在 CMD 里执行 'where pythonw'，再用 -PythonExe 传入完整路径。"
    }
}
Write-Ok "pythonw：$pythonw"

$python = Join-Path (Split-Path -Parent $pythonw) "python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = $pythonw }

if ($pythonw -like "$env:USERPROFILE*") {
    Write-Warn "pythonw 位于当前用户目录下。SYSTEM 通常仍可读取，但建议改用机器级安装的 Python，更稳妥。"
}

# ---------------------------------------------------------------- 复制文件

Write-Step "安装到 $InstallDir"
if (-not (Test-Path -LiteralPath $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}

Copy-Item -LiteralPath (Join-Path $SourceDir "run_monitor.py") -Destination $InstallDir -Force
Copy-Item -LiteralPath (Join-Path $SourceDir "config.example.json") -Destination $InstallDir -Force

$targetSrc = Join-Path $InstallDir "src"
if (Test-Path -LiteralPath $targetSrc) { Remove-Item -LiteralPath $targetSrc -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $SourceDir "src") -Destination $InstallDir -Recurse -Force

Get-ChildItem -LiteralPath $InstallDir -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# scripts\ 也拷一份过去：看门狗任务要用绝对路径调 watchdog.ps1，
# 顺手让运维脚本在安装目录里也有一份。
$sourceScripts = Join-Path $SourceDir "scripts"
if (Test-Path -LiteralPath $sourceScripts) {
    $targetScripts = Join-Path $InstallDir "scripts"
    if (Test-Path -LiteralPath $targetScripts) { Remove-Item -LiteralPath $targetScripts -Recurse -Force }
    Copy-Item -LiteralPath $sourceScripts -Destination $InstallDir -Recurse -Force
}

Write-Ok "文件已复制"

# ---------------------------------------------------------------- 配置文件

Write-Step "准备 config.json"
$configPath = Join-Path $InstallDir "config.json"
if (-not (Test-Path -LiteralPath $configPath)) {
    Copy-Item -LiteralPath (Join-Path $InstallDir "config.example.json") $configPath
    Write-Ok "已从模板生成 config.json"
}
else {
    Write-Ok "沿用已存在的 config.json"
}

$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json

if ($AppId) { $config.wechat.app_id = $AppId }
if ($TemplateId) { $config.wechat.template_id = $TemplateId }
if ($ToUsers.Count -gt 0) { $config.wechat.to_users = @($ToUsers) }
if ($AppSecret) {
    # 密钥进环境变量，配置文件里保持空值，避免明文落盘。
    [Environment]::SetEnvironmentVariable("CUDA_MONITOR_WECHAT_SECRET", $AppSecret, "Machine")
    $config.wechat.app_secret = ""
    Write-Ok "AppSecret 已写入机器级环境变量 CUDA_MONITOR_WECHAT_SECRET"
}

# 部署场景下日志固定写到安装目录，权限已在后面统一收紧。
if (-not $config.runtime) {
    $config.runtime = [PSCustomObject]@{
        log_file         = (Join-Path $InstallDir "monitor.log")
        log_max_bytes    = 5242880
        log_backup_count = 3
    }
}
$config.runtime.log_file = (Join-Path $InstallDir "monitor.log")

# ⚠️ 不要用 Set-Content -Encoding UTF8。
# Windows PowerShell 5.1 的这个写法产出的是「UTF-8 **带 BOM**」，
# 而 Python 的 json 模块读到 BOM 直接抛 "Unexpected UTF-8 BOM" ——
# 程序会死在加载配置这一步，任务状态退回 Ready，且看不出跟权限有任何关系。
# 用 .NET 的 UTF8Encoding($false) 明确要求「不带 BOM」。
$jsonText = $config | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText(
    $configPath,
    $jsonText,
    (New-Object System.Text.UTF8Encoding($false))
)

# 写完回读校验：既要能解析，也要确认首字节不是 BOM。
$head = [System.IO.File]::ReadAllBytes($configPath)[0..2]
if ($head[0] -eq 0xEF -and $head[1] -eq 0xBB -and $head[2] -eq 0xBF) {
    Write-Warn "config.json 竟然带上了 BOM，Python 会解析失败，请手工去掉。"
}
else {
    $null = $jsonText | ConvertFrom-Json
    Write-Ok "配置已写入 $configPath（无 BOM，JSON 可解析）"
}

# ---------------------------------------------------------------- 注册任务

Write-Step "注册计划任务 $TaskName"

$arguments = '"{0}" --config "{1}"' -f (Join-Path $InstallDir "run_monitor.py"), $configPath
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $InstallDir

$trigger = New-ScheduledTaskTrigger -AtStartup

# SYSTEM 账号 + 最高权限：无需保存密码，普通用户无法结束。
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

# 关于任务优先级：New-ScheduledTaskSettingsSet -Priority 的语义官方文档前后矛盾
# （旧文档说 1 最低 10 最高，新文档说 0 最高 10 最低），故意不用它。
# 进程调度优先级改由 config.json 的 process_priority 在进程内设置，语义明确可验证。
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -Hidden `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "监控 GPU 显存占用，超过阈值时通过微信推送告警。" `
    -Force | Out-Null

Write-Ok "任务已注册（开机触发，SYSTEM 身份，最高权限，任务列表里隐藏，失败每分钟重启）"

# ---------------------------------------------------------------- 看门狗

if ($NoWatchdog) {
    Write-Step "跳过看门狗（-NoWatchdog）"
    Write-Warn "没有看门狗时，管理员一次 Stop-ScheduledTask 就能让监控永久停摆。"
}
else {
    Write-Step "注册看门狗任务"

    $watchdogName = "${TaskName}_Watchdog"
    $watchdogScript = Join-Path $InstallDir "scripts\watchdog.ps1"

    if (-not (Test-Path -LiteralPath $watchdogScript)) {
        Write-Warn "找不到 $watchdogScript，看门狗未注册。"
    }
    else {
        $watchdogArgs = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -TaskName "{1}" -InstallDir "{2}"' -f $watchdogScript, $TaskName, $InstallDir
        $watchdogAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $watchdogArgs -WorkingDirectory $InstallDir

        # 3 分钟后开始、每 3 分钟一次 —— 晚 3 分钟是为了让主任务在开机时先起来，别抢跑。
        $watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(3) `
            -RepetitionInterval (New-TimeSpan -Minutes 3)

        # Repetition.Duration 为空才表示"无限重复"。
        # 某些 PowerShell 版本会把它默认填成等于间隔，那样看门狗只跑一次就再也不动了。
        if ($watchdogTrigger.Repetition) {
            $watchdogTrigger.Repetition.Duration = ""
        }
        else {
            Write-Warn "触发器没有生成 Repetition 节点，看门狗可能只运行一次，请手工检查任务设置。"
        }

        $watchdogSettings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -Hidden `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
            -MultipleInstances IgnoreNew

        try {
            Register-ScheduledTask `
                -TaskName $watchdogName `
                -Action $watchdogAction `
                -Trigger $watchdogTrigger `
                -Principal $principal `
                -Settings $watchdogSettings `
                -Description "看门狗：主任务不在运行时自动拉起，防止被强杀后永久停摆。" `
                -Force | Out-Null
            Write-Ok "看门狗 $watchdogName 已注册（每 3 分钟检查一次）"
            Write-Warn "要正规停止监控，用下面的 Disable-ScheduledTask 或 .\uninstall.ps1；"
            Write-Warn "单独 Stop-ScheduledTask 会在 3 分钟内被看门狗拉起来。"
        }
        catch {
            Write-Warn "看门狗注册失败：$($_.Exception.Message)"
            Write-Warn "主任务不受影响，只是失去自动拉起能力。"
        }
    }
}

# ---------------------------------------------------------------- 收紧权限

if ($SkipAcl) {
    Write-Step "跳过 ACL 收紧（-SkipAcl）"
}
else {
    Write-Step "收紧目录权限"

    # 两个关键点，改之前务必读完：
    #
    # ① `/inheritance:r` 与 `/grant:r` 必须在**同一次 icacls 调用**里完成。
    #    分成两条命令时，第一条执行完的瞬间目录 DACL 是空的 ——
    #    连管理员自己都被锁在外面（所有者只有 READ_CONTROL / WRITE_DAC，
    #    没有读目录和写数据的权利），第二条再去递归就报"拒绝访问"。
    #
    # ② **不要加 /T**。带 (OI)(CI) 的 ACE 会被系统自动传播到所有子项，
    #    本来就够用；加 /T 反而会踩到 ①，最后 ACL 停在半完成状态：
    #    SYSTEM 拿不到完全控制 → SYSTEM 身份的计划任务启动即退出，
    #    任务状态停在 Ready，而且不会有任何报错。
    $aclSpec = @(
        "SYSTEM:(OI)(CI)F",
        "BUILTIN\Administrators:(OI)(CI)F",
        "BUILTIN\Users:(OI)(CI)RX"
    )
    $null = icacls $InstallDir /inheritance:r /grant:r $aclSpec /C /Q 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "icacls 返回 $LASTEXITCODE，ACL 可能未完整应用。"
    }

    # config.json 含接口凭据，连读权限都收掉。
    # 必须放在父目录之后，否则会被父目录传播下来的 Users:RX 覆盖。
    $null = icacls $configPath /inheritance:r /grant:r "SYSTEM:F" "BUILTIN\Administrators:F" /C /Q 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "config.json 的 icacls 返回 $LASTEXITCODE。"
    }

    # 绝不"发完命令就报 OK" —— icacls 失败时上面的 OK 就是假象，会把人带偏。
    # 这里回读实际 ACL 做校验。
    $acl = Get-Acl -LiteralPath $InstallDir
    $sysOk = $false
    $admOk = $false
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        $name = "$($rule.IdentityReference)"
        # FullControl = 0x1F01FF
        $full = (([int]$rule.FileSystemRights -band 0x1F01FF) -eq 0x1F01FF)
        if (-not $full) { continue }
        if ($name -match 'SYSTEM') { $sysOk = $true }
        if ($name -match 'Administrators') { $admOk = $true }
    }

    if ($sysOk) {
        Write-Ok "SYSTEM 对 $InstallDir 拥有完全控制"
    }
    else {
        Write-Warn "SYSTEM 没有完全控制！计划任务会启动即退出（状态停在 Ready），且不报错。"
        Write-Warn "手工修复：icacls `"$InstallDir`" /inheritance:r /grant:r `"SYSTEM:(OI)(CI)F`" `"BUILTIN\Administrators:(OI)(CI)F`" `"BUILTIN\Users:(OI)(CI)RX`" /C /Q"
    }

    if ($admOk) {
        Write-Ok "Administrators 拥有完全控制"
    }
    else {
        Write-Warn "Administrators 没有完全控制，后续维护会不便。"
    }

    Write-Ok "普通用户对 $InstallDir 只有读取与执行权限（Users:RX，不含写与删除）"

    Write-Warn "ACL 挡得住普通用户误删，但挡不住管理员夺取所有权后删除。"
    Write-Warn "如需真正的抗删除能力，需要走 Windows 服务化方案。"
}

# ---------------------------------------------------------------- 收尾提示

Write-Step "安装完成"

Write-Host ""
Write-Host "下一步（按顺序执行）：" -ForegroundColor White
Write-Host ""
Write-Host "  1. 确认 config.json 里的微信参数已填好：" -ForegroundColor White
Write-Host "     $configPath" -ForegroundColor Gray
Write-Host "     （若刚才已用 -AppId / -AppSecret / -TemplateId / -ToUsers 传入，可跳过）"
Write-Host ""
Write-Host "  2. 在真机上先跑自检。务必用 python.exe（有控制台），不要用 pythonw.exe：" -ForegroundColor White
Write-Host "     & '$python' '$InstallDir\run_monitor.py' --config '$configPath' --selftest" -ForegroundColor Gray
Write-Host ""
Write-Host "  3. 自检全绿后启动任务：" -ForegroundColor White
Write-Host "     Start-ScheduledTask -TaskName $TaskName" -ForegroundColor Gray
Write-Host ""
Write-Host "  日常管理：" -ForegroundColor White
Write-Host "     查看状态   Get-ScheduledTask -TaskName $TaskName | Select-Object State" -ForegroundColor Gray
Write-Host "     查看日志   notepad '$InstallDir\monitor.log'" -ForegroundColor Gray
Write-Host "     看门狗日志 notepad '$InstallDir\watchdog.log'" -ForegroundColor Gray
Write-Host "     停止服务   Disable-ScheduledTask -TaskName $TaskName" -ForegroundColor Gray
Write-Host "                （必须 Disable；只 Stop 的话看门狗 3 分钟内会拉回来）" -ForegroundColor DarkGray
Write-Host "     卸载全部   .\uninstall.ps1 -InstallDir '$InstallDir'" -ForegroundColor Gray
Write-Host ""

if ($StartNow) {
    Write-Step "按 -StartNow 要求立即启动"
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Ok "任务当前状态：$state"
    Write-Host "    若状态不是 Running，请直接看日志：$InstallDir\monitor.log" -ForegroundColor Gray
}
