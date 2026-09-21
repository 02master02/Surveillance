"""只读健康检查：把 monitor.log 里的关键事件时间线抽出来，判断服务是否真的在跑。

动机：看门狗反复"已重新启动"既可能是真被杀了、也可能是主程序启动即崩，
两种情况在 watchdog.log 里长得一样。要区分必须看 monitor.log 有没有对应的启动横幅。

用法（在目标机上）：

    D:\\Anaconda\\python.exe log_timeline.py
"""

import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

CREATE_NO_WINDOW = 0x08000000
INSTALL = r"C:\ProgramData\CudaMonitor"

KEYS = (
    "监控服务已启动",   # 启动横幅（含生效口径、接收人数）
    "接收人",
    "告警",             # 覆盖「告警条件」「新告警」「命中告警」「自愈告警」
    "已结束",
    "补发",
    "推送成功",
    "推送失败",
    "失败",             # 采集失败 / 连续失败 / 启动失败
    "采集已恢复",
    "Traceback",
    "错误",
)

path = os.path.join(INSTALL, "monitor.log")
text = open(path, encoding="utf-8", errors="replace").read()
lines = text.splitlines()

print(f"monitor.log 行数={len(lines)}  大小={os.path.getsize(path)}  "
      f"最后修改={__import__('time').strftime('%Y-%m-%d %H:%M:%S', __import__('time').localtime(os.path.getmtime(path)))}")
print()
print("=== 关键事件（按时间顺序）===")
for line in lines:
    if any(k in line for k in KEYS):
        print("  " + line)

print()
print("=== 尾部 12 行 ===")
for line in lines[-12:]:
    print("  " + line)

print()
print("=== 当前进程 ===")
out = subprocess.run(
    ["tasklist", "/FI", "IMAGENAME eq pythonw.exe", "/FO", "CSV", "/NH"],
    capture_output=True, creationflags=CREATE_NO_WINDOW,
).stdout.decode("gbk", errors="replace")
print("  " + (out.strip() or "(无 pythonw 进程)"))

print()
print("=== 任务状态 ===")
for name in ("CUDA_Monitor", "CUDA_Monitor_Watchdog"):
    out = subprocess.run(
        ["schtasks", "/Query", "/TN", name, "/FO", "LIST"],
        capture_output=True, creationflags=CREATE_NO_WINDOW,
    ).stdout.decode("gbk", errors="replace")
    for line in out.splitlines():
        if "模式" in line or "下次运行" in line or "任务名" in line:
            print(f"  [{name}] {line.strip()}")
