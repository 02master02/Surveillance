"""重启监控服务：/End → 等进程真正消失 → /Run → 校验新横幅。

**为什么不能 `schtasks /End ... & schtasks /Run ...` 连着发**：
`/End` 只是让任务进入"已终止"状态，`pythonw.exe` 进程可能还在退出过程中。
紧接着 `/Run`，新实例会撞上命名互斥体，日志里留一句

    检测到已有实例在运行，本次启动退出。

然后新实例自己退出 —— 你以为重启完了，**其实还在跑旧进程、旧配置**。
这个假象非常隐蔽：任务状态显示"正在运行"，日志也有内容，就是代码/配置没换。
所以必须**轮询到进程真的消失**，再 `/Run`。

用法（在目标机上）：

    python restart_service.py            # 正常重启
    python restart_service.py --dry-run  # 只看当前进程与任务状态，不动手

另外它会打印新启动横幅 —— **旧的 `pythonw` 进程用的是内存里的旧代码，
"日志里出现新横幅"才是代码真的换了的证据**（配合版本号/新增的横幅行一起看）。
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
LOG = os.path.join(INSTALL, "monitor.log")
TASK = "CUDA_Monitor"


def sh(args) -> str:
    proc = subprocess.run(args, capture_output=True, creationflags=CREATE_NO_WINDOW)
    raw = proc.stdout or b""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("gbk", errors="replace") or (proc.stderr or b"").decode("gbk", "replace")


def pythonw_pids() -> list[str]:
    out = sh(["tasklist", "/FI", "IMAGENAME eq pythonw.exe", "/FO", "CSV", "/NH"])
    pids = []
    for line in out.splitlines():
        cells = [c.strip('"') for c in line.split('","')]
        if len(cells) >= 2 and cells[1].isdigit():
            pids.append(cells[1])
    return pids


def banner_count() -> tuple[int, list[str]]:
    """数一数日志里有几条启动横幅，并返回最后两条。用它判断有没有出现新横幅。"""
    if not os.path.isfile(LOG):
        return 0, []
    lines = []
    with open(LOG, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if "监控服务已启动" in line:
                lines.append(line.strip())
    return len(lines), lines[-2:]


dry = "--dry-run" in sys.argv

print("=" * 68)
print("重启监控服务" + ("（dry-run，不动手）" if dry else ""))
print("=" * 68)

before_pids = pythonw_pids()
before_count, before_lines = banner_count()
print(f"当前 pythonw PID：{before_pids or '(无)'}")
print(f"当前启动横幅数：{before_count}")

if dry:
    state = sh(["schtasks", "/Query", "/TN", TASK, "/FO", "LIST"])
    for line in state.splitlines():
        if "模式" in line or "状态" in line:
            print("  " + line.strip())
    raise SystemExit(0)

print()
print("[1/4] 结束任务")
print("   " + (sh(["schtasks", "/End", "/TN", TASK]).strip() or "(已发出结束指令)"))

print("[2/4] 等 pythonw 进程真正消失（最多 30 秒）")
waited = 0
while waited < 30:
    time.sleep(1)
    waited += 1
    left = pythonw_pids()
    if not left:
        print(f"   已退出（等待 {waited}s）")
        break
    if waited % 5 == 0:
        print(f"   仍在退出中 ... {waited}s，PID={left}")
else:
    left = pythonw_pids()
    print(f"   ⚠️ {waited}s 后进程仍在：{left}")
    print("   强行结束（这些是本监控服务自己的进程，任务已 /End，属于清理残留）")
    for pid in left:
        print("   " + sh(["taskkill", "/F", "/PID", pid]).strip())
    time.sleep(3)
    left = pythonw_pids()
    if left:
        print(f"   ✗ 仍有残留 {left}，**不执行 /Run**（否则新实例会撞互斥体自杀）")
        raise SystemExit(1)
    print("   已清理干净")

print("[3/4] 启动任务")
print("   " + (sh(["schtasks", "/Run", "/TN", TASK]).strip() or "(已发出启动指令)"))

print("[4/4] 等待新启动横幅（最多 20 秒）")
ok = False
for _ in range(20):
    time.sleep(1)
    count, lines = banner_count()
    if count > before_count:
        ok = True
        break
print()
if ok:
    print("✅ 出现新启动横幅，代码/配置已生效：")
    for line in lines:
        print("   " + line)
    pids = pythonw_pids()
    print(f"   新 PID：{pids or '(无)'}（旧 {before_pids or '(无)'}）")
else:
    print("✗ 没有出现新的启动横幅 —— 服务可能没起来。请查 monitor.log 与")
    print("  cuda_monitor_bootstrap_error.log（脚本会在多个位置各留一份）。")
print()
print("提示：横幅里的「接收人：N 人」可用来确认改完 to_users 后是否真的生效。")
print("=" * 68)
raise SystemExit(0 if ok else 1)
