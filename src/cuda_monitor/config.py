"""配置加载。

优先级：环境变量 > config.json > 内置默认值。

敏感字段（微信 AppSecret）建议只放在机器级环境变量里，
避免落盘后被同机其他用户读取：

    [Environment]::SetEnvironmentVariable("CUDA_MONITOR_WECHAT_SECRET", "...", "Machine")
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

ENV_APP_ID = "CUDA_MONITOR_WECHAT_APPID"
ENV_APP_SECRET = "CUDA_MONITOR_WECHAT_SECRET"
ENV_TEMPLATE_ID = "CUDA_MONITOR_WECHAT_TEMPLATE_ID"

DEFAULT_LOG_FILE = r"C:\ProgramData\CudaMonitor\monitor.log"

#: 去重记忆的落盘位置。**这是"重启之后不再重复推送"的关键**：
#: 「同一进程只提醒一次」原本只活在内存里，重启一次名单就清空，
#: 于是同一个进程在排查过程中每重启一次就被推一条（踩过：进程 11672 被推 3 次）。
#: 存盘后重启会接着算，进程不退出就一直不重复提醒。
DEFAULT_STATE_FILE = r"C:\ProgramData\CudaMonitor\notified.json"

#: 可选的进程调度优先级名称，对应 Windows 的优先级类。
PRIORITY_NAMES = ("idle", "below", "normal", "above", "high", "realtime")

#: 触发指标：CUDA 引擎利用率 / 该进程的 GPU 显存。
#: 这是两个完全不同的量，量级也差很远，别混着配。
METRIC_CUDA_UTIL = "cuda_util"
METRIC_CUDA_MEMORY = "cuda_memory"
METRIC_NAMES = (METRIC_CUDA_UTIL, METRIC_CUDA_MEMORY)


class ConfigError(RuntimeError):
    """配置文件缺失或内容非法。"""


def _num(value: Any, name: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError(f"配置项 {name} 必须是数字，当前值：{value!r}")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"配置项 {name} 必须是数字，当前值：{value!r}")


def _positive(value: Any, name: str, default: float) -> float:
    result = _num(value, name, default)
    if result <= 0:
        raise ConfigError(f"配置项 {name} 必须大于 0，当前值：{result!r}")
    return result


def _bool(value: Any, name: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0"}:
            return False
    raise ConfigError(f"配置项 {name} 必须是 true 或 false，当前值：{value!r}")


def _priority(value: Any) -> str:
    name = str(value if value is not None else "high").strip().lower()
    if name not in PRIORITY_NAMES:
        raise ConfigError(
            f"process_priority 必须是 {' / '.join(PRIORITY_NAMES)} 之一，当前值：{value!r}"
        )
    return name


def _parse_thresholds(raw: Dict[str, Any]) -> "Thresholds":
    """解析 thresholds 段。

    注意区分「键不存在」和「键是 null」：
    键不存在 → 用默认值（老配置照常工作）；
    键是 null → 明确表示关掉这一条判定（例如只按绝对 MiB 判定）。
    """
    metric = str(raw.get("metric") or METRIC_CUDA_UTIL).strip().lower()
    if metric not in METRIC_NAMES:
        raise ConfigError(
            f"thresholds.metric 必须是 {' / '.join(METRIC_NAMES)} 之一，"
            f"当前值：{raw.get('metric')!r}"
        )

    def optional_number(key: str, default: Optional[float]) -> Optional[float]:
        if key not in raw:
            return default
        value = raw[key]
        if value is None:
            return None
        return _num(value, f"thresholds.{key}", default if default is not None else 0.0)

    thresholds = Thresholds(
        metric=metric,
        cuda_util_percent=_num(
            raw.get("cuda_util_percent"), "thresholds.cuda_util_percent", 15.0
        ),
        memory_percent=optional_number("memory_percent", None),
        min_memory_mb=_num(raw.get("min_memory_mb"), "thresholds.min_memory_mb", 1000.0),
        cuda_only=_bool(raw.get("cuda_only"), "thresholds.cuda_only", True),
    )

    if metric == METRIC_CUDA_UTIL:
        if not 0.0 < thresholds.cuda_util_percent <= 100.0:
            raise ConfigError(
                f"thresholds.cuda_util_percent 必须在 (0, 100] 之间，"
                f"当前值：{thresholds.cuda_util_percent!r}。"
                "它是 CUDA 引擎利用率的百分比。"
            )
    else:
        # 显存口径下两条线全关掉的话，任何进程都会命中 —— 那不是配置失误就是笔误，
        # 与其每天推一百条告警，不如启动时直接拒绝。
        if thresholds.memory_percent is None and thresholds.min_memory_mb <= 0:
            raise ConfigError(
                "metric=cuda_memory 时，thresholds 里 memory_percent 和 min_memory_mb "
                "不能同时为空/0，否则所有进程都会被判定为超限。二选一即可："
                '按占比写 "memory_percent": 10，或按绝对量写 '
                '"memory_percent": null 加 "min_memory_mb": 1000。'
            )

    return thresholds


@dataclass
class Thresholds:
    """告警触发条件。

    ## 触发指标（metric）

    * `cuda_util`（默认）：**CUDA 引擎利用率**。这是"这个进程到底在不在跑 CUDA"
      最直接的度量，也是任务管理器「Cuda」引擎那一列的数字。
    * `cuda_memory`：**该进程占用的 GPU 显存**。适合"谁把显存吃满了"这类诉求。

    为什么要分成两个：显存和利用率是两件事，而且量级差很远。真机实测同一个训练
    进程，CUDA 利用率一轮能到 66.6%，而显存只占整卡的 4.8%。用显存去配 15%
    这种数字永远触发不了（48GB 卡上 15% = 7371 MiB，项目峰值才 2360 MiB）。

    ## 去重

    **同一进程只提醒一次**（按 卡号+PID+进程名 去重），由 judge 层的 `_notified`
    记忆实现。所以这里**不再需要迟滞（退出线）**：迟滞是为了防"集合反复进出"，
    而"按进程去重"比它更强。`exit_memory_percent` / `exit_memory_mb` 已删除。
    """

    #: 触发指标：`cuda_util` 或 `cuda_memory`。
    metric: str = METRIC_CUDA_UTIL
    #: metric=cuda_util 时的触发线：CUDA 引擎利用率严格大于此百分比才提醒。
    cuda_util_percent: float = 15.0
    #: metric=cuda_memory 时的相对线：该进程显存 ÷ 单卡显存总量，严格大于才提醒。
    #: `None` = 不按百分比判定，只看绝对条件。
    memory_percent: Optional[float] = None
    #: metric=cuda_memory 时的绝对线：占用低于此 MiB 数则不提醒（过滤桌面进程）。
    min_memory_mb: float = 1000.0
    #: metric=cuda_memory 时是否只统计 CUDA 进程（`cuda_util` 下无意义）。
    #:
    #: 为什么要筛：显存占用是进程级的，但 `Local Usage` 把图形和计算混在一起。
    #: 靠引擎类型（engtype_Cuda）才能回答"这到底是不是 CUDA 的占用"。
    #: 拿不到引擎计数器时**不筛选**（宁可多报，也不静默漏报）。
    cuda_only: bool = True

    def label(self) -> str:
        """触发指标的中文短名，进推送标题。"""
        return "CUDA" if self.metric == METRIC_CUDA_UTIL else "显存"

    def evaluate(
        self,
        used_mb: float,
        total_mb: float,
        cuda_util_pct: Optional[float] = None,
    ) -> Optional[float]:
        """判断这个进程是否达到触发条件。

        达到 → 返回**触发指标的数值**（用于展示和排序）；没达到 → `None`。
        返回数值而不是布尔，是为了让推送里显示的百分比和触发线口径一致 ——
        否则会出现"告警说 4.8%，但触发线是 15%"这种自相矛盾的消息。
        """
        if self.metric == METRIC_CUDA_UTIL:
            if cuda_util_pct is None:
                # 拿不到利用率就**不能触发**，也不能当成 0。
                # 上层的 cuda_tagged / note 会明确写出这条通路不可用，
                # 免得变成"静默不报警"。
                return None
            return cuda_util_pct if cuda_util_pct > self.cuda_util_percent else None

        if self.min_memory_mb > 0 and used_mb < self.min_memory_mb:
            return None
        if total_mb <= 0:
            return None
        percent = used_mb / total_mb * 100.0
        if self.memory_percent is not None and percent <= self.memory_percent:
            return None
        return percent

    def cuda_eligible(self, cuda_active: Optional[bool]) -> bool:
        """CUDA 筛选：这个进程算不算"在跑 CUDA"。

        `cuda_active is None` 表示本机拿不到引擎计数器 —— 这时**放行**。
        宁可多报也不能静默漏报，这是这个项目栽过的跟头。
        """
        if self.metric == METRIC_CUDA_UTIL:
            return True
        if not self.cuda_only:
            return True
        return cuda_active is not False

    def summary(self) -> str:
        if self.metric == METRIC_CUDA_UTIL:
            return (
                f"单进程 CUDA 引擎利用率 > {self.cuda_util_percent:g}% 就提醒"
                "；同一进程只提醒一次"
            )
        parts: List[str] = []
        if self.memory_percent is not None:
            parts.append(f"显存占整卡 > {self.memory_percent:g}%")
        if self.min_memory_mb > 0:
            parts.append(f"显存 >= {self.min_memory_mb:g} MiB")
        entry = " 且 ".join(parts) if parts else "（未配置触发条件）"
        scope = "仅 CUDA 进程" if self.cuda_only else "含图形进程"
        return f"单进程 {entry} 就提醒（{scope}）；同一进程只提醒一次"


@dataclass
class Runtime:
    """日志与运行状态文件的位置。"""

    log_file: str = DEFAULT_LOG_FILE
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 3
    #: 去重记忆（已提醒过的进程）落盘位置，供重启后接着算。
    #: 留空字符串 = 不落盘，退化成"重启即忘"的老行为（排障时可以用）。
    state_file: str = DEFAULT_STATE_FILE

    @property
    def log_dir(self) -> Path:
        return Path(self.log_file).parent


@dataclass
class WeChat:
    """微信测试号（公众号测试号）推送参数。"""

    app_id: str = ""
    app_secret: str = ""
    template_id: str = ""
    to_users: List[str] = field(default_factory=list)
    #: 形如 "http://127.0.0.1:7890"，留空则走系统默认（含系统代理环境变量）。
    proxy: str = ""
    timeout_sec: float = 8.0
    #: 模板里进程行的槽位数，必须与后台模板的 {{p1.DATA}} … {{pN.DATA}} 个数一致。
    #: 改模板就得改这里，否则对应行会显示成空白（--selftest 会报字段不匹配）。
    process_slots: int = 5

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret and self.template_id and self.to_users)

    def missing_fields(self) -> List[str]:
        missing = []
        if not self.app_id:
            missing.append("app_id")
        if not self.app_secret:
            missing.append("app_secret")
        if not self.template_id:
            missing.append("template_id")
        if not self.to_users:
            missing.append("to_users")
        return missing


@dataclass
class Config:
    thresholds: Thresholds = field(default_factory=Thresholds)
    runtime: Runtime = field(default_factory=Runtime)
    wechat: WeChat = field(default_factory=WeChat)
    #: 轮询间隔（秒）。
    poll_interval_sec: float = 5.0
    #: 两次推送之间的最小间隔（秒）。告警集合变化太频繁时先压住，避免刷屏。
    #: 注意：集合没变化本来就不会推，这一项只用来兜住"频繁变动"这种极端情况。
    min_push_interval_sec: float = 30.0
    #: 告警全部解除时是否推一条恢复通知。想少收消息就设 false。
    notify_on_clear: bool = True
    #: 只监控这些 GPU 序号；None 表示全部。
    gpus: Optional[List[int]] = None
    #: nvidia-smi 可执行文件路径；留空则自动探测。
    nvidia_smi: str = ""
    #: 连续失败达到该次数后，发一条"监控自己出问题了"的告警。
    self_alert_after_failures: int = 3
    #: 自愈告警的冷却时间（秒），避免故障时刷屏。
    self_alert_cooldown_sec: float = 1800.0
    #: 采集超时（秒）。
    command_timeout_sec: float = 15.0
    #: 进程调度优先级：idle / below / normal / above / high / realtime。
    #: 监控进程几乎不吃 CPU，设 high 只是保证机器满载时它仍能按时轮询。
    #: realtime 可能饿死系统（连鼠标键盘都会卡），非必要别用。
    process_priority: str = "high"

    def describe(self) -> List[str]:
        gpu_scope = "全部 GPU" if not self.gpus else "GPU " + ", ".join(str(i) for i in self.gpus)
        # 接收人数也放进启动横幅：改完 to_users 重启后，从日志就能确认新配置生效了。
        # 否则"到底发给了几个人"只能靠翻 config.json，很容易出现"以为改好了其实没生效"。
        count = len(self.wechat.to_users)
        recipients = f"接收人：{count} 人" if count else "接收人：**未配置**（消息发不出去）"
        return [
            f"监控范围：{gpu_scope}",
            f"告警条件：{self.thresholds.summary()}",
            f"轮询间隔：{self.poll_interval_sec:g}s；两次推送最小间隔：{self.min_push_interval_sec:g}s",
            f"日志文件：{self.runtime.log_file}",
            # 摆到横幅里，是为了让"重启后到底还记不记得已经推过谁"一眼可见 ——
            # 否则只能靠猜，而这正是之前重复推送 3 次却没被发现的原因。
            f"去重记忆：{self.runtime.state_file or '**不落盘**（重启即忘，进程会被重复提醒）'}",
            recipients,
        ]


def _section(raw: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"配置节 {key} 必须是一个对象，当前值：{value!r}")
    return value


def _parse_users(value: Any) -> List[str]:
    """支持 ["a", "b"] 和 "a, b" 两种写法。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ConfigError(f"wechat.to_users 必须是数组或逗号分隔的字符串，当前值：{value!r}")


def _parse_gpus(value: Any) -> Optional[List[int]]:
    if value is None or value == [] or value == "":
        return None
    if isinstance(value, (int, float)):
        return [int(value)]
    if isinstance(value, (list, tuple)):
        return [int(item) for item in value]
    if isinstance(value, str):
        return [int(item.strip()) for item in value.split(",") if item.strip()]
    raise ConfigError(f"gpus 必须是数组、数字或逗号分隔的字符串，当前值：{value!r}")


def load(path: str | os.PathLike[str]) -> Config:
    """从 JSON 文件加载配置。"""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(
            f"找不到配置文件：{config_path}\n"
            f"请先复制 config.example.json 为 config.json 并填写微信参数。"
        )

    try:
        # 必须用 utf-8-sig：Windows PowerShell 5.1 的 `Set-Content -Encoding UTF8`
        # 写出来的是「UTF-8 带 BOM」，而 json 模块碰到 BOM 会直接抛
        # "Unexpected UTF-8 BOM"。utf-8-sig 对「有 BOM」和「没 BOM」都能读。
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{config_path}\n{exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 UTF-8 文本：{config_path}\n{exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件根节点必须是对象：{config_path}")

    th_raw = _section(raw, "thresholds")
    rt_raw = _section(raw, "runtime")
    wx_raw = _section(raw, "wechat")

    thresholds = _parse_thresholds(th_raw)

    runtime = Runtime(
        log_file=str(rt_raw.get("log_file") or DEFAULT_LOG_FILE),
        log_max_bytes=int(_positive(rt_raw.get("log_max_bytes"), "runtime.log_max_bytes", 5 * 1024 * 1024)),
        log_backup_count=int(_num(rt_raw.get("log_backup_count"), "runtime.log_backup_count", 3)),
        # 注意区分「键不存在」和「键是 ""」：
        # 不写 = 用默认路径（老配置照常工作）；显式写 "" = 明确关掉落盘。
        state_file=DEFAULT_STATE_FILE if rt_raw.get("state_file") is None
        else str(rt_raw.get("state_file") or "").strip(),
    )

    wechat = WeChat(
        app_id=str(wx_raw.get("app_id") or "").strip(),
        app_secret=str(wx_raw.get("app_secret") or "").strip(),
        template_id=str(wx_raw.get("template_id") or "").strip(),
        to_users=_parse_users(wx_raw.get("to_users")),
        proxy=str(wx_raw.get("proxy") or "").strip(),
        timeout_sec=_positive(wx_raw.get("timeout_sec"), "wechat.timeout_sec", 8.0),
        process_slots=int(_positive(wx_raw.get("process_slots"), "wechat.process_slots", 5)),
    )

    config = Config(
        thresholds=thresholds,
        runtime=runtime,
        wechat=wechat,
        poll_interval_sec=_positive(raw.get("poll_interval_sec"), "poll_interval_sec", 5.0),
        min_push_interval_sec=_num(raw.get("min_push_interval_sec"), "min_push_interval_sec", 30.0),
        notify_on_clear=_bool(raw.get("notify_on_clear"), "notify_on_clear", True),
        gpus=_parse_gpus(raw.get("gpus")),
        nvidia_smi=str(raw.get("nvidia_smi") or "").strip(),
        self_alert_after_failures=int(_num(raw.get("self_alert_after_failures"), "self_alert_after_failures", 3)),
        self_alert_cooldown_sec=_positive(raw.get("self_alert_cooldown_sec"), "self_alert_cooldown_sec", 1800.0),
        command_timeout_sec=_positive(raw.get("command_timeout_sec"), "command_timeout_sec", 15.0),
        process_priority=_priority(raw.get("process_priority")),
    )

    _resolve_relative_paths(config, config_path)
    _apply_env_overrides(config)
    return config


def _resolve_relative_paths(config: Config, config_path: Path) -> None:
    """相对路径一律相对"配置文件所在目录"解析，而不是当前工作目录。

    这样无论任务计划程序把进程的 CWD 设成什么，行为都一致。
    """
    base = config_path.resolve().parent

    if config.nvidia_smi and not Path(config.nvidia_smi).is_absolute():
        config.nvidia_smi = str((base / config.nvidia_smi).resolve())

    if config.runtime.log_file and not Path(config.runtime.log_file).is_absolute():
        config.runtime.log_file = str((base / config.runtime.log_file).resolve())

    if config.runtime.state_file and not Path(config.runtime.state_file).is_absolute():
        config.runtime.state_file = str((base / config.runtime.state_file).resolve())


def _apply_env_overrides(config: Config) -> None:
    """环境变量优先，便于把密钥排除在配置文件之外。"""
    for env_name, attr in ((ENV_APP_ID, "app_id"), (ENV_APP_SECRET, "app_secret"), (ENV_TEMPLATE_ID, "template_id")):
        value = os.environ.get(env_name)
        if value and value.strip():
            setattr(config.wechat, attr, value.strip())


def resolve_default_path() -> Path:
    """找不到 --config 时的默认位置：项目根目录下的 config.json。"""
    # 本文件位于 <root>/src/cuda_monitor/config.py，向上三级即项目根。
    return Path(__file__).resolve().parents[2] / "config.json"
