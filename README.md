# CUDA 占用监控

在装有 NVIDIA 显卡的 Windows 机器上常驻运行，发现某个进程的 **CUDA 引擎利用率**
超过设定比例时，通过微信推送告警到手机。**同一个进程只提醒一次** ——
周期性的训练任务一次运行只推一条，不会每个峰谷刷一条。

**零第三方依赖**，只用 Python 标准库 —— 目标机不需要配 venv、不需要联网装包。

---

## 目录结构

```
CUDA/
├─ run_monitor.py              启动入口（任务计划调用它）
├─ config.example.json         配置模板（含字段说明）
├─ src/cuda_monitor/
│   ├─ config.py               配置加载 + 触发条件定义（阈值语义只在这一个地方实现）
│   ├─ collector.py            采集层：nvidia-smi + Windows 性能计数器（显存 / CUDA 活动）
│   ├─ judge.py                判定层：触发条件 + 按进程去重 + 节流
│   ├─ notifier.py             通知层：微信测试号模板消息
│   └─ app.py                  编排层：日志、主循环、自检、预览、单实例
├─ scripts/
│   ├─ deploy.ps1              目标机安装 + 注册开机自启 + 注册看门狗
│   ├─ uninstall.ps1           完整卸载（先删看门狗，再删主任务）
│   └─ watchdog.ps1            看门狗：主任务不在运行就把它拉起来
└─ tools/
    ├─ fake_nvidia_smi.py      假 nvidia-smi，供无显卡的开发机联调
    ├─ test_judge.py           判定层回归测试（无 GPU / 无网络）
    ├─ probe_template.py       按当前模板格式手工发一条测试推送
    ├─ probe_gpu_engine.py     只读探测：本机 GPU Engine 计数器有哪些实例
    ├─ probe_cuda_filter.py    只读验证：CUDA 筛选能不能区分训练进程与桌面进程
    ├─ config.dev.json         开发机配置（无凭据，可提交）
    └─ config.local.json       本机联调配置（含凭据，已 gitignore）
```

分层只做到「采集 / 判定 / 通知 / 编排」四个模块，
再细就是为分层而分层了。好处是换推送渠道不动采集代码，换数据源不动通知代码。

---

## 推送行为（先看这节）

**一代告警 = 一个进程集合，不是一条进程。** 一轮扫描最多产出一条推送，
里面列出全部新达标的进程。

去重口径是 **进程身份**（卡号 + PID + 进程名）：

| 情况 | 会不会推送 |
|---|---|
| 第一次发现某进程达标记 | 推，一条列出本轮全部新达标进程 |
| **同一进程继续达标、甚至变得更高** | **不推** |
| **同一进程回落到线下、之后又涨上来** | **仍然不推** —— 记忆只在进程退出时清除 |
| 有新进程达标记（哪怕同时有旧进程退出） | 推，`新增` 与 `同时结束` 都写进日志 |
| 已提醒过的进程退出了 | 推一条"已结束"（可用 `notify_on_clear: false` 关掉） |
| 集合变化太频繁（间隔小于 `min_push_interval_sec`） | 先压住，等窗口过了补发最新状态 |

**为什么"回落也不重推"是刻意的**：CUDA 利用率在真机上一轮能到 66.6%，
取数据 / 存 checkpoint 的间隙就掉到 0%，约 20 秒一个来回。
若按"跌破阈值就忘掉"来做，每个峰谷都会重新推一条 ——
一个 20 秒周期的任务几分钟就能把微信日限额刷光。
按"进程退出才忘"来做，周期性负载天然只推一次。

这也意味着**不再需要迟滞（退出线）**：迟滞是为了压住"集合反复进出"，
而"按进程去重"比它更强 —— 不是压住推送，而是根本不重复判定为新告警。
所以 `exit_memory_percent` / `exit_memory_mb` 已经从配置里删掉了。

**推送节流** 是唯一的另一道防线：`min_push_interval_sec` 内不发第二条。
节流时**不提交任何状态变更** —— 既不忘掉退出的进程，也不记下新达标的进程，
下一轮重算，窗口过了自然补发。所以"被压住"不等于"信息丢了"。

想先看会推什么但不想发，用 `--preview`（见下文）。

---

## 一、开发机联调（不需要显卡）

**这台机器没有可用的 NVIDIA 设备也能跑通全链路**，包括真实的微信推送。

```bash
# 0. 判定层回归测试：无 GPU、无网络，纯逻辑
python tools/test_judge.py

# 1. 看当前 GPU 与进程 + 本轮会推什么（不发送）
python run_monitor.py --config tools/config.local.json --preview

# 2. 部署前自检（会真的发一条测试消息到你的微信）
python run_monitor.py --config tools/config.local.json --selftest
```

`fake_nvidia_smi.py` 用环境变量切换场景：

| 场景 | 命令 | 说明 |
|---|---|---|
| `normal` | 默认 | 双卡三进程，CUDA 利用率 42% / 8% / 30% —— 两个达标，进程名带逗号 |
| `many` | `CUDA_MONITOR_FAKE_SCENARIO=many` | 双卡三进程**全部达标**，验证一条推送列全 |
| `evolve` | `CUDA_MONITOR_FAKE_SCENARIO=evolve` | **每 15 秒切换一次进程集合并改利用率**，演示"同一进程只提醒一次" |
| `wddm` | `CUDA_MONITOR_FAKE_SCENARIO=wddm` | **单张 48GB 卡，逐进程显存全是 `[N/A]`**，复刻真机 WDDM 行为，验证采集层切性能计数器兜底 |
| `empty` | `CUDA_MONITOR_FAKE_SCENARIO=empty` | 有卡但无计算进程 |
| `fail` | `CUDA_MONITOR_FAKE_SCENARIO=fail` | 模拟 NVML 初始化失败 |
| `single` | `CUDA_MONITOR_FAKE_SCENARIO=single` | 单卡单进程，最小化验证 |

> **假 smi 还额外实现了 `--query-cuda-activity`**（每行 `pid, cuda_util`），
> 用来给假进程补上 CUDA 引擎活动。没有它，开发机上 `metric: cuda_util`
> 一条都触发不了 —— 因为本机根本没有那些假 PID 的引擎计数器实例。
> 采集层检测到 `nvidia_smi` 指向 `.py` 就走这条通路，真机不受影响，
> 并且会在 `--once` 输出里写明 `CUDA 活动标记：已识别（来源：假 nvidia-smi（开发模式））`。

> `wddm` 场景是排查"为什么不报警"的第一现场。
> 用 `--once` 跑它，输出里的 **`进程占用来源：windows-pdh`** 就说明兜底生效了；
> 如果显示 `nvidia-smi` 且进程列表为空，说明兜底也没接上，去日志里找 `PdhUnavailable`。
> （注意：开发机上跑 `wddm` 拿到的是本机真实进程，这个场景在开发机只用来验证"N/A 行被识别"。）

跑 `evolve` 看推送节奏：

```bash
CUDA_MONITOR_FAKE_SCENARIO=evolve python run_monitor.py --config tools/config.dev.json --dry-run --verbose
```

三个阶段各有看点，实测日志长这样：

```
相位 0  A(41872) 60% + B(33512) 40%   → 推一条，两个进程
相位 1  A 回落到 5%，B 不变            → 静默（A 已提醒过，不再重推）★ 关键
相位 2  A 退出、C(51900) 25% 新达标    → 混合轮：推 C，同时补一条 A 的结束通知
```

> 真实凭据放 `tools/config.local.json`（已 gitignore），
> `tools/config.dev.json` 保持无凭据、可安全提交。
> 先在开发机用 `--selftest` 确认手机真能收到消息，再去真机部署，
> 这样能把「代码问题」和「微信配置问题」分开排查。
>
> `config.local.json` 的字段结构照 `config.dev.json` 抄即可，只是 `wechat` 段填真值。

---

## 二、目标机部署

### 部署前准备清单

在目标机上逐项确认，**前面没过就别往下走**。

这 9 项里的大多数可以用一个只读脚本一次性查完（不改任何配置）：

```powershell
# 管理员身份打开 PowerShell，进到项目根目录
powershell -ExecutionPolicy Bypass -File .\scripts\precheck.ps1
```

脚本输出一张 `OK / 注意 / 失败` 的表，没过的会直接给修法。
项目还没拷过来时，加 `-SkipProjectFiles` 先看机器环境。

| # | 项 | 怎么确认 / 注意 |
|---|---|---|
| 1 | **能提管理员权限** | 注册任务、收紧 ACL 都需要。开始菜单搜 PowerShell → 右键"以管理员身份运行" |
| 2 | **NVIDIA 驱动正常** | CMD 跑 `nvidia-smi`，要能看到显卡型号与进程列表。报 `Failed to initialize NVML` 就是驱动没装好 |
| 3 | **Python 3.9+，机器级安装** | `python --version`。**别用 Windows Store 版** —— 它装在 `WindowsApps` 下，SYSTEM 读不到。装到 `C:\Python3xx`、`Program Files` 或独立的 Anaconda 根目录（如 `D:\Anaconda`）都行 |
| 4 | **`pythonw.exe` 可用** | `where pythonw` 有输出。它在 SYSTEM 身份下也必须能执行，所以同样避开用户目录版 |
| 5 | **能访问微信接口** | `curl https://api.weixin.qq.com/cgi-bin/token` 有响应即可（哪怕是错误 JSON）。公司网络要放行 `api.weixin.qq.com:443`，或给 `wechat.proxy` 填代理 |
| 6 | **测试号四件套齐了** | appID / appsecret / template_id / 接收人 OpenID。OpenID 必须用 `--list-users` 从接口取，**别手抄或截图认字** |
| 7 | **接收人已关注测试号** | 没关注会报 `errcode 43004` |
| 8 | **系统时间准确** | 偏差过大会让 access_token 校验失败。`w32time` 服务建议保持运行 |
| 9 | **项目目录整体拷过来** | 含 `src/`、`run_monitor.py`、`scripts/`、`config.example.json`。`tools/` 是开发期用的（假 nvidia-smi），可以不拷。**`tools/config.local.json` 千万别拷** —— 里面有你的 appSecret |

> 顺序是：`precheck.ps1` 看环境 → `deploy.ps1` 装服务 → `--selftest` 验链路。
> 这三步分别回答"机器行不行""装没装上""微信通不通"，分开排查才不互相干扰。

### 先准备微信测试号

1. 打开 <https://mp.weixin.qq.com/debug/cgi-bin/sandbox?t=sandbox/login>，用微信扫码登录。
2. 页面上拿到 **appID** 和 **appsecret**。
3. 让需要收告警的人各自扫码关注测试号。
4. **不要从页面上手抄 OpenID。** 那个字符串里 `g/q`、`0/O`、`l/1` 肉眼极易看错，
   抄错一个字就会收到 `errcode 40003 invalid openid`。
   正确做法是先填好 `app_id` / `app_secret`，然后让程序去问微信要：

   ```bash
   python run_monitor.py --config tools/config.local.json --list-users
   ```

   输出里会直接列出真实 OpenID，已配置的那个还会标出来。复制粘贴，别手打。
5. 点 **新增测试模板**。**模板标题**随便填（比如 `CUDA 显存监控`），
   **模板内容**按下图填 —— 注意每行都有**字面前缀**，且第一行是占位：

   ```
   CUDA 显存监控
   标题：{{title.DATA}}
   1. {{p1.DATA}}
   2. {{p2.DATA}}
   3. {{p3.DATA}}
   4. {{p4.DATA}}
   5. {{p5.DATA}}
   统计：{{usage.DATA}}
   ```

   > ⚠️ **必须知道的微信 2023 模板消息规范**（以下均已实测确认，不是猜测）：
   >
   > | 现象 | 后果 |
   > |---|---|
   > | 模板内容的**第一行** | **必被删**。放占位文字当"牺牲行"，别放真数据 |
   > | **整行只有一个变量** | **整行消失**。只写 `{{p1.DATA}}` 会导致该行没了 |
   > | 字段值含 `\n` 换行 | **换行之后的内容不展示** → 不能靠换行列清单 |
   > | 单个字段值过长 | 建议 ≤17 字（20 字上限要扣掉 `1. ` 这类前缀） |
   >
   > 所以：**每行都要有字面文字 + 一个进程占一行（各自一个字段）**。
   > 网上大量老教程说"字段值支持 `\n`、可以拼多行清单"，**已经过时**。

   字段含义：

   | 字段 | 放什么 | 示例 |
   |---|---|---|
   | `title` | 标题（含进程数量） | `显存告警 3 个进程` |
   | `p1` … `p5` | **每个进程一行**，值自动裁到 17 字 | `python 41872 38%` |
   | `usage` | 变化 / 占用摘要 | `新增3个 最高50%` |

   进程行的条数必须与 `config.json` 里的 `wechat.process_slots` 一致（默认 5）。
   想少几行就两边一起改，`--selftest` 会校验并告诉你哪里对不上。

   改完用 `--preview` 看实际渲染效果，不满意再调。

6. 保存后复制生成的 **模板 ID**。

> 如果后台不能直接编辑已有模板，删掉重新新增即可 —— 但**模板 ID 会变**，
> 记得把新的填回 `config.json` 的 `wechat.template_id`。

> ⚠️ 模板字段名是最容易卡住的地方。代码发送 `title` / `p1`…`pN` / `usage`，
> 模板里的字段名必须**逐字一致**，否则接口返回 40037 / 47003。
> 自检模式会自动拉取模板定义并比对，**字段不匹配**和**纯变量行**都会明确报出来。

### 然后按顺序执行

```powershell
# 0. 用管理员身份打开 PowerShell，进入项目目录
cd <项目目录>

# 1. 安装（一条命令搞定复制、配置、注册任务、收紧权限）
.\scripts\deploy.ps1 `
    -AppId    "你的appID" `
    -AppSecret "你的appsecret" `
    -TemplateId "你的模板ID" `
    -ToUsers  "OpenID1","OpenID2"

# 2. 先自检。必须用 python.exe（有控制台），不要用 pythonw.exe
& 'C:\Python311\python.exe' 'C:\ProgramData\CudaMonitor\run_monitor.py' `
    --config 'C:\ProgramData\CudaMonitor\config.json' --selftest

# 3. 自检全绿后再启动任务
Start-ScheduledTask -TaskName CUDA_Monitor
```

**不要跳过第 2 步。** 自检通过意味着驱动、日志写入、微信链路三个环节都通了。

> 但**自检全绿 ≠ 一定会报警**。真机上踩到过"自检全绿、任务 Running、
> 日志没有任何错误，却一条告警都不发"，原因是三个互相独立的故障：
>
> 1. WDDM 下 nvidia-smi 的逐进程显存全是 `[N/A]`，
>    老实现静默丢弃这些行 → 判定层永远看到空集合；
> 2. 阈值按 15% 显存配在 48GB 卡上 = 7371 MiB，而项目峰值只有 2360 MiB，
>    本来就触发不了 —— 这是**指标选错**，不是阈值调错；
> 3. 只有"集合变化"才推送，而真实负载 20 秒一个来回，
>    集合每轮都在进出 → 推送节奏跟着负载抖。
>
> 三个现在都修了：切性能计数器兜底；默认指标改成 `cuda_util`；
> 去重改成按进程记忆。万一以后再遇到"不报警"，按这个顺序查：
>
> | 步骤 | 命令 / 位置 | 看什么 |
> |---|---|---|
> | 1 | `--once` | `进程占用来源：` 这行。是 `windows-pdh` 说明兜底生效；进程列表为空就是采集没拿到数据 |
> | 2 | `--once` | `CUDA 活动标记：` 这行。显示"不可用"时不会按 CUDA 筛选，等于筛选失效 |
> | 3 | `--once` 表格 | `CUDA%` 列是不是全 0。全 0 → 引擎计数器没读到（不要只看 `占用 MiB`） |
> | 4 | `--once` 表格 | 有没有 `·` 开头的行。有说明**进程被 CUDA 筛选挡掉了**，不是没检测到 |
> | 5 | `--once` 表格 | 有没有 `★` 开头的行。没有 → 触发线配高了，或者看错了指标（`metric` 是 `cuda_util` 还是 `cuda_memory`） |
> | 6 | `monitor.log` | 搜 `性能计数器`：会明确写出切兜底的原因 |
> | 7 | `--verbose` | `本轮无变化（当前 N 个进程处于告警态）` 说明采集与判定都正常，只是没到线 |

### 静默 / 自启 / 优先级 / 防误杀，各自怎么实现

| 要求 | 实现 | 说明 |
|---|---|---|
| 静默无窗口 | `pythonw.exe` + SYSTEM 身份 | 子进程调用再加 `CREATE_NO_WINDOW`，连黑框一闪都不会有 |
| 开机自启 | 计划任务 `-AtStartup` | 配 `-StartWhenAvailable`，开机那刻错过了也会补跑 |
| **以最高权限运行** | `-RunLevel Highest` + SYSTEM | 就是任务计划程序里的"使用最高权限运行" |
| **进程优先级高** | `config.json` 的 `process_priority` | 进程内调 `SetPriorityClass`，默认 `high`。`realtime` 会饿死系统（含输入），别用 |
| 任务列表里不显眼 | `-Hidden` | 计划任务库里默认不列出 |
| 普通用户杀不掉 | SYSTEM 身份 | 不带提权在任务管理器里结束它 → "拒绝访问" |
| **杀了会自动回来** | 看门狗任务 `CUDA_Monitor_Watchdog` | 每 3 分钟巡检一次，主任务不在就拉起。加 `-NoWatchdog` 可跳过 |

### 关于权限的三个事实（别抱不切实际的期待）

部署脚本会把 `C:\ProgramData\CudaMonitor` 的 ACL 收紧成：
SYSTEM 与管理员完全控制，普通用户只有**读取 + 执行**，**不含写入与删除**。
`config.json` 因为含接口凭据，连读取权限也收掉了。

但要说清楚：

- **ACL 挡得住普通用户误删误改，挡不住管理员** —— 管理员随时能夺取所有权后删掉。
- **SYSTEM 身份挡得住普通用户结束进程，同样挡不住管理员** —— 管理员能停任务、删任务。
- **"杀了也会回来"由看门狗任务负责**：每 3 分钟检查一次，主任务没跑就拉起。
  它同时看两件事 —— 计划任务状态、以及进程列表里是否真有带本安装目录的 `pythonw.exe`；
  因为被强杀时常出现"任务状态还挂在 Running、进程其实已经没了"的假象，只看状态会漏判。
- 看门狗**尊重 `Disabled` 状态**：用 `Disable-ScheduledTask` 停掉主任务是有效的，
  看门狗不会擅自启用它。这是留给你的正规停止开关。
  ⚠️ 反过来，**单独 `Stop-ScheduledTask` 会被看门狗在 3 分钟内拉回来**。
- 想更彻底（服务级抗删抗停），正规做法是**做成 Windows 服务**（需要 nssm / pywin32 包装），
  复杂度高一个量级，而且要引入第三方依赖 —— 与这个项目"零依赖"的取向冲突。

---

## 三、配置项说明

`config.json` 里最常改的几项：

| 键 | 默认 | 说明 |
|---|---|---|
| `thresholds.metric` | `"cuda_util"` | **触发指标**：`cuda_util` = CUDA 引擎利用率；`cuda_memory` = 该进程的显存占用。两个是完全不同的量，详见下一节 |
| `thresholds.cuda_util_percent` | `15` | `metric=cuda_util` 时的触发线：**严格大于**这个百分比才提醒。取值 `(0, 100]` |
| `thresholds.memory_percent` | `null` | `metric=cuda_memory` 时的相对线：单进程显存占该卡总量，严格大于才提醒。`null` = 不按百分比判定 |
| `thresholds.min_memory_mb` | `1000` | `metric=cuda_memory` 时的绝对线：占用低于此 MiB 数忽略，用来过滤桌面进程 |
| `thresholds.cuda_only` | `true` | `metric=cuda_memory` 时只统计在 **CUDA 引擎**上跑过的进程。`cuda_util` 下无意义（那个指标本身就是 CUDA 专属的） |
| `poll_interval_sec` | `5` | 轮询间隔 |
| `min_push_interval_sec` | `30` | 两次推送之间的最小间隔；`0` 表示不节流 |
| `notify_on_clear` | `true` | 已提醒过的进程退出时，是否推一条"已结束" |
| `gpus` | `null` | `null` = 全部卡；`[0]` = 只盯 0 号卡 |
| `nvidia_smi` | `""` | 留空则自动探测；也可指向 `fake_nvidia_smi.py` 做联调 |
| `process_priority` | `high` | 进程调度优先级：`idle` / `below` / `normal` / `above` / `high` / `realtime`。`realtime` 会饿死系统（连输入都卡），别用 |
| `self_alert_after_failures` | `3` | 连续采集失败几次后，反向告警"监控自己出问题了" |
| `self_alert_cooldown_sec` | `1800` | 自愈告警的冷却时间，避免故障时刷屏 |
| `wechat.process_slots` | `5` | 模板里 `p1`…`pN` 的个数，**必须与后台模板一致**，否则对应行空白 |
| `wechat.timeout_sec` | `8` | 微信接口超时（秒） |
| `wechat.proxy` | `""` | 走代理时填 `http://127.0.0.1:7890` |

### 两个触发指标：选 `cuda_util` 还是 `cuda_memory`

`metric` 决定"超标"是按哪个数量算的。**别混着配** —— 这两个量在真机上差一个量级：

| 指标 | 含义 | 真机实测（同一个训练进程） | 15% 触发线意味着 |
|---|---|---|---|
| `cuda_util`（默认） | CUDA 引擎利用率，就是任务管理器「Cuda」那一列 | 周期峰值 **66.6%**，间歇 **0%** | 利用率超过 15% 就报。**能触发** |
| `cuda_memory` | 该进程占用的 GPU 显存 | 峰值 2360 MiB / 49140 MiB ＝ **4.8%** | 48GB 卡上要 7371 MiB。**永远不触发** |

**要"监控 CUDA 占用"，用 `cuda_util`。** 它天然只对 CUDA 进程有值 ——
图形/桌面进程的 `engtype_Cuda` 利用率恒为 0，不会进来。

**要"看谁把显存吃满了"，才用 `cuda_memory`，并且优先写绝对 MiB。**

```json
// 谁在跑 CUDA 且利用率超过 15% → 提醒（默认，也是真机在用的）
"thresholds": { "metric": "cuda_util", "cuda_util_percent": 15 }

// 谁吃了超过 1 GB 显存 → 提醒（含图形进程）
"thresholds": { "metric": "cuda_memory", "memory_percent": null, "min_memory_mb": 1000 }

// 同上，但只算在 CUDA 引擎上跑过的进程
"thresholds": { "metric": "cuda_memory", "memory_percent": null, "min_memory_mb": 1000, "cuda_only": true }
```

要点：

* `metric=cuda_util` 时 `cuda_util_percent` **必须落在 `(0, 100]`**，否则启动即报错。
* `metric=cuda_memory` 时 `memory_percent` 与 `min_memory_mb` **不能同时为空/0**，
  否则所有进程都会被判定为超限，程序启动时直接拒绝。
* **拿不到利用率（`None`）不算触发，也不算 0。** 这是刻意的：
  上层会把"这条通路不可用"明确写进输出和自检，免得变成静默漏报。
* 老配置里的 `exit_memory_percent` / `exit_memory_mb` 已被**忽略**（不会报错），
  但也不再有任何效果 —— 去重由"按进程记忆"负责，见上文《推送行为》。

### 为什么默认用 CUDA 引擎利用率

显存占用是**进程级**的（不会把整卡数字摊到某个进程头上），但 Windows 上
`Local Usage` 把**图形和计算混在一起**；而"利用率"这条通路天生带引擎类型：

```
pid_26660_luid_0x00000000_0x0000C4F0_phys_0_eng_11_engtype_Cuda
```

真机实测（同一时刻），一眼就能看出这个区分的价值：

| PID | 进程 | CUDA 利用率 | 其它引擎活动 | 会被告警吗 |
|---|---|---|---|---|
| 26660 | `envs\yolo_ultra\python.exe` | **66.6%**（`engtype_Cuda`） | 无 —— **完全没有 3D 活动** | **会** |
| 2772 | msedgewebview2 | 0 | `3D` 1.1% | 不会 |
| 24376 | 向日葵 | 0 | `VideoEncode` 0.9% / `Compute_0` 0.7% / `3D` 0.2% | 不会 |

要点：

* **只用 `engtype_Cuda`**。`Compute_0` / `Compute_1` 是 D3D 的计算着色器路径，
  不是 CUDA —— 向日葵就落在那里，把它算进来等于又把图形混进来了。
* **`cuda_util` 指标看的是瞬时利用率**，所以一个 CPU 阶段（取数据、存 checkpoint）
  会掉到 0%。这不会造成重复推送，因为去重是按进程做的，不是按"是否达标"做的。
* **`cuda_memory` 指标下"是不是 CUDA 进程"看累计运行时间，不看瞬时利用率。**
  累计值是单调的 —— 这个进程生命周期内跑过 CUDA，就一直算候选。
  否则进程会每 20 秒被筛出去一次，集合跟着抖。
* **拿不到引擎计数器时不筛选**，并且会在输出和自检里明确写出来。
  宁可多报，也不能因为筛选条件缺失就静默漏报（这个项目栽过这个跟头）。
* `--once` 表格里标 `·` 的行 = **数值达标但不是在跑 CUDA，被筛掉了**。
  刻意不静默隐藏：看不到它，你会以为没检测到；标出来才知道是"检测到但不算"。
  被筛掉的行也照常显示 CUDA 利用率列，便于复核判断对不对。

**关于「CUDA 显存」这个数字本身**：Windows 上**没有**独立计数器能只报 CUDA 那部分，
图形与计算在数值上拆不开 —— 这是平台限制。但对**无窗口的纯计算进程**
（实测完全没有 `engtype_3D` 活动）图形贡献为 0，它的 `Local Usage` 就等于
它的 CUDA 显存。这是这个平台能达到的最精确程度。

### 阈值怎么定（`cuda_memory` 口径）

如果要用显存口径，百分比的分母是**该卡自身的显存总量**，
同一个数字在不同卡上完全不是一回事：

| 显存总量 | 15% 折合 |
|---|---|
| 24 GB（RTX 4090） | 3.7 GB |
| 48 GB（RTX 4090 48G） | **7.2 GB** |
| 80 GB（A100） | 12.0 GB |

真机踩过这个坑：48 GB 卡上按 15% 配，进入线要 **7371 MiB**，
而实际项目峰值只有 **2360 MiB** —— 阈值永远不可能触发，表现就是"一条告警都不发"。
这正是后来把默认指标换成 `cuda_util` 的直接原因。

想表达"这个进程吃超过 1 GB 就叫我"，**直接写绝对量**：

```json
"thresholds": {
  "metric": "cuda_memory",
  "memory_percent": null,
  "min_memory_mb": 1000
}
```

要点：

* `memory_percent` 设成 `null` 表示彻底关掉百分比判定，只看 MiB。
* 边界语义不同，是刻意保留的：`min_memory_mb` **含等号**（`>= 1000` 进入），
  `memory_percent` **严格大于**（`> 15%` 进入）。
* `min_memory_mb` 只能让门槛**更严**，不能用它放宽百分比。

**怎么定这个数**：先用 `--once` 在目标机连续看几轮真实占用，
取「周期峰值」和「桌面进程噪声底」之间的空档。真机实测供参考：

| 类别 | 实测占用 |
|---|---|
| 桌面进程噪声底（向日葵 / dwm / VS Code …） | 最大 159 MiB |
| 训练项目空闲相位 | 737 MiB |
| 训练项目峰值相位 | 2360 MiB |

1000 MiB 落在噪声底和项目空闲值之间 —— 桌面进程被挡掉，
项目一启动就进集合，整轮跑完进程退出才解除。**一代负载一条推送**，
不会因为显存周期性涨跌而刷屏。

**相对路径**一律相对 `config.json` 所在目录解析，不受进程工作目录影响。

**环境变量优先于配置文件**，用于把密钥排除在磁盘之外：

| 环境变量 | 覆盖 |
|---|---|
| `CUDA_MONITOR_WECHAT_APPID` | `wechat.app_id` |
| `CUDA_MONITOR_WECHAT_SECRET` | `wechat.app_secret` |
| `CUDA_MONITOR_WECHAT_TEMPLATE_ID` | `wechat.template_id` |

`deploy.ps1 -AppSecret xxx` 会自动把它写进机器级环境变量，并把配置文件里的明文清空。

---

## 四、日常运维

| 目的 | 命令 |
|---|---|
| 看任务状态 | `Get-ScheduledTask -TaskName CUDA_Monitor \| Select-Object State` |
| 看日志 | `notepad C:\ProgramData\CudaMonitor\monitor.log` |
| 看门狗状态 | `Get-ScheduledTask -TaskName CUDA_Monitor_Watchdog \| Select-Object State` |
| 看门狗日志 | `notepad C:\ProgramData\CudaMonitor\watchdog.log` |
| 查真实 OpenID | `python.exe ...\run_monitor.py --config ...\config.json --list-users` |
| 手动跑一次看结果 | `python.exe ...\run_monitor.py --config ...\config.json --once` |
| **预览会推什么（不发送）** | `python.exe ...\run_monitor.py --config ...\config.json --preview` |
| 看每轮扫描明细 | 加 `--verbose`，会记录"本轮无变化，不推送"这类调试行 |
| 只记日志不推送（演练） | 加 `--dry-run` 参数 |
| **停止服务** | `Disable-ScheduledTask -TaskName CUDA_Monitor`（需管理员） |
| 改配置后生效 | `Disable-ScheduledTask` → 改 `config.json` → `Enable-ScheduledTask` |
| 更新代码 | 在项目目录重跑 `deploy.ps1`（覆盖程序文件，保留 `config.json`） |
| 完全卸载 | `.\scripts\uninstall.ps1`（先删看门狗，再删主任务） |

> ⚠️ 停服务请用 `Disable-ScheduledTask`，**别只用 `Stop-ScheduledTask`** ——
> 后者会在 3 分钟内被看门狗拉回来。看门狗尊重 `Disabled` 状态，不会擅自启用。

日志带轮转（默认单文件 5 MB、保留 3 份），长期运行不会撑爆磁盘。

---

## 五、相对原始方案的改动

| # | 原始做法 | 问题 | 现在 |
|---|---|---|---|
| 1 | 直接 `subprocess` 调 nvidia-smi | pythonw 无控制台，Windows 会为子进程新建控制台窗口，**每 5 秒闪一次黑框** | 加 `CREATE_NO_WINDOW` + `STARTUPINFO` 双重保险 |
| 2 | 先锁目录权限，再去手动跑脚本测试 | 目录锁上后普通用户写不了日志，脚本**启动即崩** | 强制顺序：先自检跑通，最后才收紧权限 |
| 3 | 各卡显存总量求和当分母 | 多卡时百分比被稀释，阈值失效 | 按 `gpu_uuid` 关联到单卡，分母是该卡自身容量 |
| 4 | 主循环无异常兜底 | 一次异常即进程退出，重启次数用完就永久静默 | 全异常捕获 + 指数退避 + 连续失败反向告警 |
| 5 | AppSecret 明文写在脚本里 | `C:\ProgramData` 默认 Users 可读，凭据泄露 | 支持机器级环境变量，且 `config.json` 单独收读取权限 |
| 6 | 引入 `requests` | 目标机要额外装依赖 | 改用标准库 `urllib.request`，零依赖 |
| 7 | 冷却键只用 PID | PID 会被复用，且记录不清理 | 用 (类型,卡号,PID,进程名) 四元组 + 定时清理 |
| 8 | 用你自己的账号 + 存密码跑任务 | 需要存凭据；同账号提权仍可结束 | 改用 **SYSTEM** 身份，无需密码 |
| 9 | 日志无轮转 | 长期运行文件无限增长 | `RotatingFileHandler` |
| 10 | 无重复启动保护 | 任务重启策略可能拉起多个实例 | 命名互斥体，第二实例直接退出 |
| 11 | 无自检手段 | 部署完只能靠"等出问题" | `--selftest` 覆盖驱动 / 日志 / 微信 / 模板字段四项 |
| 12 | WDDM 下把 nvidia-smi 的 `[N/A]` 行静默跳过 | 逐进程显存**一个都拿不到** → 判定层永远空集合 → **一条告警都不发**，日志上却完全正常。真机上真实踩到 | 识别"有行但全部 N/A"，切 Windows 性能计数器 `\GPU Process Memory(*)\Local Usage` 兜底；来源写进快照和日志 |
| 13 | 阈值只按百分比配 | 百分比分母是**单卡显存总量**，48GB 卡上 15% = 7371 MiB，小项目永远触发不了。和 12 是**两个独立的故障**，只修一个仍然不报警 | 支持绝对 MiB 阈值（`min_memory_mb`），并在启动时校验配置合法性 |
| 14 | 把 `Local Usage` 当"CUDA 显存"直接上 | 它把图形和计算混在一起，纯图形进程也会被告警 —— 那就成了"监控显卡"而不是"监控 CUDA" | 用 `\GPU Engine(*)\...engtype_Cuda` 打 CUDA 活动标记；`cuda_memory` 口径下用 `cuda_only` 筛掉非 CUDA 进程，拿不到标记时不筛 |
| 15 | 用瞬时 CUDA 利用率判定"是不是 CUDA 进程" | 训练任务在取数据/存盘的间隙利用率就是 0，进程会被反复筛进筛出，告警集合跟着抖 | `cuda_memory` 口径下改用**累计** `Running Time > 0`（单调）；`cuda_util` 口径下按进程去重，不靠"集合是否稳定" |
| 16 | 只按"集合是否变化"推送 | 真实负载 20 秒一个来回，利用率在 66.6% ↔ 0% 之间跳，集合每轮都在进出 → 推送跟着负载刷屏 | 改成**按进程身份去重**：提醒过就记住，只在进程退出时清除。因此迟滞（`exit_*`）整套删掉 |
| 17 | 「cuda 占用 15%」按显存理解 | 真机项目显存峰值只占整卡 4.8%，15% 的显存线永远不触发 —— 配了等于没配 | 新增 `metric` 区分两个量，默认改成 `cuda_util`（引擎利用率），并把两者的量级对照写进 README 与配置模板 |

---

## 六、已知边界

- **`cuda_util` 只是"当前这一刻"的利用率，可能瞬时为 0。**
  训练任务在取数据、存 checkpoint 的间隙利用率为 0，所以告警本身有偶然性 ——
  一次运行中是否被捕捉到，取决于那一刻正好落在哪个阶段。
  这是"按利用率报警"的固有性质，不是 bug。**收益是立刻能区分 CUDA 进程与桌面进程**，
  代价是不如显存口径稳定。想更稳就换 `cuda_memory` 口径（见上一节）。
- **Windows（WDDM）下拿不到"纯 CUDA 显存"这个粒度。**
  GeForce/消费级卡无法切到 TCC，`nvidia-smi --query-compute-apps` 的
  per-process `used_memory` 一律返回 `[N/A]` —— 这是平台限制，不是实现偷懒。
  兜底用的性能计数器 `\GPU Process Memory(*)\Local Usage` 是进程在 GPU 上的
  **本地段总量（图形 + 计算混在一起）**，也就是任务管理器「进程」页那一列。
  用 `cuda_only` + 引擎类型把纯图形进程排除掉，再用 `min_memory_mb`
  挡掉剩余的小进程（真机噪声底约 160 MiB）。Linux / TCC 环境下
  `nvidia-smi` 通路正常，不会走兜底。
- **阈值是按"单个进程"算的，不按作业聚合。** 如果一个训练作业拆成多个
  CUDA 进程（DDP、多卡、或 DataLoader worker 里也起了 CUDA 上下文），
  利用率/显存会分散在多个 PID 上，可能每个都够不着阈值。
  需要"整个作业合计"的话得按进程树聚合，属于另一个特性。
  同理，**去重也是按 PID 的**：同一个作业换了个 PID 重跑，会再提醒一次。
- 进程名取不到时（真正受保护的系统进程，或跨会话且权限不足）会退回 `pid <N>`
  展示。占用统计和进程身份判定不受影响。监控跑在 SYSTEM 下，除系统进程外都能解析。
- **系统进程（PID 4，`System`）会被标记为 CUDA 活动** —— WDDM 内核态驱动的一部分
  CUDA 工作算在它头上。它的显存占用是几 MiB 量级，任何合理阈值都能挡住，不用管。
- 告警依据是**单进程显存占比**（回答"谁在占着卡"），不是计算利用率。
  利用率数据仍然采集、也仍然显示在 `--once` 表格里，只是没有参与告警判定 ——
  按进程看的时候，显存占比才是能归因到具体进程的那个指标。
  想加整卡利用率告警，采集层的数据已经就绪，判定层加一条规则即可。
- 进程集合的"是否变化"只比对**进程身份**（卡号 + PID + 进程名），
  进程没变、只是占比涨跌不会重新推送。这是刻意的 —— 要的是"谁在占卡"的变化通知，
  不是占用数字的实时曲线。
- MIG 实例下 `nvidia-smi` 报告的显存总量是实例切片容量，百分比含义与整卡不同。
- 测试号有效期一年，到期需要重新申请并更新 `app_id` / `app_secret`。
- 模板消息有每日调用上限。集合式去重本身已经很省，`min_push_interval_sec`
  再兜一道，正常不会触顶。
#   S u r v e i l l a n c e  
 