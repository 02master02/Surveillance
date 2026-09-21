"""Windows 专属：按进程读 GPU 显存与 CUDA 引擎活动，补 nvidia-smi 报 N/A 的空缺。

为什么需要它
------------
Windows 上消费级/工作站卡跑在 WDDM 显示驱动模型下（GeForce 无法切到 TCC），
`nvidia-smi --query-compute-apps=...,used_memory` 会对**所有**进程返回 [N/A]，
而 `--query-gpu=memory.used` 的整卡数字是正常的。结果是采集层一个进程都拿不到，
判定层永远是空集合 —— 一个告警都不会发，而且日志上看起来"一切正常"。

替代源是 Windows 性能计数器，也就是任务管理器「进程」页的数据来源。
本模块读三个计数器，用**一个 PDH query** 一次采完：

    \\GPU Process Memory(*)\\Local Usage            进程的 GPU 本地显存（字节）
    \\GPU Engine(*)\\Utilization Percentage          进程在各引擎上的瞬时利用率
    \\GPU Engine(*)\\Running Time                    进程在各引擎上的累计运行时间

### 显存：为什么选 Local Usage

四个候选计数器在 48GB 卡上的实测对照（同一次采样，整卡 nvidia-smi 为 3053 MiB）:

    计数器              单进程最大值      合计      结论
    Local Usage          2359.5 MiB     2888.8   同量级 → 用它
    Total Committed      4131.5 MiB     5329.0   含借用系统内存，偏大
    Dedicated Usage     79455.1 MiB    82581.3   目标机上这个计数器是坏的
    Shared Usage         1772.0 MiB     1877.0   只统计借用系统内存的部分

### 怎么区分"CUDA 占用"和"显卡占用"

`Local Usage` 是进程级的，但把图形和计算混在一起。引擎计数器补上了这个区分：
实例名里带 engtype，形如

    pid_26660_luid_0x00000000_0x0000C4F0_phys_0_eng_3_engtype_Cuda

真机实测（同一时刻）：

    PID 26660  python.exe（训练）  engtype_Cuda 66.6%  没有 3D 活动
    PID 2772   SearchApp           engtype_3D    1.1%
    PID 24376  向日葵               VideoEncode 0.9% / Compute_0 0.7% / 3D 0.2%

所以 `cuda_activity()` 能回答"这个进程到底是不是在跑 CUDA"，
判定层据此把纯图形/桌面进程排除掉 —— 这才叫"监控 CUDA 占用"。

**必须说清楚的边界**：Windows 上**没有**"CUDA 显存"这个独立计数器，
`Local Usage` 里图形与计算无法在数值上拆开。但对**无窗口的纯计算进程**
（训练用的 python.exe 之类，实测完全没有 engtype_3D 活动）图形贡献为 0，
它的 `Local Usage` 就等于它的 CUDA 显存。这是这个平台上能达到的最精确程度。

### 其它实现细节

用 PdhAddEnglishCounterW 注册 —— 它接受英文计数器名，中文 Windows 上
不用去猜"GPU 进程内存"这类本地化名称。

多实例计数器必须合并：同一进程可能有多个 luid/phys/eng 实例。
显存按 PID **求和**（不同 phys 段），引擎活动按 PID 取 **max**（同一引擎多实例）。
"""

from __future__ import annotations

import ctypes
import os
import re
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

#: 本模块只在 Windows 上有意义，非 Windows 上一律返回空，不抛异常。
IS_WINDOWS = os.name == "nt"

#: 计数器路径（英文名注册用）。
COUNTER_MEMORY = r"\GPU Process Memory(*)\Local Usage"
COUNTER_ENGINE_UTIL = r"\GPU Engine(*)\Utilization Percentage"
COUNTER_ENGINE_TIME = r"\GPU Engine(*)\Running Time"

#: 判定"这个进程在跑 CUDA"的引擎类型名（实例名里的 engtype_<X>）。
#: 不用 Compute_0/Compute_1 —— 那是 D3D 的计算着色器路径，不是 CUDA。
ENGTYPE_CUDA = "Cuda"

_PDH_FMT_DOUBLE = 0x00000200
_ERROR_SUCCESS = 0

#: 两个采样点之间的间隔。这些计数器要两次 collect 才出值，
#: 这里给 0.1s —— 相对 5s 的轮询周期可以忽略。
_SAMPLE_GAP_SEC = 0.1

#: 从实例名里抠 PID：pid_26660_luid_...
_INSTANCE_PID = re.compile(r"\bpid_(\d+)", re.IGNORECASE)
_INSTANCE_ENGTYPE = re.compile(r"engtype_(\w+)", re.IGNORECASE)

#: 查询进程镜像路径用的两种权限。
#: 先试最小权限 LIMITED（够用时最安全）；少数受保护进程只认 QUERY_INFORMATION，
#: 而监控进程跑在 SYSTEM 下，本来就有这个权限，所以第二档兜一下。
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_QUERY_INFORMATION = 0x0400
_NAME_BUFFER_CHARS = 1024

#: PDH 常见错误码 → 人话。这些状态码在 MSDN 上是 PDH_* 常量。
_PDH_ERRORS = {
    0xC0000BB8: "PDH_CSTATUS_NO_OBJECT（没有这个计数器对象）",
    0xC0000BB9: "PDH_CSTATUS_NO_COUNTER（没有这个计数器）",
    0xC0000BBC: "PDH_CSTATUS_NO_INSTANCE（没有实例）",
    0xC0000BBD: "PDH_CSTATUS_NO_MACHINE",
    0xC0000BBF: "PDH_CSTATUS_NO_OBJECT",
    0xC0000BC6: "PDH_INVALID_DATA（数据无效）",
    0x800007D0: "PDH_INVALID_HANDLE（句柄无效）",
    0xC0000BCF: "PDH_ACCESS_DENIED（权限不足）",
}

_pdh: Optional[object] = None
_kernel32: Optional[object] = None


class PdhUnavailable(RuntimeError):
    """性能计数器通路不可用：非 Windows、计数器缺失、权限不足等。"""


@dataclass(frozen=True)
class CudaActivity:
    """一个进程的 CUDA 引擎活动情况。"""

    #: 本次进程生命周期内是否在 CUDA 引擎上跑过。
    active: bool
    #: 当前瞬时 CUDA 引擎利用率（百分比，0–100）。
    util_pct: float


class _FMT_COUNTERVALUE(ctypes.Structure):
    """PDH_FMT_COUNTERVALUE。用 DOUBLE 取格式化结果，字节数在 double 里是精确的。"""

    _fields_ = [
        ("CStatus", wintypes.DWORD),
        ("doubleValue", ctypes.c_double),
    ]


class _FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    """PDH_FMT_COUNTERVALUE_ITEM_W：一个实例名 + 一个值。"""

    _fields_ = [
        ("szName", wintypes.LPWSTR),
        ("FmtValue", _FMT_COUNTERVALUE),
    ]


def describe_status(status: int) -> str:
    """把 PDH 状态码翻译成可读文本，排障时直接看日志。"""
    return _PDH_ERRORS.get(status, f"未知状态 0x{status & 0xFFFFFFFF:08X}")


def _sig(func, argtypes, restype=wintypes.DWORD):
    func.argtypes = argtypes
    func.restype = restype
    return func


def _load_pdh():
    """惰性加载 pdh.dll 并声明函数签名（import 时不碰 DLL）。"""
    global _pdh
    if _pdh is not None:
        return _pdh
    if not IS_WINDOWS:
        raise PdhUnavailable("非 Windows 平台，没有性能计数器通路。")

    dll = ctypes.WinDLL("pdh", use_last_error=True)
    _sig(dll.PdhOpenQueryW, [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)])
    _sig(
        dll.PdhAddEnglishCounterW,
        [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)],
    )
    _sig(dll.PdhCollectQueryData, [wintypes.HANDLE])
    _sig(
        dll.PdhGetFormattedCounterArrayW,
        [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_FMT_COUNTERVALUE_ITEM_W),
        ],
    )
    _sig(dll.PdhCloseQuery, [wintypes.HANDLE])
    _pdh = dll
    return _pdh


def _load_kernel32():
    """惰性加载 kernel32 并声明函数签名。"""
    global _kernel32
    if _kernel32 is not None:
        return _kernel32
    if not IS_WINDOWS:
        raise PdhUnavailable("非 Windows 平台，没有进程查询通路。")

    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    # restype 必须是 HANDLE，写成默认的 c_int 会在 64 位下把句柄截断。
    _sig(dll.OpenProcess, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
    _sig(
        dll.QueryFullProcessImageNameW,
        [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)],
        wintypes.BOOL,
    )
    _sig(dll.CloseHandle, [wintypes.HANDLE], wintypes.BOOL)
    _kernel32 = dll
    return _kernel32


def _sample_counters(paths: Sequence[str]) -> Dict[str, List[Tuple[str, float]]]:
    """在**一个** PDH query 里采多个计数器，返回 {路径: [(实例名, 值), ...]}。

    合成一个 query 是有意义的：一次 PdhCollectQueryData 就覆盖全部计数器，
    比逐个开 query 采样省掉多次采样间隔和重复的实例枚举。
    """
    pdh = _load_pdh()

    query = wintypes.HANDLE()
    status = pdh.PdhOpenQueryW(None, 0, ctypes.byref(query))
    if status != _ERROR_SUCCESS:
        raise PdhUnavailable(f"PdhOpenQueryW 失败：{describe_status(status)}")

    result: Dict[str, List[Tuple[str, float]]] = {}
    try:
        handles: List[Tuple[str, object]] = []
        for path in paths:
            counter = wintypes.HANDLE()
            status = pdh.PdhAddEnglishCounterW(query, path, 0, ctypes.byref(counter))
            if status != _ERROR_SUCCESS:
                raise PdhUnavailable(
                    f"PdhAddEnglishCounterW({path}) 失败：{describe_status(status)}"
                )
            handles.append((path, counter))

        pdh.PdhCollectQueryData(query)
        time.sleep(_SAMPLE_GAP_SEC)
        status = pdh.PdhCollectQueryData(query)
        if status != _ERROR_SUCCESS:
            raise PdhUnavailable(f"PdhCollectQueryData 失败：{describe_status(status)}")

        for path, counter in handles:
            size = wintypes.DWORD(0)
            count = wintypes.DWORD(0)
            # 第一次调用只为拿缓冲区大小，必然返回 PDH_MORE_DATA（或 size=0）。
            pdh.PdhGetFormattedCounterArrayW(
                counter, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None
            )
            if size.value == 0:
                result[path] = []
                continue

            buffer = ctypes.create_string_buffer(size.value)
            items = ctypes.cast(buffer, ctypes.POINTER(_FMT_COUNTERVALUE_ITEM_W))
            status = pdh.PdhGetFormattedCounterArrayW(
                counter, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), items
            )
            if status != _ERROR_SUCCESS:
                raise PdhUnavailable(
                    f"PdhGetFormattedCounterArrayW({path}) 失败：{describe_status(status)}"
                )

            result[path] = [
                (items[index].szName, float(items[index].FmtValue.doubleValue))
                for index in range(count.value)
            ]
        return result
    finally:
        pdh.PdhCloseQuery(query)


def pid_from_instance(instance: str) -> Optional[int]:
    """实例名 `pid_26660_luid_0x..._phys_0` → 26660。取不到返回 None。"""
    match = _INSTANCE_PID.search(instance or "")
    return int(match.group(1)) if match else None


def engtype_from_instance(instance: str) -> str:
    """实例名 `...eng_3_engtype_Cuda` → 'Cuda'。取不到返回空串。"""
    match = _INSTANCE_ENGTYPE.search(instance or "")
    return match.group(1) if match else ""


def per_process_mb() -> Dict[int, float]:
    """PID → 该进程占用的 GPU 本地显存（MiB）。

    同一 PID 的多个 luid/phys 实例会被**求和**（不同物理段要相加）。
    """
    rows = _sample_counters([COUNTER_MEMORY])[COUNTER_MEMORY]
    by_pid: Dict[int, float] = {}
    for instance, value in rows:
        pid = pid_from_instance(instance)
        if pid is None or value <= 0:
            continue
        by_pid[pid] = by_pid.get(pid, 0.0) + value / (1024.0 * 1024.0)
    return by_pid


def cuda_activity() -> Dict[int, CudaActivity]:
    """PID → 该进程的 CUDA 引擎活动。

    `active` 的口径是「**本次进程生命周期内**在 CUDA 引擎上跑过」，
    依据是累计 `Running Time > 0`（单调递增）。

    刻意不用"瞬时利用率 > 0"当唯一依据：CUDA 程序处在 CPU 阶段
    （取数据、写日志、存 checkpoint）时瞬时利用率就是 0，而显存还占着。
    只看瞬时的会让告警集合跟着负载一涨一落反复进出，把推送刷爆。
    """
    sampled = _sample_counters([COUNTER_ENGINE_UTIL, COUNTER_ENGINE_TIME])

    if not sampled[COUNTER_ENGINE_UTIL] and not sampled[COUNTER_ENGINE_TIME]:
        # ★ 关键防线：把"计数器没有实例"和"没有进程在跑 CUDA"区分开。
        # 健康的 Windows 上 GPU Engine 一定有大量实例（真机 558 个），
        # 一个都没有说明本机不支持这条通路。这时候必须报"不可用"，
        # 否则会被上层理解成"没有任何 CUDA 进程"，把所有进程筛掉 ——
        # 那就又变成了"静默失效"，正是本次要根除的那类 bug。
        raise PdhUnavailable(
            "GPU Engine 计数器返回 0 个实例，疑似本机不支持该计数器（而不是没有 CUDA 进程）"
        )

    util_max: Dict[int, float] = {}
    for instance, value in sampled[COUNTER_ENGINE_UTIL]:
        if engtype_from_instance(instance) != ENGTYPE_CUDA:
            continue
        pid = pid_from_instance(instance)
        if pid is None:
            continue
        # 同一引擎可能有多个实例（多 phys 段），取 max 而不是求和 ——
        # 利用率是比例，相加没有意义。
        util_max[pid] = max(util_max.get(pid, 0.0), value)

    ran: Dict[int, float] = {}
    for instance, value in sampled[COUNTER_ENGINE_TIME]:
        if engtype_from_instance(instance) != ENGTYPE_CUDA:
            continue
        pid = pid_from_instance(instance)
        if pid is None:
            continue
        ran[pid] = ran.get(pid, 0.0) + value

    return {
        pid: CudaActivity(active=True, util_pct=util_max.get(pid, 0.0))
        for pid in set(util_max) | set(ran)
        if ran.get(pid, 0.0) > 0 or util_max.get(pid, 0.0) > 0
    }


def process_image_path(pid: int) -> str:
    """进程完整可执行路径；拿不到返回空串。

    先试最小权限，失败再试宽权限。真正的系统进程（PID 4 之类）两档都会失败，
    这时候调用方退回 "pid <N>" 展示，不影响占用统计与进程身份判定。
    """
    if not IS_WINDOWS:
        return ""
    try:
        kernel32 = _load_kernel32()
    except PdhUnavailable:
        return ""

    for access in (_PROCESS_QUERY_LIMITED_INFORMATION, _PROCESS_QUERY_INFORMATION):
        handle = kernel32.OpenProcess(access, False, pid)
        if not handle:
            continue
        try:
            size = wintypes.DWORD(_NAME_BUFFER_CHARS)
            buffer = ctypes.create_unicode_buffer(_NAME_BUFFER_CHARS)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
        finally:
            kernel32.CloseHandle(handle)
    return ""


def probe() -> Tuple[bool, bool, str]:
    """一次性探测本机两条通路，返回 (显存通路可用, CUDA 引擎通路可用, 说明)。

    给 --selftest / precheck 调用。两个分开报，因为**显存**是告警的依据，
    而 **CUDA 引擎活动只是筛选条件** —— 引擎计数器缺失时不该让监控停摆，
    只是退化成"不筛选"，这一点必须在自检里看得见。
    """
    if not IS_WINDOWS:
        return False, False, "非 Windows 平台，没有性能计数器通路"

    memory_ok = False
    cuda_ok = False
    notes: List[str] = []

    try:
        _sample_counters([COUNTER_MEMORY])
        memory_ok = True
    except (PdhUnavailable, OSError, AttributeError) as exc:
        notes.append(f"显存计数器不可用：{exc}")

    try:
        _sample_counters([COUNTER_ENGINE_UTIL, COUNTER_ENGINE_TIME])
        cuda_ok = True
    except (PdhUnavailable, OSError, AttributeError) as exc:
        notes.append(f"CUDA 引擎计数器不可用：{exc}")

    return memory_ok, cuda_ok, "；".join(notes)


def available() -> bool:
    """显存通路（告警依据）在本机能不能用。"""
    return probe()[0]
