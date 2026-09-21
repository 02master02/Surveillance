# 部署验收：CUDA 占用监控（2026-09-18）

目标机 `DESKTOP-FCSLQIH`，单张 RTX 4090 48GB，Windows 10 22H2。
本次把告警口径从「显存占比」换成「CUDA 引擎利用率」，并改成按进程去重。

---

## 一、本次改了什么

| 项 | 改之前 | 改之后 |
|---|---|---|
| 触发指标 | 显存占整卡比例（`memory_percent: 15`） | **CUDA 引擎利用率**（`metric: cuda_util`，`cuda_util_percent: 15`） |
| 在 48GB 卡上等于 | 7371 MiB —— 项目峰值只有 2360 MiB，**永远不触发** | 利用率 15% —— 项目峰值 66~70%，**能触发** |
| 重复提醒 | 集合变化就推，负载 20 秒一个来回 → 反复刷屏 | **同一进程只提醒一次**，只在进程退出时清除记忆 |
| 迟滞（退出线） | `exit_memory_percent: 13` | 已删除 —— 按进程去重比迟滞更强 |
| 图形进程 | 会被当成"CUDA 占用"误报 | 用 `engtype_Cuda` 排除，桌面/图形进程恒为 0% |

代码上新增 `win_gpu_mem.cuda_activity()`（读 Windows 性能计数器
`\GPU Engine(*)\Utilization Percentage` / `Running Time` 里带 `engtype_Cuda` 的实例），
判定层重写为「按进程身份去重」。

---

## 二、真机实测证据

### 2.1 采集层认得出 CUDA 进程

`--once` 输出（16:17:22，训练正跑在峰值）：

```
GPU 概览
  0   NVIDIA GeForce RTX 4090     2913/49140 MiB (5.9%)       58%
  进程占用来源：windows-pdh
  CUDA 活动标记：已识别（来源：性能计数器）

进程列表（单进程 CUDA 引擎利用率 > 15% 就提醒；同一进程只提醒一次）
    卡   PID     进程                      CUDA%    占用 MiB      显存占比
  ★ 0   26660   python.exe               69.2      2360      4.8%
    0   2108    dwm.exe                   0.0       100      0.2%
    0   27512   Code.exe                  0.0        87      0.1%
    0   16124   explorer.exe              0.0        36      0.1%
    ...（其余 16 个桌面进程全部 0.0%）
```

关键点：训练进程 `python.exe`（PID 26660，`train_new1217.py`）**CUDA 69.2%**，
而显存只有整卡的 **4.8%** —— 这正是旧阈值永远不触发的原因。
桌面进程（dwm / explorer / Code / 向日葵）全部 0.0%，`engtype_Cuda` 筛选生效。

### 2.2 推送恰好一条

服务启动横幅与推送记录（`monitor.log`）：

```
[16:18:55] [INFO] 告警条件：单进程 CUDA 引擎利用率 > 15% 就提醒；同一进程只提醒一次
[16:18:56] [WARNING] 进程占用改走性能计数器（windows-pdh）：nvidia-smi 未提供可用的进程显存
[16:18:56] [WARNING] 告警：python.exe (PID 26660) 在 GPU 0 上，CUDA占用 70.2%（显存 2360 MiB / 49140 MiB）
[16:18:56] [INFO] 新告警，共 1 个进程（已记入去重名单，之后不再重复提醒）。
[16:18:57] [INFO] 推送成功 -> oZkP73... (msgid=4699755663063252995)
```

### 2.3 涨落期间没有重复推送

推送之后连续采样，训练进程的 CUDA 利用率一直在跨过 15% 线反复涨落：

| 时间 | CUDA 利用率 | 是否越过 15% | 有没有再推 |
|---|---|---|---|
| 16:18:56 | 70.2% | 是 | 推了（唯一一条告警） |
| 16:21:58 | 28.6% | 是 | **没有** |
| 16:22:09 | 28.7% | 是 | **没有** |
| 16:22:20 | 0%（间歇相位） | 否 | 没有 |

同一进程反复越过阈值不再重推 —— 需求达成。

### 2.4 进程退出时正确发出"已结束"

训练进程（PID 26660，07:27 启动，跑了约 9 小时）在 16:24:04 退出，
监控检测到并补发了一条结束通知：

```
[16:24:04] [INFO] 已提醒的 1 个进程已结束（持续 5 分钟）
[16:24:05] [INFO] 推送成功 -> oZkP73... (msgid=4699760833046249474)
```

至此三条行为都在真机上验证过了：**达标推一次 → 涨落不重推 → 退出推结束**。

### 2.5 推送总数

| # | 时间 | 内容 | 代码版本 |
|---|---|---|---|
| 1 | 15:09:10 | 旧口径的手工测试推送 | 旧 |
| 2 | 16:18:57 | `CUDA告警：1 个进程`（PID 26660，70.2%） | 新 |
| 3 | 16:24:05 | `CUDA告警已结束`（持续 5 分钟） | 新 |

新代码运行约 5 分钟内，该项目只有 **2 条**消息（1 条告警 + 1 条结束），
期间利用率在 0% ↔ 70% 之间涨落至少 5 个来回。

---

## 三、部署动作记录

| 步骤 | 做法 |
|---|---|
| 停任务 | `schtasks /Change /TN CUDA_Monitor /DISABLE` |
| 说明 | **禁用不会停掉已在运行的实例**，旧实例（15:11 启动、跑着旧代码）仍在轮询，必须 `schtasks /End` 才能真正终止 |
| 更新代码 | 覆盖 `src\`、`run_monitor.py`、`config.example.json`（跳过 `__pycache__`） |
| 更新配置 | 以新 `config.example.json` 为底 + 沿用原 `wechat` 凭据，写成不带 BOM 的 `config.json` |
| 备份 | 原配置备份为 `C:\Users\AIAgent\cuda_file\config.json.bak-20260918-161526` |
| 起任务 | `schtasks /Change /ENABLE` + `schtasks /Run` |

> 没有重跑 `deploy.ps1`：任务定义、ACL、目录结构都没变，
> 只改了代码与 `thresholds` 段，属于最小变更集。

---

## 四、需要知道的三件事

1. **告警有偶然性。** `cuda_util` 是瞬时值，训练任务在取数据、存 checkpoint 的
   间隙就是 0%。一次运行是否被捕捉到，取决于那 5 秒的轮询正好落在哪个阶段。
   这是"按利用率报警"的固有性质。想要更稳就改用 `cuda_memory` 口径。
2. **进程退出时会推一条"已结束"。** 由 `notify_on_clear: true` 控制。
   训练任务正常结束（或被杀）后，PID 26660 消失，会收到
   `CUDA告警已结束` + 持续时长 —— 本次实测就收到了（持续 5 分钟）。
   **不想收这类消息就把它设成 `false`**，改完重启任务即可。
3. **阈值与去重都是按 PID 的，不按作业聚合。** 该训练项目有两个
   DataLoader 子进程（PID 30008 / 2492），但它们不持有 CUDA 上下文，
   引擎计数器只认 PID 26660 —— 本例没问题。将来若上 DDP / 多卡，
   利用率会分散在多个 PID 上，可能每个都够不着 15%。

---

## 五、常用排障命令（目标机）

```bat
:: 看这一轮采集到什么、谁在达标（不推送）
D:\Anaconda\python.exe C:\ProgramData\CudaMonitor\run_monitor.py ^
    --config C:\ProgramData\CudaMonitor\config.json --once

:: 部署前自检（会真发一条测试消息）
D:\Anaconda\python.exe C:\ProgramData\CudaMonitor\run_monitor.py ^
    --config C:\ProgramData\CudaMonitor\config.json --selftest

:: 看日志尾部
type C:\ProgramData\CudaMonitor\monitor.log
```

本机（无显卡）跑判定层回归测试：

```bash
python tools/test_judge.py     # 75 项，无 GPU 无网络
```
