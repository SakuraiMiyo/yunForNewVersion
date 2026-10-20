# GPS Track Lab

> 这块是**轨迹拟合 / 仿真的算法实验**（标定、生成、卡片、地图回放），与上层的提交链解耦：
> 提交、闸门、令牌那些在 `yunrun/mcp_bridge.py` 与 `yunrun/runner/`。

一个离线的操场 GPS 标定与轨迹仿真工具。它可以从历史 GPS 导出文件中估计操场的中心、方向和尺寸，并生成带有物理约束与时间相关 GPS 误差的测试轨迹。

> 本项目只生成本地 CSV、GeoJSON 和 manifest 文件，不包含登录、加密、请求签名或上传代码。生成结果会标记为 `synthetic=true`，适合路线可视化、定位算法测试和实际跑步前的路线规划，不应作为真实运动记录提交。

## 特性

- 从 JSON、GeoJSON 或 CSV 中提取经纬度；兼容常见的 `point: "lon,lat"` 历史导出格式。
- 通过稳健分位数和 PCA 拟合标准的“直道 + 半圆弯道”操场模型。
- 速度使用均值回归随机过程，并限制加速度，避免瞬时跳速。
- 跑道横向偏移与 GPS 测量误差均为时间相关噪声，不是对整条轨迹做固定平移。
- 每次未指定 `--seed` 时自动使用新的随机种子，并建立独立输出目录。
- 输出 CSV、GeoJSON 与 JSON manifest；可用 `inspect` 做基本质量检查。

## 快速开始

无需第三方依赖，要求 Python 3.10+。

### 一键网页生成

直接双击 gps_track_lab 目录里的 start_admin.bat（或在仓库根目录跑 `python launcher.py`），
它会在本机 127.0.0.1:8437 起服务，`/` 就是这张地图：

- 设置距离、配速、候选数量和可选随机种子；
- 点击“生成并显示”，后端会生成候选、完成物理质量检查并自动把最佳轨迹加载到地图；
- 页面可回放轨迹、切换底图、载入最近记录，并下载 CSV、GeoJSON 和质量报告；
- 高级选项可以调整采样间隔、GPS 漂移、白噪声和丢点概率。

本地服务不提供校园跑登录或上传接口，只生成带 synthetic=true 标记的测试数据。关闭启动窗口即可停止服务。

### 命令行

```powershell
cd yunrun\gps_track_lab          # 或 macOS/Linux：cd yunrun/gps_track_lab
$env:PYTHONPATH = "src"

# 1. 用自己的真实 GPS 导出记录标定操场
python -m gps_track_lab calibrate `
  --input ..\tasks_else `
  --output config\track.json

# 2. 每次调用都会生成一条新的本地仿真轨迹
python -m gps_track_lab generate `
  --track config\track.json `
  --settings config.example.json `
  --distance-km 3.05 `
  --pace-min-per-km 6.0 `
  --candidates 8 `
  --output runs

# 3. 检查生成结果
python -m gps_track_lab inspect --input runs\<本次目录>\track.csv

# 4. 按本校“>3 km 且 <=30 min”规则审计本地历史导出
python -m gps_track_lab analyze `
  --input ..\tasks_else `
  --rule-min-km 3.0 --rule-max-minutes 30 `
  --output analysis\tasks_else.json
```

如需完全可复现的结果，可显式传入 `--seed 12345`。不传时使用系统随机源，所以连续两次调用不会生成相同轨迹。

当输入目录里混有相距较远的不同操场时，`calibrate` 会按位置聚类，默认只拟合点数最多的簇，并逐项打印纳入的 task 文件。可以用 `--cluster-index 1` 选择第二个场地，避免把两个操场错误合成一个超大跑道。

## 标定建议

- 最好使用一次真实、连续、至少两圈的跑步 GPS 记录。
- 输入点应来自同一种坐标系；工具不会自动在 WGS-84、GCJ-02、BD-09 之间转换。
- 若轨迹包含从宿舍到操场的路段，先在导出数据里裁掉，否则操场尺寸拟合会被拉大。
- 标定结果中的 `fit_residual_m` 是点云到拟合跑道的中位距离，数值越小越贴合。

## 噪声参数

`config.example.json` 中：

- `gps_sigma_m`：稳态 GPS 平面误差标准差。
- `gps_correlation_seconds`：漂移保持同一方向的典型时间。
- `lateral_sigma_m`：跑者在跑道中心线两侧的缓慢横向变化。
- `speed_sigma_mps`：自然速度波动。
- `max_acceleration_mps2`：每秒允许的最大加减速。
- `dropout_probability`：单次采样丢点概率。
- `reporting_latency_*`：传感器报告延迟的均值、波动和相关时间。
- `latency_spike_*`：低概率延迟尖峰；只写入离线数据，不用于网络发送。

这些参数用于传感器与路线算法测试，不以规避第三方检测为目标。

**当前取值是按真实记录反标定的**，不是默认示例值；方法见下两节。`sample_interval_seconds`
固定为 2，因为全部 36 份真实导出的 runTime 间隔都精确等于 2 秒。

## 业务口径与仿真流水线

本地健康跑的合格标准是：**单次里程 > 3.0 km 且 用时 <= 30 min**。
生成窗口刻意收窄（默认 **距离 3.01~3.20 km、用时 16~25 min**），使结果远离 30 分钟边界。

`yunrun/tools/tasklist_builder.py`（相对本文是 `..\tools\tasklist_builder.py`）是这条口径的
唯一实现处，要点：

- **距离**从 beta 形状分布抽取（集中在 3.0x，长尾到 3.20），对齐真实记录的分布形状。
- **配速**从对数正态抽取（中位 5.80 min/km），**时长**由 `距离 × 配速` 得出，再吸附到仿真
  采样网格上。
- **步频**从干净记录分布抽取并与配速轻耦合（真实数据：5.18 min/km→157，8.07→137）。
- **步幅**不是独立随机的，而是由 `平均速度 × 60 / 步频` **推导**——这个恒等式在 19 份真实
  记录上逐条验证过（误差 ±0.008 m），因此里程、用时、步数、步频四个字段永远自洽。
- 派生步幅若落在观测区间之外，会重抽步频而不是直接输出。
- 仿真器的 tick 数是"距离到达"的涌现结果（实测 912~1336 s），因此时长探测不在窗内时会
  换种子重试，而**不会裁剪点列**——裁点会同时削掉里程。

`sim_pipeline.py` 在此之上负责写盘（`tasks_sim/`）与 CLI 参数。

## 标定与对比工具

| 工具 | 用途 |
|---|---|
| `tools/track_stats.py` | 把一组导出量成统一指标（用时/点距/逐点速度/中心线偏移/步幅步频…） |
| `tools/compare_generator.py` | 生成一批记录，与真实导出逐指标对比并给出对齐分 |
| `tools/recalibrate_track.py` | 从导出重新标定操场（形状筛选 + 稳健中位 + 周长归一化） |

关键实测目标（19 份干净记录，`history/server_gt3km`）：逐点观测速度 **sd 1.41 m/s、
p95 4.68、中位 3.34**；中心线偏移 **sd 1.37 m**；拟合残差 **1.61 m**；步频 **130~157
（中位 147）**；步幅 **0.85~1.26 m**；采样周期恒为 **2 s**。

复现一次对比（`history/` 是本机的历史导出，**不随仓库分发**——`**/history/` 在 `.gitignore` 里；
没有它就换成你自己 `tasks_else/` 里导出的那批）：

```powershell
cd gps_track_lab
python tools/compare_generator.py --reference ..\history\server_gt3km --count 36
```

> 参考集里 36 份中有 17 份是"跑出跑道"的记录（中心线偏移 3.8~29.5 m，拟合圈长
> 500~1700 m），对比工具默认按 `offset_sigma_m <= 3.5` 剔除它们：把普通记录改造成
> 那副样子才能对齐，是反向优化。

