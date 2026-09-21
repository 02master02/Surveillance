"""接收人管理：把 wechat.to_users 同步成权威的关注者列表。

**为什么专门写个工具，而不是手工改 config.json**：
OpenID 是 28 位大小写混排的串，`g/q`、`0/O`、`l/1` 肉眼分不清。
**禁止手抄、禁止照后台截图认字** —— 抄错一个字就是 `errcode 40003`，
而且往往过很久才被发现（推送一直失败，人却以为配好了）。
本工具直接从微信 API 取关注者列表，一个字符都不会错。

用法（在目标机上）：

    python set_recipients.py --show                  # 只看：当前接收人 + 与关注者交叉核对
    python set_recipients.py --all-followers         # 同步为「全部关注者」
    python set_recipients.py --to <openid> --to ...  # 显式指定（仍校验必须是关注者）

两条必须守住的行为：

1. **只改 `wechat.to_users`，其它键原样保留。**
   绝不能把 Config 对象整个 dump 回去 —— 那会把环境变量里的 `app_secret` 落盘，
   直接破坏"密钥不落盘"的约定。
2. **原地覆盖写，不要"新建临时文件再改名"。**
   改名会丢掉 config.json 上那条「仅 Administrators + SYSTEM」的 DACL，
   凭据就等于对普通用户开放了。写完要回读校验（本脚本会做）。

日志刻意只打到 stderr，**不碰 monitor.log** —— 服务正在跑，
两个进程轮流写同一个 RotatingFileHandler 会互相搅乱，还会误触发轮转。
"""

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

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

from cuda_monitor.config import ConfigError  # noqa: E402
from cuda_monitor.config import load as load_config  # noqa: E402
from cuda_monitor.notifier import WeChatNotifier  # noqa: E402

DEFAULT_CONFIG = r"C:\ProgramData\CudaMonitor\config.json"


def build_logger() -> logging.Logger:
    logger = logging.getLogger("set_recipients")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    return logger


def load_raw(path: Path) -> dict:
    """按 utf-8-sig 读原始 JSON。写入侧一律不带 BOM，读取侧仍宽容处理。"""
    return json.loads(path.read_text(encoding="utf-8-sig"))


def show(config_path: Path, raw: dict, followers: list[dict]) -> None:
    configured = list(raw.get("wechat", {}).get("to_users", []) or [])
    print("=" * 66)
    print("接收人现状")
    print("=" * 66)
    print(f"配置文件：{config_path}")
    print(f"app_secret 字段：{'已填在文件里（建议留空，改走环境变量）' if raw.get('wechat', {}).get('app_secret') else '空（走环境变量，符合约定）'}")
    print()
    print(f"当前接收人 {len(configured)} 个：")
    follower_ids = [item["openid"] for item in followers]
    for index, openid in enumerate(configured, start=1):
        if openid in follower_ids:
            print(f"  {index}. {openid}   ← 在关注者列表里（可送达）")
        else:
            print(f"  {index}. {openid}   ⚠️ 不在关注者列表里，发过去会 43004（未关注）")
    print()
    print(f"测试号关注者共 {len(followers)} 个：")
    for index, item in enumerate(followers, start=1):
        mark = "  <- 已配置" if item["openid"] in configured else ""
        print(f"  {index}. {item['openid']}{mark}")
    print()
    if raw.get("wechat", {}).get("process_slots"):
        print(f"提示：后台模板需有 {raw['wechat']['process_slots']} 个 p 槽位（process_slots）。")
    print("提示：--selftest 只给「第一个」接收人发测试消息（notifier.send_test 的行为）。")
    print("=" * 66)


def write_back(config_path: Path, raw: dict, targets: list[str]) -> bool:
    before = config_path.read_bytes()
    backup = config_path.with_name(
        config_path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    )
    shutil.copy2(config_path, backup)
    print(f"  原文件已备份：{backup}")

    raw.setdefault("wechat", {})["to_users"] = targets
    text = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"

    # 原地覆盖（不要新建再改名）—— 见文件头说明 2。
    config_path.write_text(text, encoding="utf-8")

    ok = True
    after = config_path.read_bytes()
    if after[:3] == b"\xef\xbb\xbf":
        print("  ⚠️ 写出的文件带 BOM —— Python 读取会抛 Unexpected UTF-8 BOM，必须修掉")
        ok = False
    else:
        print(f"  无 BOM ✅（首 3 字节 {after[:3]!r}）")

    try:
        reparsed = load_raw(config_path)
        got = reparsed.get("wechat", {}).get("to_users", [])
        if got == targets:
            print(f"  回读校验通过：to_users 共 {len(got)} 项 ✅")
        else:
            print(f"  ⚠️ 回读不一致：{got!r}")
            ok = False
    except (OSError, ValueError) as exc:
        print(f"  ⚠️ 回读解析失败：{exc}")
        ok = False

    acl = subprocess.run(
        ["icacls", str(config_path)], capture_output=True, text=True
    ).stdout
    if "Users" in acl or "Everyone" in acl:
        print("  ⚠️ ACL 里出现了 Users/Everyone —— 凭据可能已对普通用户开放：")
        for line in acl.splitlines():
            print("      " + line.strip())
        ok = False
    else:
        print("  ACL 仍仅 Administrators + SYSTEM ✅")

    if not ok:
        print(f"  可用备份回滚：copy /Y \"{backup}\" \"{config_path}\"")
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="管理微信告警接收人（OpenID 只从 API 取）")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--show", action="store_true", help="只显示，不修改")
    parser.add_argument("--all-followers", action="store_true", help="同步为全部关注者")
    parser.add_argument("--to", action="append", default=[], help="指定 OpenID，可重复")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    raw = load_raw(config_path)
    logger = build_logger()

    print("正在从微信 API 拉取关注者列表 ...")
    try:
        wechat = load_config(config_path).wechat
    except ConfigError as exc:
        print(f"配置加载失败，终止（未做任何修改）：{exc}")
        return 2
    notifier = WeChatNotifier(wechat, logger)
    followers = notifier.list_followers()
    if not followers:
        print("没有取到关注者（没人关注测试号，或 token 拿不到）。终止，不做任何修改。")
        return 1
    print(f"拿到 {len(followers)} 个关注者。")

    if args.show or (not args.all_followers and not args.to):
        show(config_path, raw, followers)
        return 0

    follower_ids = [item["openid"] for item in followers]
    if args.all_followers:
        targets = follower_ids
    else:
        unknown = [item for item in args.to if item not in follower_ids]
        if unknown:
            print("以下 OpenID 不在关注者列表里，发过去会 43004，已终止：")
            for item in unknown:
                print("  " + item)
            return 1
        targets = list(dict.fromkeys(args.to))

    print()
    print(f"将把 to_users 设为 {len(targets)} 个（原 {len(raw.get('wechat', {}).get('to_users', []) or [])} 个）：")
    for index, openid in enumerate(targets, start=1):
        print(f"  {index}. {openid}")
    print()
    ok = write_back(config_path, raw, targets)
    print()
    print("改完还需重启任务才会生效（配置只在启动时读一次）：")
    print("  schtasks /End /TN CUDA_Monitor & schtasks /Run /TN CUDA_Monitor")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
