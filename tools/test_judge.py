"""判定层行为验证：触发条件、**同一进程只提醒一次**、推送节流、CUDA 筛选、进程标识、跨重启记忆。

不需要 GPU，不需要网络，纯本地跑：

    python tools/test_judge.py

2026-09-18 按用户要求改过两次语义，核心是这两句：

    只要 cuda 占用超过百分之十五就发送提醒，但是同一进程不要重复提醒
    相同进程在结束前永远不重复推送

改动要点（改 judge.py 之前先读这里）：

* 触发指标默认是 **CUDA 引擎利用率 > 15%**（不是显存占比）。
* 去重靠 `_notified` 记忆，**只在进程退出时清除**，不是在跌破阈值时清除。
  这条是防刷屏的关键：CUDA 利用率在一轮里 66%、间歇掉到 0%，
  如果在低谷清记忆，每个周期都会重推一条。
* 迟滞（退出线）因此被删掉了 —— 它本来就是为了防集合反复进出，
  而"按进程去重"比它更强。
* **退出要缺席确认**：连着 `EXIT_MISS_LIMIT`（3）轮没见到才算退出。
  因为 `collect()` 拿不到数据时返回的是空列表而不是异常，一见不到就判退出
  会先推"已结束"、下一轮又当新告警推一遍。所以下面的时间轴里，
  进程消失后要再走两轮才出现结束通知 —— 这不是测试写慢了，是刻意的。
* **记忆要落盘**：重启后从状态文件接着算，同一进程照样不重复提醒（场景九）。
* 告警正文用**完整镜像路径**（`D:\\Anaconda\\envs\\yolo_ultra\\python.exe`），
  不再是短名 `python.exe` —— 真机上 20 多个 python 只写短名认不出是哪个。
  微信有 20 字硬限制，按「目录\\程序名 → 目录 → 程序名」阶梯降级，见场景八。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
from dataclasses import replace
from typing import List, Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cuda_monitor import win_gpu_mem  # noqa: E402
from cuda_monitor.collector import (  # noqa: E402
    SOURCE_PDH,
    Gpu,
    ProcessUsage,
    Snapshot,
    _apps_via_pdh,
    list_compute_apps,
    path_label,
)
from cuda_monitor.config import (  # noqa: E402
    DEFAULT_STATE_FILE,
    METRIC_CUDA_MEMORY,
    METRIC_CUDA_UTIL,
    ConfigError,
    Runtime,
    Thresholds,
    _parse_thresholds,
)
from cuda_monitor.config import load as load_config  # noqa: E402
from cuda_monitor.judge import (  # noqa: E402
    EXIT_MISS_LIMIT,
    Alert,
    BatchJudge,
    Decision,
    format_snapshot,
)
from cuda_monitor.notifier import _format_process, build_alert_payload  # noqa: E402

TOTAL_MB_24G = 24564.0
#: 真机那张卡：RTX 4090 48GB 版。
TOTAL_MB_48G = 49140.0

FAKE_SMI = ROOT / "tools" / "fake_nvidia_smi.py"

# --- 真机实测的参考数字（48GB 卡，2026-09-18 采样）---------------------------
JOB_PID = 26660
JOB_NAME = r"D:\Anaconda\envs\yolo_ultra\python.exe"
JOB_IDLE_MB = 737.5
JOB_PEAK_MB = 2359.5
JOB_CUDA_UTIL_PEAK = 66.6
DESKTOP_MB = 158.9


def gpu48() -> Gpu:
    return Gpu(0, "GPU-0", "NVIDIA GeForce RTX 4090", TOTAL_MB_48G, 3053.0, 60.0)


def snap48(*apps: ProcessUsage) -> Snapshot:
    return Snapshot(gpus=[gpu48()], apps=list(apps), source=SOURCE_PDH)


def job(util: float, mib: float = JOB_PEAK_MB, pid: int = JOB_PID) -> ProcessUsage:
    """训练进程。真机实测：空闲 737.5MiB，峰值 2359.5MiB / CUDA 66.6%。"""
    return ProcessUsage(
        gpu_index=0, gpu_uuid="GPU-0", pid=pid, name=JOB_NAME,
        used_memory_mb=mib, cuda_active=True, cuda_util_pct=util,
    )


def desktop(pid: int = 24376, util: float = 0.0, mib: float = DESKTOP_MB) -> ProcessUsage:
    """桌面进程（向日葵）。真机实测 CUDA 利用率 0，但有 VideoEncode/Compute_0 活动。"""
    return ProcessUsage(
        gpu_index=0, gpu_uuid="GPU-0", pid=pid,
        name=r"D:\SunloginClient\SunloginClient.exe",
        used_memory_mb=mib, cuda_active=False, cuda_util_pct=util,
    )


def describe(decision: Optional[Decision]) -> str:
    if decision is None:
        return "静默"
    if decision.cleared:
        return f"结束通知 · 此前 {len(decision.previous)} 个"
    if decision.is_first:
        return f"提醒 · {len(decision.current)} 个进程（新）"
    return f"提醒 · 新 {len(decision.current)} 个 / 同时结束 {len(decision.previous)} 个"


class Runner:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def step(
        self,
        now: float,
        label: str,
        snapshot: Snapshot,
        judge: BatchJudge,
        expect_silent: bool = False,
        expect_cleared: Optional[bool] = None,
        expect_count: Optional[int] = None,
        expect_removed: Optional[int] = None,
    ) -> Optional[Decision]:
        decision = judge.evaluate(snapshot, now)
        problems: List[str] = []

        if expect_silent:
            if decision is not None:
                problems.append(f"本应静默，却产生了决定（{describe(decision)}）")
        else:
            if decision is None:
                problems.append("本应有决定，却静默了")
            else:
                if expect_cleared is not None and decision.cleared != expect_cleared:
                    problems.append(f"cleared 期望 {expect_cleared}，实际 {decision.cleared}")
                if expect_count is not None and len(decision.current) != expect_count:
                    problems.append(f"current 期望 {expect_count}，实际 {len(decision.current)}")
                if expect_removed is not None and len(decision.removed) != expect_removed:
                    problems.append(f"removed 期望 {expect_removed}，实际 {len(decision.removed)}")

        if problems:
            self.failed += 1
            mark = "FAIL"
        else:
            self.passed += 1
            mark = " ok "
        print(f"  [{mark}] t={now:<4} {label:<30} -> {describe(decision)}")
        for item in problems:
            print(f"           !! {item}")
        return decision


class Checker:
    """给非"逐轮推进"的断言用：一条一条对答案，不涉及时间轴。"""

    def __init__(self, title: str) -> None:
        print("")
        print(title)
        self.passed = 0
        self.failed = 0

    def check(self, label: str, actual, expected) -> None:
        ok = actual == expected
        self.passed += ok
        self.failed += not ok
        mark = " ok " if ok else "FAIL"
        print(f"  [{mark}] {label:<46} -> {actual!r}")
        if not ok:
            print(f"           !! 期望 {expected!r}")

    def check_raises(self, label: str, func, exc_type=ConfigError) -> None:
        try:
            func()
        except exc_type as exc:
            self.passed += 1
            print(f"  [ ok ] {label:<46} -> 已拒绝：{str(exc)[:30]}…")
            return
        except Exception as exc:  # noqa: BLE001
            self.failed += 1
            print(f"  [FAIL] {label:<46} -> 抛了别的异常 {type(exc).__name__}: {exc}")
            return
        self.failed += 1
        print(f"  [FAIL] {label:<46} -> 本该被拒绝，却通过了")

    def note(self, text: str) -> None:
        print(f"         {text}")


def scenario_once_per_process() -> Runner:
    """核心需求：CUDA 利用率超 15% 提醒，**同一进程只提醒一次**。

    复刻真机负载形状 —— 一轮 66.6%、间歇 0%，约 20 秒一个来回。
    要验证的不是"能提醒"，而是**不会每个来回都提醒一次**。
    """
    print("")
    print("场景一：同一进程只提醒一次（48GB 卡上的周期性 CUDA 负载）")
    thresholds = Thresholds(metric=METRIC_CUDA_UTIL, cuda_util_percent=15.0)
    print(f"        {thresholds.summary()}")
    judge = BatchJudge(thresholds, min_push_interval_sec=0)
    runner = Runner()

    runner.step(0, "只有桌面进程（CUDA 0%）", snap48(desktop()), judge, expect_silent=True)
    runner.step(5, "一轮开始，CUDA 66.6%", snap48(desktop(), job(66.6)), judge,
                expect_cleared=False, expect_count=1)
    runner.step(10, "间歇，CUDA 掉到 0%", snap48(desktop(), job(0.0, JOB_IDLE_MB)), judge,
                expect_silent=True)
    runner.step(15, "第二轮开始，又 66.6%", snap48(desktop(), job(66.6)), judge,
                expect_silent=True)
    runner.step(20, "又掉到 0%", snap48(desktop(), job(0.0, JOB_IDLE_MB)), judge,
                expect_silent=True)
    runner.step(25, "第三轮 70%", snap48(desktop(), job(70.0)), judge, expect_silent=True)
    # 进程消失后要连续 3 轮"缺席"才算退出（EXIT_MISS_LIMIT）。
    # 这不是测试写慢了：采集层拿不到数据时返回的是空列表而不是异常，
    # 一见不到就判退出会先推"已结束"、下一轮又当新告警推一遍。
    runner.step(30, "进程整轮跑完退出（缺席第 1 轮）", snap48(desktop()), judge, expect_silent=True)
    runner.step(35, "缺席第 2 轮", snap48(desktop()), judge, expect_silent=True)
    runner.step(40, "缺席第 3 轮 → 判定真的结束", snap48(desktop()), judge, expect_cleared=True)
    runner.step(45, "仍然只有桌面进程", snap48(desktop()), judge, expect_silent=True)
    runner.step(50, "重新起了一个新进程（新 PID）66%", snap48(desktop(), job(66.6, pid=30001)),
                judge, expect_cleared=False, expect_count=1)
    runner.step(55, "新进程回落到 0%", snap48(desktop(), job(0.0, JOB_IDLE_MB, pid=30001)),
                judge, expect_silent=True)
    runner.step(60, "新进程 66% 再次达标", snap48(desktop(), job(66.6, pid=30001)), judge,
                expect_silent=True)
    return runner


def scenario_throttle() -> Runner:
    """推送节流：窗口内的变化压住不发，且**不提交状态**，窗口过了补发。"""
    print("")
    print("场景二：推送节流（最小间隔 60s）")
    thresholds = Thresholds(metric=METRIC_CUDA_UTIL, cuda_util_percent=15.0)
    judge = BatchJudge(thresholds, min_push_interval_sec=60.0)
    runner = Runner()

    runner.step(0, "A 60%", snap48(job(60.0, pid=1001)), judge,
                expect_cleared=False, expect_count=1)
    runner.step(5, "B 也 60%，但在节流窗口内", snap48(job(60.0, pid=1001), job(60.0, pid=1002)),
                judge, expect_silent=True)
    runner.step(30, "仍在窗口内", snap48(job(60.0, pid=1001), job(60.0, pid=1002)), judge,
                expect_silent=True)
    runner.step(65, "窗口已过，补发新出现的 B（A 不再重复）",
                snap48(job(60.0, pid=1001), job(60.0, pid=1002)), judge,
                expect_cleared=False, expect_count=1)
    runner.step(80, "无变化", snap48(job(60.0, pid=1001), job(60.0, pid=1002)), judge,
                expect_silent=True)
    # 退出同样要走缺席确认：A 从 t=100 起就不见了，但要到第 3 轮才判结束。
    # 节流窗口（上次推送 t=65，窗口到 t=125）在这之前就已经过去了，
    # 所以这里能干净地看出"结束通知是被缺席确认推迟的，不是被节流压住的"。
    runner.step(100, "A 退出（缺席第 1 轮）", snap48(job(60.0, pid=1002)), judge,
                expect_silent=True)
    runner.step(130, "缺席第 2 轮", snap48(job(60.0, pid=1002)), judge, expect_silent=True)
    runner.step(160, "缺席第 3 轮 → 补发 A 的结束通知", snap48(job(60.0, pid=1002)), judge,
                expect_cleared=True, expect_removed=1)
    return runner


def scenario_trigger_rule() -> Checker:
    """触发线本身的边界语义。"""
    checker = Checker("场景三：触发条件（CUDA 引擎利用率 > 15%）")
    t = Thresholds(metric=METRIC_CUDA_UTIL, cuda_util_percent=15.0)
    checker.note(f"摘要：{t.summary()}")

    checker.check("15.0% 恰好等于触发线 → 不触发（严格大于）",
                  t.evaluate(JOB_PEAK_MB, TOTAL_MB_48G, 15.0), None)
    checker.check("15.1% 触发", t.evaluate(JOB_PEAK_MB, TOTAL_MB_48G, 15.1), 15.1)
    checker.check("66.6%（真机一轮峰值）触发",
                  t.evaluate(JOB_PEAK_MB, TOTAL_MB_48G, 66.6), 66.6)
    checker.check("0.0%（真机间歇）不触发", t.evaluate(JOB_IDLE_MB, TOTAL_MB_48G, 0.0), None)
    checker.check("1.1%（桌面进程）不触发", t.evaluate(DESKTOP_MB, TOTAL_MB_48G, 1.1), None)
    # 拿不到引擎计数器时不能触发，也不能当成 0 —— 上层会明确报"标记不可用"。
    checker.check("拿不到利用率（None）不触发", t.evaluate(0, TOTAL_MB_48G, None), None)

    # 显存口径作对照：真机项目峰值 2359.5MiB 在 48G 卡上只有 4.8%，
    # 用 15% 的显存线永远触发不了 —— 这就是要区分两个指标的原因。
    mem = Thresholds(metric=METRIC_CUDA_MEMORY, memory_percent=15.0, min_memory_mb=0)
    checker.note(f"对照（显存口径）：{mem.summary()}")
    checker.check("显存 15% 线：项目峰值 4.8% 不触发",
                  mem.evaluate(JOB_PEAK_MB, TOTAL_MB_48G, 66.6), None)
    checker.note(f"        15% 在 48G 卡上要 {0.15 * TOTAL_MB_48G:.0f} MiB，"
                 f"而项目峰值只有 {JOB_PEAK_MB:.0f} MiB。")
    return checker


def scenario_config() -> Checker:
    """配置解析与校验。"""
    checker = Checker("场景四：配置解析与校验")

    default = _parse_thresholds({})
    checker.check("缺省指标是 cuda_util", default.metric, METRIC_CUDA_UTIL)
    checker.check("缺省触发线 15%", default.cuda_util_percent, 15.0)

    parsed = _parse_thresholds({"metric": "cuda_util", "cuda_util_percent": 15})
    checker.check("解析出的触发线", parsed.cuda_util_percent, 15.0)
    checker.check("指标名", parsed.metric, METRIC_CUDA_UTIL)
    checker.check("标签", parsed.label(), "CUDA")

    mem = _parse_thresholds({
        "metric": "cuda_memory", "min_memory_mb": 1000, "cuda_only": False
    })
    checker.check("显存口径的标签", mem.label(), "显存")
    checker.check("显存口径只筛开关被读到", mem.cuda_only, False)

    # 老配置里那些键（exit_memory_percent / exit_memory_mb）已删除，
    # 解析器应当忽略它们而不是报错 —— 否则老 config.json 会启动即崩。
    old = _parse_thresholds({
        "memory_percent": 15.0, "exit_memory_percent": 13.0, "min_memory_mb": 0
    })
    checker.check("老配置的 exit_* 键被忽略（不报错）", old.metric, METRIC_CUDA_UTIL)

    checker.check_raises("指标名非法被拒绝",
                         lambda: _parse_thresholds({"metric": "gpu_util"}))
    checker.check_raises("触发线 0 被拒绝",
                         lambda: _parse_thresholds({"cuda_util_percent": 0}))
    checker.check_raises("触发线 101 被拒绝",
                         lambda: _parse_thresholds({"cuda_util_percent": 101}))
    checker.check_raises(
        "显存口径下两条线同时为空被拒绝",
        lambda: _parse_thresholds({"metric": "cuda_memory", "min_memory_mb": 0}),
    )

    # 去重记忆文件的位置也要能配，且相对路径必须按"配置文件所在目录"解析 ——
    # 计划任务的工作目录是不确定的，按 CWD 解析会写到一个谁也找不到的地方。
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        cfg_path = base / "config.json"

        cfg_path.write_text(
            json.dumps({
                "thresholds": {"metric": "cuda_util", "cuda_util_percent": 15},
                "runtime": {"log_file": "logs/monitor.log", "state_file": "notified.json"},
                "wechat": {},
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        cfg = load_config(cfg_path)
        checker.check("state_file 相对路径按配置目录解析",
                      cfg.runtime.state_file, str(base / "notified.json"))
        checker.check("log_file 同样按配置目录解析",
                      cfg.runtime.log_file, str(base / "logs" / "monitor.log"))

        # 键不存在 = 用默认（老配置照常工作）；显式写 "" = 明确关掉落盘。
        cfg_path.write_text(json.dumps({"wechat": {}}), encoding="utf-8")
        checker.check("不写 state_file 时取默认路径",
                      load_config(cfg_path).runtime.state_file, DEFAULT_STATE_FILE)

        cfg_path.write_text(json.dumps({"runtime": {"state_file": ""}}), encoding="utf-8")
        checker.check("显式写空字符串 = 关掉落盘",
                      load_config(cfg_path).runtime.state_file, "")
    checker.check("Runtime 的默认值", Runtime().state_file, DEFAULT_STATE_FILE)
    return checker


def scenario_wddm_signature() -> Checker:
    """WDDM 下 nvidia-smi 逐进程显存全 [N/A]：能不能识别，兜底决策对不对。"""
    checker = Checker("场景五：WDDM 全 N/A 的识别与兜底决策")

    os.environ["CUDA_MONITOR_FAKE_SCENARIO"] = "wddm"
    try:
        scan = list_compute_apps(str(FAKE_SMI), 20)
    finally:
        os.environ.pop("CUDA_MONITOR_FAKE_SCENARIO", None)

    checker.check("可用进程数为 0", len(scan.apps), 0)
    checker.check("识别出被丢弃的 N/A 行", scan.unusable, 4)
    checker.check("has_data 为假 → 会触发兜底", scan.has_data, False)
    checker.note("        假 smi 的 wddm 场景返回 4 个进程，全部 [N/A]；"
                 "老实现会静默丢弃这 4 行，判定层看到空集合。")

    os.environ["CUDA_MONITOR_FAKE_SCENARIO"] = "normal"
    try:
        ok_scan = list_compute_apps(str(FAKE_SMI), 20)
    finally:
        os.environ.pop("CUDA_MONITOR_FAKE_SCENARIO", None)
    checker.check("正常场景仍能拿到进程（不误触发兜底）", len(ok_scan.apps), 3)
    checker.check("正常场景没有丢弃行", ok_scan.unusable, 0)

    multi = [
        Gpu(0, "GPU-0", "Fake 0", TOTAL_MB_24G, 5000.0, 10.0),
        Gpu(1, "GPU-1", "Fake 1", TOTAL_MB_24G, 5000.0, 10.0),
    ]
    apps, note = _apps_via_pdh(multi, "（测试：nvidia-smi 全 N/A）")
    checker.check("多卡时不兜底", apps, [])
    checker.check("多卡时给出可读说明", bool(note), True)

    checker.check(
        "实例名抠 PID",
        win_gpu_mem.pid_from_instance("pid_26660_luid_0x00000000_0x0000C4F0_phys_0"),
        JOB_PID,
    )
    checker.check("无法解析时返回 None", win_gpu_mem.pid_from_instance("garbage"), None)
    checker.check(
        "engtype 解析（CUDA）",
        win_gpu_mem.engtype_from_instance(
            "pid_26660_luid_0x00000000_0x0000C4F0_phys_0_eng_11_engtype_Cuda"
        ),
        "Cuda",
    )
    checker.check(
        "engtype 解析（图形）",
        win_gpu_mem.engtype_from_instance(
            "pid_2772_luid_0x00000000_0x0000BBE7_phys_0_eng_0_engtype_3D"
        ),
        "3D",
    )
    checker.check("engtype 缺失返回空串",
                  win_gpu_mem.engtype_from_instance("pid_1_luid_0x0_phys_0"), "")
    return checker


def scenario_cuda_filter() -> Checker:
    """显存口径下的 CUDA 进程筛选：区分 CUDA 占用与图形占用。"""
    checker = Checker("场景六：显存口径 + CUDA 进程筛选")

    def full(pid: int, name: str, mib: float, cuda_active, util: float = 0.0):
        return ProcessUsage(
            gpu_index=0, gpu_uuid="GPU-0", pid=pid, name=name,
            used_memory_mb=mib, cuda_active=cuda_active, cuda_util_pct=util,
        )

    cuda_job = full(JOB_PID, JOB_NAME, JOB_PEAK_MB, True, JOB_CUDA_UTIL_PEAK)
    # 假想一个吃显存很多的图形进程：本地显存 4 GiB，但完全不跑 CUDA。
    heavy_graphics = full(9999, r"C:\Games\render.exe", 4096.0, False, 0.0)
    unknown = full(7777, r"C:\Python311\python.exe", 3000.0, None)

    strict = Thresholds(metric=METRIC_CUDA_MEMORY, min_memory_mb=1000, cuda_only=True)
    checker.note(f"摘要：{strict.summary()}")

    def enters(app, thresholds) -> bool:
        judge = BatchJudge(thresholds, min_push_interval_sec=0)
        decision = judge.evaluate(snap48(app), 0.0)
        return bool(decision and decision.current)

    checker.check("CUDA 进程 2360MiB 提醒", enters(cuda_job, strict), True)
    checker.check("吃 4GiB 的图形进程不提醒", enters(heavy_graphics, strict), False)
    checker.check("拿不到引擎标记时放行（不静默漏报）", enters(unknown, strict), True)

    loose = Thresholds(metric=METRIC_CUDA_MEMORY, min_memory_mb=1000, cuda_only=False)
    checker.note(f"关掉筛选：{loose.summary()}")
    checker.check("cuda_only=false 时图形进程也提醒",
                  enters(heavy_graphics, loose), True)

    judge = BatchJudge(strict, min_push_interval_sec=0)
    decision = judge.evaluate(snap48(cuda_job, desktop(), heavy_graphics), 0.0)
    names = [a.process_name for a in (decision.current if decision else [])]
    checker.check("混合场景只报 CUDA 进程", names, ["python.exe"])
    return checker


def scenario_mixed_round() -> Runner:
    """同一轮里既有新告警、又有进程退出 —— 两边信息都不能丢。"""
    print("")
    print("场景七：混合轮（新告警 + 进程退出同时发生）")
    thresholds = Thresholds(metric=METRIC_CUDA_UTIL, cuda_util_percent=15.0)
    judge = BatchJudge(thresholds, min_push_interval_sec=0)
    runner = Runner()

    runner.step(0, "A 60% 提醒", snap48(job(60.0, pid=1001)), judge,
                expect_cleared=False, expect_count=1)
    # A 走了、B 来了。A 此时只是"缺席第 1 轮"，还不能判退出 ——
    # 但 B 是新达标进程，这条推送不能被 A 的缺席确认拖住。
    runner.step(5, "A 消失，B 新达标（A 缺席 1）",
                snap48(job(60.0, pid=1002)), judge,
                expect_cleared=False, expect_count=1, expect_removed=0)
    runner.step(10, "A 缺席 2，B 还在", snap48(job(60.0, pid=1002)), judge,
                expect_silent=True)
    # 第 15 秒：A 缺席满 3 轮（判定退出），同时 C 新达标 → 真正意义的混合轮。
    decision = runner.step(15, "A 判定退出 + C 新达标",
                           snap48(job(60.0, pid=1002), job(60.0, pid=1003)), judge,
                           expect_cleared=False, expect_count=1, expect_removed=1)
    if decision is not None:
        added = [item.pid for item in decision.added]
        removed = [item.pid for item in decision.removed]
        ok = added == [1003] and removed == [1001]
        runner.passed += ok
        runner.failed += not ok
        detail = "" if ok else f"  !! added={added} removed={removed}"
        print(f"  [{' ok ' if ok else 'FAIL'}] 新增/结束两侧都带上了{detail}")
    runner.step(20, "之后 B、C 一直在 60%，不再重复",
                snap48(job(60.0, pid=1002), job(60.0, pid=1003)), judge, expect_silent=True)
    return runner


def scenario_process_identity_text() -> Checker:
    """告警正文必须让人认得出「是哪个 python」。

    真机上同时跑着 26 个 `python.exe`，只写短名等于没有信息。
    日志正文用完整路径；微信单个字段只有 20 字（模板前缀还占 3 个），
    完整路径塞不下，于是按「目录名\\程序名 → 目录名 → 程序名」阶梯降级，
    **宽度不够时宁可丢 PID 也要留住所在目录**。
    """
    checker = Checker("场景八：告警正文的进程标识（完整路径 / 微信宽度预算）")

    checker.check("完整路径拆出「目录名\\程序名」",
                  path_label(JOB_NAME), r"yolo_ultra\python.exe")
    checker.check("只有文件名时原样返回", path_label("python.exe"), "python.exe")
    checker.check("只有盘符时不编造目录", path_label(r"D:\python.exe"), "python.exe")
    checker.check("拿不到路径的占位原样返回", path_label(f"pid {JOB_PID}"), f"pid {JOB_PID}")
    checker.check("空串不炸", path_label(""), "")

    thresholds = Thresholds(metric=METRIC_CUDA_UTIL, cuda_util_percent=15.0)
    judge = BatchJudge(thresholds, min_push_interval_sec=0)
    decision = judge.evaluate(snap48(job(JOB_CUDA_UTIL_PEAK), desktop()), 0.0)
    alert = decision.current[0] if decision and decision.current else None

    checker.check(
        "日志正文带完整路径（不再只有 python.exe）",
        alert.detail if alert else None,
        rf"{JOB_NAME} (PID {JOB_PID}) 在 GPU 0 上，"
        f"CUDA占用 {JOB_CUDA_UTIL_PEAK:.1f}%（显存 {JOB_PEAK_MB:.0f} MiB / {TOTAL_MB_48G:.0f} MiB）",
    )
    checker.check("名称阶梯从有辨识度到最短",
                  alert.labels if alert else None,
                  (r"yolo_ultra\python", "yolo_ultra", "python"))
    checker.check("去重身份仍是短名（不能被展示改动带偏）",
                  alert.identity if alert else None,
                  (0, JOB_PID, "python.exe"))

    payload = build_alert_payload(decision, 5) if decision else {}
    checker.check("微信正文：目录和 PID 都在，让出的是百分比",
                  payload.get("p1"), f"yolo_ultra {JOB_PID}")
    checker.note("        `yolo_ultra\\python 26660 67%` 27 字、`yolo_ultra\\python 26660` 23 字、")
    checker.note("        `yolo_ultra 26660 67%` 20 字，都放不进 17 字的字段预算；")
    checker.note("        完整路径 38 字更不可能 —— 这是平台限制，不是取舍失误。")

    # 用户明确提过两条要求，一条都不能回退：
    #   ① "看不出是哪个 python"  → 必须带所在目录（conda 环境名）
    #   ② "pid 号都看不全了"     → 必须带 PID
    # 所以降级只允许动名称和百分比，**PID 永远保留**。
    checker.check("目录较短时三样俱全",
                  _format_process(replace(alert, process_path=r"D:\Anaconda\envs\my_env\python.exe")),
                  f"my_env {JOB_PID} 67%")
    checker.check("目录长到放不下时先让出百分比",
                  _format_process(alert),
                  f"yolo_ultra {JOB_PID}")
    checker.check("目录名本身极长时退回短名，PID 仍在",
                  _format_process(replace(alert, process_path=(
                      r"C:\Program Files\WindowsApps"
                      r"\Microsoft.YourPhone_1.25072.81.0_x64__8wekyb3d8bbwe\python.exe"))),
                  f"python {JOB_PID} 67%")
    checker.check("进程名长到装不下时退到「PID + 百分比」",
                  _format_process(replace(alert, process_name="x" * 40, process_path="")),
                  f"{JOB_PID} 67%")

    # 拿不到路径时（老行为）必须一字不变，否则短名进程的消息会莫名变样。
    plain_alert = Alert(
        gpu_index=0, gpu_name="NVIDIA GeForce RTX 4090", pid=JOB_PID,
        process_name="python.exe", used_mb=JOB_PEAK_MB, total_mb=TOTAL_MB_48G,
        percent=JOB_PEAK_MB / TOTAL_MB_48G * 100.0,
        metric_value=42.0, metric_label="CUDA",
    )
    checker.check("拿不到路径时退回短名", plain_alert.display_path, "python.exe")
    checker.check("拿不到路径时名称阶梯只有短名", plain_alert.labels, ("python",))
    checker.check("拿不到路径时正文与老版本完全一致",
                  _format_process(plain_alert), f"python {JOB_PID} 42%")
    checker.check("`pid 1234` 占位不重复拼一次 PID",
                  _format_process(replace(plain_alert, process_name=f"pid {JOB_PID}")),
                  f"pid {JOB_PID} 42%")

    # 采集层同一个口径：name 带目录才有 label / path。
    plain = ProcessUsage(
        gpu_index=0, gpu_uuid="GPU-0", pid=JOB_PID, name="python.exe",
        used_memory_mb=JOB_PEAK_MB, cuda_active=True, cuda_util_pct=JOB_CUDA_UTIL_PEAK,
    )
    checker.check("ProcessUsage.label（无路径）", plain.label, "python.exe")
    checker.check("ProcessUsage.path（无路径 → 空串，避免写出 pid X (PID X)）",
                  plain.path, "")
    checker.check("ProcessUsage.label（有路径）", job(0.0).label, r"yolo_ultra\python.exe")
    checker.check("ProcessUsage.path（有路径）", job(0.0).path, JOB_NAME)

    # --once 的表格列宽固定：UWP 包名有 60+ 字，不截断会把后面几列顶歪。
    long_app = ProcessUsage(
        gpu_index=0, gpu_uuid="GPU-0", pid=20224,
        name=r"C:\Program Files\WindowsApps"
             r"\Microsoft.YourPhone_1.25072.81.0_x64__8wekyb3d8bbwe\PhoneExperienceHost.exe",
        used_memory_mb=11.0, cuda_active=False, cuda_util_pct=0.0,
    )
    table = format_snapshot(snap48(desktop(), long_app), thresholds)
    rows = [
        line for line in table.splitlines()
        if line.startswith("  ") and ("SunloginClient" in line or "PhoneExperienceHost" in line)
    ]
    checker.check("表格里超长包名行与普通行等宽（列没被顶歪）",
                  len({len(line) for line in rows}), 1)
    long_row = next((line for line in rows if "PhoneExperienceHost" in line), "")
    checker.check("超长包名按 `..` 左截断，程序名不被砍掉",
                  r"..\PhoneExperienceHost.exe" in long_row, True)
    checker.note("        完整路径在 `--once` 的「命中告警」行里是全的，表格只为对齐做截断。")
    return checker


def scenario_restart_memory() -> Checker:
    """重启之后，同一个进程也不能被重复推送。

    复刻真机事故：进程 11672 一直在跑，服务在一轮排查里被重启 3 次
    （17:34 / 17:35 / 17:42），于是它被推了 3 次、每次 5 条给 5 个接收人。
    根因是"已提醒名单"只活在内存里 —— 重启即清空，同一个进程又"变回"新告警。
    用户的要求：**相同进程在结束前永远不重复推送**。
    """
    checker = Checker("场景九：去重记忆跨重启（重启后同一进程不再重复推送）")
    thresholds = Thresholds(metric=METRIC_CUDA_UTIL, cuda_util_percent=15.0)
    # 真机那个一直跑着的训练进程。
    running = snap48(job(JOB_CUDA_UTIL_PEAK, pid=JOB_PID))

    with tempfile.TemporaryDirectory() as tmp:
        state = pathlib.Path(tmp) / "notified.json"

        # --- 第一次运行：达标 → 推一条 → 记忆落盘 ---
        first = BatchJudge(thresholds, 0.0, state)
        checker.check("首次运行没有记忆可恢复", first.restored_count, 0)
        decision = first.evaluate(running, 100.0)
        checker.check("首次达标推送 1 个进程",
                      len(decision.current) if decision else None, 1)
        checker.check("状态文件已写盘", state.is_file(), True)
        checker.check("状态文件不带 BOM",
                      state.read_bytes()[:3] != b"\xef\xbb\xbf", True)

        payload = json.loads(state.read_text(encoding="utf-8"))
        checker.check("状态文件版本号", payload.get("version"), 1)
        checker.check("落盘的身份是 (卡号, PID, 短名)",
                      payload["entries"][0]["identity"], [0, JOB_PID, "python.exe"])
        # 落盘口径必须与运行期一致。踩过：present 用完整路径、_notified 用短名，
        # 於每轮都把已提醒进程误判成"已退出"再当新告警加回来。
        checker.check("落盘身份口径与运行期一致（短名，不是完整路径）",
                      first.notified_identities, ((0, JOB_PID, "python.exe"),))

        # --- 模拟重启：内存全新，从同一个文件恢复 ---
        second = BatchJudge(thresholds, 0.0, state)
        checker.check("重启后恢复了 1 条记忆", second.restored_count, 1)
        checker.check("重启后同一进程（同 PID）不再重推",
                      second.evaluate(running, 105.0), None)
        checker.check("再扫一轮依然静默", second.evaluate(running, 110.0), None)
        checker.check("记忆还在（进程没退出就永不清除）", second.active_count, 1)
        checker.note("        这一条就是用户要的「相同进程在结束前永远不重复推送」。")

        # --- 单轮空扫描 ≠ 进程退出 ---
        # collect() 在性能计数器不可用时返回的是空进程列表而不是异常。
        checker.check("一轮扫不到任何进程 → 不发结束通知",
                      second.evaluate(snap48(), 115.0), None)
        checker.check("记忆保留", second.active_count, 1)
        checker.check("下一轮进程又出现 → 依然静默（没有被当成新告警重推）",
                      second.evaluate(running, 120.0), None)

        # --- 重启恢复、但从没露过面的条目：静默淘汰，不补发"已结束" ---
        third = BatchJudge(thresholds, 0.0, state)
        checker.check("再次重启，仍恢复 1 条", third.restored_count, 1)
        for tick in range(EXIT_MISS_LIMIT):
            checker.check(f"恢复的条目连续缺席第 {tick + 1} 轮",
                          third.evaluate(snap48(), 200.0 + tick * 5), None)
        checker.note("        全程没有推送「已结束」：监控当时不在场，"
                     "说不出它是几点结束的，每重启一次补一堆结束通知又是新的刷屏源。")
        checker.check("缺席够了就静默移除记忆", third.active_count, 0)
        recurred = third.evaluate(running, 220.0)
        checker.check("之后同 PID 重新出现 → 才当新告警推一条",
                      len(recurred.current) if recurred else None, 1)

        # --- 坏文件 / 不认识的版本：不能让监控起不来 ---
        broken = pathlib.Path(tmp) / "broken.json"
        broken.write_text("{这不是 JSON", encoding="utf-8")
        checker.check("状态文件损坏 → 从空名单开始，不抛异常",
                      BatchJudge(thresholds, 0.0, broken).restored_count, 0)

        future = pathlib.Path(tmp) / "future.json"
        future.write_text(json.dumps({"version": 99, "entries": []}), encoding="utf-8")
        checker.check("版本不认识 → 整个忽略",
                      BatchJudge(thresholds, 0.0, future).restored_count, 0)

        half = pathlib.Path(tmp) / "half.json"
        half.write_text(json.dumps({
            "version": 1,
            "entries": [
                {"identity": [0, 4321, "python.exe"]},            # 缺 alert
                {"identity": [0, 4321], "alert": {}},              # 身份不合法
                "根本不是对象",
            ],
        }), encoding="utf-8")
        checker.check("单条记录坏掉只丢那一条，不牵连整份文件",
                      BatchJudge(thresholds, 0.0, half).restored_count, 0)

        # --- 写盘失败：只记警告，绝不能影响推送本身 ---
        blocker = pathlib.Path(tmp) / "blocker"
        blocker.write_text("我是个文件，不是目录", encoding="utf-8")
        unwritable = blocker / "notified.json"
        judge = BatchJudge(thresholds, 0.0, unwritable)
        decision = judge.evaluate(running, 300.0)
        checker.check("状态文件写不进去也不影响推送",
                      len(decision.current) if decision else None, 1)
        checker.check("写盘失败后记忆仍在内存里（下次重启前照常去重）",
                      judge.active_count, 1)

        # --- 不配 state_file 就是纯内存模式（--once/--preview 必须走这条） ---
        checker.check("state_file=None 时连磁盘都不碰",
                      BatchJudge(thresholds, 0.0, None).state_file, None)
    return checker


def main() -> int:
    print("=" * 70)
    print("判定层回归测试：只提醒一次 / 触发条件 / 节流 / CUDA 筛选 / WDDM 兜底 / 跨重启记忆")
    print("=" * 70)

    runners = [
        scenario_once_per_process(),
        scenario_throttle(),
        scenario_mixed_round(),
    ]
    checkers = [
        scenario_trigger_rule(),
        scenario_config(),
        scenario_wddm_signature(),
        scenario_cuda_filter(),
        scenario_process_identity_text(),
        scenario_restart_memory(),
    ]

    passed = sum(item.passed for item in runners) + sum(item.passed for item in checkers)
    failed = sum(item.failed for item in runners) + sum(item.failed for item in checkers)

    print("")
    print("=" * 70)
    if failed:
        print(f"结果：{passed} 通过 / {failed} 失败")
        return 1
    print(f"结果：{passed} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
