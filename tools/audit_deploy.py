"""目标机部署达标审查：逐条核对监控服务是否满足全部需求。

**纯只读**，不改任何配置、不重启任何任务，可以随时跑。

用法（在目标机上，脚本放用户目录即可，纯只读）：

    D:\\Anaconda\\python.exe C:\\Users\\AIAgent\\audit_deploy.py

它核对六组需求，每项打「通过 / 不达标」，末尾给汇总：

    1. 静默运行  —— pythonw.exe、任务 Hidden、进程在 session 0
    2. 开机自启  —— BootTrigger + StartWhenAvailable
    3. 最高权限  —— SYSTEM / HighestAvailable
    4. 杀不掉+自愈 —— 进程属主、RestartOnFailure、看门狗任务与重复周期
    5. 权限收敛  —— 目录 ACL、config.json ACL
    6. 监控功能  —— 性能计数器兜底、CUDA 标记、生效口径、推送链路

写这个脚本的动机：**光看配置与日志证明不了"服务真的活着"**。
看门狗可能在 10 分钟前就停摆了、却因为主任务一直在跑而不留任何痕迹。
所以第 4 组的正确验法是配合一次真刀真枪的演练：

    schtasks /End /TN CUDA_Monitor        # 模拟被强杀
    # 等一个巡检周期（≤ 3 分钟），然后跑本脚本，看 Python 进程 PID 是否变了、
    # monitor.log 有没有新的「监控服务已启动」横幅。

读任务定义走 `C:\\Windows\\System32\\Tasks\\<name>`（就是标准 XML），
比解析 `schtasks /XML` 的输出干净，也能避开本地化字段名。
"""

import datetime
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

CREATE_NO_WINDOW = 0x08000000
INSTALL = r"C:\ProgramData\CudaMonitor"
MAIN_TASK = "CUDA_Monitor"
WATCH_TASK = "CUDA_Monitor_Watchdog"

PASS: list[str] = []
FAIL: list[str] = []
NOTE: list[str] = []


def decode(raw: bytes) -> str:
    """cmd 的输出是 UTF-16LE（带 BOM）或 OEM 代码页，两种都要认。"""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def run(args) -> str:
    proc = subprocess.run(args, capture_output=True, creationflags=CREATE_NO_WINDOW)
    return decode(proc.stdout or b"") or decode(proc.stderr or b"")


def log(text: str = "") -> None:
    print(text)


def check(label: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(label)
    mark = "  [通过]" if ok else "  [不达标]"
    log(f"{mark} {label}" + (f"   → {detail}" if detail else ""))
    return ok


def strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def task_xml(name: str) -> ET.Element:
    text = run(["schtasks", "/Query", "/TN", name, "/XML"])
    start = text.find("<?xml")
    if start < 0:
        start = text.find("<Task")
    if start < 0:
        raise RuntimeError(f"拿不到 {name} 的任务定义：{text[:200]}")
    return ET.fromstring(text[start:])


def find_all(root: ET.Element, tag: str) -> list[ET.Element]:
    return [e for e in root.iter() if strip_ns(e.tag) == tag]


def first_text(root: ET.Element, tag: str) -> str:
    nodes = find_all(root, tag)
    return (nodes[0].text or "").strip() if nodes else ""


def ns_attr(root: ET.Element, tag: str, attr: str) -> str:
    for element in find_all(root, tag):
        for key, value in element.attrib.items():
            if strip_ns(key) == attr:
                return value
    return ""


# =====================================================================
log("=" * 70)
log("需求达标审查：CUDA 占用监控 @ DESKTOP-FCSLQIH（只读，未改动任何配置）")
log("=" * 70)

# ---------------------------------------------------------------- 需求一
log()
log("【需求 1】静默运行 —— 不弹窗、不闪黑框、任务列表里不显眼")

try:
    xml = task_xml(MAIN_TASK)
    exe = first_text(xml, "Command")
    args = first_text(xml, "Arguments")
    workdir = first_text(xml, "WorkingDirectory")
    check("任务启动的是 pythonw.exe（无控制台解释器）",
          os.path.basename(exe).lower() == "pythonw.exe", exe)
    check("命令行指向 run_monitor.py --config config.json",
          "run_monitor.py" in args and "--config" in args, args)
    check("工作目录 = 安装目录", workdir.rstrip("\\") == INSTALL, workdir)
    hidden = first_text(xml, "Hidden")
    check("任务设为隐藏（计划任务库里默认不列出）",
          hidden.lower() == "true", f"Hidden={hidden}")
    idle = first_text(xml, "StopOnIdleEnd")
    check("空闲时不停止（DontStopOnIdleEnd）", idle.lower() == "false", f"StopOnIdleEnd={idle}")
except Exception as exc:  # noqa: BLE001
    check("读取主任务定义", False, str(exc))
    xml = None

# 进程侧证据：session 0 (Services) 的进程在 Windows 上无法显示任何窗口
# tasklist CSV 列序：映像名称,PID,会话名,会话#,内存使用
rows = [line for line in run(
    ["tasklist", "/FI", "IMAGENAME eq pythonw.exe", "/FO", "CSV", "/NH"]
).splitlines() if line.strip()]
if rows and "INFO" not in rows[0].upper():
    cells = [c.strip('"') for c in rows[0].split('","')]
    check("监控进程在 session 0 / Services —— 该会话无法显示窗口，也不可能闪黑框",
          len(cells) >= 4 and cells[2].lower() == "services" and cells[3] == "0",
          f"会话名={cells[2] if len(cells) > 2 else '?'}, 会话#={cells[3] if len(cells) > 3 else '?'}")
    check("监控进程恰好 1 个（没有重复实例）", len(rows) == 1, f"共 {len(rows)} 个 pythonw")
else:
    check("监控进程在 session 0 / Services", False, "当前没有 pythonw 进程在跑")

# 属主：tasklist /FO CSV /V 会多出一列「用户名」
vrows = [line for line in run(
    ["tasklist", "/FI", "IMAGENAME eq pythonw.exe", "/FO", "CSV", "/V", "/NH"]
).splitlines() if line.strip()]
owner = "?"
if vrows and "INFO" not in vrows[0].upper():
    cells = [c.strip('"') for c in vrows[0].split('","')]
    for cell in reversed(cells):
        if "\\" in cell or cell.upper().startswith("SYSTEM"):
            owner = cell
            break
check("进程属主是 SYSTEM —— 普通用户（含不带提权的会话）结束不了它",
      owner.upper().endswith("SYSTEM"), f"属主={owner}")

# ---------------------------------------------------------------- 需求二
log()
log("【需求 2】开机自启 —— 重启后自动跑起来，不依赖用户登录")

if xml is not None:
    boots = find_all(xml, "BootTrigger")
    check("触发器含「系统启动时」（BootTrigger）", len(boots) >= 1,
          f"找到 {len(boots)} 个启动触发器")
    swa = first_text(xml, "StartWhenAvailable")
    check("StartWhenAvailable —— 开机那刻错过了也会补跑", swa.lower() == "true", swa)
else:
    check("读取启动触发器", False, "任务定义不可读")

# ---------------------------------------------------------------- 需求三
log()
log("【需求 3】最高权限 —— 与任务计划程序里的「使用最高权限运行」等价")

if xml is not None:
    user_id = first_text(xml, "UserId")
    runlevel = first_text(xml, "RunLevel")
    # 注意：schtasks 写出来的是 SID 而不是名字 —— S-1-5-18 就是 NT AUTHORITY\SYSTEM。
    check("以 SYSTEM 身份运行（无需保存密码）",
          user_id.upper().endswith("SYSTEM") or user_id == "S-1-5-18", user_id)
    logon = first_text(xml, "LogonType")
    # 主任务用 SYSTEM 时这一项通常整个省略（缺省即按服务账号登录），省略不算问题。
    check("登录方式未指定或为服务账号（SYSTEM 的缺省行为）",
          logon in ("", "ServiceAccount", "InteractiveToken"), f"LogonType={logon or '(未指定)'}")
    check("RunLevel = HighestAvailable（最高权限）",
          runlevel == "HighestAvailable", runlevel)
else:
    check("读取主体信息", False, "任务定义不可读")

# ---------------------------------------------------------------- 需求四
log()
log("【需求 4】杀不掉 / 杀了会自动回来")

if xml is not None:
    count = first_text(xml, "Count")
    # ★ 只认 RestartOnFailure 里的 Interval。Settings 里还有个 IdleSettings.Duration
    #   （空闲检测时长，与重启无关），直接 find_all("Interval") 会把两者混在一起 ——
    #   我第一版就是这么误报的。
    restart = find_all(xml, "RestartOnFailure")
    r_interval = ""
    if restart:
        for child in restart[0]:
            if strip_ns(child.tag) == "Interval":
                r_interval = (child.text or "").strip()
    limit = first_text(xml, "ExecutionTimeLimit")
    multi = first_text(xml, "MultipleInstancesPolicy")
    check("任务失败自动重启（RestartCount 999 / 1 分钟）",
          count == "999" and r_interval == "PT1M", f"Count={count} Interval={r_interval}")
    check("无执行时长上限（ExecutionTimeLimit = PT0S，不会被计划任务掐掉）",
          limit == "PT0S", limit)
    check("MultipleInstances = IgnoreNew（不会被重启策略拉起多个实例）",
          multi == "IgnoreNew", multi)

try:
    wxml = task_xml(WATCH_TASK)
    w_enabled_xml = first_text(wxml, "Enabled")
    wexe = first_text(wxml, "Command")
    # XML 里 <Enabled> 缺省（空）= 启用。再用 schtasks 的状态复核一次，避免误判。
    wstatus = run(["schtasks", "/Query", "/TN", WATCH_TASK, "/FO", "LIST"])
    w_state_ok = "已禁用" not in wstatus and "Disabled" not in wstatus
    next_run = ""
    for line in wstatus.splitlines():
        if "下次运行时间" in line:
            next_run = line.split(":", 1)[1].strip()
    check("看门狗任务存在且已启用",
          w_state_ok and w_enabled_xml.lower() != "false",
          f"Command={wexe}, 状态未禁用={w_state_ok}, 下次运行={next_run}")
    check("看门狗有排定在未来的下次运行时间（证明触发器还活着）",
          bool(next_run) and next_run.upper() not in ("N/A", ""), next_run)

    # ★ 重复周期必须只看 TimeTrigger/Repetition 里的那两个字段。
    rep_interval, rep_duration = "", ""
    for rep in find_all(wxml, "Repetition"):
        for child in rep:
            name = strip_ns(child.tag)
            if name == "Interval":
                rep_interval = (child.text or "").strip()
            elif name == "Duration":
                rep_duration = (child.text or "").strip()
    check("看门狗每 3 分钟巡检一次（Repetition.Interval = PT3M）",
          rep_interval == "PT3M", f"Interval={rep_interval}")
    check("看门狗无限重复（Repetition 里没有 Duration）—— "
          "有 Duration 会在到期后彻底停摆，这是最容易漏的一条",
          rep_duration == "", f"Duration={rep_duration or '(无，即无限)'}")
except Exception as exc:  # noqa: BLE001
    check("读取看门狗任务", False, str(exc))

# ★ 连续拉起检测。看门狗每次写"已重新启动"既可能是主任务真被杀了，也可能是
#   主程序启动即崩 —— 两种情况在 watchdog.log 里长得一模一样。
#   实测本机 2026-09-18 13:38–15:11 因 config.json 带 BOM 导致主程序启动即崩，
#   看门狗空拉了 1.5 小时，日志里只有一排"已重新启动"，从外部看毫无异常。
#   这里只看计数：末尾连续 N 次、且相邻间隔都在一个巡检周期左右，说明拉起来没活下来。
_watch_log = os.path.join(INSTALL, "watchdog.log")
streak = 0
try:
    with open(_watch_log, encoding="utf-8", errors="replace") as handle:
        _wlines = handle.read().splitlines()
    _prev = None
    for _line in reversed(_wlines):
        _m = re.match(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] 检测到主任务未运行", _line)
        if not _m:
            break
        _stamp = datetime.datetime.strptime(_m.group(1), "%Y-%m-%d %H:%M:%S")
        if _prev is not None and (_prev - _stamp).total_seconds() > 270:
            break
        _prev = _stamp
        streak += 1
except OSError as exc:
    log(f"  读取 watchdog.log 失败：{exc}")

check("看门狗没有在连续空拉（无「主程序启动即崩」迹象）", streak < 3,
      f"末尾连续拉起 {streak} 次"
      + ("，链路已断开即中间稳定运行过" if streak < 3 else "，请查 monitor.log 与 config.json"))

# ---------------------------------------------------------------- 需求五
log()
log("【需求 5】权限收敛 —— 普通用户改不了、删不掉，凭据不外泄")

dir_acl = run(["icacls", INSTALL])
log("  ── 安装目录 ACL ──")
for line in dir_acl.splitlines():
    if line.strip():
        log("     " + line.strip())

# icacls 的 (I) 标记表示"继承来的"。断继承之后不应再出现；
# 而 Users 那一项必须是只读执行 (RX)，不能出现 (W)/(M)/(F)。
def acl_of(acl_text: str, principal: str) -> str:
    for line in acl_text.splitlines():
        if principal in line:
            match = re.search(r":((?:\([A-Z]+\))+)", line.replace(" ", ""))
            if match:
                return match.group(1)
    return ""


users_on_dir = acl_of(dir_acl, "Users")
check("安装目录已断开继承（DACL 里没有 (I) 标记）", "(I)" not in dir_acl,
      "见上方 ACL")
check("普通用户对安装目录只有读取+执行，没有写/删权限",
      users_on_dir in ("(OI)(CI)(RX)", "(RX)") and not re.search(r"\([WMF]", users_on_dir),
      f"Users={users_on_dir}")

cfg = os.path.join(INSTALL, "config.json")
cfg_acl = run(["icacls", cfg])
log("  ── config.json ACL ──")
for line in cfg_acl.splitlines():
    if line.strip():
        log("     " + line.strip())
check("config.json 对普通用户完全无权限（凭据不外泄）",
      "Users" not in cfg_acl and "Everyone" not in cfg_acl, "见上方 ACL")

# ---------------------------------------------------------------- 需求六
log()
log("【需求 6】监控功能本体（本次改造的核心）")

log_path = os.path.join(INSTALL, "monitor.log")
text = ""
try:
    with open(log_path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
except OSError as exc:
    log(f"  读取 monitor.log 失败：{exc}")

check("采集走性能计数器兜底（WDDM 下 nvidia-smi 逐进程是 [N/A]）",
      "进程占用改走性能计数器" in text, "日志里有该行")
check("CUDA 活动标记可识别（引擎计数器可用）",
      "CUDA 引擎利用率" in text, "启动横幅已打印新口径")

last_banner = ""
for line in text.splitlines():
    if "告警条件：" in line:
        last_banner = line.split("] ", 1)[-1]
check("生效口径 = CUDA 引擎利用率 > 15%，同一进程只提醒一次",
      "CUDA 引擎利用率 > 15%" in last_banner and "只提醒一次" in last_banner,
      last_banner)

pushes = re.findall(r"\[([\d\- :]+)\] \[INFO\] 推送成功", text)
check("微信推送链路通（有成功送达的记录）", len(pushes) >= 1,
      f"共 {len(pushes)} 条，最后一条 {pushes[-1] if pushes else '-'}")

watch_log = os.path.join(INSTALL, "watchdog.log")
if os.path.isfile(watch_log):
    with open(watch_log, encoding="utf-8", errors="replace") as handle:
        wtext = handle.read().splitlines()
    log("  ── watchdog.log 尾部 ──")
    for line in wtext[-6:]:
        log("     " + line)
    check("看门狗确实在跑（日志有记录）", bool(wtext), f"{len(wtext)} 行")
else:
    check("看门狗确实在跑", False, "watchdog.log 不存在")

# ---------------------------------------------------------------- 汇总
log()
log("=" * 70)
log(f"汇总：{len(PASS)} 项通过 / {len(FAIL)} 项不达标")
if FAIL:
    log("不达标项：")
    for item in FAIL:
        log(f"  - {item}")
else:
    log("全部达标。")
log("=" * 70)
