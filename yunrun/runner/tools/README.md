# runner/tools 与上游 tools 的关系（vendor 说明）

`yunrun/runner/main.py` 是当脚本跑的：它把 `yunrun/runner/` 插进 `sys.path`，然后
`from tools.drift import add_drift`。为了让这条 import 能成立，这里放了一份
`tools/` 包，内容来自上游根目录的 `tools/`（基点 `59e0e25`）。

同步策略很简单，就一句话：**上游那份仍然是唯一的上游版本，这里只放跑起来需要的副本。**

| 文件 | 状态 |
|---|---|
| `drift.py`、`pace_changer.py`、`__init__.py` | 与上游逐字节相同（换行也统一成了 LF，`git diff` 为空），改上游的就行，别在这里改 |
| `Login.py` | 已改写：配置路径贯穿调用方、请求走 `yun_http`、失败返回 None 而不是 `exit()`、输出脱敏 |
| `getUrl_Id.py` | 已改写：学校目录改「新路线 + 旧路线 + 离线缓存」三条腿，`load_conf` 支持缺 config.ini 时读模板 |
| `proxy.py` | 已改写：`config.ini` 与 `configs/` 的路径推导适配「文件挪进了 `runner/tools/`」这件事 |

要重新同步上游时：先跑

```powershell
git diff --ignore-cr-at-eol 59e0e25:tools/drift.py HEAD:yunrun/runner/tools/drift.py
```

输出为空就说明还是同一份；对 `Login.py` / `getUrl_Id.py` / `proxy.py` 做同样的事，
就能看到本 fork 到底改了什么（这也是为什么我没在这些文件头加 provenance 注释：
加了之后这个 diff 就不再干净了）。

根目录那份 `tools/` 仍然是上游原样，本 fork 没有删除也没有修改它。
