"""诊断"是谁在占 GPU"：列出 python 进程的 PID / 启动时间 / 父进程 / 命令行。

动机：监控告警里只有**短进程名**（`python.exe`），而一台机器上可能有十几二十个
`python.exe` 分属不同 conda 环境、不同项目。想知道是哪一个，必须拿到**命令行**和
**启动时间** —— 这两样 PDH 计数器都不给。

用法（在目标机上）：

    D:\\Anaconda\\python.exe whois_gpu.py

输出里会按命令行归并（DataLoader 的 worker 进程命令行完全一样），
并把当前真正持有 GPU 上下文的 PID 标出来。
"""

import subprocess
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

CREATE_NO_WINDOW = 0x08000000
CMD_LIMIT = 260


def sh(args) -> str:
    proc = subprocess.run(args, capture_output=True, creationflags=CREATE_NO_WINDOW)
    raw = proc.stdout or b""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("gbk", errors="replace") or (proc.stderr or b"").decode("gbk", "replace")


def processes() -> list[dict]:
    """拿全部 python.exe 的 PID / 父 PID / 启动时间 / 命令行。

    ⚠️ 必须用 `/format:list`，不能用 `/format:csv`：
    wmic 的 CSV 输出**不给 CommandLine 加引号**，而命令行里到处是逗号
    （`-m torch.distributed.run --nproc_per_node=4 a.py b.py`），
    结果列全部错位，`csv.DictReader` 还会抛
    `AttributeError: 'list' object has no attribute 'strip'`。
    list 格式是 `Key=Value` 一行一个，逗号再多也不影响。
    """
    text = sh([
        "wmic", "process", "where", "name='python.exe'", "get",
        "ProcessId,ParentProcessId,CreationDate,CommandLine", "/format:list",
    ])

    rows: list[dict] = []
    current: dict = {}

    def flush() -> None:
        if current.get("ProcessId", "").isdigit():
            rows.append({
                "pid": current["ProcessId"],
                "ppid": current.get("ParentProcessId", ""),
                "created": current.get("CreationDate", ""),
                "cmd": current.get("CommandLine", ""),
            })

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            flush()
            current = {}
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        current[key.strip()] = value.strip()
    flush()
    return rows


def gpu_holders() -> dict[str, str]:
    """当前 nvidia-smi 认得、真正持有 GPU 上下文的进程：pid -> process_name。"""
    text = sh([
        "nvidia-smi", "--query-compute-apps=pid,process_name", "--format=csv,noheader",
    ])
    holders = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            holders[parts[0]] = parts[1]
    return holders


def parent_name(ppid: str) -> str:
    if not ppid.isdigit():
        return "?"
    text = sh(["wmic", "process", "where", f"ProcessId={ppid}", "get", "Name", "/value"])
    for line in text.splitlines():
        if line.strip().startswith("Name="):
            return line.split("=", 1)[1].strip()
    return "(已退出)"


rows = processes()
holders = gpu_holders()

print("=" * 78)
print(f"python.exe 进程共 {len(rows)} 个；nvidia-smi 认得持有 GPU 上下文的 {len(holders)} 个")
print("=" * 78)
print()

holders_in_our_list = set(holders) & {r["pid"] for r in rows}

print("── 真正持有 GPU 上下文的（PID 是 nvidia-smi 给的，进程名是完整路径）──")
for pid, name in holders.items():
    tag = "  ← 在 python.exe 列表里" if pid in holders_in_our_list else ""
    print(f"  PID {pid}: {name}{tag}")
print()

# 命令行归并：DataLoader worker 的命令行与主进程完全一致，归并后一眼能看出有几个"批次"
groups: dict[str, list[dict]] = {}
for row in rows:
    groups.setdefault(row["cmd"], []).append(row)

print(f"── 按命令行归并（{len(groups)} 种命令行）──")
for index, (cmd, items) in enumerate(
    sorted(groups.items(), key=lambda kv: -len(kv[1])), start=1
):
    items_sorted = sorted(items, key=lambda r: r["created"])
    marked = [r for r in items_sorted if r["pid"] in holders_in_our_list]
    head = "★" if marked else " "
    print(f"{head} [{index}] {len(items)} 个进程"
          + (f"，其中持有 GPU 的是 PID {', '.join(r['pid'] for r in marked)}" if marked else ""))
    print(f"      最早启动 {items_sorted[0]['created']}   最晚启动 {items_sorted[-1]['created']}")
    print(f"      父进程 PID {items_sorted[0]['ppid']} ({parent_name(items_sorted[0]['ppid'])})")
    shown = cmd if len(cmd) <= CMD_LIMIT else cmd[:CMD_LIMIT] + " ..."
    print(f"      命令行：{shown}")
    if len(items) <= 6:
        print(f"      PID：{', '.join(r['pid'] for r in items_sorted)}")
    print()

print("── 命令行里出现的解释器/脚本路径（判断是哪个环境、哪个项目）──")
for token in Counter(
    part for row in rows for part in row["cmd"].replace('"', " ").split()
    if part.lower().endswith((".py", ".exe", ".yaml", ".yml", ".pyc"))
).most_common(12):
    print(f"  {token[1]:>3} 次  {token[0]}")
print("=" * 78)
