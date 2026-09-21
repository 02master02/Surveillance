"""读远程/本地文本文件的诊断小工具：自动识别编码，避免"路径被吃掉"和"乱码"两类事故。

为什么需要它：
1. 远程 cmd 的 `type "C:\\ProgramData\\..."` **经常被吃掉路径**，报
   "系统找不到指定的文件" —— 和"文件真的不存在"长得一模一样，极易误判。
2. 目标机上的文件编码五花八门：UTF-8、UTF-8 with BOM、GBK、UTF-16LE，
   直接 `type` 出来不是乱码就是 NUL 字节，`grep` 还会回一句
   "Binary file (standard input) matches"。
3. 脚本里的**中文关键词**在 GBK 终端上根本没法用 grep 匹配。

本工具用 Python 读，编码按 BOM 自动判定，输出统一成 UTF-8。

用法（在目标机上或本地都行）：

    python tailfile.py <路径>                      # 看尾部 30 行
    python tailfile.py <路径> --tail 80            # 看尾部 80 行
    python tailfile.py <路径> --head 40            # 看头部 40 行
    python tailfile.py <路径> --grep "seed"        # 只看匹配行（大小写不敏感）
    python tailfile.py <路径> --grep "seed" -n     # 带行号
"""

import argparse
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass


def read_text(path: Path) -> str:
    """按 BOM 自动判编码读出文本。目标是"看得见内容"，不追求 100% 正确。"""
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe\x00\x00") or raw.startswith(b"\x00\x00\xfe\xff"):
        return raw.decode("utf-32", errors="replace")
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="读文件（自动判编码），尾部/头部/匹配")
    parser.add_argument("path")
    parser.add_argument("--tail", type=int, default=30)
    parser.add_argument("--head", type=int, default=0)
    parser.add_argument("--grep", default="")
    parser.add_argument("-n", "--number", action="store_true", help="带行号")
    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.is_file():
        print(f"文件不存在或不是普通文件：{path}")
        return 1
    if path.stat().st_size == 0:
        print(f"文件是空的（0 字节）：{path}")
        return 0

    text = read_text(path)
    lines = text.splitlines()
    print(f"# {path}   共 {len(lines)} 行 / {path.stat().st_size} 字节")
    print()

    if args.grep:
        pattern = args.grep.lower()
        selected = [
            (index, line) for index, line in enumerate(lines, start=1)
            if pattern in line.lower()
        ]
        print(f"# 匹配「{args.grep}」共 {len(selected)} 行")
        for index, line in selected:
            print(f"{index:>5}: {line}" if args.number else line)
        return 0

    if args.head:
        selected = list(enumerate(lines[: args.head], start=1))
    else:
        start = max(0, len(lines) - args.tail)
        selected = list(enumerate(lines[start:], start=start + 1))
    for index, line in selected:
        print(f"{index:>5}: {line}" if args.number else line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
