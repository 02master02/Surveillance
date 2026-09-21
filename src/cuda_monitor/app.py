"""编排层：日志、生命周期、主循环。

主循环的所有异常都在这一层被吸收。
监控程序最忌"自己挂了却没人知道" —— 所以除了记录日志，
连续失败达到阈值时还会反向推送一条"我出问题了"的告警。
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import __version__
from .collector import SOURCE_PDH, CollectorError, collect, find_nvidia_smi
from .config import Config, ConfigError, resolve_default_path
from .config import load as load_config
from .judge import BatchJudge, Decision, format_snapshot
from .notifier import (
    PROCESS_SLOTS,
    WeChatNotifier,
    build_alert_payload,
    build_cleared_payload,
    expected_fields,
)

LOGGER_NAME = "cuda_monitor"
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

#: 进程调度优先级类，取值与 WinBase.h 一致。
_PRIORITY_CLASSES: Dict[str, int] = {
    "idle": 0x00000040,      # IDLE_PRIORITY_CLASS
    "below": 0x00004000,     # BELOW_NORMAL_PRIORITY_CLASS
    "normal": 0x00000020,    # NORMAL_PRIORITY_CLASS
    "above": 0x00008000,     # ABOVE_NORMAL_PRIORITY_CLASS
    "high": 0x00000080,      # HIGH_PRIORITY_CLASS
    "realtime": 0x00000100,  # REALTIME_PRIORITY_CLASS
}

#: 持有互斥体句柄，进程存活期间不能释放。
_MUTEX_HANDLES: List[int] = []


# ---------------------------------------------------------------- 基础工具


def echo(message: str) -> None:
    """向控制台输出。pythonw 下 sys.stdout 为 None，需静默跳过。"""
    if sys.stdout is None:
        return
    try:
        print(message, flush=True)
    except (OSError, ValueError):
        pass


def _bootstrap_log_dirs() -> List[Path]:
    """bootstrap 阶段错误落盘的候选目录，按优先级排列。

    以 SYSTEM 身份跑计划任务时 %TEMP% 指向
    C:\\Windows\\system32\\config\\systemprofile\\AppData\\Local\\Temp，
    这个目录在不少机器上根本不存在 —— 偏偏它又是最需要留下线索的场景
    （服务起不来、日志系统还没就绪），所以在 pythonw 下连 echo 都失效的情况下，
    必须往多个位置都写一份，才有机会被找到。
    """
    candidates: List[Path] = []
    for key in ("TEMP", "TMP"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value))
    # 项目根目录：部署后即安装目录，部署脚本已授予 SYSTEM 完全控制。
    try:
        candidates.append(Path(__file__).resolve().parents[2])
    except (IndexError, OSError):
        pass
    for key in ("PROGRAMDATA", "PUBLIC"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value))
    candidates.append(Path.cwd())

    unique: List[Path] = []
    for item in candidates:
        if item not in unique:
            unique.append(item)
    return unique


def bootstrap_error(message: str) -> None:
    """日志系统就绪之前的错误出口：stderr + 多位置落盘。

    pythonw 下 sys.stdout / sys.stderr 均为 None，echo 与 stderr 都是哑的，
    落盘是唯一可靠的诊断通道，因此逐个候选目录尝试写入。
    """
    if sys.stderr is not None:
        try:
            sys.stderr.write(message + "\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    written: List[str] = []
    for directory in _bootstrap_log_dirs():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / "cuda_monitor_bootstrap_error.log"
            with target.open("a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {message}\n")
        except OSError:
            continue
        written.append(str(target))

    for path in written:
        echo(f"详细错误已写入：{path}")


def setup_logging(config: Config, verbose: bool = False) -> logging.Logger:
    """文件日志始终开启（带轮转）；有控制台时额外输出一份。"""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    attached = 0

    log_path = Path(config.runtime.log_file)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=config.runtime.log_max_bytes,
            backupCount=config.runtime.log_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        attached += 1
    except OSError as exc:
        bootstrap_error(
            f"无法写入日志文件 {log_path}：{exc}\n"
            f"常见原因：目录 ACL 已收紧但当前进程没有足够权限。"
            f"请以管理员身份运行，或在部署脚本中确认任务账号为 SYSTEM。"
        )

    if sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
        attached += 1

    if attached == 0:
        raise OSError("没有任何可用的日志输出目标，进程无法记录运行状态。")

    return logger


def acquire_single_instance(name: str = "CudaMonitorSingleton") -> Optional[bool]:
    """命名互斥体防止重复运行。

    返回 True 表示取得所有权；False 表示已有实例在运行；
    None 表示当前平台不支持该机制（非 Windows）。
    """
    if os.name != "nt":
        return None

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    error_already_exists = 183

    for namespace in ("Global", "Local"):
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, f"{namespace}\\{name}")
        last_error = ctypes.get_last_error()
        if not handle:
            continue
        if last_error == error_already_exists:
            kernel32.CloseHandle(handle)
            return False
        _MUTEX_HANDLES.append(handle)
        return True
    return None


def apply_process_priority(name: str, logger: logging.Logger) -> None:
    """把当前进程的调度优先级调高。

    用 ctypes 直接调 SetPriorityClass，不引 psutil —— 这个项目坚持零第三方依赖。
    失败不影响主流程：优先级只是锦上添花，不是能跑起来的必要条件。
    """
    if os.name != "nt":
        return

    mask = _PRIORITY_CLASSES.get(name.lower())
    if mask is None:
        logger.warning("未知的进程优先级 %r，已跳过。", name)
        return

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        handle = kernel32.GetCurrentProcess()
        if not kernel32.SetPriorityClass(ctypes.c_void_p(handle), mask):
            logger.warning("设置进程优先级失败，WinError=%s", ctypes.get_last_error())
            return
        actual = kernel32.GetPriorityClass(ctypes.c_void_p(handle))
        logger.info("进程优先级已设为 %s（读回 0x%X）", name, actual)
    except OSError as exc:  # noqa: BLE001
        logger.warning("设置进程优先级异常：%s", exc)
        return

    if name.lower() == "realtime":
        logger.warning("realtime 会抢占一切，可能连鼠标键盘都卡，确认这是你要的。")


# ---------------------------------------------------------------- 运行模式


def run_preview(config: Config, logger: logging.Logger, notifier: WeChatNotifier) -> int:
    """把这一轮"会推送什么"原样打印出来，但绝不发送。"""
    smi = find_nvidia_smi(config.nvidia_smi)
    snapshot = collect(smi, config.command_timeout_sec, config.gpus)
    echo(format_snapshot(snapshot, config.thresholds))
    echo("")

    judge = BatchJudge(config.thresholds, config.min_push_interval_sec)
    decision = judge.evaluate(snapshot, time.time())
    if decision is None or not decision.current:
        echo("当前没有进程超过阈值，不会产生推送。")
        return 0

    payload = build_alert_payload(decision, config.wechat.process_slots)

    echo("=" * 62)
    echo("推送预览（以下内容不会真的发送）")
    echo("=" * 62)
    echo("")

    rendered = notifier.render_template(payload)
    if rendered:
        echo(rendered)
    else:
        echo("（拉不到后台模板定义，下面按字段列出）")
        for key, value in payload.items():
            echo(f"  {key}: {value}")
    return 0


def run_once(config: Config, logger: logging.Logger) -> int:
    """单次扫描并打印结果，用于目标机排障。不推送消息。"""
    smi = find_nvidia_smi(config.nvidia_smi)
    judge = BatchJudge(config.thresholds, config.min_push_interval_sec)
    snapshot = collect(smi, config.command_timeout_sec, config.gpus)
    report = format_snapshot(snapshot, config.thresholds)

    logger.info("单次扫描完成（进程占用来源 %s）", snapshot.source)
    echo(report)

    decision = judge.evaluate(snapshot, time.time())
    alerts = decision.current if decision else []
    if alerts:
        echo("")
        echo(f"当前有 {len(alerts)} 个进程超过阈值（这一批会合并成一条推送）：")
        for index, alert in enumerate(alerts, start=1):
            echo(f"  {index}. {alert.detail}")
            logger.warning("命中告警：%s", alert.detail)
    else:
        echo("")
        echo("没有进程超过阈值。")
    return 0


def dispatch(decision: Decision, config: Config, notifier: WeChatNotifier,
             logger: logging.Logger, dry_run: bool) -> None:
    """把判定结果落成一条推送（或者什么都不做）。"""
    if decision.cleared:
        # 走到这里说明没有新告警，只有"曾经提醒过的进程退出了"。
        # 注意不是"跌回阈值以下" —— 判定层只在进程退出时才清记忆，
        # 否则周期性负载会在每个峰谷各推一条（见 judge.py 的说明）。
        logger.info("已提醒的 %d 个进程已结束（持续 %s）",
                    len(decision.previous), _duration_text(decision.duration))
        if not config.notify_on_clear:
            logger.info("notify_on_clear=false，跳过结束通知。")
            return
        if dry_run:
            logger.info("dry-run 模式，跳过结束通知推送")
            return
        notifier.send_cleared(decision)
        return

    for alert in decision.current:
        logger.warning("告警：%s", alert.detail)

    if decision.is_first:
        logger.info("新告警，共 %d 个进程（已记入去重名单，之后不再重复提醒）。",
                    len(decision.current))
    else:
        if decision.added:
            logger.info("新增：%s", "、".join(item.one_line for item in decision.added))
        if decision.removed:
            logger.info("同时结束：%s", "、".join(item.one_line for item in decision.removed))

    if dry_run:
        logger.info("dry-run 模式，跳过微信推送")
        return

    notifier.send_alert_batch(decision)

    # 同一轮里既有新告警、又有进程结束：告警优先，结束通知补发一条。
    # 不吞掉它 —— 否则"进程结束了"这个信息就永久丢了。
    if decision.removed and config.notify_on_clear:
        logger.info("补发 %d 个进程的结束通知。", len(decision.removed))
        notifier.send_cleared(decision)


def _duration_text(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} 秒"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟"
    return f"{seconds / 3600:.1f} 小时"


def run_selftest(config: Config, logger: logging.Logger, notifier: WeChatNotifier) -> int:
    """部署前自检：环境、驱动、日志、微信链路。"""
    problems: List[str] = []

    echo("=" * 62)
    echo("CUDA 监控服务 自检")
    echo("=" * 62)

    echo("")
    echo("[1/5] 运行环境")
    echo(f"  Python {sys.version.split()[0]} / {sys.platform}")
    echo(f"  监控服务版本 {__version__}")
    echo(f"  进程优先级 {config.process_priority}")
    for line in config.describe():
        echo(f"  {line}")

    echo("")
    echo("[2/5] nvidia-smi")
    smi: Optional[str] = None
    try:
        smi = find_nvidia_smi(config.nvidia_smi)
        echo(f"  路径：{smi}")
    except CollectorError as exc:
        echo(f"  [失败] {exc}")
        problems.append("nvidia-smi 不可用")

    echo("")
    echo("[3/5] GPU 与进程采集")
    if smi:
        try:
            snapshot = collect(smi, config.command_timeout_sec, config.gpus)
            echo(format_snapshot(snapshot, config.thresholds))
            if snapshot.source == SOURCE_PDH:
                echo("")
                echo("  [正常] nvidia-smi 拿不到逐进程显存，已自动改走 Windows 性能计数器。")
            elif not snapshot.apps and snapshot.note:
                echo("")
                echo(f"  [注意] 没有拿到任何进程占用：{snapshot.note}")
            if not snapshot.cuda_tagged:
                echo("")
                echo(
                    "  [注意] 拿不到 CUDA 引擎活动标记，本轮不会按 CUDA 筛选进程。"
                    "若配置里开着 cuda_only，等于暂时退化成'所有进程都算候选'。"
                )
        except CollectorError as exc:
            echo(f"  [失败] {exc}")
            problems.append("GPU 采集失败")
    else:
        echo("  已跳过（nvidia-smi 不可用）")

    echo("")
    echo("[4/5] 日志与去重记忆写入")
    log_path = Path(config.runtime.log_file)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("")
        echo(f"  [正常] {log_path} 可写")
    except OSError as exc:
        echo(f"  [失败] {log_path} 不可写：{exc}")
        problems.append("日志目录不可写")

    # 去重记忆也要写盘。写不进去不会让程序崩，但重启后同一个进程会被重复提醒 ——
    # 这种"表面健康、行为退化"的故障必须在这里就查出来（典型诱因是 ACL 收紧）。
    state_file = config.runtime.state_file
    if not state_file:
        echo("  [注意] runtime.state_file 为空：去重记忆不落盘，重启后会重复提醒已提醒过的进程。")
    else:
        state_path = Path(state_file)
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            probe = state_path.with_name(state_path.name + ".selftest")
            probe.write_text("{}", encoding="utf-8")
            probe.unlink()
            # 建一个只读的判官走一遍真实读取路径：坏文件在这里就会打警告进日志。
            restored = BatchJudge(config.thresholds, 0.0, state_file).restored_count
            echo(f"  [正常] {state_path} 可写；当前记录 {restored} 条已提醒进程")
        except OSError as exc:
            echo(f"  [失败] {state_path} 不可写：{exc}")
            echo("         记忆写不进去时程序照常跑，但重启后同一进程会被重复提醒。")
            problems.append("去重记忆文件不可写")

    echo("")
    echo("[5/5] 微信推送链路")
    if config.wechat.missing_fields():
        echo(f"  [跳过] 配置缺失：{', '.join(config.wechat.missing_fields())}")
        echo("         填好 config.json（或设置对应环境变量）后重跑自检。")
        problems.append("微信参数未配置完整")
    else:
        token = notifier.access_token()
        if token:
            echo(f"  [正常] access_token 获取成功（{token[:8]}...）")
        else:
            echo("  [失败] access_token 获取失败，详见上方日志")
            problems.append("微信令牌获取失败")

        fields = notifier.list_template_fields()
        if fields:
            expected = set(expected_fields(config.wechat.process_slots))
            missing = expected - set(fields)
            extra = set(fields) - expected
            echo(f"  后台模板字段：{', '.join(fields)}")
            if missing:
                echo(f"  [警告] 模板里缺少字段：{', '.join(sorted(missing))}")
                echo("         模板消息字段名必须与代码发送的完全一致，请到测试号后台调整模板。")
                problems.append("模板字段不匹配")
            if extra:
                echo(f"  [提示] 模板里有代码不会填充的字段：{', '.join(sorted(extra))}")

        bare = notifier.bare_variable_lines()
        if bare:
            echo(f"  [警告] 模板里有 {len(bare)} 行是纯变量、没有字面文字：{'、'.join(bare)}")
            echo("         微信 2023 规范会把这类行整行删掉，导致消息正文全空。")
            echo("         请改成「字面文字 + {{字段.DATA}}」，例如：1. {{p1.DATA}}")
            problems.append("模板存在纯变量行")

        echo(f"  正在向 {len(config.wechat.to_users)} 个接收人发送测试消息 ...")
        if notifier.send_test():
            echo("  [正常] 测试消息已发出，请查看手机微信。")
        else:
            echo("  [失败] 测试消息发送失败，详见日志")
            problems.append("测试消息发送失败")

    echo("")
    echo("=" * 62)
    if problems:
        echo(f"自检发现 {len(problems)} 个问题：")
        for item in problems:
            echo(f"  - {item}")
        echo("请先解决问题，再注册开机自启任务。")
    else:
        echo("自检全部通过，可以执行 scripts\\deploy.ps1 注册开机自启。")
    echo("=" * 62)
    return 1 if problems else 0


def run_list_users(config: Config, notifier: WeChatNotifier) -> int:
    """打印关注了测试号的用户，用于拿到正确的 OpenID。"""
    echo("=" * 62)
    echo("测试号关注者列表")
    echo("=" * 62)
    echo("")

    if not (config.wechat.app_id and config.wechat.app_secret):
        echo("app_id / app_secret 未配置，无法查询。")
        return 1

    followers = notifier.list_followers()
    if not followers:
        echo("没有查到任何关注者。")
        echo("请先让接收人扫描测试号页面上的二维码关注，然后重试。")
        return 1

    configured = set(config.wechat.to_users)
    echo(f"共 {len(followers)} 人关注：")
    echo("")
    for index, item in enumerate(followers, start=1):
        mark = "   <- 已配置" if item["openid"] in configured else ""
        echo(f"  {index}. {item['openid']}   {item['nickname'] or '(未取到昵称)'}{mark}")
    echo("")
    echo("把这串 OpenID 原样复制进 config.json 的 wechat.to_users。")
    echo("不要手打或照着截图认字 —— g/q、0/O 这类字符肉眼极易看错。")
    return 0


def run_forever(config: Config, logger: logging.Logger, notifier: WeChatNotifier, dry_run: bool) -> int:
    """主循环。任何异常都不允许让进程退出。"""
    smi = find_nvidia_smi(config.nvidia_smi)

    for line in config.describe():
        logger.info(line)

    # 只有这条常驻路径才带 state_file —— 去重记忆落盘，重启后接着算。
    # `--once` / `--preview` 一律传 None（默认值），它们绝不能改写这份记忆：
    # 跑一次排障就把进程记成"已提醒"，真正该发的那条告警就被自己压掉了。
    judge = BatchJudge(
        config.thresholds, config.min_push_interval_sec, config.runtime.state_file
    )
    logger.info("监控服务已启动 v%s（nvidia-smi=%s，dry-run=%s）", __version__, smi, dry_run)

    consecutive_failures = 0
    last_self_alert = 0.0
    interval = config.poll_interval_sec
    last_source: Optional[str] = None

    while True:
        delay = interval
        try:
            snapshot = collect(smi, config.command_timeout_sec, config.gpus)
            if consecutive_failures:
                logger.info("采集已恢复正常（此前连续失败 %d 次）", consecutive_failures)
            consecutive_failures = 0

            # 数据来源变化只报一次。WDDM 机器上首次必然从 nvidia-smi 切到
            # 性能计数器，这条日志是排查"为什么不报警"的第一现场。
            if snapshot.source != last_source:
                if snapshot.source == SOURCE_PDH:
                    logger.warning(
                        "进程占用改走性能计数器（%s）：%s",
                        snapshot.source,
                        snapshot.note or "nvidia-smi 未提供可用的进程显存",
                    )
                elif last_source is not None:
                    logger.info("进程占用来源恢复为 %s。", snapshot.source)
                last_source = snapshot.source

            if not snapshot.apps and snapshot.note:
                logger.debug("本轮没有可用的进程占用数据：%s", snapshot.note)

            # 集合没变时 evaluate 返回 None，这里什么都不做 —— 这正是
            # "同一批进程不重复推送" 的实现位置。
            decision = judge.evaluate(snapshot, time.time())
            if decision is not None:
                dispatch(decision, config, notifier, logger, dry_run)
            else:
                logger.debug(
                    "本轮无变化（当前 %d 个进程处于告警态），不推送。",
                    judge.active_count,
                )
        except Exception as exc:  # noqa: BLE001 —— 这里必须兜住一切
            consecutive_failures += 1
            if isinstance(exc, CollectorError):
                logger.error("采集失败（连续第 %d 次）：%s", consecutive_failures, exc)
            else:
                logger.exception("未预期异常（连续第 %d 次）：%s", consecutive_failures, exc)

            # 指数退避，最长 5 分钟，避免故障时空转刷日志。
            delay = min(interval * (2 ** min(consecutive_failures, 6)), 300.0)

            if consecutive_failures >= config.self_alert_after_failures:
                now = time.time()
                if now - last_self_alert > config.self_alert_cooldown_sec:
                    last_self_alert = now
                    logger.error("连续失败 %d 次，发送自愈告警。", consecutive_failures)
                    if dry_run:
                        logger.info("dry-run 模式，跳过自愈告警推送")
                    else:
                        fields: Dict[str, str] = {"title": "监控自身异常"}
                        for index in range(1, PROCESS_SLOTS + 1):
                            fields[f"p{index}"] = "GPU 采集连续失败" if index == 1 else ""
                        fields["usage"] = f"失败{consecutive_failures}次"
                        notifier.send_template(fields)

        time.sleep(delay)


# ---------------------------------------------------------------- 入口


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cuda_monitor",
        description="监控 GPU 上超出显存阈值的进程，并通过微信推送告警。",
    )
    parser.add_argument("--config", default=None, help="配置文件路径，默认取项目根目录的 config.json")
    parser.add_argument("--once", action="store_true", help="只扫描一次并打印结果（不推送），用于排障")
    parser.add_argument("--preview", action="store_true", help="预览这一轮会推送的内容，但不发送")
    parser.add_argument("--selftest", action="store_true", help="部署前自检：驱动、日志、微信链路")
    parser.add_argument("--list-users", action="store_true", help="列出关注了测试号的用户，拿到正确的 OpenID")
    parser.add_argument("--dry-run", action="store_true", help="正常轮询但只记日志，不真正发送微信")
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 级日志")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    config_path = Path(args.config) if args.config else resolve_default_path()
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        bootstrap_error(str(exc))
        return 2

    try:
        logger = setup_logging(config, verbose=args.verbose)
    except OSError as exc:
        bootstrap_error(str(exc))
        return 2

    notifier = WeChatNotifier(config.wechat, logger)

    if args.selftest:
        return run_selftest(config, logger, notifier)

    if args.list_users:
        return run_list_users(config, notifier)

    if args.preview:
        try:
            return run_preview(config, logger, notifier)
        except CollectorError as exc:
            logger.error("预览失败：%s", exc)
            echo(f"预览失败：{exc}")
            return 1

    if args.once:
        try:
            return run_once(config, logger)
        except CollectorError as exc:
            logger.error("单次扫描失败：%s", exc)
            echo(f"单次扫描失败：{exc}")
            return 1

    acquired = acquire_single_instance()
    if acquired is False:
        logger.error("检测到已有实例在运行，本次启动退出。")
        return 3
    if acquired is None:
        logger.warning("当前平台不支持单实例互斥，跳过重复启动检查。")

    apply_process_priority(config.process_priority, logger)

    try:
        return run_forever(config, logger, notifier, dry_run=args.dry_run)
    except KeyboardInterrupt:
        logger.info("收到中断信号，监控服务退出。")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("监控服务异常终止：%s", exc)
        return 1
