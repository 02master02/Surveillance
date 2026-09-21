#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手工往微信测试号发一条「新格式」模板消息，用于验证模板排版。

背景：微信 2023 内容规范会把「整行只有变量、没有字面文字」的行**整行删掉**，
并且不支持换行、单个中间内容不超过 20 字。所以模板必须写成：

    CUDA 显存监控
    标题：{{title.DATA}}
    1. {{p1.DATA}}
    2. {{p2.DATA}}
    3. {{p3.DATA}}
    4. {{p4.DATA}}
    5. {{p5.DATA}}
    统计：{{usage.DATA}}
    CUDA 显存监控

本脚本按这个形状发一条三进程的样例，用来肉眼确认排版。

用法：

    python tools/probe_template.py
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
    WeChatNotifier,
    expected_fields,
)

CONFIG = ROOT / "tools" / "config.local.json"

SAMPLE = {
    "title": "显存告警 3 个进程",
    "p1": "infer 33512 50%",
    "p2": "python 41872 38%",
    "p3": "train 23456 30%",
    "usage": "新增3个 最高50%",
}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    config = load(CONFIG)
    notifier = WeChatNotifier(config.wechat)

    print("template_id:", config.wechat.template_id)
    print("后台模板内容：")
    print(notifier.template_content())

    fields = notifier.list_template_fields()
    print("后台模板字段：", fields)
    missing = set(expected_fields()) - set(fields)
    if missing:
        print(f"[!] 模板缺少字段：{sorted(missing)}  —— 请先按上面注释里的形状重建模板")

    bare = notifier.bare_variable_lines()
    if bare:
        print(f"[!] 模板有纯变量行（会被平台删掉）：{bare}")

    payload = {f"p{index}": "" for index in range(1, PROCESS_SLOTS + 1)}
    payload.update(SAMPLE)
    print("-" * 60)
    for key, value in payload.items():
        flag = "  <-- 超 20 字" if len(value) > FIELD_CHAR_LIMIT else ""
        print(f"  {key:<6} (长度 {len(value):>2}) = {value}{flag}")
    print("-" * 60)

    ok = notifier.send_template(payload)
    print("发送结果：", "成功（请看手机）" if ok else "失败（见上方日志）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
