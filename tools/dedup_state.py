"""去重记忆（已提醒过的进程名单）的查看与维护。

落盘之后，"为什么这个进程没再提醒"就变成了一个需要查的东西 ——
重启后先跑一次 `--show`，确认在跑的进程还在名单里，才说明
「重启不会重复推送」真的生效了。

用法（在目标机上）：

    python dedup_state.py                       # 显示当前名单（默认动作）
    python dedup_state.py --forget 11672        # 忘掉某个 PID，让它下次重新提醒
    python dedup_state.py --clear               # 清空整份名单

三条边界，改本工具之前先读：

1. **只读写状态文件，绝不碰 config.json。**
   config.json 上挂着"仅 Administrators + SYSTEM"的 ACL，重建文件会把它弄丢；
   而且把 Config 对象整个 dump 回去会把环境变量里的 app_secret 落到磁盘上。
2. **日志只打到 stderr，不碰 monitor.log。**
   服务正在跑，两个进程轮流写同一个 RotatingFileHandler 会互相搅乱，还会误触发轮转。
3. **`--forget` / `--clear` 之后，那个进程下次达到触发条件会重新提醒一次** ——
   这就是这两个操作的用途。重启本身**不会**清空这份名单（那正是要修的毛病）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

_CANDIDATE_ROOTS = [
    Path(__file__).resolve().parents[1],          # 脚本本来就放在安装目录的 tools\ 下
    Path(r"C:\ProgramData\CudaMonitor"),          # 或放在任意位置，指到默认安装目录
]
for _root in _CANDIDATE_ROOTS:
    if (_root / "src" / "cuda_monitor" / "__init__.py").is_file():
        sys.path.insert(0, str(_root / "src"))
        break
else:
    print("找不到 cuda_monitor 包。请把本脚本放到 <安装目录>\\tools\\ 下，"
          "或用 PYTHONPATH 指向安装目录的 src\\。")
    sys.exit(2)

from cuda_monitor import win_gpu_mem  # noqa: E402
from cuda_monitor.config import ConfigError  # noqa: E402
from cuda_monitor.config import load as load_config  # noqa: E402

DEFAULT_CONFIG = r"C:\ProgramData\CudaMonitor\config.json"


def _candidates(explicit: Optional[str] = None) -> List[Path]:
    """config.json 的候选位置：显式指定 > 安装目录默认 > 脚本旁边的几处。"""
    roots: List[Path] = []
    if explicit:
        roots.append(Path(explicit))
    roots.append(Path(DEFAULT_CONFIG))
    roots.extend(root / "config.json" for root in _CANDIDATE_ROOTS)
    return roots


def resolve_state_path(args: argparse.Namespace) -> Optional[Path]:
    """优先用 --state 直接指定；否则从 config.json 的 runtime.state_file 取。"""
    if args.state:
        return Path(args.state)

    tried: List[Path] = []
    for path in _candidates(args.config):
        tried.append(path)
        if not path.is_file():
            continue
        try:
            config = load_config(path)
        except (ConfigError, OSError) as exc:
            print(f"[警告] 读不了配置 {path}：{exc}", file=sys.stderr)
            continue
        if not config.runtime.state_file:
            print(f"配置 {path} 里 runtime.state_file 是空的 —— "
                  "去重记忆没有落盘，重启后同一进程会被重复提醒。", file=sys.stderr)
            return None
        return Path(config.runtime.state_file)

    print("找不到 config.json（试过：" + "、".join(str(item) for item in tried) + "）",
          file=sys.stderr)
    print("用 --state <文件> 直接指定状态文件的位置。", file=sys.stderr)
    return None


def read_state(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        print(f"状态文件还不存在：{path}")
        print("  要么一次告警都没发过，要么这份记忆没有落盘（runtime.state_file 为空）。")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"[错误] 状态文件读不出来：{path}\n        {exc}", file=sys.stderr)
        return None


def write_state(path: Path, payload: Dict[str, Any]) -> bool:
    """原子落盘：先写 .tmp 再替换，中途断电也不会留下半个 JSON。"""
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        print(f"[错误] 写盘失败：{path}\n        {exc}", file=sys.stderr)
        return False
    return True


def _stamp(value: Any) -> str:
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError):
        return "—"


def show(path: Path, payload: Dict[str, Any]) -> None:
    entries = payload.get("entries")
    entries = entries if isinstance(entries, list) else []

    print(f"去重记忆：{path}")
    print(f"  写于 {_stamp(payload.get('saved_at'))}；共 {len(entries)} 条")
    if not entries:
        print("")
        print("  名单是空的：任何进程达到触发条件都会被提醒。")
        return

    print("  这些进程只要还在跑，就不会再提醒第二次。")
    # 刻意不做等宽表格：这个字段里有中文（"指标"）和中文提示语，
    # 按字符数补齐会被中文按 2 列显示顶歪（`format_snapshot` 踩过同一个坑）。
    # 一条一块、完整路径单独一行，比对齐好看得多，也不怕路径变长。
    print("")

    alive = 0
    for index, item in enumerate(entries, start=1):
        if not isinstance(item, dict):
            continue
        identity = item.get("identity") or [0, 0, "?"]
        alert = item.get("alert") or {}
        gpu = identity[0] if len(identity) > 0 else "?"
        pid = identity[1] if len(identity) > 1 else "?"
        short = identity[2] if len(identity) > 2 else "?"
        full_path = str(alert.get("process_path") or "")

        # 只回答"这个 PID 现在有没有进程"，不确认是不是同一个进程 ——
        # PID 会被系统复用，真要区分得靠进程启动时间，而采集层拿不到。
        exists = bool(win_gpu_mem.process_image_path(int(pid))) if str(pid).isdigit() else False
        alive += exists

        value = alert.get("metric_value")
        metric = "触发值未记录" if value is None else f"{value:.0f}% {alert.get('metric_label') or ''}".strip()
        print(f"  {index}) GPU {gpu} · PID {pid} · {short}")
        print(f"     首次提醒 {_stamp(item.get('notified_at'))} · 触发时 {metric}"
              f" · 现在{'仍在运行' if exists else '没有这个 PID 了'}")
        if full_path:
            print(f"     {full_path}")

    print("")
    print(f"  其中 {alive} 个 PID 现在还活着"
          + ("；其余的在监控没在场的时段结束了，不会补发结束通知。"
             if alive < len(entries) else "。"))
    print("  记忆只在进程确认退出时清除，所以这里的条目不会因为掉到阈值以下而消失。")
    print("  要让它重新提醒：--forget <PID>。")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="dedup_state",
        description="查看/维护去重记忆（已提醒过的进程名单）。默认只显示。",
    )
    parser.add_argument("--config", help="config.json 路径（用于取 runtime.state_file）")
    parser.add_argument("--state", help="直接指定状态文件路径，跳过 config.json")
    parser.add_argument("--forget", type=int, action="append", default=[],
                        metavar="PID", help="忘掉这个 PID（下次达到触发条件会重新提醒）")
    parser.add_argument("--clear", action="store_true", help="清空整份名单")
    args = parser.parse_args()

    path = resolve_state_path(args)
    if path is None:
        return 2

    if args.clear:
        if not path.is_file():
            print(f"状态文件不存在，无需清空：{path}")
            return 0
        payload = read_state(path)
        if payload is None:
            return 1
        payload["entries"] = []
        payload["saved_at"] = time.time()
        if not write_state(path, payload):
            return 1
        print(f"已清空去重记忆：{path}")
        print("  重启服务后，任何进程达到触发条件都会被提醒一次。")
        return 0

    if args.forget:
        payload = read_state(path)
        if payload is None:
            return 1
        entries = payload.get("entries") or []
        wanted = set(args.forget)
        kept = [
            item for item in entries
            if not (isinstance(item, dict)
                    and isinstance(item.get("identity"), (list, tuple))
                    and len(item["identity"]) > 1
                    and item["identity"][1] in wanted)
        ]
        removed = len(entries) - len(kept)
        payload["entries"] = kept
        payload["saved_at"] = time.time()
        if not write_state(path, payload):
            return 1
        print(f"已忘掉 {removed} 条（请求 PID：{sorted(wanted)}）")
        for pid in sorted(wanted):
            print(f"  PID {pid} 下次达到触发条件会重新提醒。")
        return 0

    payload = read_state(path)
    if payload is None:
        return 1 if path.is_file() else 0
    show(path, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
