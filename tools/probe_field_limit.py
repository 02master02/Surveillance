#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实测微信模板消息「单个字段不超过 20 字」到底按什么算。

## 为什么要实测

官方公告（《关于规范公众号模板消息的再次公告》，2023-05-04 生效）的原话是：

    中间的主内容中，单个字段内容不超过20个字，且不支持换行

它没写清楚这「20 字」算的是**字段值本身**，还是**整行（含模板里的
`1. ` 这类字面前缀）**。两种理解下，一行里能写进多少信息差 3 个字 ——
而真机告警正文「目录名 + PID + 百分比」（`mmdet_py39 11672 86%` = 20 字）
正好卡在这 3 个字上：

    按「字段值 ≤ 20」算 → `mmdet_py39 11672 86%` 能完整显示
    按「整行   ≤ 20」算 → 只能显示到 `mmdet_py39 11672`

告警正文里放不下「是哪个进程」的信息，用户就只能再去翻日志 ——
所以这 3 个字值一次实测。

## 怎么读结果

发出去的是 5 行，长度分别 17 / 18 / 19 / 20 / 22 字，
每行**结尾**是一个标记 `|NN`（NN = 这一行的字符数）。
平台截断是从尾部砍的，所以：

    手机上还能看到 `|NN` 的行 = 完整送达
    看不到 `|NN` 尾巴的行 = 被截断了

看一眼哪几行带尾巴，就知道真正的上限落在哪。

## 用法

    python tools/probe_field_limit.py            # 发给第一个接收人
    python tools/probe_field_limit.py --dry-run  # 只打印，不发送

只发给 `to_users[0]`，不会打扰其余接收人 —— 这是探针，不是告警。
"""

from __future__ import annotations

import logging
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cuda_monitor.config import load  # noqa: E402
from cuda_monitor.notifier import (  # noqa: E402
    FIELD_CHAR_LIMIT,
    PROCESS_SLOTS,
    VALUE_CHAR_LIMIT,
    WeChatNotifier,
    _mask,
    expected_fields,
)

CONFIG = ROOT / "tools" / "config.local.json"

#: 探针各行长度。17 = 当前实现的上限；20 = 官方公告的数字；22 = 故意超一点。
PROBE_LENGTHS = (17, 18, 19, 20, 22)

#: 每行用不同的填充字符，方便一眼对上号。
FILLERS = "ABCDE"


def probe_value(length: int, filler: str) -> str:
    """造一个正好 `length` 字、且结尾带 `|NN` 标记的字段值。

    标记在结尾是刻意的：平台截断砍尾部，标记没了就是被砍了。
    """
    marker = f"|{length:02d}"
    body = filler * (length - len(marker))
    return body + marker


def build_probe() -> dict[str, str]:
    payload: dict[str, str] = {"title": "字段长度探针", "usage": "哪些行带|NN"}
    for index in range(1, PROCESS_SLOTS + 1):
        if index <= len(PROBE_LENGTHS):
            payload[f"p{index}"] = probe_value(
                PROBE_LENGTHS[index - 1], FILLERS[index - 1]
            )
        else:
            payload[f"p{index}"] = ""
    return payload


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    dry = "--dry-run" in sys.argv

    config = load(CONFIG)
    notifier = WeChatNotifier(config.wechat)

    if not config.wechat.to_users:
        print("config.local.json 里没有接收人，无法发送。")
        return 1

    payload = build_probe()

    print("=" * 62)
    print("字段长度探针")
    print("=" * 62)
    print(f"当前实现：FIELD_CHAR_LIMIT={FIELD_CHAR_LIMIT}"
          f"（含模板前缀），VALUE_CHAR_LIMIT={VALUE_CHAR_LIMIT}（字段值）")
    print("")

    content = notifier.template_content()
    if content:
        print("后台模板内容（`{{字段.DATA}}` 未替换）：")
        for line in content.splitlines():
            print("   " + line)
        missing = set(expected_fields(config.wechat.process_slots)) - set(
            notifier.list_template_fields()
        )
        if missing:
            print(f"[!] 模板缺少字段：{sorted(missing)}")
    else:
        print("[!] 拉不到后台模板内容（检查网络 / 凭据），下面按字段列出。")

    print("")
    print("将要发送的字段：")
    for key, value in payload.items():
        print(f"   {key:<6} 长度 {len(value):>2}  {value}")

    print("")
    print("预期在手机上看到的样子（模板前缀 + 字段值）：")
    rendered = notifier.render_template(payload)
    print(rendered or "   （拉不到模板，跳过）")

    print("")
    print("-" * 62)
    print("收件人：", _mask(config.wechat.to_users[0]))
    print("读法：哪一行的结尾还带着 `|NN`，那一行就是完整送达的。")
    print("-" * 62)

    if dry:
        print("dry-run：没有发送。")
        return 0

    ok = notifier.send_template(payload, to_user=config.wechat.to_users[0])
    print("发送结果：", "成功，请看手机" if ok else "失败，见上方日志")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
