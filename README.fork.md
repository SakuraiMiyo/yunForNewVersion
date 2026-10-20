# 我在原项目基础上做了什么

底座是 [Zirconium233/yunForNewVersion](https://github.com/Zirconium233/yunForNewVersion)

vibe了一个简单的网页控制台，新加的功能就是 网页前台、手机端 token
抓取、操场标定、跑步仿真、轨迹卡片渲染，另外还加了人脸库、提交前的三道闸和独立执行进程
每块干啥的，详见[README.md](README.md)；这份文件是剩下的：怎么用、为什么这么弄、哪儿我还没验证

初次进入控制台会去引导你配置并且管理你的跑步：取令牌、下历史、标定、生成、确认、提交，一路点下来完成操场标定的检查后你就可以开跑了

## 你必须知道使用风险！

服务端在查重复轨迹，在原项目的
[issue #82](https://github.com/Zirconium233/yunForNewVersion/issues/82)
中出现了被延迟标记不合格的情况，说明可能还存在人工复审运动记录的情况，特别是现在开启人脸的学校，一定别一直用同一张照片，也别用场景容易被看穿的照片

针对路径重复，我这套里做的「根据历史轨迹标定操场 → 场地锁场 → 跑步仿真 → 卡片核对 → 生成窗口收窄」都是往这个方向使劲，但是我依旧不能说保证服务器不判违规！

万一你发现历史记录真被标记为不合格或作弊了：直接暂停使用本项目，免得损失更大。使用中遇到问题，到我这个仓库的 issue 区来提就好

这玩意毕竟是绕学校考核的东西，用之前自己考量好使用风险。我没法承担，也不会承担本项目造成的损失！

## 目录

```
yunyundongRun-release/
├── launcher.py              入口：起本地服务 + 开浏览器
├── run.command              Mac 上双击用（自己建 .venv-mac 装依赖，再跑 launcher.py）
├── .gitattributes           只钉 .sh/.command/.bat 的换行，不动源码
├── README.md                我的说明在上半部分，原项目 README 原文原样接在下面
├── README.fork.md           本文件
├── main.py / history.py / tools/ / image/ / tasks_fch|txl|xc/
│                            上游原有的脚本和素材，没动
├── scripts/                 跑在独立进程里的小脚本
│   ├── run_worker.py        真正提交的那段，由业务层拉起
│   ├── login_once.py        账号密码登录（方式 B，会把手机端会话踢掉）
│   ├── retina_detect.py     RetinaFace 检测，只在这里实现一遍
│   └── make_face_annotation.py  给单张照片生成 --face-detection 标注（命令行用）
└── yunrun/
    ├── config.example.ini   配置模板（含 App 通用协议常量）；真实的 config.ini 不入库
    ├── requirements.txt     依赖（装这份，不是根目录那份）
    ├── refresh_token_adb.py 从手机 logcat 抓 token
    ├── pull_token.bat / pull_token.command   上面那个脚本的双击入口
    ├── sim_pipeline.py      生成一版轨迹并打印打表 JSON（不联网，不出卡片）
    ├── dry_run_home.json    离线演练用的假「主页响应」（占位数据）
    ├── face_lib.py          人脸库：多张照片导入、自动检测、挑一张激活
    ├── mcp_bridge.py        业务层，三道闸只在这里实现
    ├── run_control.py       一次性令牌（落盘、跨进程、30 分钟）
    ├── map.json             点位，默认空
    ├── models/              人脸检测模型（随仓库带 1.7 MB；其它模型不入库）
    ├── data/ tasks_else/ tasks_sim/   本机数据，不入库
    ├── runner/              执行链路 start → split → isStandard → finish
    │   ├── main.py          打表上传 + 选任务（按几何锁定）
    │   ├── yun_http.py      协议层：SM2/SM4 信封、每请求 sign/uuid、三种解码
    │   ├── yun_face.py      人脸子系统：取景质量门、窗口触发、上传状态机
    │   ├── history.py       导历史记录（网页里那一版是它的无头版）
    │   ├── paths.py         路径解析（项目根 / 资源目录）
    │   ├── live_probe.py    登录前的只读探测（退出码 0/2/3/4/5 有语义）
    │   └── tools/           Login / 学校目录 / drift / proxy（上游脚本的包内副本）
    ├── gps_track_lab/       轨迹生成器 + 本地 Web 服务
    │   ├── src/gps_track_lab/
    │   │   ├── admin.py     引导页 /admin
    │   │   ├── console.py   控制台 /ui（两阶段确认）
    │   │   ├── web.py       服务本体，Python 标准库 http.server
    │   │   ├── calibration.py  操场拟合（中心/朝向/半径/圈长）
    │   │   ├── generator.py    轨迹仿真
    │   │   ├── retention.py    生成物清理
    │   │   ├── cli.py          命令行入口（serve / calibrate / simulate）
    │   │   └── frontend/       页面源文件（admin.html / console.html / studio.css / studio.js）
    │   ├── tools/recalibrate_track.py  拿历史记录重新标定
    │   ├── tests/          轨迹模块的离线测试（46 项）
    │   ├── config.example.json  生成器参数示例
    │   ├── track_viewer.html    轨迹实验室页面（就是 `/`）
    │   └── start_admin.bat  双击起服务（三个页面一起服务，只是默认开 /admin）
    ├── track_card/          轨迹卡片：拼 HTML/SVG，再用无头浏览器截成图
    ├── tests/               测试（fixtures 随仓库分发；token/密码是占位值）
    ├── tools/tasklist_builder.py  打表文件生成，换算只在这里做
    └── docs/                ARCHITECTURE / USAGE / INTEGRATION / CONCEPTS
```

## 跑起来

先检查你本机的环境配置

- Python 3.10 及以上
- 配置好 adb 环境 + 手机开着 USB 调试 + 云运动保持登录，需要用来从手机日志拉取令牌
- Edge / Chrome 之一 —— 出轨迹卡片要用它无头截图。**没有浏览器不是「降级出 HTML」，是直接
  失败**：`render_card` 先找浏览器、找不到就抛错，而确认前又必须看卡片，所以缺浏览器等于
  整条提交链走不通。只想生成轨迹、不提交的话可以不装

### Windows

```powershell
cd yunrun
copy config.example.ini config.ini      # 推荐：网页也能在缺文件时以模板为底自己建
pip install -r requirements.txt
cd gps_track_lab
.\start_admin.bat
```

浏览器会打开 `http://127.0.0.1:8437/admin`，后面跟着引导配置就好

### macOS / Linux

在仓库根目录：

```bash
cp yunrun/config.example.ini yunrun/config.ini
python3 launcher.py
```

8437 被占的话它会自己换一个空闲端口；加 `--no-browser` 就不自动开浏览器。
`start_admin.bat` 那条路没有端口兜底（它是直接 `python -m gps_track_lab serve`），
端口冲突时在**仓库根目录**跑 `python launcher.py --port 8438` 更省事。

Mac 上也可以直接双击 `run.command`：它会找一个能用的 python3，在项目里建 `.venv-mac/`
把 `yunrun/requirements.txt` 装好，再跑 `launcher.py`，第一次需要联网。只想体检不启动，
用 `./run.command --check`。

### 不经过网页

```powershell
cd yunrun
python sim_pipeline.py --show                          # 生成一版轨迹、打印打表 JSON
python runner\main.py --dry-run -t ..\tasks_fch        # 打表链路空跑（假传输，不实际提交）
python runner\history.py                               # 导历史记录（会连服务器）
```

三条都有前置：

- `sim_pipeline.py` 要 `gps_track_lab/config/track.json`（先标定过操场；标定又得先有 token
  下历史）。它只产出打表文件，**不出卡片**。
- `runner\main.py --dry-run` 要一份完整的 `config.ini`，**还要同一份 `track.json`**——选任务
  只看几何，没标定它直接停（「没有可用的场地标定…已停止」），试运行也一样。所以严格说这条也
  不算纯离线：标定那一步得先联网下过历史。
- `runner\history.py` **也不是离线的**：它会真的调服务器的学期/记录列表接口，还会写回历史文件。

`--dry-run` 那条还必须带 `-t`：runner 默认找 `yunrun/tasks_fch`，这个目录不入库（那是你自己
的历史表落点），不指定就 `FileNotFoundError`。`-t ..\tasks_fch` 指的是上游自带的那 9 张样例表。

## 生成和提交按什么口径

云运动判合格的常见条件是单次里程大于 3 km 且用时不超过 30 分钟，所以我把生成窗口收窄到
3.01–3.20 km、16–25 分钟，两头都留了余量。

几个不能破的换算：步幅 = 平均速度 × 60 / 步频（在真实记录上对过），
所以里程、用时、步数、步频之间会保持自洽，不会出现「3.05 公里配步频 190」这种一眼假的组合，配速也是一条忽快忽慢、夹着短停顿的线，不是水平直线

## 标定和仿真的算法

### 通过历史数据标定操场

先假定跑道是标准体育场——两条直道加两个半圆（`model: stadium-centerline`）——然后把历史记录里那些点全拟到这条中心线上。四个参数：中心经纬度、朝向、直道半长 `half_straight`、弯道半径`radius`

1. 以所有点的中位经纬度为原点做等距投影
2. 对全部点做一次 PCA 拿主轴角度 `0.5·atan2(2·cov_xy, var_x − var_y)`，整体转到这个朝向上
3. 旋转后取 x、y 各自的 2% 和 98% 分位量尺幅
   半径留 8m 防退化下限
4. 分位框中心差就是偏移量，转回原坐标系加回去。残差 = 各点到中心线距离的中位，保存为 `fit_residual_m`，可以去实验室在地图上确认标定是否正确

多份导出混一起时先按位置聚类（250 m 内算同一个操场，不然两个操场会被拟成一个不
存在的大椭圆），逐点去重去跳（相邻 <5 cm 算重复，单步 >80 m 算跳点）。还配了个
`tools/recalibrate_track.py` 拿一批历史重估。

偏差先说好：样本不够一圈时朝向偏约 3.7°（采样在圆周上不对称）；只给一条直道，它会给个中心偏几十米、半径钳到 8 m 的结果，**而且不报错**。所以标完先瞄一眼 `/admin` 的摘要。

### 仿真

不让点列瞎随机。先定这一次跑起来的速度形状，别的都从它长出来。

1. 速度形状（`SpeedProfile`）：分段常速，几段跑配几段走。走段按每公里几段抽、位置带抖动，配速和
   时长各段自己抽（多数是 2–40 秒的短段，偶尔来一段 2–3 分钟），最后按时间预算把多的裁掉。这段最决定「像不像人在跑」——只给一个目标速度画出来，配速图就是条微微起伏的直线；
2. 步频 → 步幅（`tasklist_builder.plan_run`）：步频从观测区间抽，**步幅是算出来的**，
   `步幅 = 平均速度 × 60 / 步频`。反过来做（先抽步幅再算步数），步数和配速就开始互相打脸。派生值
   掉出观测带 0.82–1.32 m 就重抽步频，最多试 24 次；抽不出来就按中位步长兜底反推一个步频，所以
   偶尔还是会略微偏出去——我拿 60 个种子量过，步长 0.95–1.34 m、步频 127–162 spm，各有
   一两条踩在带外（约 1%），不是硬约束。
3. 沿跑道吐点（`generator.simulate`）：给个已跑距离，从体育场模型上取坐标和切线方向，叠横向偏移
   （人不会精确压线）、GPS 噪声（OU 过程，相关十几秒，不是逐点独立白噪声）、偶发丢点和延迟尖峰，
   按 2 秒一格出。速度噪声也压在 `max_acceleration` 之内，走段波动更小——走路是另一种步态，
   不是「跑慢一点」。
4. 挑候选（`generate_best_candidate`）：一次 8 个，各自过 `assess_physics` 打分（里程比、最大瞬时
   速度、最大加速度、时间/里程单不单调），在物理有效的里挑分最低的；一个都没有就退回全体里最好的，
   顺手把 `valid_candidate_count` 记进质量报告。

生成窗口压在 3.01–3.20 km、16–25 分钟，比 3 km/30 min 的判定线窄一截，两头留余量。打表 10 点一批、
尾批补齐、finish 前按 `isStandard` 逐条核。

## 操场的锁场

`getHomeRunInfo` 返回的 `cralist` 里，一个操场对应一个任务。但任务号在切换不同的学期时可能会发生变化，列表顺序也不固定，靠顺序去选必然挂错场

我大一下学期用上学期的操场 A 数据、拿原项目跑校园跑，出现了很多记录里轨迹位置和实际场地对不上的情况，不过目前这些记录还是合格的、我们这边还没人查过（

选任务时只看几何，打卡点围栏能包住你标定的那个中心的，才算这个操场。一个都匹配不上就直接报错停下，不退回去拿 `cralist[0]` 凑数。生成之后还会再核一遍：轨迹要经过所选场地
每个打卡点附近（容差 25 米），核不过就不生成。服务器连不上时允许生成，但会标
`verified=False`，执行时拒绝真实提交——能本地看着玩，不能往上传。

- **几何锁场**是这里自己加的。上游 `main.py` 直接把 `cralist[0]` 当第一个任务，任务号每学期一旦发生变动就可能会出现挂错场的情况
- **人脸池**：多张照片轮流激活，避免每次提交都是同一张

## 提交前的检查

三层都写在 `mcp_bridge.py` 一个地方，网页、命令行、外部接入走的是同一份代码：

1. 令牌：只有 `prepare_run` 能签发，30 分钟、一次、落盘所以跨进程也认；
2. 确认：`execute_run` 必须收到 `confirmed=True`。网页里这个 true 由确认弹窗产生；
   但要说清楚——这是**防手滑的约定**，不是服务端强制边界：服务端那层只校验同源
   （`web.py` 的 Origin / Sec-Fetch-Site 检查），自己用 `curl` 从本机发、或者在
   devtools 里手敲 fetch，是能过的。别把「我不会手敲」当成安全机制。
3. 开关：`allow_submit` 关掉，就只做本地仿真。

真正的提交跑在独立进程（`scripts/run_worker.py`）里，网页关掉也不中断。
中止会停在两批之间，走不到 finish，所以不会留下一条不完整的成绩——不管这个「中止」是从
同一个进程点的，还是从别的进程（网页、机器人）点的：跨进程那条路是写
`abort.<pid>.flag`，执行进程在每个分批间隔读它。

生成物会自己清，每次生成前先删旧的：轨迹和打表文件各留最近 10 条（`retention.py`）

- 根目录的 `config.ini` 只是提交样例而已，请使用 `yunrun/config.ini`。

- 出轨迹卡片默认拉高德卫星瓦片（`yunrun/track_card/basemap.py` 的 `DEFAULT_PROVIDER`），
  这意味着你的操场瓦片坐标会发给高德。换 provider 就行：`--provider osm`。
  （CARTO 现在要 API key，不带 key 返回的是水印瓦片，所以不推荐它。）

## 换个学校还能不能用

能。密钥和端点是云运动 App 全客户端通用的（按 3.6.6 取证），学校地址和 ID 从公共目录里点选，
令牌用 ADB 从手机里取，历史、标定、锁场、人脸、打表全都跟着服务器下发的数据走。
因校而异的地方有三个：

1. 账号密码登录的接口路径各校不同。方式 B（账号密码）失败就照自己学校的 App 抓包确认；
   方式 A（ADB 拉令牌）不走登录接口，所以我一直推荐 A
2. 打表规则不一样时：「场地核对」那一步会显示服务器下发的真实规则带，执行端 `isStandard`
   一条条核对，有一条不满足就拒绝 finish。差得多的话，改
   `yunrun/tools/tasklist_builder.py` 顶上的常量。
3. 协议会随 App 版本变，**现在就已经在变了**：主体是按 3.6.6 对的，3.6.7 的两处差异
   （`cralist` 不再下发 `runFaceStatus`、`manageList` 里 index 可能是空串）已经跟着
   [PR #86](https://github.com/Zirconium233/yunForNewVersion/pull/86) 的口径补上了
   （那个 PR 是别人提的，不是原作者）：3.6.7+ 上字段缺失不再按「未启用人脸」放行，而是回落查
   `/run/getRlStatus`，只有 `runFaceStudentStatus == 'Y'` 才放行，其余一律停在 start 之前；
   index 空串也不会再让整个 finish 挂掉。不过这两处只有离线测试（`yunrun/tests/`），我手上
   没有 3.6.7 的实机账号验过，第一次在 3.6.7 上跑之前请自己盯一眼 `/admin` 的场地核对和卡片。

## 想接给别的程序

看 `yunrun/docs/INTEGRATION.md`。业务层 `mcp_bridge.YunLocalBridge` 就是给这个准备的公开接口，
两阶段令牌跨进程通用，接到机器人框架或 MCP 客户端里不用改业务代码

