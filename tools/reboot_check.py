"""开机自启验证：重启前后各跑一次，对比就知道自启到底成没成。

用法（在目标机上）：

    D:\\Anaconda\\python.exe reboot_check.py

关心四件事：

1. **系统启动时间**（`GetTickCount64` 算，不依赖本地化的 `net statistics`）
   —— 重启后这个值必须变成"刚刚"，否则你根本没重启成功。
2. **主任务是否被"开机"这条路径拉起**：任务「上次运行时间」要落在本次启动之后，
   且主日志里出现新的"监控服务已启动"横幅。
   ⚠️ 只看到进程在跑是不够的 —— 看门狗也能把它拉起来，
   那样证明的是看门狗，不是 BootTrigger。
   ⚠️ **只有当机器是刚重启的（`FRESH_BOOT_SECONDS` 以内）这个判定才有意义**：
   机器已经跑了几小时的话，"上次运行时间在开机之后"是句废话，脚本会明说无法判定。
3. **有没有"本次开机后"新增的 bootstrap 错误**：`cuda_monitor_bootstrap_error.log`
   是日志系统就绪前的唯一出口（SYSTEM 下 `%TEMP%` 可能不存在），
   出现新记录就说明"启动即崩"。**历史遗留的旧记录会按时间戳排除**。
4. **GPU 上的计算进程** —— 重启会杀掉它们，动手前先确认。

关于任务「上次结果」的取值：`0` = 成功退出；`0x41301`（十进制 267009）
= "任务当前正在运行"，也是正常码；其余非零才要当错误看。
"""

import ctypes
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

CREATE_NO_WINDOW = 0x08000000
INSTALL = r"C:\ProgramData\CudaMonitor"
MAIN = "CUDA_Monitor"
WATCH = "CUDA_Monitor_Watchdog"

#: 开机多久以内，才认为"上次运行时间"能证明开机自启。超过这个时长无法判定。
FRESH_BOOT_SECONDS = 900

#: bootstrap_error() 会把错误往这些位置各写一份，逐个找。
BOOTSTRAP_DIRS = [
    os.environ.get("TEMP", ""),
    INSTALL,
    os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
    os.environ.get("PUBLIC", r"C:\Users\Public"),
    r"C:\Windows\system32\config\systemprofile\AppData\Local\Temp",
]

#: schtasks 的非零"上次结果"里，这些其实是正常码。
BENIGN_RESULTS = {
    "0": "成功",
    "267009": "0x41301 任务正在运行（正常）",
    "0x41301": "任务正在运行（正常）",
}

STAMP_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
BOOT_LINE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[INFO\] 监控服务已启动")


def sh(args) -> str:
    proc = subprocess.run(args, capture_output=True, creationflags=CREATE_NO_WINDOW)
    raw = proc.stdout or b""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("gbk", errors="replace") or (proc.stderr or b"").decode("gbk", "replace")


def uptime_seconds() -> float:
    """开机到现在过了多久。不读 WMI，避开本地化字段名。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    return kernel32.GetTickCount64() / 1000.0


def human(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.1f} 分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


def task_list(name: str) -> dict:
    out = sh(["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"])
    keep = ("上次运行时间", "上次结果", "下次运行时间", "模式", "状态", "要运行的任务")
    found = {}
    for line in out.splitlines():
        text = line.strip()
        for key in keep:
            if text.startswith(key) and ":" in text:
                found[key] = text.split(":", 1)[1].strip()
    return found


def parse_task_time(value: str):
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def judge(label: str, stamp, booted_at: datetime, boot_age: float) -> None:
    """判定某时间戳是否落在"本次开机之后"。机器开着太久时明确说无法判定。"""
    if stamp is None:
        print(f"  ⚠️  {label}：读不到时间，无法判定")
        return
    shown = f"{stamp:%Y-%m-%d %H:%M:%S}"
    if boot_age > FRESH_BOOT_SECONDS:
        print(f"  ─  {label}（{shown}）：本机已连续运行 {human(boot_age)}，"
              f"此刻无法据此判定开机自启 —— 重启后再跑一次才有结论")
        return
    if stamp >= booted_at:
        print(f"  ✅ {label}（{shown}）在本次开机（{booted_at:%H:%M:%S}）之后 "
              f"—— 确实是开机拉起的")
    else:
        print(f"  ⚠️  {label}（{shown}）早于本次开机（{booted_at:%Y-%m-%d %H:%M:%S}）"
              f" —— 本次开机后它没被启动过")


print("=" * 68)
print("开机自启验证 @ " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 68)

boot_age = uptime_seconds()
booted_at = datetime.now() - timedelta(seconds=boot_age)
print()
print(f"系统已运行：{human(boot_age)}（{boot_age:.0f} 秒）")
print(f"上次开机时间：{booted_at.strftime('%Y-%m-%d %H:%M:%S')}")
if boot_age > FRESH_BOOT_SECONDS:
    print("           ↑ 不是刚重启的，下面的开机自启判定只能是『无法判定』")

print()
print("── 任务状态 ──")
main_state, watch_state = task_list(MAIN), task_list(WATCH)
for label, state in (("主任务 " + MAIN, main_state), ("看门狗 " + WATCH, watch_state)):
    print(f"  [{label}]")
    for key in ("状态", "模式", "上次运行时间", "上次结果", "下次运行时间"):
        if key in state:
            value = state[key]
            if key == "上次结果":
                note = BENIGN_RESULTS.get(value)
                value = f"{value}（{note}）" if note else f"{value} ← 非正常码，需排查"
            print(f"      {key}: {value}")

print()
print("── 主任务是否由『本次开机』拉起 ──")
judge("主任务上次运行", parse_task_time(main_state.get("上次运行时间", "")),
      booted_at, boot_age)

print()
print("── 进程 ──")
out = sh(["tasklist", "/FI", "IMAGENAME eq pythonw.exe", "/FO", "CSV", "/NH"])
rows = [r for r in out.splitlines() if r.strip() and "INFO" not in r.upper()]
if rows:
    for row in rows:
        cells = [c.strip('"') for c in row.split('","')]
        print(f"  pythonw PID={cells[1]}  会话={cells[2]}#{cells[3]}  内存={cells[4]}")
else:
    print("  ⚠️  没有 pythonw 进程在跑")

print()
print("── 主日志启动横幅 ──")
log_path = os.path.join(INSTALL, "monitor.log")
banners = []
if os.path.isfile(log_path):
    with open(log_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = BOOT_LINE_RE.match(line.strip())
            if match:
                banners.append(datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"))
print(f"  共 {len(banners)} 条，最近 3 条：")
for stamp in banners[-3:]:
    print("    " + stamp.strftime("%Y-%m-%d %H:%M:%S"))
judge("最近一次启动", banners[-1] if banners else None, booted_at, boot_age)

print()
print("── bootstrap 错误日志 ──")
print("   判据不依赖开机时间：失败记录只要早于『最近一次成功启动』就算已恢复。")
newest_success = banners[-1] if banners else None
found_new = False
seen = set()
for directory in BOOTSTRAP_DIRS:
    if not directory or directory in seen:
        continue
    seen.add(directory)
    candidate = os.path.join(directory, "cuda_monitor_bootstrap_error.log")
    if not os.path.isfile(candidate):
        continue
    with open(candidate, encoding="utf-8", errors="replace") as handle:
        lines = handle.read().splitlines()
    stamps = []
    for line in lines:
        match = STAMP_RE.match(line)
        if match:
            stamps.append(datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"))
    newest = max(stamps) if stamps else None
    if newest is None:
        print(f"  ?  {candidate}（{len(lines)} 行，无时间戳，人工看一下）")
        continue
    if newest_success is None:
        found_new = True
        print(f"  ⚠️  {candidate}：有 {len(stamps)} 条失败记录，"
              f"但主日志里一条成功启动横幅都没有")
    elif newest > newest_success:
        found_new = True
        print(f"  ⚠️  {candidate}")
        print(f"      最后一条失败 {newest:%Y-%m-%d %H:%M:%S} "
              f"晚于最近一次成功启动 {newest_success:%Y-%m-%d %H:%M:%S}：")
        for line in lines[-4:]:
            print("        " + line)
    else:
        print(f"  ✅ {candidate} 仅有历史遗留（最后一条 {newest:%Y-%m-%d %H:%M:%S}，"
              f"早于最近一次成功启动 {newest_success:%Y-%m-%d %H:%M:%S}）")
if not found_new:
    print("  → 所有启动失败记录都早于最近一次成功启动，已恢复")

print()
print("── GPU 上的计算进程（重启会杀掉它们，动手前确认）──")
smi_out = sh([
    "nvidia-smi",
    "--query-compute-apps=pid,process_name,used_memory",
    "--format=csv,noheader",
]).strip()
print("  " + (smi_out.replace("\n", "\n  ") if smi_out else "(无 / nvidia-smi 不可用)"))

print()
if boot_age > FRESH_BOOT_SECONDS:
    print("提示：本机不是刚重启的 → 开机自启尚未被真实验证。")
    print("      验证方法：重启目标机，等 1~2 分钟后重跑本脚本，")
    print("      届时『主任务上次运行』与『最近一次启动』都应落在开机之后。")
else:
    print("本机是刚重启的 → 上面两条判定的结论有效。")
print("=" * 68)
