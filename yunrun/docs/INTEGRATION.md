# 外部接入指南（MCP / 机器人）

本发布版**不包含**任何机器人插件代码；机器人只是这套能力的**辅助接入方**。
所有业务都收敛在一个类上：`mcp_bridge.YunLocalBridge`。控制台网页用它，
你自己的接入层也用它——三道安全闸（令牌 / 确认位 / allow_submit）只在
bridge 一处实现，接什么宿主都绕不过去。

## 1. 核心握手：两阶段确认

一次真实提交必须分成两个调用，中间夹一个**人的明确确认**：

```python
import sys
sys.path.insert(0, r"<发布版>\yunrun")            # mcp_bridge.py 所在目录
sys.path.insert(0, r"<发布版>\yunrun\gps_track_lab\src")
from mcp_bridge import YunLocalBridge

bridge = YunLocalBridge(
    project_dir=r"<发布版>\yunrun",
    allow_submit=True,          # 不传则默认 False：只读 + 本地仿真，无法提交
    face_config={"photo": r"...jpg", "detection": r"...json", "mirror": False},  # 可选
)

# 阶段一：生成打表 + 渲染卡片。不联网，可随意调用。返回一次性 token（30 分钟）。
prepared = bridge.prepare_run()          # -> {"ok": True, "prepared": {...token...}, "card": {"png": ...}}
card_png = prepared["card"]["png"]       # 把卡片发给用户看

# ……把卡片展示给用户，得到明确同意（“确认开跑”）……

# 阶段二：用同一个 token 兑现执行。confirmed=False 一律被拒。
result = bridge.start_background_run(
    prepared["prepared"]["token"],
    confirmed=True,        # 只能来自人的确认动作，不许代码写死
    dry_run=False,         # True = 假传输全链路演练，不联网
)
state = bridge.run_state()             # 轮询进度（每批一行日志）
bridge.abort_run(token)                # 中止：停在下一批之间，不 finish
```

## 2. 只读面（不涉及提交）

| 方法 | 作用 |
|---|---|
| `status()` | 本地状态（模式 / 令牌 / 保留策略 / 人脸源） |
| `task_rules()` | 只读拉取学校任务规则（距离/步频带、是否需要人脸） |
| `stadium_status()` | 只读核对几何锁定选中的任务与原因 |
| `list_simulations()` / `simulation_detail(id)` | 本地生成物列表与详情 |
| `card_from_run(id)` | 渲染/取回轨迹卡片 PNG |
| `prune()` / `retention_status()` | 清理与保留状态 |

## 3. 跨进程共享（这是踩过坑的设计，别绕开）

令牌与执行状态**落盘**在 `tasks_sim/`（`pending_runs.json` / `active_run.json`），
文件锁 + 原子替换 + “磁盘为准”刷新：

- 网页、你的插件、独立执行进程**互相能兑现对方的令牌**；
- 令牌一次性：任一端消耗后其他端不会从内存副本“复活”它；
- 执行进度任何进程都能读；发起进程退出后状态如实报 `lost` 而不是假装在跑；
- 中止按 PID 写 `abort.<pid>.flag`，执行进程在下一批边界停下。

**执行在独立进程里**：`start_background_run` 会拉起 `../scripts/run_worker.py`
（`project_dir.parent/scripts/`，发布版目录结构已保证）。宿主（机器人框架等）重启
不会打断一条十几分钟的上传。给宿主写工具时注意：**不要同步等待执行完成**，
立刻返回“已在后台开始”，让用户轮询 `run_state()`。

## 4. 人脸源

任务 `runFaceStatus=Y` 时 runner 要求人脸源，没有就明确停止。接入层传：

```python
face_config = {"photo": "<本人照片>", "detection": "<检测标注 JSON>", "mirror": False}
```

- 标注格式见 `runner/yun_face.py` 的 `DetectionBundle`（box + 5 点 + score +
  `source_sha256` 内容绑定 + `bind_apply` 坐标系声明）；
- 检测标注的生成器：`scripts/make_face_annotation.py`（跑 App 同款 RetinaFace）；
- 管理前台的人脸库（`face_lib.py`）维护多张照片并激活一张，
  `face_lib.active_config()` 返回的就是上面这个三元组——最简单的做法是
  让用户在管理前台维护人脸，你的接入层只调 `face_lib.active_config()`。

## 5. 接入示例（速写）

不依赖任何现成框架，几行就能接（在宿主插件/服务的 `__init__` 里）：

```python
import sys
sys.path[:0] = [r"<发布版>\yunrun", r"<发布版>\yunrun\gps_track_lab\src"]
from mcp_bridge import YunLocalBridge

class MyHost:
    def __init__(self):
        self.bridge = YunLocalBridge(r"<发布版>\yunrun", allow_submit=True)

    def prepare(self):
        result = self.bridge.prepare_run()
        card = result.get("card", {}).get("png")      # 把这张图发给用户
        token = result.get("prepared", {}).get("token")
        return card, token                            # 让用户看着卡片回复“确认”

    def confirm(self, token: str, *, from_admin: bool):
        if not from_admin:                            # 确认权限收敛到管理员
            return {"ok": False, "reason": "只有管理员能确认"}
        return self.bridge.start_background_run(token, confirmed=True)
```

要点：

- **卡片必须先发出去**，确认动作必须来自看到卡片的人；
- `confirmed=True` 只能出现在用户确认分支里；
- 一次执行十几分钟：用后台执行 + 轮询，不要在工具调用里同步等待
  （多数机器人框架对单次工具调用有几十秒的硬超时，本项目按 60 秒设计）；
- 宿主重载后令牌仍在（落盘），不需要迁移。

MCP 方向同理：写一个薄薄的 FastMCP server，把 `prepare_run` /
`start_background_run` / `run_state` / `abort_run` 包成工具即可；不要复制
业务逻辑，全部委托 bridge。

## 6. 安全边界（接入方必须保留的）

1. `allow_submit` 默认 False；只在真正需要提交的接入层显式开启；
2. `confirmed=True` 应当来自人的动作（确认按钮 / 管理员指令），不要代码写死。
   注意这是**约定**不是服务端强制边界：服务端只校验同源，本机脚本自己发请求是拦不住的；
3. 卡片先于人出现——没看过轨迹就确认，等于没有确认；
4. `/ui` 与 `/admin` 只在本机回环可用；接到公网前请自行为入口加鉴权。
