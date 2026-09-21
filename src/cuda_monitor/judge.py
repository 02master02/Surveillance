"""判定层：决定「这一次要不要提醒，提醒谁」。

设计要点（2026-09-18 按用户要求改过，改动理由写在这里免得以后被"优化"掉）：

* **触发条件由 `Thresholds` 定义**，默认是「**CUDA 引擎利用率 > 15%**」。
  改判定规则只改 config.py，这一层不重复实现 —— 见 `Thresholds.evaluate()`。
* **同一进程只提醒一次。** 按进程身份（卡号 + PID + 进程名）去重：
  提醒过的进程进 `_notified` 记忆，之后**哪怕它回落后再涨上来也不再推**。
  这是用户明确要的（"同一进程不要重复提醒"）。
* **记忆只在进程退出时清除，不是在它跌破阈值时清除。**
  这条是整套逻辑的关键：CUDA 利用率在一轮里 66%、间歇掉到 0%，
  如果按"跌破阈值就忘掉"来做，每个峰谷都会重新推一条，
  一个 20 秒周期的任务几分钟就能把微信日限额刷光。
  用"进程退出才忘"，周期性负载天然只推一次。
* 因为上一条，**不再需要迟滞（退出线）**。迟滞原本是为了防"集合反复进出"，
  而"按进程去重"比它更强：不是压住推送，而是根本不会重复判定为"新告警"。
  所以 `exit_memory_percent` / `exit_memory_mb` 已经删掉。
* **推送节流。** 距上次推送太近时压住不发，并且**不提交任何状态变更**，
  下一轮自然重试。注意"被压住"不等于"忘了" —— 解除通知也不会丢。
* **记忆要落盘（`state_file`）。** 上面那条"同一进程只提醒一次"如果只活在内存里，
  重启一次名单就清空 —— 而重启恰恰是最常见的事（改配置、升级、断电）。
  实测踩过：同一个进程 11672 在一轮排查里被重启 3 次，就被推了 3 次，
  用户的原话是"相同进程在结束前永远不重复推送"。
  所以提交状态时把 `_notified` 原子写到 `state_file`，启动时读回来。
* **退出要"缺席确认"（`EXIT_MISS_LIMIT` 轮）。** 一轮扫不到不等于进程走了 ——
  采集层拿不到数据时返回的是空列表而非异常。规则细节见
  `_resolve_departures()`：它同时决定了"哪些退出要通知、哪些要静默丢弃"。

这一层不碰 nvidia-smi，也不碰网络，纯计算 + 状态（+ 一个状态文件），方便单独测试。
`state_file=None` 时连文件都不碰，退化成"重启即忘"的纯内存模式，
`--once` / `--preview` 这类诊断命令必须走这条路 —— 否则跑一次排障就会
把进程记进去，真正该发的那条告警反而被自己压掉。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .collector import Snapshot, split_process_path
from .config import METRIC_CUDA_MEMORY, Thresholds

logger = logging.getLogger("cuda_monitor.judge")

#: 判定「还是不是同一个进程」的依据：卡号 + PID + 进程名。
#: 只用 PID 不够 —— PID 会被系统复用；只用名字更不够 —— 同名多开很常见。
Identity = Tuple[int, int, str]

#: 状态文件格式版本。结构一变就 +1；读到不认识的版本按"从零开始"，不猜。
STATE_VERSION = 1

#: 判定"进程真的退出了"需要连续缺席几轮。
#:
#: 为什么不能一缺席就判退出：`collector.collect()` 在性能计数器不可用时
#: 返回的是**空进程列表而不是异常**（见 `_apps_via_pdh`），那只是"这一轮没拿到
#: 数据"，不是"进程走了"。一见不到就判退出，会先推一条"已结束"，
#: 下一轮进程重新出现又被当成新告警推一遍 —— 两条垃圾推送，正是要消灭的形状。
#: 3 轮 ≈ 15 秒（默认 5s 轮询），足够跨过瞬时抖动，用户感知不到延迟。
EXIT_MISS_LIMIT = 3


def _strip_exe(name: str) -> str:
    """展示时把 .exe 去掉，省下来的字用来显示所在目录。"""
    return name[:-4] if name.lower().endswith(".exe") else name


def _restore_entry(item: Any) -> Optional[Tuple[Identity, Alert, float]]:
    """还原状态文件里的一条记录。任何一处不对就只丢这一条，不牵连别的。"""
    if not isinstance(item, dict):
        return None

    raw_identity = item.get("identity")
    if not (isinstance(raw_identity, (list, tuple)) and len(raw_identity) == 3):
        return None
    gpu_index, pid, short_name = raw_identity
    if not isinstance(short_name, str) or not short_name:
        return None
    try:
        identity: Identity = (int(gpu_index), int(pid), short_name)
    except (TypeError, ValueError):
        return None

    payload = item.get("alert")
    if not isinstance(payload, dict):
        return None
    known = {field.name for field in fields(Alert)}
    try:
        alert = Alert(**{key: value for key, value in payload.items() if key in known})
    except TypeError:
        # 缺必填字段（比如更早版本写的文件）。
        return None

    # 身份与详情对不上，说明文件被手工改坏了。宁可整条丢掉，
    # 也不要让"详情是 A、身份是 B"这种记录留在名单里。
    if alert.identity != identity:
        return None

    try:
        notified_at = float(item.get("notified_at"))
    except (TypeError, ValueError):
        # 时间戳缺失不该让这条记录作废 —— 丢掉记忆可能会导致重复推送，
        # 比"持续时长算得不准"严重得多。
        notified_at = time.time()
    return identity, alert, notified_at


@dataclass(frozen=True)
class Alert:
    gpu_index: int
    gpu_name: str
    pid: int
    #: 短进程名（`python.exe`）。**它同时是去重身份的一部分**，不能改成完整路径。
    process_name: str
    used_mb: float
    total_mb: float
    #: 该进程显存占整卡显存的百分比（始终是显存口径，用于展示内存压力）。
    percent: float
    #: **触发指标**的数值。默认指标是 CUDA 利用率，所以这里就是利用率百分比。
    metric_value: Optional[float] = None
    #: 触发指标的名字，进推送标题（"CUDA" / "显存"）。
    metric_label: str = "显存"
    #: 完整镜像路径，例如 `D:\Anaconda\envs\yolo_ultra\python.exe`。
    #: 拿不到时为空串，展示层退回 `process_name`。
    #: 目标机上同时有 26 个 `python.exe`，只写短名根本认不出是哪一个 ——
    #: 所以日志正文一律用完整路径（微信另有 20 字硬限制，见 `labels`）。
    process_path: str = ""

    @property
    def identity(self) -> Identity:
        return (self.gpu_index, self.pid, self.process_name)

    @property
    def display_path(self) -> str:
        """正文里用什么标识这个进程：优先完整路径，拿不到才退回短名。"""
        return self.process_path or self.process_name

    @property
    def labels(self) -> Tuple[str, ...]:
        """展示用名称候选，**从最有辨识度到最短**。

            D:\\Anaconda\\envs\\yolo_ultra\\python.exe
                → ("yolo_ultra\\python", "yolo_ultra", "python")

        微信单个字段（模板行）只有 20 字，还要扣掉 "1. " 前缀、PID 和百分比，
        完整路径**根本放不下**。所以这里给出阶梯，由 notifier 按宽度预算
        从前往后挑第一个塞得下的。

        注意：**降级只降名称，不丢 PID** —— 真机上 20 多个 `python.exe`
        靠"目录 + PID"才认得出来。宽度不够时先让出百分比，见
        `notifier._format_process()` 的说明。
        """
        stem = _strip_exe(self.process_name)
        names: List[str] = []
        parent, _ = split_process_path(self.process_path)
        if parent:
            names.append(f"{parent}\\{stem}")
            names.append(parent)
        names.append(stem)
        # 去重保序（无路径时 parent 为空，只剩 stem，与老行为一致）。
        return tuple(dict.fromkeys(name for name in names if name))

    @property
    def display_percent(self) -> float:
        """要展示给用户的百分比 —— 用触发指标的值，不是显存占比。

        否则会出现"告警说 4.8%，但触发线是 15%"这种自相矛盾的消息。
        """
        return self.metric_value if self.metric_value is not None else self.percent

    @property
    def detail(self) -> str:
        return (
            f"{self.display_path} (PID {self.pid}) 在 GPU {self.gpu_index} 上，"
            f"{self.metric_label}占用 {self.display_percent:.1f}%"
            f"（显存 {self.used_mb:.0f} MiB / {self.total_mb:.0f} MiB）"
        )

    @property
    def one_line(self) -> str:
        return (
            f"{self.display_path} (PID {self.pid}) "
            f"GPU{self.gpu_index} {self.metric_label}{self.display_percent:.1f}%"
            f" 显存{self.used_mb:.0f}MiB"
        )


@dataclass
class Decision:
    """一轮扫描得出的结论。仅在「有新达标进程」或「有进程退出」时才返回。"""

    #: 本轮**新**达标、需要提醒的进程（已提醒过的不在里面）。
    current: List[Alert]
    #: 本轮退出、需要发结束通知的进程（曾经提醒过的）。
    previous: List[Alert]
    since: float
    now: float

    @property
    def cleared(self) -> bool:
        """本轮没有新告警，只有进程退出 → 走"已结束"通知。"""
        return not self.current

    @property
    def is_first(self) -> bool:
        return not self.previous

    @property
    def added(self) -> List[Alert]:
        known = {item.identity for item in self.previous}
        return [item for item in self.current if item.identity not in known]

    @property
    def removed(self) -> List[Alert]:
        known = {item.identity for item in self.current}
        return [item for item in self.previous if item.identity not in known]

    @property
    def duration(self) -> float:
        return max(self.now - self.since, 0.0)


class BatchJudge:
    """触发条件判定 + 按进程去重。状态在实例上，长跑时一路复用同一个实例。

    `state_file` 不为空时，那份去重记忆会在提交状态的同时落盘，
    并在构造时读回来 —— 这是"重启后同一进程仍不重复推送"的实现位置。
    """

    def __init__(
        self,
        thresholds: Thresholds,
        min_push_interval_sec: float = 0.0,
        state_file: Optional[os.PathLike | str] = None,
        exit_miss_limit: int = EXIT_MISS_LIMIT,
    ) -> None:
        self.thresholds = thresholds
        self.min_push_interval_sec = min_push_interval_sec
        self.exit_miss_limit = max(1, int(exit_miss_limit))

        #: 已经提醒过的进程。**只在进程退出时清除**，见模块开头的说明。
        self._notified: Dict[Identity, Alert] = {}
        #: 每个已提醒进程的首次提醒时间，用来算"持续多久"。
        self._notified_at: Dict[Identity, float] = {}

        # 用 -inf 而不是 0：表示"从没推过"，否则第一次推送会被自己的节流挡住。
        self._last_push_at: float = float("-inf")
        self._suppressed: int = 0

        self._state_path: Optional[Path] = Path(state_file) if state_file else None
        self._save_failed: bool = False
        #: 本次启动从盘上恢复的条目数（排障用，恢复后不再变）。
        self._restored: int = 0
        #: 从状态文件恢复、还没在本轮运行里露过面的条目。它们被淘汰时**不发通知**，
        #: 见 `_resolve_departures` 的规则二。
        self._unverified: Set[Identity] = set()
        #: 每个条目的连续缺席次数，够了才判退出，见 EXIT_MISS_LIMIT。
        self._misses: Dict[Identity, int] = {}
        self._restore_announced: bool = False

        self._restored = self._load_state()

    # ---------------------------------------------------------------- 对外

    def evaluate(self, snapshot: Snapshot, now: float) -> Optional[Decision]:
        """没有新达标进程、也没有进程退出 → 返回 None。"""
        # 必须与 `Alert.identity` 口径一致（都用 short_name）。
        # 用完整路径会与 _notified 的键不匹配，导致每轮都把已提醒进程
        # 误判为"已退出"，重复推送。
        present = {
            (app.gpu_index, app.pid, app.short_name)
            for app in snapshot.apps
            if app.gpu_index is not None
        }

        # 退出判定必须走"缺席确认"，且要在算 fresh 之前完成。
        gone, silently_dropped = self._resolve_departures(present)
        for identity in silently_dropped:
            self._forget(identity)

        candidates = self._collect_candidates(snapshot)
        fresh = [alert for ident, alert in candidates.items() if ident not in self._notified]

        if not fresh and not gone:
            self._suppressed = 0
            return None

        if self._throttled(now):
            # 距上次推送太近。这里刻意**什么都不提交** ——
            # 既不移除退出的记忆，也不记下新达标的进程，
            # 下一轮扫描会重新算出来，等间隔到了自然补发。
            self._suppressed += 1
            return None

        removed = [self._notified[ident] for ident in gone]
        removed_at = [self._notified_at.get(ident, now) for ident in gone]
        since = min(removed_at) if removed_at else now

        for ident in gone:
            self._forget(ident)
        for alert in fresh:
            self._notified[alert.identity] = alert
            self._notified_at[alert.identity] = now

        self._last_push_at = now
        self._suppressed = 0
        # 唯一写盘点：轮询本身不产生任何磁盘 IO，只有"真的推了一条"才落盘。
        self._save_state()

        sort_key = lambda item: -item.display_percent  # noqa: E731
        return Decision(
            current=sorted(fresh, key=sort_key),
            previous=sorted(removed, key=sort_key),
            since=since,
            now=now,
        )

    @property
    def active_count(self) -> int:
        """已经提醒过、还没退出的进程数，供日志/排障使用。"""
        return len(self._notified)

    @property
    def notified_identities(self) -> Tuple[Identity, ...]:
        """已提醒进程的快照，测试用。"""
        return tuple(self._notified)

    @property
    def state_file(self) -> Optional[Path]:
        """去重记忆的落盘位置；`None` 表示这一层不碰磁盘。"""
        return self._state_path

    @property
    def restored_count(self) -> int:
        """本次启动从盘上恢复了几条记忆（0 = 没落盘 / 首次运行）。"""
        return self._restored

    # ---------------------------------------------------------------- 状态文件

    def _load_state(self) -> int:
        """把上次运行的去重记忆读回来。**任何异常都不许冒泡**，读不动就当没记过。"""
        path = self._state_path
        if path is None:
            return 0

        try:
            if not path.is_file():
                return 0
            # utf-8-sig：连同"带 BOM 的 UTF-8"一起容忍（写侧不带 BOM，
            # 但文件可能被人用 PowerShell 的 `>>` 或记事本改过）。
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("去重记忆读取失败（%s）：%s —— 本次启动按空名单开始。", path, exc)
            return 0

        if not isinstance(raw, dict):
            logger.warning("去重记忆的根节点不是对象（%s），已忽略。", path)
            return 0
        version = raw.get("version")
        if version != STATE_VERSION:
            logger.warning(
                "去重记忆版本不认识（%s 里是 %r，本程序是 %d），已忽略。",
                path, version, STATE_VERSION,
            )
            return 0

        entries = raw.get("entries")
        if not isinstance(entries, list):
            return 0

        restored = 0
        for item in entries:
            parsed = _restore_entry(item)
            if parsed is None:
                continue
            identity, alert, notified_at = parsed
            self._notified[identity] = alert
            self._notified_at[identity] = notified_at
            self._unverified.add(identity)
            restored += 1

        if restored:
            logger.info(
                "已从 %s 恢复 %d 条去重记忆：这些进程在重启前提醒过，"
                "只要还在跑就不会再重复推送。",
                path, restored,
            )
        return restored

    def _save_state(self) -> None:
        """把当前记忆原子落盘。失败只记警告，绝不打断监控。"""
        path = self._state_path
        if path is None:
            return

        payload = {
            "version": STATE_VERSION,
            "saved_at": time.time(),
            "entries": [
                {
                    "identity": list(identity),
                    "notified_at": self._notified_at.get(identity, 0.0),
                    "alert": asdict(alert),
                }
                for identity, alert in self._notified.items()
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        temp = path.with_name(path.name + ".tmp")

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再 os.replace：中途断电也只会留下半个 .tmp，
            # 不会把好文件截断成坏 JSON —— 那会导致下次启动静默丢掉全部记忆。
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, path)
        except OSError as exc:
            if not self._save_failed:
                # 只在"第一次失败"和"恢复成功"时记日志，否则每推一次刷一条。
                self._save_failed = True
                logger.warning(
                    "去重记忆写盘失败（%s）：%s —— 监控继续运行，"
                    "但这份记忆只在内存里，重启后会重新提醒一次。",
                    path, exc,
                )
            try:
                temp.unlink()
            except OSError:
                pass
            return

        if self._save_failed:
            self._save_failed = False
            logger.info("去重记忆写盘已恢复正常：%s", path)

    # ---------------------------------------------------------------- 内部

    def _resolve_departures(
        self, present: Set[Identity]
    ) -> Tuple[List[Identity], List[Identity]]:
        """把"这一轮没见到的已提醒进程"分成两类。

        返回 `(要发结束通知的, 要静默丢弃的)`。

        **规则一：缺席要确认。** 连着 `EXIT_MISS_LIMIT` 轮见不到才算退出。
        `collector.collect()` 在性能计数器不可用时返回的是空进程列表而不是异常，
        那只是这一轮没拿到数据；一见不到就判退出会先推"已结束"、
        下一轮进程重新出现再当新告警推一遍 —— 两条垃圾推送。

        **规则二：重启恢复的条目不发通知。** 从状态文件读回来、且本轮运行里
        从没露过面的条目，淘汰时静默处理。监控当时不在场，说不出它是几点结束的；
        而每重启一次就补一堆"已结束"，本身就是新的刷屏源。
        """
        notified: List[Identity] = []
        discarded: List[Identity] = []
        revived: List[Identity] = []

        for identity in list(self._notified):
            if identity in present:
                self._misses.pop(identity, None)
                if identity in self._unverified:
                    self._unverified.discard(identity)
                    revived.append(identity)
                continue

            count = self._misses.get(identity, 0) + 1
            if count < self.exit_miss_limit:
                self._misses[identity] = count
                continue

            self._misses.pop(identity, None)
            if identity in self._unverified:
                self._unverified.discard(identity)
                discarded.append(identity)
            else:
                notified.append(identity)

        if revived and not self._restore_announced:
            self._restore_announced = True
            shown = "、".join(
                self._notified[identity].one_line
                for identity in sorted(revived, key=lambda item: item[1])[:3]
            )
            if len(revived) > 3:
                shown += f" 等 {len(revived)} 个"
            logger.info(
                "重启前已提醒过的 %d 个进程仍在运行，不再重复提醒：%s",
                len(revived), shown,
            )

        return notified, discarded

    def _forget(self, identity: Identity) -> None:
        self._notified.pop(identity, None)
        self._notified_at.pop(identity, None)
        self._unverified.discard(identity)
        self._misses.pop(identity, None)

    def _throttled(self, now: float) -> bool:
        if self.min_push_interval_sec <= 0:
            return False
        return (now - self._last_push_at) < self.min_push_interval_sec

    def _collect_candidates(self, snapshot: Snapshot) -> Dict[Identity, Alert]:
        """本轮达到触发条件的进程。**不含**迟滞逻辑 —— 去重由 `_notified` 负责。"""
        gpu_by_index = {gpu.index: gpu for gpu in snapshot.gpus}
        thresholds = self.thresholds

        candidates: Dict[Identity, Alert] = {}
        for app in snapshot.apps:
            if app.gpu_index is None:
                continue
            gpu = gpu_by_index.get(app.gpu_index)
            if gpu is None or not gpu.memory_total_mb:
                continue

            # CUDA 筛选：只在 cuda_memory 指标下有意义
            # （cuda_util 指标本身就是 CUDA 专属的，天然不会命中图形进程）。
            if not thresholds.cuda_eligible(app.cuda_active):
                continue

            value = thresholds.evaluate(
                used_mb=app.used_memory_mb,
                total_mb=gpu.memory_total_mb,
                cuda_util_pct=app.cuda_util_pct,
            )
            if value is None:
                continue

            # 同上：身份键统一用 short_name，与 Alert.identity 保持一字不差。
            identity: Identity = (app.gpu_index, app.pid, app.short_name)
            candidates[identity] = Alert(
                gpu_index=gpu.index,
                gpu_name=gpu.name,
                pid=app.pid,
                process_name=app.short_name,
                process_path=app.path,
                used_mb=app.used_memory_mb,
                total_mb=gpu.memory_total_mb,
                percent=app.used_memory_mb / gpu.memory_total_mb * 100.0,
                metric_value=value,
                metric_label=thresholds.label(),
            )
        return candidates


def format_snapshot(snapshot: Snapshot, thresholds: Optional[Thresholds] = None) -> str:
    """把一次采集渲染成可读的文本表，用于 --once / --selftest 排障。"""
    lines: List[str] = ["GPU 概览"]
    lines.append(f"  {'序号':<4}{'型号':<28}{'显存占用':>18}{'利用率':>10}")
    for gpu in snapshot.gpus:
        total = gpu.memory_total_mb
        used = gpu.memory_used_mb
        if total and used is not None:
            mem = f"{used:.0f}/{total:.0f} MiB ({used / total * 100:.1f}%)"
        else:
            mem = "N/A"
        util = "N/A" if gpu.utilization_pct is None else f"{gpu.utilization_pct:.0f}%"
        lines.append(f"  {gpu.index:<4}{gpu.name:<28}{mem:>18}{util:>10}")

    # 数据来源必须打出来。WDDM 下 nvidia-smi 的 per-process 全是 [N/A]，
    # 这时候如果不写明是走了性能计数器，看图的人会以为数字是 nvidia-smi 给的。
    lines.append(f"  进程占用来源：{snapshot.source}")
    if snapshot.cuda_tagged:
        lines.append(f"  CUDA 活动标记：已识别（来源：{snapshot.cuda_source or '性能计数器'}）")
    else:
        lines.append("  CUDA 活动标记：不可用（本轮不按 CUDA 活动筛选）")
    if snapshot.note:
        lines.append(f"  说明：{snapshot.note}")

    lines.append("")
    if not snapshot.apps:
        lines.append("当前没有进程在使用 GPU。")
        return "\n".join(lines)

    if thresholds is not None:
        lines.append(f"进程列表（{thresholds.summary()}）")
    else:
        lines.append("进程列表")
    # 表头刻意只有「进程」两个字 —— 这个字段是 `{:<26}` 按**字符数**补齐的，
    # 而数据行全是 ASCII 路径（26 字符 = 26 显示列）；表头若写成一长串中文，
    # 中文按 2 个显示列算，会把后面几列整体顶歪。
    lines.append(
        f"  {'':<2}{'卡':<4}{'PID':<8}{'进程':<26}{'CUDA%':>7}{'占用 MiB':>10}{'显存占比':>10}"
    )

    memory_metric = thresholds is not None and thresholds.metric == METRIC_CUDA_MEMORY
    hit_count = 0
    excluded = 0
    for app in snapshot.apps:
        gpu = next((item for item in snapshot.gpus if item.index == app.gpu_index), None)
        percent = None
        if gpu and gpu.memory_total_mb:
            percent = app.used_memory_mb / gpu.memory_total_mb * 100.0

        hit = False
        if thresholds is not None and gpu is not None:
            hit = (
                thresholds.evaluate(
                    used_mb=app.used_memory_mb,
                    total_mb=gpu.memory_total_mb or 0.0,
                    cuda_util_pct=app.cuda_util_pct,
                )
                is not None
            )
        eligible = thresholds is None or thresholds.cuda_eligible(app.cuda_active)

        if hit and eligible:
            flag = "★"
            hit_count += 1
        elif hit:
            # 达到数值条件，但不是在跑 CUDA —— 明确标出来。
            # 静默隐藏会让人以为"没检测到"，标出来才知道是"检测到但不算"。
            flag = "·"
            excluded += 1
        else:
            flag = " "

        if not snapshot.cuda_tagged or app.cuda_util_pct is None:
            cuda_text = "—"
        else:
            cuda_text = f"{app.cuda_util_pct:.1f}"
        percent_text = "N/A" if percent is None else f"{percent:.1f}%"
        index_text = "?" if app.gpu_index is None else str(app.gpu_index)
        # 这一列固定 26 字。UWP 包名动辄 60+ 字（`Microsoft.YourPhone_1.25...`），
        # 不截断会把后面几列整体顶歪；从左截并标 `..`，保住最右边的程序名 ——
        # 那才是认进程的部分，包名前缀没有必要看全。
        label = app.label
        if len(label) > 26:
            label = ".." + label[-24:]
        lines.append(
            f"  {flag:<2}{index_text:<4}"
            f"{app.pid:<8}{label:<26}{cuda_text:>7}"
            f"{app.used_memory_mb:>10.0f}{percent_text:>10}"
        )

    legend = [f"★ = 达到触发条件（{hit_count} 个）；同一进程只提醒一次"]
    if excluded:
        legend.append(f"· = 数值达标但非 CUDA 进程，不算（{excluded} 个）")
    lines.append(f"  （{'；'.join(legend)}）")
    lines.append("  （进程写成「所在目录\\程序名」，同名进程（如一堆 python.exe）靠目录区分）")
    if memory_metric and thresholds is not None:
        lines.append("  （当前触发指标是显存，不是 CUDA 利用率）")
    return "\n".join(lines)
