"""采集层：调用 nvidia-smi 读取 GPU 与进程占用。

三个关键实现细节：

1. 子进程必须带 CREATE_NO_WINDOW。
   pythonw.exe 本身没有控制台，调用 nvidia-smi 这类控制台程序时，
   Windows 会为它新建一个控制台窗口 —— 表现为屏幕上每隔几秒闪一次黑框，
   直接毁掉"后台隐藏运行"这个需求。

2. 显存百分比的分母必须是"单张卡的显存总量"。
   nvidia-smi --query-compute-apps 返回的 used_memory 是单个进程在
   某一张卡上的占用；如果分母用多卡总量之和，双卡机器上算出来的
   百分比会被稀释一半，阈值形同虚设。

3. **WDDM 下进程占用必须走性能计数器兜底。**
   Windows 上（GeForce 不能切 TCC）nvidia-smi 的 per-process used_memory
   对**所有**进程都返回 [N/A]，只有整卡数字可用。这时如果只是"跳过 N/A 行"，
   拿到的永远是空列表 → 判定层永远空集合 → 静默不报警，
   而且日志上看起来一切正常。所以这里做二次兜底：nvidia-smi 没给出一条
   可用数据时，改问 `win_gpu_mem`（性能计数器 "GPU Process Memory"）。
   实际用的是哪条通路会写进 Snapshot.source，排障时一眼可见。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import win_gpu_mem

#: Windows 进程创建标志：不为控制台程序分配控制台窗口。
CREATE_NO_WINDOW = 0x08000000

#: 数据来源标识，会进日志与快照，用于排障。
SOURCE_NVIDIA_SMI = "nvidia-smi"
SOURCE_PDH = "windows-pdh"

#: CUDA 活动标记是从哪来的。真机走性能计数器；开发机由假 smi 提供。
#: 这个字段存在的唯一目的是**不要在排障输出里说假话** ——
#: 否则开发机上会看到"引擎计数器可用"，然后去真机找一个根本不存在的计数器。
CUDA_SOURCE_COUNTER = "性能计数器"
CUDA_SOURCE_FAKE = "假 nvidia-smi（开发模式）"

NVSMI_FALLBACKS = (
    r"C:\Windows\System32\nvidia-smi.exe",
    r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
)


class CollectorError(RuntimeError):
    """采集失败（nvidia-smi 不可用、驱动异常、超时等）。"""


@dataclass(frozen=True)
class Gpu:
    index: int
    uuid: str
    name: str
    memory_total_mb: Optional[float]
    memory_used_mb: Optional[float]
    utilization_pct: Optional[float]


@dataclass(frozen=True)
class ProcessUsage:
    gpu_index: Optional[int]
    gpu_uuid: str
    pid: int
    name: str
    used_memory_mb: float
    #: 该进程是否在 CUDA 引擎上跑过。`None` = 本机拿不到引擎计数器（Linux、
    #: 或性能计数器不可用）。判定层遇到 None 时**不过滤** —— 宁可多报，
    #: 也不能因为筛选条件缺失就静默漏报。
    cuda_active: Optional[bool] = None
    #: 当前瞬时 CUDA 引擎利用率（%），仅用于展示。
    cuda_util_pct: Optional[float] = None

    @property
    def short_name(self) -> str:
        """把 C:\\path\\to\\python.exe 压成 python.exe，便于推送展示。"""
        base = os.path.basename(self.name.replace("\\", "/"))
        return base or self.name

    @property
    def path(self) -> str:
        """完整镜像路径；**拿不到路径时返回空串**。

        `name` 在拿不到路径时会退化成 "pid 1234" 这类占位文本，
        所以这里用"有没有目录层级"来判，而不是直接返回 `name` ——
        否则告警正文会写出 "pid 1234 (PID 1234)" 这种废话。
        """
        parent, _ = split_process_path(self.name)
        return self.name if parent else ""

    @property
    def label(self) -> str:
        """展示用标签：`yolo_ultra\\python.exe`。比短名多一层目录，短名无法区分时靠它。"""
        return path_label(self.name)


def find_nvidia_smi(configured: str = "") -> str:
    """定位 nvidia-smi：显式配置 > PATH > 驱动默认安装位置。"""
    if configured:
        path = Path(configured)
        if path.is_file():
            return str(path)
        raise CollectorError(f"配置指定的 nvidia-smi 不存在：{configured}")

    found = shutil.which("nvidia-smi")
    if found:
        return found

    for candidate in NVSMI_FALLBACKS:
        if Path(candidate).is_file():
            return candidate

    raise CollectorError(
        "找不到 nvidia-smi。请确认已安装 NVIDIA 驱动，或在配置里显式指定 nvidia_smi 路径。"
    )


def _startup_info() -> Optional[subprocess.STARTUPINFO]:
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def _build_command(smi: str, args: Sequence[str]) -> List[str]:
    """把 nvidia-smi 路径翻译成完整命令行。

    若路径指向 .py 文件，说明这是开发用的模拟脚本，改用当前解释器执行。
    """
    if smi.lower().endswith(".py"):
        return [sys.executable, smi, *args]
    return [smi, *args]


def _run(smi: str, args: Sequence[str], timeout: float) -> str:
    """执行 nvidia-smi 并返回 stdout。全程不创建可见窗口。"""
    creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
    command = _build_command(smi, args)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=creationflags,
            startupinfo=_startup_info(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise CollectorError(f"无法执行 {smi}：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CollectorError(f"nvidia-smi 执行超时（{timeout:g}s），GPU 可能处于异常状态。") from exc
    except OSError as exc:
        raise CollectorError(f"启动 nvidia-smi 失败：{exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        hint = ""
        if "NVML" in detail:
            hint = (
                "（NVML 初始化失败：通常是驱动未加载、GPU 被独占、"
                "或进程运行在受限环境/容器里无设备访问权限）"
            )
        raise CollectorError(
            f"nvidia-smi 返回码 {completed.returncode}：{detail or '无输出'}{hint}"
        )

    return completed.stdout or ""


def _to_float(text: str) -> Optional[float]:
    value = text.strip()
    if not value or value.startswith("[") or value.upper() in {"N/A", "UNKNOWN"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_int(text: str) -> Optional[int]:
    value = _to_float(text)
    return None if value is None else int(value)


def split_process_path(path: str) -> Tuple[str, str]:
    """把进程镜像路径拆成 (上层目录名, 文件名)。

        D:\\Anaconda\\envs\\yolo_ultra\\python.exe → ("yolo_ultra", "python.exe")
        C:\\Windows\\System32\\dwm.exe            → ("System32", "dwm.exe")
        python.exe（只有名字）                     → ("", "python.exe")
        D:\\python.exe（只有盘符）                 → ("", "python.exe")
        pid 1234（拿不到路径时的占位）             → ("", "pid 1234")
    """
    text = (path or "").strip()
    if not text:
        return "", ""
    parts = [chunk for chunk in text.replace("\\", "/").split("/") if chunk]
    if len(parts) < 2:
        return "", text
    parent = parts[-2]
    # "D:python.exe" 这种父级只剩下盘符的，等于没有目录信息。
    if not parent or parent.endswith(":"):
        return "", parts[-1]
    return parent, parts[-1]


def path_label(path: str) -> str:
    """把完整路径压成「上层目录名\\文件名」。

    为什么不是直接用短名：目标机上同时跑着 26 个 `python.exe`，
    只写短名等于没有信息 —— 而多带一个 conda 环境目录名（`yolo_ultra\\python`
    对照 `mmdet_py39\\python`）就能一眼分清，长度却只多了几个字。
    """
    parent, base = split_process_path(path)
    return f"{parent}\\{base}" if parent else base


def list_gpus(smi: str, timeout: float) -> List[Gpu]:
    """读取所有 GPU 的基本信息。"""
    out = _run(
        smi,
        [
            "--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        timeout,
    )

    gpus: List[Gpu] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # 用 maxsplit 拆前两列，再对余下部分从右侧拆，避免显卡名里的逗号导致错位。
        head = line.split(",", 2)
        if len(head) < 3:
            continue
        index = _to_int(head[0])
        if index is None:
            continue
        tail = head[2].rsplit(",", 3)
        if len(tail) < 4:
            continue
        gpus.append(
            Gpu(
                index=index,
                uuid=head[1].strip(),
                name=tail[0].strip(),
                memory_total_mb=_to_float(tail[1]),
                memory_used_mb=_to_float(tail[2]),
                utilization_pct=_to_float(tail[3]),
            )
        )
    return gpus


@dataclass(frozen=True)
class ComputeAppScan:
    """一次「谁在用 GPU」的扫描结果。

    apps 为空不一定是"没人用" —— 也可能是本机这条通路拿不到数据。
    unusable 记录被丢掉的行数（典型情况：WDDM 下全部返回 [N/A]），
    调用方据此决定要不要切到性能计数器兜底。
    """

    apps: List[ProcessUsage]
    unusable: int = 0

    @property
    def has_data(self) -> bool:
        return bool(self.apps)


def list_compute_apps(smi: str, timeout: float) -> ComputeAppScan:
    """读取占用 GPU 的计算进程。没有进程时 apps 为空，而不是报错。"""
    out = _run(
        smi,
        [
            "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        ],
        timeout,
    )

    apps: List[ProcessUsage] = []
    unusable = 0
    for line in out.splitlines():
        line = line.strip()
        if not line or "no running processes" in line.lower():
            continue
        head = line.split(",", 2)
        if len(head) < 3:
            continue
        pid = _to_int(head[1])
        if pid is None:
            continue
        # 进程名（完整路径）里可能带逗号，所以从右侧取 used_memory。
        name, sep, mem_text = head[2].rpartition(",")
        if not sep:
            continue
        memory = _to_float(mem_text)
        if memory is None:
            # 关键分支：WDDM 模式下这里对每个进程都会命中。
            # 老实现直接 continue，等于把所有进程静默丢掉。
            unusable += 1
            continue
        apps.append(
            ProcessUsage(
                gpu_index=None,
                gpu_uuid=head[0].strip(),
                pid=pid,
                name=name.strip(),
                used_memory_mb=memory,
            )
        )
    return ComputeAppScan(apps=apps, unusable=unusable)


def _apps_via_pdh(gpus: Sequence[Gpu], reason: str) -> Tuple[List[ProcessUsage], str]:
    """性能计数器兜底：返回 (进程列表, 说明)。说明非空表示没能拿到数据。"""
    if not win_gpu_mem.IS_WINDOWS:
        return [], reason

    if len(gpus) != 1:
        # 性能计数器的实例名里只有 luid，没有 nvidia-smi 的卡序号，
        # 多卡机器上无法可靠映射。宁可不报，也不要把进程挂到错误的卡上。
        return [], (
            f"{reason}；机器上有 {len(gpus)} 张卡，性能计数器的 luid 无法可靠"
            "对应到 nvidia-smi 的卡序号，故不兜底（避免报错卡号）"
        )

    try:
        usage = win_gpu_mem.per_process_mb()
    except win_gpu_mem.PdhUnavailable as exc:
        return [], f"{reason}；性能计数器兜底也不可用：{exc}"

    gpu = gpus[0]
    apps: List[ProcessUsage] = []
    for pid, mib in usage.items():
        path = win_gpu_mem.process_image_path(pid)
        apps.append(
            ProcessUsage(
                gpu_index=gpu.index,
                gpu_uuid=gpu.uuid,
                pid=pid,
                # 拿不到路径就退回 "pid 1234"：进程身份判定和展示都还能用，
                # 总比丢掉这条占用记录好。
                name=path or f"pid {pid}",
                used_memory_mb=mib,
            )
        )
    return apps, ""


#: 开发机联调用的附加查询：只有假 smi（*.py）才认这个开关。
#: 真机上 CUDA 活动来自 Windows 性能计数器，不经过 nvidia-smi。
FAKE_CUDA_QUERY = "--query-cuda-activity"


def _fake_cuda_activity(
    smi: str, timeout: float
) -> Dict[int, win_gpu_mem.CudaActivity]:
    """从假 smi 取 CUDA 引擎活动。输出格式：每行 `pid, cuda_util`。"""
    out = _run(smi, [FAKE_CUDA_QUERY], timeout)
    activity: Dict[int, win_gpu_mem.CudaActivity] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, sep, util_text = line.partition(",")
        if not sep:
            continue
        pid = _to_int(pid_text)
        util = _to_float(util_text)
        if pid is None or util is None:
            continue
        activity[pid] = win_gpu_mem.CudaActivity(active=True, util_pct=util)
    return activity


def _cuda_activity(
    smi: str, timeout: float
) -> Tuple[Dict[int, win_gpu_mem.CudaActivity], str, str]:
    """PID → CUDA 引擎活动。返回 (映射, 失败原因, 来源)。

    拿不到就返回空映射 + 原因 —— **调用方必须把"拿不到"和"没有 CUDA 进程"
    区分开**：前者要让 cuda_active 保持 None（判定层因此不过滤），
    后者才是真的没有。
    """
    if not win_gpu_mem.IS_WINDOWS:
        return {}, "", ""

    if smi.lower().endswith(".py"):
        # 开发机：smi 指向假脚本，本机没有可用的引擎计数器。
        # 不从假脚本要 CUDA 活动的话，所有假进程都会被标成"非 CUDA"，
        # metric=cuda_util 在本地一条都触发不了 —— 等于这条链路没法联调。
        try:
            return _fake_cuda_activity(smi, timeout), "", CUDA_SOURCE_FAKE
        except CollectorError as exc:
            return (
                {},
                f"假 smi 没有提供 CUDA 活动数据（{exc}），本轮不按 CUDA 活动筛选",
                "",
            )

    try:
        return win_gpu_mem.cuda_activity(), "", CUDA_SOURCE_COUNTER
    except win_gpu_mem.PdhUnavailable as exc:
        return {}, f"CUDA 引擎计数器不可用（{exc}），本轮不按 CUDA 活动筛选", ""


@dataclass(frozen=True)
class Snapshot:
    gpus: List[Gpu]
    apps: List[ProcessUsage]
    #: 进程占用是从哪条通路拿到的，排障时看这个字段。
    source: str = SOURCE_NVIDIA_SMI
    #: 兜底时的说明文本（为什么换通路 / 为什么没换成）。空串表示一切正常。
    note: str = ""
    #: 本轮的进程是否带 CUDA 活动标记。False 表示引擎计数器拿不到，
    #: 判定层将不做 CUDA 筛选（而不是把所有进程都当成非 CUDA）。
    cuda_tagged: bool = True
    #: CUDA 活动标记的来源，进排障输出。
    cuda_source: str = CUDA_SOURCE_COUNTER


def collect(smi: str, timeout: float, gpu_filter: Optional[List[int]] = None) -> Snapshot:
    """一次完整采集，并把进程关联到它所在的 GPU 序号上。"""
    gpus = list_gpus(smi, timeout)
    if not gpus:
        raise CollectorError("nvidia-smi 未返回任何 GPU 信息。")

    if gpu_filter:
        wanted = set(gpu_filter)
        selected = [gpu for gpu in gpus if gpu.index in wanted]
        missing = wanted - {gpu.index for gpu in selected}
        if missing:
            raise CollectorError(
                f"配置里指定了 GPU {sorted(missing)}，但机器上只有 "
                f"{sorted(gpu.index for gpu in gpus)}。"
            )
        gpus = selected

    by_uuid = {gpu.uuid: gpu.index for gpu in gpus}
    scan = list_compute_apps(smi, timeout)
    source = SOURCE_NVIDIA_SMI
    note = ""

    apps: List[ProcessUsage] = []
    if scan.has_data:
        for app in scan.apps:
            index = by_uuid.get(app.gpu_uuid)
            if index is None:
                # 进程在未被监控的卡上，跳过。
                continue
            apps.append(
                ProcessUsage(
                    gpu_index=index,
                    gpu_uuid=app.gpu_uuid,
                    pid=app.pid,
                    name=app.name,
                    used_memory_mb=app.used_memory_mb,
                )
            )
    else:
        # nvidia-smi 一条可用数据都没给（WDDM 下的 [N/A] 是常态）。
        # 换性能计数器兜底，否则判定层会永远看到空集合而无告警。
        if scan.unusable:
            reason = (
                f"nvidia-smi 返回的 {scan.unusable} 个进程显存全部是 [N/A]"
                "（WDDM 模式下的已知限制）"
            )
        else:
            reason = "nvidia-smi 没有报告任何计算进程"
        apps, note = _apps_via_pdh(gpus, reason)
        if not note:
            source = SOURCE_PDH

    # 打 CUDA 活动标记。真机上显存与 CUDA 活动来自同一个 PDH query，
    # 所以这一步几乎不额外花钱。
    cuda_map, cuda_error, cuda_source = _cuda_activity(smi, timeout)
    # cuda_tagged 表示"本轮的 cuda_active 标记可信"。
    # 只有 Windows 上跑通了引擎计数器才算 —— Linux 上压根没有这条通路，
    # 那边的 nvidia-smi 本来只列计算进程，也不需要筛。
    cuda_tagged = win_gpu_mem.IS_WINDOWS and not cuda_error
    if cuda_error:
        note = "；".join(filter(None, [note, cuda_error]))
    if cuda_tagged:
        apps = [
            replace(
                app,
                cuda_active=bool(cuda_map.get(app.pid)),
                cuda_util_pct=cuda_map[app.pid].util_pct if app.pid in cuda_map else 0.0,
            )
            for app in apps
        ]

    apps.sort(
        key=lambda item: (
            item.gpu_index if item.gpu_index is not None else -1,
            -item.used_memory_mb,
        )
    )
    return Snapshot(
        gpus=gpus,
        apps=apps,
        source=source,
        note=note,
        cuda_tagged=cuda_tagged,
        cuda_source=cuda_source or CUDA_SOURCE_COUNTER,
    )
