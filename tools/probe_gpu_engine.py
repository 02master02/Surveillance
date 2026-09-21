"""探针：枚举 GPU 引擎计数器，看能不能区分 CUDA / 图形 / 计算引擎。

目的：`\\GPU Process Memory(*)\\Local Usage` 给的是进程的 GPU 本地显存，
但图形和计算混在一起。要回答"这个进程的占用是不是 CUDA 的"，
得知道它在哪个引擎上干活 —— `\\GPU Engine(*)\\Running Time` 的实例名里带
engtype，这就有了区分依据。

实例名形如：
    pid_26660_luid_0x00000000_0x0000C4F0_phys_0_eng_3_engtype_Cuda

只做只读探测。输出保持 ASCII，避免 ssh 到代码页 936 的机器上中文乱码。
"""

import ctypes
import re
import sys
import time
from ctypes import wintypes

pdh = ctypes.WinDLL("pdh", use_last_error=True)

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_LARGE = 0x00000100
ERROR_SUCCESS = 0

PDH_ERRORS = {
    0xC0000BB8: "NO_OBJECT",
    0xC0000BB9: "NO_COUNTER",
    0xC0000BBC: "NO_INSTANCE",
    0xC0000BC6: "INVALID_DATA",
}


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", PDH_FMT_COUNTERVALUE)]


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


def sample(path, fmt=PDH_FMT_DOUBLE, gap=0.6):
    query = wintypes.HANDLE()
    status = pdh.PdhOpenQueryW(None, 0, ctypes.byref(query))
    if status != ERROR_SUCCESS:
        return None, f"PdhOpenQueryW 0x{status:08X}"

    try:
        counter = wintypes.HANDLE()
        status = pdh.PdhAddEnglishCounterW(query, path, 0, ctypes.byref(counter))
        if status != ERROR_SUCCESS:
            return None, f"AddEnglishCounter 0x{status:08X} {PDH_ERRORS.get(status, '')}"

        pdh.PdhCollectQueryData(query)
        time.sleep(gap)
        status = pdh.PdhCollectQueryData(query)
        if status != ERROR_SUCCESS:
            return None, f"CollectQueryData 0x{status:08X}"

        size = wintypes.DWORD(0)
        count = wintypes.DWORD(0)
        pdh.PdhGetFormattedCounterArrayW(
            counter, fmt, ctypes.byref(size), ctypes.byref(count), None)
        if size.value == 0:
            return [], "no instances"

        buffer = ctypes.create_string_buffer(size.value)
        items = ctypes.cast(buffer, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))
        status = pdh.PdhGetFormattedCounterArrayW(
            counter, fmt, ctypes.byref(size), ctypes.byref(count), items)
        if status != ERROR_SUCCESS:
            return None, f"GetFormattedCounterArray 0x{status:08X}"

        return [(items[i].szName, items[i].FmtValue.doubleValue) for i in range(count.value)], ""
    finally:
        pdh.PdhCloseQuery(query)


PID_RE = re.compile(r"pid_(\d+)", re.IGNORECASE)
ENGTYPE_RE = re.compile(r"engtype_(\w+)", re.IGNORECASE)
ENG_RE = re.compile(r"_eng_(\d+)", re.IGNORECASE)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    print("=" * 74)
    print("GPU ENGINE PROBE  (which engine is each process using?)")
    print("=" * 74)

    candidates = [
        r"\GPU Engine(*)\Running Time",
        r"\GPU Engine(*)\Utilization Percentage",
        r"\GPU Engine(*)\Running Time Base",
    ]

    for path in candidates:
        print(f"\n--- {path} ---")
        rows, err = sample(path)
        if rows is None:
            print(f"  FAILED: {err}")
            continue
        if not rows:
            print(f"  no instances ({err})")
            continue

        engtypes = {}
        by_pid_eng = {}
        for name, value in rows:
            m = ENGTYPE_RE.search(name)
            eng = m.group(1) if m else "?"
            engtypes[eng] = engtypes.get(eng, 0) + 1

            pid_m = PID_RE.search(name)
            if not pid_m:
                continue
            key = (int(pid_m.group(1)), eng)
            by_pid_eng[key] = by_pid_eng.get(key, 0.0) + value

        print(f"  instances={len(rows)}  engtypes={sorted(engtypes)}")
        for eng, n in sorted(engtypes.items(), key=lambda kv: -kv[1]):
            print(f"    engtype_{eng:<12} instances={n}")

        hot = {k: v for k, v in by_pid_eng.items() if v > 0}
        print(f"  --- non-zero (pid, engtype) pairs: {len(hot)} ---")
        for (pid, eng), value in sorted(hot.items(), key=lambda kv: -kv[1])[:15]:
            print(f"    pid {pid:<8d} engtype_{eng:<12} {value:>16.1f}")

        print("  --- sample instance names ---")
        for name, _ in rows[:4]:
            print(f"    {name}")

    print("\n" + "=" * 74)
    print("nvidia-smi accounting support check")
    print("=" * 74)
    import subprocess
    for args in (["--query-accounting=mode,bufferSize"],
                 ["--query-compute-apps=pid,used_memory"]):
        try:
            r = subprocess.run(["nvidia-smi", *args], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=15)
            out = (r.stdout or "").strip().splitlines()[:3]
            err = (r.stderr or "").strip().splitlines()[:2]
            print(f"  nvidia-smi {' '.join(args)} -> rc={r.returncode}")
            for line in out:
                print(f"      OUT: {line}")
            for line in err:
                print(f"      ERR: {line}")
        except Exception as exc:  # noqa: BLE001
            print(f"  nvidia-smi {' '.join(args)} -> {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
