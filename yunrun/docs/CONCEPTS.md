# 易混概念对照（接手的人先看这一页）

这个仓库里不少概念有 **2–3 个并行真相**（同名变量不同值、同一判断多处实现）。下面这张表把
每个易混点指向**唯一的权威位置**；有冲突时以「权威位置」为准，其它都是历史残留。

| 概念 | 唯一权威 | 容易误认的另一个 | 说明 |
|---|---|---|---|
| 「项目根」 | `yunrun/`（`runner/paths.py:23`、`runner/main.py:26`） | `yunrun/gps_track_lab/`（`gps_track_lab/.../web.py:27` 的 `PROJECT_ROOT`） | 后者只在轨迹实验室里指自己的目录；`console.py` 用 `web.PROJECT_ROOT.parent` 绕回 `yunrun/`。要绝对路径请一律走 `paths.py` / `launcher.py` |
| 「合格」 | `yunrun/tools/tasklist_builder.py:32-37`（生成窗口 + `MIN_QUALIFIED_KM`/`MAX_QUALIFIED_MIN`） | `runner/main.py:735-738`（学校规则带 + 偏移）、`runner/main.py:1281-1319`（服务端 `isStandard` 终审） | 本地生成只在第一处把关；第二处的 `raSingleMileageMin/Max` 赋值后**没有读取点**（只 `raCadenceMin/Max` 在 `finish()` 里用）；第三处才是「判不合格」的真凶，失败时尾批与 finish 都不发 |
| `config.ini [Run]` | `tasklist_builder.py` 顶部常量与 `plan_run()` 参数 | `config.example.ini` 的 `[Run]` 段 |
**`build_tasklist()` 不读 `config.ini`**，生成窗口由代码常量决定；`[Run]` 里的 `min_consume`/`max_consume`/`strides`
只在**打表模式的 `finish()`** 里被读（`runner/main.py`），`exclude_points` 则是死配置——只有赋值和 `global` 声明，没有读取点 |
| 「行数」 | `mcp_bridge.py:1344`（共享状态里只发布最后 **400** 行） | `mcp_bridge.py:1237`（detached 首写把 `line_count` 置 0）、`console.py:42`（`MAX_JOB_LINES=2000`）、`admin.py:60`（`MAX_JOB_LINES=4000`） | 前端的 `seen_lines` 游标按「已发布行数」推进；单次运行约 90–120 行所以现在看不出问题，**超过 400 行后页面日志会静默停止增长** |
| 「锁场 / 场地核对 / 锁场地」 | `runner/main.py:588-700`（`select_task`：几何判定） | `admin.py` 的 `stadium-lock` 路由与前端「场地核对」面板 | 三处叫法指同一件事：**用打卡点围栏是否包含标定中心来决定挂哪个任务**。注意：`admin.overview` 的六步里**没有**「场地核对」这一项，所以左侧导航那一项永远不会打勾 |
| 两份 `main.py` | `yunrun/runner/main.py`（fork 的实现） | 根目录 `main.py`（上游 3.4.8 的旧版，参数不同） | 同理还有两份 `history.py`、`proxy.py`、`Login.py`、`get_url_Id.py`、两个 `tools/`。看注释里的裸文件名引用时，先确认是哪一份 |
| 人脸检测 | `scripts/retina_detect.py` + `yunrun/face_lib.py`（管理前台的人脸库用它自动检测） | `runner/main.py` 的人脸管线 | **runner 这条链不做自动检测**：它吃 `--face-detection` 的标注 JSON，缺了就预检失败。两边共同点是「缺检测=不放行」 |
| 「中止」的状态 | `mcp_bridge.py` 的 `abort.<pid>.flag` → 执行链在批次间隔抛 `RunAborted` | 各入口自己回来的「已中止」文案 | in-process 路径会把终态记成 `aborted`，detached 路径目前记成 `failed`——语义不一致，是已知债 |
| 25 m 容差 | `mcp_bridge.CHECKPOINT_TOLERANCE_M` | `runner/main.py` 的几何锁场 | 容差判的是「轨迹**经过过**每个打卡点附近」，不是「按顺序逐点跑到」：反向跑、抄近路、跳过最后 20 m 都能过；而如果学校下发的是围栏角点而不是轨上打卡点，±75×62 m 以上的围栏会让正常记录被拒 |

## 顺带一句

代码里少量 `R2`/`R5`/`S1` 编号是我刻意留的：它们钉住「以后别改回去」的语义（结束链顺序、预算按单调
时钟计、剩余预算不足就停发）。没有编号的地方当我的判断看，别当共识。反编译产物（jadx 解包）不随仓库
分发，需要核证据时自己解一份 3.6.6 的 APK。
