"""只读验证：在真机上确认 CUDA 活动标记能把训练进程和图形/桌面进程分开。

不改目标机上的任何东西，只 import 同目录的 win_gpu_mem.py 跑一遍。

    python probe_cuda_filter.py
"""

import sys

sys.path.insert(0, ".")

import win_gpu_mem as w  # noqa: E402


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    print("=" * 78)
    print("CUDA FILTER VERIFICATION (read-only)")
    print("=" * 78)

    mem_ok, cuda_ok, note = w.probe()
    print(f"memory counter (Local Usage) : {mem_ok}")
    print(f"CUDA engine counter          : {cuda_ok}")
    if note:
        print(f"note                         : {note}")

    usage = w.per_process_mb()
    activity = w.cuda_activity()

    print("")
    print(f"processes with GPU memory   : {len(usage)}")
    print(f"processes with CUDA activity: {len(activity)}")
    print("")
    print(f"{'PID':<8}{'CUDA%':>7}  {'MiB':>10}  {'ACTIVE':<7} NAME")
    print("-" * 78)

    pids = sorted(usage, key=lambda p: -usage[p])
    for pid in pids[:18]:
        name = w.process_image_path(pid) or f"pid {pid}"
        act = activity.get(pid)
        util = f"{act.util_pct:.1f}" if act else "0.0"
        flag = "CUDA" if act else "-"
        print(f"{pid:<8}{util:>7}  {usage[pid]:>10.1f}  {flag:<7} {name}")

    print("")
    print("--- CUDA-active processes (full list) ---")
    if not activity:
        print("  (none)")
    for pid, act in sorted(activity.items(), key=lambda kv: -kv[1].util_pct):
        name = w.process_image_path(pid) or f"pid {pid}"
        print(f"  PID {pid:<8} util={act.util_pct:>6.1f}%  mem={usage.get(pid, 0.0):>9.1f} MiB  {name}")

    # 关键结论：把"有显存但不是 CUDA"的挑出来
    non_cuda = [(p, m) for p, m in usage.items() if p not in activity and m >= 100]
    print("")
    print("--- non-CUDA processes holding >= 100 MiB (would be filtered OUT) ---")
    if not non_cuda:
        print("  (none)")
    for pid, mib in sorted(non_cuda, key=lambda kv: -kv[1])[:10]:
        name = w.process_image_path(pid) or f"pid {pid}"
        print(f"  PID {pid:<8} {mib:>9.1f} MiB  {name}")

    print("")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
