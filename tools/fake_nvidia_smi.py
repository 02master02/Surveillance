#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""假的 nvidia-smi —— 专供没有 NVIDIA 显卡的开发机做联调。

用法：把 config 里的 "nvidia_smi" 指向本文件即可，
采集层检测到 .py 后缀会用当前解释器执行它。

    python run_monitor.py --config tools/config.dev.json --once
    python run_monitor.py --config tools/config.dev.json --selftest

除了 `--query-gpu` / `--query-compute-apps`，本脚本还额外支持一个
**只有它才认的**开关：

    --query-cuda-activity    每行输出 "pid, cuda_util"，表示该进程中
                             CUDA 引擎当前利用率为多少个百分点

为什么需要它：真机上这张表来自 Windows 性能计数器
（`\GPU Engine(*)\...engtype_Cuda`），开发机没有真实显卡拿不到，
于是所有假进程都会被标成"非 CUDA"，`metric=cuda_util` 在本地一条都触发不了。
采集层检测到 smi 路径是 .py 就走这条通路，真机不受影响。

场景由环境变量 CUDA_MONITOR_FAKE_SCENARIO 控制：

    normal  默认。两张卡 + 三个进程，其中两个 CUDA 利用率超过 15%
    many    两张卡 + 三个进程，**三个全部超过 15%**，用来验证一条推送列全
    empty   两张卡，但没有任何计算进程
    fail    模拟驱动异常：往 stderr 打 NVML 报错并以 255 退出
    single  单卡单进程，用于最小化验证
    evolve  按时间循环切换进程集合与利用率，用来演示"同一进程只提醒一次"
    wddm    **单张 48GB 卡，所有进程的 used_memory 都是 [N/A]**。
            复刻 Windows 真机（GeForce 跑 WDDM 显示驱动模型）的行为：
            整卡数字正常，但逐进程显存一律拿不到 —— 采集层此时必须
            切到性能计数器兜底，否则判定层永远是空集合、一个告警都不发。
            注意：真机上兜底读的是真实性能计数器；开发机上跑这个场景
            拿到的是本机真实进程，本场景在开发机只用来验证"N/A 行被识别"。

本文件只在开发期使用，部署到真机时不会被复制。
"""

from __future__ import annotations

import os
import sys
import time

GPU_0 = "GPU-11111111-2222-3333-4444-555555555555"
GPU_1 = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

# 字段顺序：uuid, pid, process_name, used_memory(MiB)
# 第二个进程名里刻意带一个逗号，用来验证采集层的容错解析。
APPS = {
    "normal": [
        f"{GPU_0}, 41872, C:\\Python311\\python.exe, 9216",
        f"{GPU_0}, 23456, C:\\Program Files\\Bench Kit, Inc\\train.exe, 2048",
        f"{GPU_1}, 33512, C:\\Python311\\python.exe, 3072",
    ],
    "single": [
        f"{GPU_0}, 41872, C:\\Python311\\python.exe, 9216",
    ],
    # 三个进程同时超阈值，用来验证"一条推送列出全部"。
    "many": [
        f"{GPU_0}, 41872, C:\\Python311\\python.exe, 9216",
        f"{GPU_0}, 23456, C:\\Program Files\\Bench Kit, Inc\\train.exe, 7370",
        f"{GPU_1}, 33512, C:\\Python311\\infer.exe, 5120",
    ],
    "empty": [],
    # WDDM 场景：整卡数字正常，但逐进程一律 [N/A]。
    # 进程清单照着真机抄：一个训练进程 + 一串桌面进程。
    "wddm": [
        f"{GPU_0}, 26660, D:\\Anaconda\\envs\\yolo_ultra\\python.exe, [N/A]",
        f"{GPU_0}, 24376, D:\\Program Files\\SunloginClient\\agent\\SunloginClient.exe, [N/A]",
        f"{GPU_0}, 2108, C:\\Windows\\System32\\dwm.exe, [N/A]",
        f"{GPU_0}, 27512, D:\\Microsoft VS Code\\Code.exe, [N/A]",
    ],
}

# 每个场景的 CUDA 引擎活动：{pid: 当前瞬时利用率%}。
#
# 出现在这张表里的 PID = 在跑 CUDA 的进程（active=True）；
# 不在表里的 = 图形/桌面进程（active=False，util=0）。
# 真机上这张表来自性能计数器，开发机由本脚本一并提供。
CUDA_ACTIVITY = {
    # 41872 42% 与 33512 30% 超过 15% 触发线；23456 只有 8%，不触发。
    "normal": {41872: 42.0, 23456: 8.0, 33512: 30.0},
    "single": {41872: 42.0},
    "many": {41872: 37.5, 23456: 30.0, 33512: 50.0},
    "empty": {},
    # 真机那个训练进程。开发机用 wddm 场景时进程来自真实计数器，
    # 这里主要给真机自检留个参照。
    "wddm": {26660: 66.6},
}

# 字段顺序：index, uuid, name, memory.total, memory.used, utilization.gpu
GPUS = {
    "normal": [
        f"0, {GPU_0}, NVIDIA GeForce RTX 4090, 24564, 11264, 42",
        f"1, {GPU_1}, NVIDIA GeForce RTX 3080, 10240, 3072, 8",
    ],
    "single": [
        f"0, {GPU_0}, NVIDIA GeForce RTX 4090, 24564, 11264, 42",
    ],
    "empty": [
        f"0, {GPU_0}, NVIDIA GeForce RTX 4090, 24564, 1024, 0",
        f"1, {GPU_1}, NVIDIA GeForce RTX 3080, 10240, 0, 0",
    ],
    # 真机的 48GB 卡：49140 MiB。整卡占用在 1500 ↔ 3100 之间来回摆动，
    # 数字取自 2026-09-18 的实测采样。
    "wddm": [
        f"0, {GPU_0}, NVIDIA GeForce RTX 4090, 49140, 3053, 60",
    ],
}


# evolve 场景：每个相位持续这么多秒，循环播放。配合 poll_interval_sec=5 时，
# 每个相位大约扫描 3 次，只有第一次会产生推送，后两次静默。
#
# 这条时间线专门用来演示「同一进程只提醒一次」，三个阶段各有看点：
#   相位 0  A、B 都达标        → 推一条，两个进程
#   相位 1  A 回落到 5%，B 不变 → **静默**。A 已提醒过就不再重推，
#                                这正是旧迟滞逻辑做不到的地方
#   相位 2  A 退出、C 新达标    → 混合轮：推 "C（新）"，并补一条 "A（已结束）"
EVOLVE_PHASE_SECONDS = 15

# 每相位一项：(进程列表, CUDA 活动表)
EVOLVE_PHASES = [
    (
        [
            f"{GPU_0}, 41872, C:\\Python311\\python.exe, 9216",
            f"{GPU_1}, 33512, C:\\Python311\\infer.exe, 3072",
        ],
        {41872: 60.0, 33512: 40.0},
    ),
    (
        [
            f"{GPU_0}, 41872, C:\\Python311\\python.exe, 9216",
            f"{GPU_1}, 33512, C:\\Python311\\infer.exe, 3072",
        ],
        {41872: 5.0, 33512: 40.0},
    ),
    (
        [
            f"{GPU_1}, 33512, C:\\Python311\\infer.exe, 3072",
            f"{GPU_0}, 51900, C:\\Python311\\serve.exe, 6144",
        ],
        {33512: 40.0, 51900: 25.0},
    ),
]


def _evolve_phase() -> int:
    return int(time.time() / EVOLVE_PHASE_SECONDS) % len(EVOLVE_PHASES)


def resolve_apps(scenario: str) -> list[str]:
    if scenario == "evolve":
        return EVOLVE_PHASES[_evolve_phase()][0]
    return APPS.get(scenario, APPS["normal"])


def resolve_cuda(scenario: str) -> dict[int, float]:
    if scenario == "evolve":
        return EVOLVE_PHASES[_evolve_phase()][1]
    return CUDA_ACTIVITY.get(scenario, {})


def main(argv: list[str]) -> int:
    scenario = os.environ.get("CUDA_MONITOR_FAKE_SCENARIO", "normal").strip().lower()
    joined = " ".join(argv)

    if scenario == "fail":
        sys.stderr.write("Failed to initialize NVML: Unknown Error\n")
        return 255

    # 必须在 query-compute-apps 之前判：两者都带 "query-" 前缀。
    if "--query-cuda-activity" in joined:
        for pid, util in resolve_cuda(scenario).items():
            print(f"{pid}, {util:g}")
        return 0

    if "query-compute-apps" in joined:
        for line in resolve_apps(scenario):
            print(line)
        return 0

    if "query-gpu" in joined:
        for line in GPUS.get(scenario, GPUS["normal"]):
            print(line)
        return 0

    # 没带 --query-* 参数时，模拟一次人类可读的概览输出。
    print("Fri Sep 18 11:49:51 2026")
    print("+-----------------------------------------------------------------------------+")
    print("|  (fake nvidia-smi — 开发机模拟输出)                                          |")
    print("+-----------------------------------------------------------------------------+")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
