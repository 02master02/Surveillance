"""探针：Windows 下按进程读 GPU 显存（nvidia-smi 在 WDDM 模式报 N/A 时的替代源）。

用的是性能计数器 "GPU Process Memory"，也就是任务管理器「进程」页看到的那个数。
关键点是用 PdhAddEnglishCounterW 注册 —— 它接受英文计数器名，
所以中文 Windows 上也不用去查"GPU 进程内存"这种本地化名字。

只做只读探测，验证这条数据通路在目标机上是否可用。
"""

import ctypes
import sys
import time
from ctypes import wintypes

pdh = ctypes.WinDLL("pdh", use_last_error=True)

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2
ERROR_SUCCESS = 0

# 常见 PDH 错误码
PDH_ERRORS = {
    0xC0000BB8: "PDH_CSTATUS_NO_OBJECT（没有这个计数器对象）",
    0xC0000BB9: "PDH_CSTATUS_NO_COUNTER（没有这个计数器）",
    0xC0000BC0: "PDH_CSTATUS_NO_INSTANCE（没有实例）",
    0xC0000BBD: "PDH_CSTATUS_NO_MACHINE",
    0xC0000BC6: "PDH_INVALID_DATA",
    0xC0000BBF: "PDH_CSTATUS_NO_OBJECT / 名称不匹配",
}


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [
        ("CStatus", wintypes.DWORD),
        ("doubleValue", ctypes.c_double),
    ]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [
        ("szName", wintypes.LPWSTR),
        ("FmtValue", PDH_FMT_COUNTERVALUE),
    ]


def _sig(func, argtypes, restype=wintypes.DWORD):
    func.argtypes = argtypes
    func.restype = restype
    return func


_sig(pdh.PdhOpenQueryW, [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)])
_sig(pdh.PdhAddEnglishCounterW,
     [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)])
_sig(pdh.PdhCollectQueryData, [wintypes.HANDLE])
_sig(pdh.PdhGetFormattedCounterArrayW, [
    wintypes.HANDLE, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W),
])
_sig(pdh.PdhCloseQuery, [wintypes.HANDLE])


def describe(status):
    return PDH_ERRORS.get(status, f"未知状态 0x{status:08X}")


def sample(path):
    """采样一个多实例计数器，返回 [(实例名, 值), ...]。"""
    query = wintypes.HANDLE()
    status = pdh.PdhOpenQueryW(None, 0, ctypes.byref(query))
    if status != ERROR_SUCCESS:
        print(f"  PdhOpenQueryW 失败：{describe(status)}")
        return None

    try:
        counter = wintypes.HANDLE()
        status = pdh.PdhAddEnglishCounterW(query, path, 0, ctypes.byref(counter))
        if status != ERROR_SUCCESS:
            print(f"  PdhAddEnglishCounterW({path}) 失败：{describe(status)}")
            return None

        # 计数器多数需要两个采样点才有值。
        pdh.PdhCollectQueryData(query)
        time.sleep(0.6)
        status = pdh.PdhCollectQueryData(query)
        if status != ERROR_SUCCESS:
            print(f"  PdhCollectQueryData 失败：{describe(status)}")
            return None

        size = wintypes.DWORD(0)
        count = wintypes.DWORD(0)
        pdh.PdhGetFormattedCounterArrayW(
            counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None)
        if size.value == 0:
            print("  计数器存在，但当前没有任何实例（size=0）。")
            return []

        buffer = ctypes.create_string_buffer(size.value)
        items = ctypes.cast(buffer, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))
        status = pdh.PdhGetFormattedCounterArrayW(
            counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), items)
        if status != ERROR_SUCCESS:
            print(f"  PdhGetFormattedCounterArrayW 失败：{describe(status)}")
            return None

        out = []
        for i in range(count.value):
            item = items[i]
            out.append((item.szName, item.FmtValue.doubleValue))
        return out
    finally:
        pdh.PdhCloseQuery(query)


def pid_from_instance(name):
    """GPU Process Memory 的实例名形如 pid_26052_luid_0x..._phys_0。"""
    for part in name.split("_"):
        if part.isdigit():
            return int(part)
    return None


def main():
    # 目标机控制台代码页是 936，走 ssh 时中文会变成乱码。
    # 强制 stdout 输出 UTF-8，ssh 原样转发字节，开发机这边就能正确解码。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    print("=" * 72)
    print("GPU 按进程显存 · 数据通路探针")
    print("=" * 72)

    # 三个候选计数器语义不同，全部拉出来对比，选出最接近「显存」的那个：
    #   Dedicated Usage  ≈ 任务管理器「专用 GPU 内存」= 真正占在显卡上的显存
    #   Shared Usage     ≈ 借用系统内存那部分
    #   Local Usage      ≈ 本地段合计
    #   Total Committed  ≈ Dedicated + Shared
    candidates = [
        r"\GPU Process Memory(*)\Dedicated Usage",
        r"\GPU Process Memory(*)\Local Usage",
        r"\GPU Process Memory(*)\Total Committed",
        r"\GPU Process Memory(*)\Shared Usage",
    ]

    for path in candidates:
        print(f"\n--- {path} ---")
        rows = sample(path)
        if rows is None or not rows:
            continue

        # 同一进程可能有多个 luid/phys 实例，必须按 PID 求和，否则会低估。
        by_pid = {}
        for name, value in rows:
            pid = pid_from_instance(name)
            if pid is None:
                continue
            by_pid[pid] = by_pid.get(pid, 0.0) + value

        nonzero = {p: v for p, v in by_pid.items() if v > 0}
        print(f"  实例数 {len(rows)} / 进程数 {len(by_pid)} / 非零 {len(nonzero)}"
              f" / 合计 {sum(nonzero.values()) / 1024 / 1024:.1f} MiB")

        for pid, value in sorted(nonzero.items(), key=lambda kv: -kv[1])[:8]:
            print(f"    PID {pid:<8d} {value / 1024 / 1024:>10.1f} MiB")

    print("\n对照：nvidia-smi 整卡 memory.used 是以上哪一个的量级。")


if __name__ == "__main__":
    main()
