"""看门狗自检：查任务上次执行结果 + 手动触发一次 + 看日志尾部。

用法（在目标机上）：

    D:\\Anaconda\\python.exe watchdog_drill.py            # 只查状态、触发一次看能否跑通
    D:\\Anaconda\\python.exe watchdog_drill.py --drill    # 真杀主任务做自愈演练

为什么需要它：计划任务的"上次结果"是判断 PowerShell 脚本有没有语法/运行时错误的
唯一途径（本会话无法直接远程执行 powershell.exe）。0x0 = 脚本正常退出；
非 0 基本就是脚本自身报错，此时"自愈"能力已经悄悄失效。
脚本自身有解析错误时什么都记不到 watchdog.log 里 —— 这是最阴的一种坏法。
"""

import os
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

CREATE_NO_WINDOW = 0x08000000
INSTALL = r"C:\ProgramData\CudaMonitor"
MAIN = "CUDA_Monitor"
WATCH = "CUDA_Monitor_Watchdog"


def sh(args) -> str:
    proc = subprocess.run(args, capture_output=True, creationflags=CREATE_NO_WINDOW)
    out = proc.stdout or b""
    if out[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return out.decode("utf-16", errors="replace")
    return out.decode("gbk", errors="replace") or (proc.stderr or b"").decode("gbk", "replace")


def pythonw_pids() -> list[str]:
    out = sh(["tasklist", "/FI", "IMAGENAME eq pythonw.exe", "/FO", "CSV", "/NH"])
    pids = []
    for line in out.splitlines():
        cells = [c.strip('"') for c in line.split('","')]
        if len(cells) >= 2 and cells[1].isdigit():
            pids.append(cells[1])
    return pids


def watch_state() -> dict:
    out = sh(["schtasks", "/Query", "/TN", WATCH, "/FO", "LIST", "/V"])
    keep = ("上次运行时间", "上次结果", "下次运行时间", "要运行的任务", "模式")
    found = {}
    for line in out.splitlines():
        text = line.strip()
        for key in keep:
            if text.startswith(key) and ":" in text:
                found[key] = text.split(":", 1)[1].strip()
    return found


def tail(path: str, n: int) -> list[str]:
    if not os.path.isfile(path):
        return [f"(缺少 {path})"]
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()[-n:]


if "--drill" in sys.argv:
    print(">>> 演练第一步：结束主任务（模拟被强杀）")
    print("   ", sh(["schtasks", "/End", "/TN", MAIN]).strip() or "(已发出结束指令)")
    time.sleep(6)
    print("    结束后 pythonw PID：", pythonw_pids() or "(无)")
    print()
    print(">>> 现在不要动它，等一个巡检周期（≤3 分钟）后重跑本脚本的查询部分。")

else:
    print(">>> 手动触发一次看门狗（主任务健康时应立即退出、不写日志）")
    print("   ", sh(["schtasks", "/Run", "/TN", WATCH]).strip() or "(已触发)")
    time.sleep(12)

print()
print("=== 看门狗任务状态 ===")
for key, value in watch_state().items():
    print(f"  {key}: {value}")
print()
print("=== 主任务 pythonw PID ===")
print("  ", pythonw_pids() or "(无)")
print()
print("=== watchdog.log 尾部 ===")
for line in tail(os.path.join(INSTALL, "watchdog.log"), 4):
    print("  " + line)
