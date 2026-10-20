# -*- coding: utf-8 -*-
"""云运动脚本主入口。

兼容承诺（旧调用方）：
- CLI 参数保持 --config_path/-f、--task_path/-t、--auto_run/-a、--drift/-d；
 新参数（--dry-run 等）只作为增量别名。
- set_args/default_post/getsign/encrypt_sm4/decrypt_sm4/encrypt_sm2/decrypt_sm2/
 generate_sm4 等旧函数签名保留，内部统一委托给 yun_http。
- 模块级全局变量保留为 set_args 时的“快照”，不承诺 from main import * 后
 跨模块动态同步；仓库内 history.py 已改为显式客户端注入。

路径契约：显式传入的相对 CLI 路径按调用者工作目录解析；
未显式传入的缺省资源按项目根目录解析。os.chdir 副作用保留以兼容旧习惯。
"""
import os
import sys

# 先记录调用者工作目录，再做传统 chdir。
INITIAL_CWD = os.getcwd()
# 注意：本文件现在位于 runner/，而 config.ini / tasks_* 在它的上一层（项目根）。
# 用 paths.py 统一解析，避免 dirname(__file__) 少算一层。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import PROJECT_ROOT as _PROJECT_ROOT, ensure_importable, project_path # noqa: E402

ensure_importable()
PROJECT_ROOT = str(_PROJECT_ROOT)
os.chdir(PROJECT_ROOT) # 有些人不喜欢cd到这个目录，帮他cd一下（保留：部分旧脚本依赖）

import base64
import math
import random
import time
import requests
import json
import configparser
import hashlib
from typing import List, Dict
from gmssl.sm4 import CryptSM4, SM4_ENCRYPT, SM4_DECRYPT
import sys
import gmssl.sm2 as sm2
from base64 import b64encode, b64decode
import traceback
import gzip
from tqdm import tqdm
import argparse
from tools.drift import add_drift
from gmssl import sm4, func
from Crypto.Util.Padding import pad, unpad
from tools.Login import Login

import yun_http
import yun_face
from yun_face import FaceRunStopError # noqa: F401 (main 的 except 分支使用)
from yun_http import (
  BusinessException,
  RunNotPermittedError,
  DecodeException,
  DeviceProfile,
  FakeTransport,
  FaceRequiredError,
  HttpStatusException,
  RequestContext,
  SM2Box,
  YunClient,
  decode_response,
  join_url,
  mask_secret,
  md5_sign,
  redact,
)

"""
加密模式：sm2非对称加密sm4密钥
"""
# 偏移量
# default_iv = '\1\2\3\4\5\6\7\x08' 失效

# PublicKey = 'BL7JvEAV7Wci0h5YAysN0BPNVdcUhuyJszJLRwnurav0CGftcrVcvrWeCPBIjIIBF371teRbrCS9V1Wyq7i3Arc=' # 旧公钥
PublicKey = 'BDdKFsuBf51UObke1pEgfER17biBg/5r8slqE4s8oOa8lVesWgIUxsRc+AmZ72GcuJ56f7avnyJe3CJY4n00LU4=' # 3.4.7
PrivateKey = 'P3s0+rMuY4Nt5cUWuOCjMhDzVNdom+W0RvdV6ngM+/E=' # 3.4.7 私钥失效（是否被服务端接受未经线上验证）
PUBLIC_KEY = b64decode(PublicKey)
PRIVATE_KEY = b64decode(PrivateKey)

my_host = None
default_key = None
CipherKeyEncrypted = None
my_app_edition = None
my_token = None
my_device_id = None
my_key = None
my_device_name = None
my_sys_edition = None
my_sys_version = None # A 阶段新增：统一设备版本（sysVersion 头/sysEdition 体）
my_utc = None
my_uuid = None
my_sign = None
min_distance = None
allow_overflow_distance = None
single_mileage_min_offset = None
single_mileage_max_offset = None
cadence_min_offset = None
cadence_max_offset = None
split_count = None
exclude_points = None
min_consume = None
max_consume = None
strides = None
md5key = None
platform = None

_CONF = configparser.ConfigParser(interpolation=None) # set_args 加载后的配置快照
_CLIENT: YunClient | None = None   # set_args 构建的统一客户端（构造不发网络）
_SM2_BOX: SM2Box | None = None


def resolve_cli_path(path: str) -> str:
  """显式 CLI 相对路径 => 调用者工作目录；绝对路径原样。"""
  if os.path.isabs(path):
    return path
  return os.path.normpath(os.path.join(INITIAL_CWD, path))


def project_resource(rel: str) -> str:
  """缺省资源 => 项目根目录。"""
  return os.path.normpath(os.path.join(PROJECT_ROOT, rel))


def set_args(conf_path: str):
  """加载配置：填充旧全局快照 + 构建 DeviceProfile/YunClient。不发起网络。"""
  global my_host, default_key, CipherKeyEncrypted, my_app_edition, my_token, my_device_id
  global my_key, my_device_name, my_sys_edition, my_sys_version, my_utc, my_uuid, my_sign
  global min_distance, allow_overflow_distance, single_mileage_min_offset, single_mileage_max_offset
  global cadence_min_offset, cadence_max_offset, split_count, exclude_points, min_consume, max_consume
  global strides, PUBLIC_KEY, PRIVATE_KEY, md5key, platform
  global _CONF, _CLIENT, _SM2_BOX, sm2_crypt

  resolved = resolve_cli_path(conf_path)
  if not os.path.exists(resolved):
    raise FileNotFoundError(f"配置文件不存在: {resolved}")

  conf = configparser.ConfigParser(interpolation=None)
  conf.read(resolved, encoding="utf-8-sig")
  _CONF = conf

  # 学校、keys和版本信息
  my_host = conf.get("Yun", "school_host") # 学校的host
  default_key = conf.get("Yun", "cipherkey") # 加密密钥
  CipherKeyEncrypted = conf.get("Yun", "cipherkeyencrypted") # 加密密钥的sm2加密版本
  my_app_edition = conf.get("Yun", "app_edition") # app版本

  # 用户信息，包括设备信息
  my_token = conf.get("User", 'token')
  my_device_id = conf.get("User", "device_id")
  # device_name 必须真正加载：旧版只声明全局没赋值，入口打印就 TypeError
  my_device_name = conf.get("User", "device_name", fallback="")
  my_key = conf.get("User", "map_key") # 高德地图开发者密钥
  my_sys_edition = conf.get("User", "sys_edition") # 安卓大版本（body sysEdition 旧来源）
  # sysVersion 头来自 3.6.6 证据；取值统一走设备配置，缺省沿用 sys_edition，不强制示例值
  my_sys_version = conf.get("User", "sys_version", fallback="") or my_sys_edition
  my_utc = conf.get('User', 'utc') or str(int(time.time()))
  my_uuid = conf.get("User", "uuid")
  my_sign = conf.get("User", "sign")

  # 跑步相关的信息
  min_distance = float(conf.get("Run", "min_distance"))
  allow_overflow_distance = float(conf.get("Run", "allow_overflow_distance"))
  single_mileage_min_offset = float(conf.get("Run", "single_mileage_min_offset"))
  single_mileage_max_offset = float(conf.get("Run", "single_mileage_max_offset"))
  cadence_min_offset = int(conf.get("Run", "cadence_min_offset"))
  cadence_max_offset = int(conf.get("Run", "cadence_max_offset"))
  split_count = int(conf.get("Run", "split_count"))
  exclude_points = json.loads(conf.get("Run", "exclude_points"))
  min_consume = float(conf.get("Run", "min_consume"))
  max_consume = float(conf.get("Run", "max_consume"))
  strides = float(conf.get("Run", "strides"))

  PUBLIC_KEY = b64decode(conf.get("Yun", "PublicKey"))
  PRIVATE_KEY = b64decode(conf.get("Yun", "PrivateKey"))
  _SM2_BOX = SM2Box(PUBLIC_KEY, PRIVATE_KEY)
  sm2_crypt = _SM2_BOX._crypt # 兼容旧全局名（快照），新代码请勿依赖

  md5key = conf.get("Yun", "md5key")
  platform = conf.get("Yun", "platform")

  # ：默认按 3.6.6 行为“每请求随机 UUID”；旧协议兼容开关显式配置
  legacy_uuid = str(conf.get("User", "legacy_uuid", fallback="")).strip().lower() in ("1", "true", "yes", "y")
  _CLIENT = YunClient(build_profile(conf), base_url=my_host, legacy_uuid=legacy_uuid)

  return {
    "my_token": my_token,
    "my_device_id": my_device_id,
    "my_device_name": my_device_name,
    "my_utc": my_utc,
    "my_uuid": my_uuid,
    "my_sign": my_sign,
    "my_key": my_key,
    "my_sys_version": my_sys_version,
  }


def build_profile(conf: configparser.ConfigParser) -> DeviceProfile:
  """从已加载的 ConfigParser 构造统一设备画像（统一实现位于 yun_http，此处保留旧名）。"""
  return yun_http.profile_from_conf(conf)


def default_client() -> YunClient:
  """全局客户端；外部只改了模块全局没走 set_args 时按当前全局兜底构造。"""
  global _CLIENT
  if _CLIENT is None:
    _CLIENT = YunClient(
      DeviceProfile(
        platform=platform or "android", md5key=md5key or "",
        token=my_token or "", device_id=my_device_id or "",
        device_name=my_device_name or "", app_edition=my_app_edition or "",
        sys_version=my_sys_version or my_sys_edition or "", uuid=my_uuid or "",
        cipherkey=default_key or "", cipherkey_encrypted=CipherKeyEncrypted or "",
        public_key=PUBLIC_KEY, private_key=PRIVATE_KEY,
      ),
      base_url=my_host or "",
    )
  return _CLIENT


def parse_args():
  parser = argparse.ArgumentParser(description='云运动自动跑步脚本')
  # 旧参数名一字不改；缺省 None 以便按“缺省资源=项目根”契约解析
  parser.add_argument('-f', '--config_path', type=str, default=None,
            help='配置文件路径（相对路径按调用者工作目录解析；缺省为项目根 config.ini）')
  parser.add_argument('-t', '--task_path', type=str, default=None,
            help='任务文件路径（相对路径按调用者工作目录解析；缺省为项目根 tasks_fch）')
  parser.add_argument('-a', '--auto_run', action='store_true', help='自动跑步，默认打表')
  parser.add_argument('-d', '--drift', action='store_true', help='是否添加漂移')
  parser.add_argument('--dry-run', '--dry_run', dest='dry_run', action='store_true',
            help='离线演练：本地 fixture + 假传输，不登录/不探测学校/不调高德/不 sleep/不写配置')
  parser.add_argument('--dry-home', dest='dry_home', type=str, default=None,
            help='dry-run 用 getHomeRunInfo 响应 fixture（JSON 文件路径）')
  # 人脸输入适配：只有提供了人脸源时，Y 任务才启用离线管线
  parser.add_argument('--face-photo', dest='face_photo', type=str, default=None,
            help='人脸输入：本人照片路径（人像照，非证件扫描）')
  parser.add_argument('--face-video', dest='face_video', type=str, default=None,
            help='人脸输入：自拍视频路径，确定性抽帧（需可选依赖 opencv-python）')
  parser.add_argument('--face-detection', dest='face_detection', type=str, default=None,
            help='人工标注检测结果 JSON（box+5 点+score）；缺省时取景质量门无法执行并按失败处理')
  parser.add_argument('--face-mirror', dest='face_mirror', action='store_true',
            help='声明输入已是镜像翻转（照片源默认不镜像，无前置相机标志）')
  return parser.parse_args()


def string_to_hex(input_string):
  # 将字符串转换为十六进制表示，然后去除前缀和分隔符
  hex_string = hex(int.from_bytes(input_string.encode(), 'big'))[2:].upper()
  return hex_string


def bytes_to_hex(input_string):
  # 将字符串转换为十六进制表示，然后去除前缀和分隔符
  hex_string = hex(int.from_bytes(input_string, 'big'))[2:].upper()
  return hex_string


sm2_crypt = sm2.CryptSM2(public_key=bytes_to_hex(PUBLIC_KEY[1:]), private_key=bytes_to_hex(PRIVATE_KEY), mode=1, asn1=True)


# ---------------- 加密/签名旧接口：统一委托 yun_http，签名保持不变 ----------------
def encrypt_sm4(value, SM_KEY, isBytes=False):
  return yun_http.encrypt_sm4(value, SM_KEY, is_bytes=isBytes)


def decrypt_sm4(value, SM_KEY):
  return yun_http.decrypt_sm4(value, SM_KEY)


def encrypt_sm2(info):
  box = _SM2_BOX or SM2Box(PUBLIC_KEY)
  return box.encrypt_b64(info)


def decrypt_sm2(info):
  box = _SM2_BOX or SM2Box(PUBLIC_KEY, PRIVATE_KEY)
  return box.decrypt_b64(info)


def generate_sm4():
  return yun_http.generate_sm4()


def getsign(utc, uuid):
  return md5_sign(platform, utc, uuid, md5key)


def default_post(router, data, headers=None, m_host=None, isBytes=False, gen_sign=True, key_ctx=None):
  """旧接口薄包装：返回解码后的响应文本。

  与旧行为的差异：HTTP/解码错误抛异常而不是 except 后返回密文；
  每请求上下文（uuid/utc/sign/SM4 key）在 yun_http.RequestContext 内绑定。
  """
  client = default_client()
  if headers is not None and gen_sign:
    # headers 在常规签名路径会被忽略，所以显式报错而不是静默丢
    raise ValueError("default_post(headers=...) 仅在 gen_sign=False 兼容路径生效；"
             "gen_sign=True 时头部由 RequestContext 统一构造")
  if not gen_sign:
    # 旧路线：使用配置文件里预生成的 utc/sign（登录前探测等场景）
    ctx = RequestContext(uuid=my_uuid or "", utc=my_utc, sign=my_sign,
               sm4_key_b64=default_key or yun_http.generate_sm4())
    key_bytes = base64.b64decode(ctx.sm4_key_b64)
    content = (yun_http.encrypt_sm4(data, key_bytes, is_bytes=True) if isBytes
          else yun_http.encrypt_sm4(data, key_bytes))
    envelope = {"cipherKey": CipherKeyEncrypted or client.build_envelope(ctx, "")["cipherKey"],
          "content": content}
    url = join_url(m_host or my_host, router)
    hd = headers or ctx.headers(client.profile)
    resp = client.transport(url=url, data=json.dumps(envelope), headers=hd,
                timeout=client.timeout)
    if isinstance(key_ctx, dict):
      key_ctx["key"] = ctx.sm4_key_b64
    if resp.status_code != 200:
      raise HttpStatusException(resp.status_code, url, getattr(resp, "text", ""))
    return decode_response(resp.text, key_bytes)
  text = client.post(router, json_text=data if not isBytes else "",
            raw_bytes=data if isBytes else None,
            absolute_url=join_url(m_host, router) if m_host else None)
  if isinstance(key_ctx, dict) and client.last_ctx is not None:
    key_ctx["key"] = client.last_ctx.sm4_key_b64
  return text


def noTokenLogin(conf_path: str = None):
  conf_path = conf_path or project_resource("config.ini")
  print("config中token为空，是否尝试使用账号密码登录？(y/n)")
  LoginChoice = input()
  if LoginChoice == 'y':
    login_result = Login.main(conf_path)
    if login_result is None:
      print("登录失败，请再试一次")
      return None

    token, DeviceId, DeviceName, uuid, sys_edition = login_result
    print("是否保存本次登录产生的token和uuid？(y/n)")
    TokenWrite = input()
    if TokenWrite == 'y':
      # 写回必须落到本次实际用的配置路径，不能硬编码 ./config.ini（同时跑两份会互相覆盖）
      config = configparser.ConfigParser(interpolation=None)
      config.read(conf_path, encoding='utf-8-sig')
      config.set('User', 'token', token)
      config.set('User', 'uuid', uuid)
      config.set('User', 'device_id', DeviceId)
      config.set('User', 'device_name', DeviceName)
      config.set('User', 'sys_edition', sys_edition)
      with open(conf_path, 'w+', encoding='utf-8') as f:
        config.write(f)
    return token, DeviceId, DeviceName, uuid, sys_edition
  elif LoginChoice == 'n':
    print("由于缺少token退出")
    exit()


def apply_login_result(token: str, device_id: str, device_name: str,
            uuid_value: str, sys_edition: str, conf_path: str = None):
  """登录成功后，无论用户是否选择写盘，都必须立即更新内存态。

  更新旧全局快照 + 已构建的 _CLIENT（token/设备身份/版本），并同步 Login
  阶段发现的新学校地址（Login 无条件写回 school_host，这里以实际配置文件为准
  重读），否则“登录后首个请求”仍会携带空 token 和旧 base_url。
  """
  global my_token, my_device_id, my_device_name, my_uuid, my_sys_edition
  global my_host, _CLIENT
  my_token = token
  my_device_id = device_id
  my_device_name = device_name
  my_uuid = uuid_value
  my_sys_edition = sys_edition

  # 学校地址同步：Login 内部已把探测结果写入实际配置文件，这里以文件为准重读
  conf_path = conf_path or project_resource("config.ini")
  try:
    conf = configparser.ConfigParser(interpolation=None)
    conf.read(conf_path, encoding="utf-8-sig")
    new_host = conf.get("Yun", "school_host", fallback=my_host or "")
    sys_version = conf.get("User", "sys_version", fallback="") or sys_edition
  except Exception:
    new_host = my_host or ""
    sys_version = sys_edition

  if _CLIENT is None:
    _CLIENT = default_client()
  from dataclasses import replace as _dc_replace
  _CLIENT.profile = _dc_replace(
    _CLIENT.profile,
    token=token, device_id=device_id, device_name=device_name,
    uuid=uuid_value, sys_version=sys_version,
  )
  if new_host:
    my_host = new_host
    _CLIENT.base_url = new_host


# ------------------------------------------------- 线上载荷对齐（3.6.6 APK 取证）
# 依据：module/running/model/UpPointsModel.java 与 UpPointModel.java（Gson 序列化，
# GsonUtils.getGson() 配 serializeNulls+disableHtmlEscaping：null 字段保留、字段序
# =声明序）、SportRunMapActivity.P1():2535-2600（finish 体，org.json 插入序）、
# SportRunMapActivity.startRun():4820-4825（start 体，HashMap<String,String>）。
# 线上审查发现的旧脚本明显字段差异全部在此收敛：
#  1) split 体的 "time" → APK 实为 "times"（long，秒）；
#  2) StepNumber/speeds/strides/runSteps 必须是数字（旧脚本发格式化字符串/浮点步数），
#   且 APK 内部这些量互为派生（Y1:2945-2960），脚本按同一公式派生保持自洽；
#  3) 点项只允许 UpPointModel 的 9 个字段，类型严格按 bean（旧脚本透传表格里的
#   id/runRecordId 等额外键、数字写成字符串）；
#  4) finish 体键序与取值格式按 P1()（sysEdition="Android_"+版本名；duration 字符串；
#   remake 为 BaseCheckUtil.detect 的 "score|evidence"，干净设备实发 "0|{}"）；
#  5) manageList 空时 APK 不写该键（P1:2560）。
# 已知语义偏差（脚本无传感器，无法复现真机数值，字段格式已对齐）：
#  StepNumber/runSteps 由里程/步幅估算（真机来自计步器）；simulateNum 恒 0（真机
#  统计 mock 点，脚本点全部按真实位置发送）；remake 恒 "0|{}"（复现“未检出克隆/
#  多开环境”的设备自检输出形态，不代表对运行环境的背书）。

_SPLIT_BODY_ORDER = ("StepNumber", "a", "b", "c", "cardPointList", "crsRunRecordId",
           "mileage", "orientationNum", "runSteps", "schoolId",
           "simulateNum", "speeds", "strides", "times", "userName")
_POINT_FIELD_ORDER = ("isFence", "isMock", "point", "runMileage", "runStatus",
           "runStep", "runTime", "speed", "ts")
_dropped_point_keys = set()


def _project_card_point(point):
  """把表格/任务字典投影为 UpPointModel 的 9 字段，类型严格对齐 bean。

  Rework R4：数值字段非法不再静默吞成 0——直接报错停止（同包一致性优先）。
  """
  for extra in point:
    if extra not in _POINT_FIELD_ORDER and extra not in _dropped_point_keys:
      _dropped_point_keys.add(extra)
      print(f"[wire] 点项含 APK bean 外字段，按 UpPointModel 裁剪（该键仅提示一次）: {extra!r}")

  def _num(v, cast, what):
    try:
      f = float(v)
    except (TypeError, ValueError) as exc:
      raise yun_face.FaceInputError(
        f"点字段 {what} 非数值（{v!r}）：拒绝静默按 0 处理") from exc
    if not math.isfinite(f):
      raise yun_face.FaceInputError(f"点字段 {what} 非有限数（{v!r}）")
    return int(f) if cast is int else f

  speed_raw = point.get("speed", "0.0")
  speed = speed_raw if isinstance(speed_raw, str) else format(float(speed_raw), ".2f")
  return {
    "isFence": str(point.get("isFence", "Y")),
    "isMock": bool(point.get("isMock", False)),
    "point": str(point.get("point", "")),
    "runMileage": _num(point.get("runMileage", 0), float, "runMileage"), # bean: double
    "runStatus": str(point.get("runStatus", "1")),
    "runStep": _num(point.get("runStep", 0), int, "runStep"),       # bean: int
    "runTime": _num(point.get("runTime", 0), int, "runTime"),       # bean: long
    "speed": speed,                             # bean: String
    "ts": str(point.get("ts", "")),
  }


def _build_split_body(record_id, user_name, school_id, points, strides_cfg):
  """splitPointCheating/splitPoints 体（UpPointsModel Gson 形态，声明序+null 保留）。

  Rework R4——步数派生与同包点列严格一致（Y1:2946 runStep 差值语义）：
  1) 点列自带累计步数（任一非零）→ StepNumber = 末点-首点（与点列零矛盾）；
    回退（末<首）视为输入错误，停止。
  2) 点列步数全零（脚本合成任务）→ 显式策略：按里程/步幅为每个点合成
    累计步数（同一来源派生），StepNumber 仍取点列差值——同包自洽，
    绝不出现"点列 0、汇总 1000"或两个数据源互相矛盾。
  3) 无步数又无步幅 → 报错停止（不静默补 0 再从他处生成汇总）。
  """
  pts = [_project_card_point(p) for p in points]
  mileage = pts[-1]["runMileage"] - pts[0]["runMileage"]    # 米，double
  times = pts[-1]["runTime"] - pts[0]["runTime"]        # 秒，long
  if mileage < 0:
    raise yun_face.FaceInputError(
      f"批内 runMileage 非单调（{pts[-1]['runMileage']} < {pts[0]['runMileage']}）")
  if times < 0:
    raise yun_face.FaceInputError(
      f"批内 runTime 非单调（{pts[-1]['runTime']} < {pts[0]['runTime']}）")
  s_cfg = float(strides_cfg or 0)
  if any(p["runStep"] != 0 for p in pts):
    step_number = pts[-1]["runStep"] - pts[0]["runStep"]
    if step_number < 0:
      raise yun_face.FaceInputError(
        "cardPointList runStep 回退（末点 < 首点）：输入矛盾，拒绝派生")
  elif s_cfg > 0:
    for p in pts:  # 合成累计步数（绝对里程/步幅），保证汇总=点列差值
      p["runStep"] = math.floor(p["runMileage"] / s_cfg + 0.5)
    step_number = pts[-1]["runStep"] - pts[0]["runStep"]
  else:
    raise yun_face.FaceInputError(
      "点列无累计步数且步幅(strides)未配置/非法：无法在与点列一致的前提下"
      "派生 StepNumber；拒绝静默补 0 后生成矛盾汇总")
  minutes = times / 60.0
  # 与 Y1() 同一组派生公式（Y1:2947-2959）：speeds 是配速 min/km。
  speeds = minutes / (mileage / 1000.0) if (times > 0 and mileage > 10.0) else 0.0
  run_steps = step_number / minutes if minutes != 0 else 0.0
  strides_val = (mileage / step_number) if step_number != 0 else 0.0
  body = {
    "StepNumber": step_number,                # bean: int
    "a": 0,
    "b": None,                        # serializeNulls → 显式 null
    "c": None,
    "cardPointList": pts,
    "crsRunRecordId": str(record_id),            # bean: String
    "mileage": mileage,
    "orientationNum": 0,
    "runSteps": run_steps,
    "schoolId": str(school_id),               # bean: String
    "simulateNum": 0,
    "speeds": speeds,
    "strides": strides_val,
    "times": times,                     # 键名是 times，不是 time
    "userName": str(user_name),
  }
  assert tuple(body) == _SPLIT_BODY_ORDER
  return body


def _sys_edition_field():
  """finish 体 sysEdition：APK 固定发 "Android_"+版本名（P1:2574-2577）。

  配置 sys_edition/sys_version 存裸版本名（如 "14"，头部 sysVersion 用裸值）；
  已带 Android_ 前缀的配置值原样透传，避免双前缀。
  """
  v = str(my_sys_version or my_sys_edition or "")
  if not v:
    return v
  return v if v.startswith("Android_") else "Android_" + v


def _as_text(value):
  """缺失 / null 一律当空串——App 会把未打卡点的字段写成 null，str(None) 会变成 "None"。"""
  return "" if value is None else str(value)


def _as_index(value):
  """index 容错：空串 / null / "3.0" / 非数字都兜底为 0。

  旧任务表和第三方工具会把未打卡点的 index 写成空串，原来这里是
  int(float(...))，遇到 '' 直接 ValueError，整个 finish 挂掉（上游 PR #86 第 2 条）。
  """
  try:
    return int(float(str(value).strip() or 0))
  except (TypeError, ValueError):
    return 0


def _norm_manage_list(manage_list):
  """manageList 项严格按 P1:2561-2567 的 {point, marked, index}；空则整体省略键。"""
  out = []
  for item in (manage_list or []):
    out.append({
      "point": _as_text(item.get("point")),
      "marked": _as_text(item.get("marked")),
      "index": _as_index(item.get("index")),
    })
  return out


def _build_finish_body(record_mileage_km, recode_cadence, recode_pace, recode_dislikes,
            manage_list, ra_run_area, ra_id, ra_type, record_id,
            duration_s, record_start_time):
  """finish 体（P1() org.json 插入序；全部字符串值；空 manageList 不写键）。"""
  body = {}
  ml = _norm_manage_list(manage_list)
  if ml:
    body["manageList"] = ml
  body["recordMileage"] = format(float(record_mileage_km), ".2f")
  body["recodeCadence"] = str(int(float(recode_cadence)))
  body["recodePace"] = format(float(recode_pace), ".2f")
  body["deviceName"] = str(my_device_name or "")
  body["sysEdition"] = _sys_edition_field()
  body["appEdition"] = str(my_app_edition or "")
  body["raIsStartPoint"] = "Y"
  body["raIsEndPoint"] = "Y"
  body["raRunArea"] = str(ra_run_area)
  body["recodeDislikes"] = str(int(float(recode_dislikes)))
  body["raId"] = str(ra_id)
  body["raType"] = str(ra_type)
  body["id"] = str(record_id)
  body["duration"] = str(int(round(float(duration_s))))
  body["recordStartTime"] = str(record_start_time)
  # P1:2588-2589 BaseCheckUtil.detect 的 "score|evidence"；干净设备 = "0|{}"。
  body["remake"] = "0|{}"
  return body


def _load_track_center():
  """Read the stadium calibration the trajectory generator uses.

  Kept as a dict with ``center_lon``/``center_lat`` so task selection can tell which of the
  server's tasks this calibration belongs to. A missing file yields ``{}`` -- and
  ``select_task`` then stops ("没有可用的场地标定") instead of falling back to
  ``cralist[0]``: that fallback is exactly the bug that filed one stadium's trajectory
  under another stadium's task.
  """
  try:
    path = project_path("gps_track_lab", "config", "track.json")
    with open(path, "r", encoding="utf-8") as handle:
      return json.load(handle)
  except (OSError, ValueError, NameError):
    return {}


def _fence_points(task):
  """Parse a task's ``points`` fence into ``[(lon, lat), ...]``."""
  raw = task.get("points") or ""
  out = []
  for pair in str(raw).split("|"):
    pair = pair.strip()
    if not pair or "," not in pair:
      continue
    try:
      lon, lat = pair.split(",", 1)
      out.append((float(lon), float(lat)))
    except (TypeError, ValueError):
      continue
  return out


def _app_edition_tuple(value) -> tuple:
  """把 config 里的 app_edition（如 ``3.6.6``）解析成可比较的元组；解析不了返回空元组。"""
  parts = []
  for piece in str(value or "").split("."):
    digits = "".join(ch for ch in piece if ch.isdigit())
    if not digits:
      break
    parts.append(int(digits))
  return tuple(parts)


def select_task(cralist, calibration=None):
  """Pick the task to run against, and say why.

  Locked to the calibrated stadium by instruction: the trajectory is generated around
  ``calibration.center_*``, so the only correct task is the one whose fence contains that
  centre. There is deliberately no override and no "nearest" fallback -- a list without a
  matching task is an error, because quietly running another field is exactly the defect this
  replaces (two real records were submitted against a stadium 1.47 km from the calibration).
  """
  tasks = [t for t in (cralist or []) if isinstance(t, dict)]
  if not tasks:
    raise BusinessException(None, "cralist 为空，没有可用跑步任务", None)
  if calibration is None or not calibration.get("center_lon"):
    # The calibration is what tells us which stadium the trajectory belongs to, so without it
    # there is nothing to match and guessing is precisely the defect being replaced.
    raise BusinessException(
      None,
      "没有可用的场地标定（gps_track_lab/config/track.json 的 center_lon/center_lat），"
      "无法确认该把轨迹挂到哪个任务上，已停止。", None)
  if len(tasks) == 1:
    task = tasks[0]
    pts = _fence_points(task)
    if not pts:
      raise BusinessException(
        None,
        f"只有一个任务（id={task.get('id')}）且服务端未下发围栏坐标，"
        "无法核对场地，已停止。", None)
    centre = (float(calibration.get("center_lon")), float(calibration.get("center_lat")))
    xs = [p[0] - centre[0] for p in pts]
    ys = [p[1] - centre[1] for p in pts]
    inside = min(xs) <= 0 <= max(xs) and min(ys) <= 0 <= max(ys)
    if not inside:
      # 原来这里只打印一行警告并把任务照发——「只有一个任务」并不等于「就是这个场地」，
      # 标定错了照样会把轨迹挂到别的操场上，而这是不可回滚的。与多任务分支同口径：报错。
      raise BusinessException(
        None,
        f"只有一个任务（id={task.get('id')}），但标定中心不在其围栏内："
        f"中心 {centre[0]}, {centre[1]}，围栏经度 {min(p[0] for p in pts)}~"
        f"{max(p[0] for p in pts)}、纬度 {min(p[1] for p in pts)}~{max(p[1] for p in pts)}。"
        "请核对 gps_track_lab/config/track.json 的标定，或换一个含该中心的任务。",
        None)
    return task, "只有一个任务，且标定中心在其围栏内"

  centre = (float(calibration.get("center_lon")), float(calibration.get("center_lat")))
  candidates = []
  for task in tasks:
    pts = _fence_points(task)
    if not pts:
      continue
    xs = [p[0] - centre[0] for p in pts]
    ys = [p[1] - centre[1] for p in pts]
    if min(xs) <= 0 <= max(xs) and min(ys) <= 0 <= max(ys):
      candidates.append(task)
  if not candidates:
    raise BusinessException(
      None,
      f"任务列表里没有围栏包含标定中心（{centre[0]}, {centre[1]}）的场地，"
      f"共 {len(tasks)} 个任务："
      + ", ".join(str(t.get("id")) for t in tasks)
      + "。轨迹是围绕标定生成的，挂到别的场地会落点不符；"
       "请确认 track.json 的标定，或换一个包含该中心的跑步任务。", None)
  if len(candidates) > 1:
    # Overlapping fences: pick the one whose centre is closest, and say so.
    def gap(task):
      """围栏中心到标定中心的曼哈顿距离，单位米。

      直接用「度数和」比较会偏：1 度经度在 31.7°N 只有 1 度纬度的 0.85 倍，
      所以正东 47 m 的场地会被判成比正北 53 m 的更远，选中更远的那个。
      """
      pts = _fence_points(task)
      cx = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2
      cy = (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2
      m_per_deg_lat = 111320.0
      m_per_deg_lon = m_per_deg_lat * math.cos(math.radians(centre[1]))
      return abs(cx - centre[0]) * m_per_deg_lon + abs(cy - centre[1]) * m_per_deg_lat

    best = min(candidates, key=gap)
    return best, f"标定中心落在 {len(candidates)} 个场地内，取最近的一个"
  return candidates[0], "标定中心在该任务围栏内（已锁定到标定场地）"



class Yun_For_New:

  def __init__(self, auto_generate_task=False, client: YunClient = None, home_info=None,
         face_runner=None, calibration=None):
    """home_info: getHomeRunInfo 响应 dict（离线注入用）。不传才发真实请求。

    face_runner: yun_face.FaceRunner。提供时 runFaceStatus=Y 不再直接停止，
    而是由距离窗口触发人脸执行；不提供时保持 A 阶段停止门行为。

    calibration: 轨迹标定 dict（需要 center_lon/center_lat）。留空则从项目的
    gps_track_lab/config/track.json 读取，因为轨迹就是围绕它生成的。
    """
    self.client = client or default_client()
    # No override: the calibrated stadium is the only correct target.
    self.calibration = calibration if calibration is not None else _load_track_center()
    if home_info is not None:
      obj = home_info if isinstance(home_info, dict) else json.loads(home_info)
      if obj.get("code") not in (None, 200):
        raise BusinessException(obj.get("code"), obj.get("msg", ""), obj)
      data, why = select_task(obj['data']['cralist'], self.calibration)
      print(f"[任务选择] id={data.get('id')} 区域={data.get('raRunArea')}"
         f"（{why}；标定中心 {self.calibration.get('center_lon')},"
         f"{self.calibration.get('center_lat')}）")
    else:
      obj = self.client.post_json('/run/getHomeRunInfo', '',
                    raise_on_business_code=True)
      cralist = (obj.get('data') or {}).get('cralist') or []
      if not cralist:
        raise BusinessException(obj.get('code'), 'cralist 为空，没有可用跑步任务', obj)
      # The calibration decides which stadium the trajectory was built for; taking
      # cralist[0] submitted runs against whatever task happened to be listed first.
      data, why = select_task(cralist, self.calibration)
      print(f"[任务选择] id={data.get('id')} 区域={data.get('raRunArea')}"
         f"（{why}；标定中心 {self.calibration.get('center_lon')},"
         f"{self.calibration.get('center_lat')}）")

    # 人脸开关：启用开关是任务 runFaceStatus
    # （SportRunMapActivity.java:4314），faceTime 只是窗口参数。
    # 'Y' 且无照片源 → 停止（A 阶段行为保持）。
    # 字段缺失 = 未启用（2026-10-10 按使用者确认恢复）：学校把校验关掉的那几天，
    # 服务端就不再下发这个字段。缺字段既不等于"要求核验"，也不等于"认不出的新值"。
    # 只有明确 Y 才要求人脸；非空但认不出的值仍然停止，不默认放行。
    # 展示层同一口径（mcp_bridge.py：face_status 缺失即"未启用"）。
    raw_face_status = data.get('runFaceStatus')
    face_status = str(raw_face_status).strip().upper() if raw_face_status is not None else ''
    # 3.6.7 起 cralist 不再下发 runFaceStatus（上游 PR #86）。缺字段在 3.6.6 上确实意味着
    # 「学校把校验关了」，但在 3.6.7 上不能这么理解：那会静默跳过门禁、一路绿灯跑到 finish。
    # 所以按客户端版本分流——3.6.7+ 缺字段时回落查准入状态，口径与 #86 一致：
    # 只有 runFaceStudentStatus == 'Y'（已登记且通过）才按「无需当场核验」放行，其余一律停。
    if raw_face_status is None and _app_edition_tuple(my_app_edition) >= (3, 6, 7):
      try:
        _admission = self.client.post_json(
          '/run/getRlStatus',
          json.dumps({'raRunArea': str(data.get('raRunArea') or '')}),
          raise_on_business_code=True)
      except Exception as exc: # noqa: BLE001 - 拿不到准入就当未通过
        raise FaceRequiredError(
          f"客户端版本 {my_app_edition} 的响应里没有 runFaceStatus，回落查准入状态失败"
          f"（{type(exc).__name__}: {exc}），按未通过处理，未发送 start。"
          "可先看上游 issue/PR #86。") from exc
      _adata = _admission.get('data') if isinstance(_admission, dict) else None
      _status = _adata.get('runFaceStudentStatus') if isinstance(_adata, dict) else None
      if _status == 'Y':
        face_status = 'N'
        print("[人脸] 3.6.7+ 响应无 runFaceStatus，回落 getRlStatus：已登记通过，"
           "按「无需当场核验」处理")
      else:
        _meaning = {'N': '未登记', 'N0': '需重新采集',
              'N1': '审核中'}.get(_status, '未知/缺失')
        raise FaceRequiredError(
          f"3.6.7+ 响应无 runFaceStatus，回落 getRlStatus 得到 {_meaning}"
          f"（runFaceStudentStatus={_status!r}），按未通过处理，未发送 start；"
          "请本人在官方 App 核对登记状态（见上游 PR #86）。")
    self.face_runner = face_runner
    self._face_trigger = None
    if face_status == 'Y':
      if face_runner is None:
        raise FaceRequiredError(
          "该跑步任务要求人脸核验（runFaceStatus=Y）。自动流程已停止；"
          "可使用官方 App，或提供 --face-photo/--face-video 启用离线人脸管线。")
    elif face_status not in ('N', ''):
      raise FaceRequiredError(
        f"人脸核验状态未知（runFaceStatus={raw_face_status!r}），"
        "按未通过处理，自动流程已停止。")
    self.runFaceStatus = face_status

    self.raType = data['raType']
    self.raId = data['id']
    self.strides = strides
    self.schoolId = data['schoolId']
    self.raRunArea = data['raRunArea']
    self.raDislikes = data['raDislikes']
    self.raMinDislikes = data['raDislikes']
    self.raSingleMileageMin = data['raSingleMileageMin'] + single_mileage_min_offset
    self.raSingleMileageMax = data['raSingleMileageMax'] + single_mileage_max_offset
    self.raCadenceMin = data['raCadenceMin'] + cadence_min_offset
    self.raCadenceMax = data['raCadenceMax'] + cadence_max_offset
    points = data['points'].split('|')
    if auto_generate_task:
      # 如果只要打表，完全可以不执行下面初始化代码
      self.my_select_points = ""
      with open("./map.json") as f:
        my_s = f.read()
        tmp = json.loads(my_s)
        self.my_select_points = tmp["mypoints"]
        self.my_point = tmp["origin_point"]
      for my_select_point in self.my_select_points: # 手动取点
        if my_select_point in points:
          print(my_select_point + " 存在")
        else:
          print(my_select_point + " 不存在")
          raise ValueError
      print('开始标记打卡点...')
      self.now_dist = 0
      i = 0
      while (self.now_dist / 1000 > min_distance + allow_overflow_distance) or self.now_dist == 0:
        i += 1
        print('第' + str(i) + '次尝试...')
        self.manageList: List[Dict] = [] # 列表的每一个元素都是字典
        self.now_dist = 0
        self.now_time = 0
        self.task_list = []
        self.task_count = 0
        self.myLikes = 0
        self.generate_task(self.my_select_points)
      self.now_time = int(random.uniform(min_consume, max_consume) * 60 * (self.now_dist / 1000))
      print('打卡点标记完成！本次将打卡' + str(self.myLikes) + '个点，处理' + str(len(self.task_list)) + '个点，总计'
         + format(self.now_dist / 1000, '.2f')
         + '公里，将耗时' + str(self.now_time // 60) + '分' + str(self.now_time % 60) + '秒')
      # 这三个只是初始化，并非最终值
      self.recordStartTime = ''
      self.crsRunRecordId = 0
      self.userName = ''

  def generate_task(self, points):
    for point_index, point in enumerate(points):
      if self.now_dist / 1000 < min_distance or self.myLikes < self.raMinDislikes: # 里程不足或者点不够
        self.manageList.append({
          'point': point,
          'marked': 'Y',
          'index': str(point_index)
        })
        self.add_task(point)
        self.myLikes += 1
        # 必须的任务
      else:
        self.manageList.append({
          'point': point,
          'marked': 'N',
          'index': ''
        })
        # 多余的点
    # 如果跑完了表都不够
    if self.now_dist / 1000 < min_distance:
      print("跑完了一圈关键点，长度仍然不够，会自动回跑绕圈圈")
      print('公里数不足' + str(min_distance) + '公里，将自动回跑...')
      index = 0
      while self.now_dist / 1000 < min_distance:
        self.add_task(self.manageList[index]['point'])
        index = (index + 1) % self.raDislikes

  # 每10个路径点作为一组splitPoint;
  # 若最后一组不满10个且多于1个，则将最后一组中每两个点位分取10点（含终点而不含起点），作为一组splitPoint
  # 若最后一组只有1个（这种情况只会发生在len(splitPoints) > 0），则将已插入的最后一组splitPoint的最后一个点替换为最后一组的点
  def add_task(self, point): # add_task 传一个点，开始跑
    if not self.task_list:
      origin = self.my_point
    else:
      origin = self.task_list[-1]['originPoint'] # 列表的-1项当起始点
    data = {
      'key': my_key,
      'origin': origin, # 起始点
      'destination': point # 传入的点
    }
    resp = requests.get("https://restapi.amap.com/v4/direction/bicycling", params=data,
              timeout=yun_http.DEFAULT_TIMEOUT) # A.4：外部调用同样加超时
    # 规划的点
    j = json.loads(resp.text)
    split_points = []
    split_point = []
    for path in j['data']['paths']:
      self.now_dist += path['distance'] # 路径长度
      path['steps'][-1]['polyline'] += ';' + point # 补上了一个起始点
      for step in path['steps']:
        polyline = step['polyline']
        points = polyline.split(';')
        for p in points:
          i = len(split_point)
          distForthis = self.now_dist - path['distance'] * (split_count - i) / split_count
          timeForthis = int(((min_consume + max_consume) / 2) * 60 * (self.now_dist - path['distance'] * (split_count - i)) / 1000)
          split_point.append({
            'point': p,
            'runStatus': '1',
            'speed': format((min_consume + max_consume) / 2, '.2f'),
            # 最小和最大速度之间的随机
            'isFence': 'Y',
            'isMock': False,
            "runMileage": distForthis,
            "runTime": timeForthis
          })
          if len(split_point) == split_count:
            # 到了10个，加入列表组中
            split_points.append(split_point)
            # 任务数量加一
            self.task_count = self.task_count + 1
            # 清空组
            split_point = []

    if len(split_point) > 1: # 不满10个且多于一个
      b = split_point[0]['point']
      # 上一个点坐标
      for i in range(1, len(split_point)):
        # 建立一个分割列表
        new_split_point = []
        # 保存上一个点的信息
        a = b
        b = split_point[i]['point']
        # 对a和b求坐标
        a_split = a.split(',')
        b_split = b.split(',')
        a_x = float(a_split[0])
        a_y = float(a_split[1])
        b_x = float(b_split[0])
        b_y = float(b_split[1])
        # 真就均匀等分啊
        d_x = (b_x - a_x) / split_count
        d_y = (b_y - a_y) / split_count
        # 补上10个点
        for j in range(0, split_count):
          distForthis = self.now_dist - (path['distance'] / len(split_point)) * (split_count - j) / split_count
          timeForthis = int(((min_consume + max_consume) / 2) * 60 * (self.now_dist - (path['distance'] / len(split_point)) * (split_count - j) / split_count) / 1000)
          new_split_point.append({
            'point': str(a_x + (j + 1) * d_x) + ',' + str(a_y + (j + 1) * d_y),
            'runStatus': '1',
            'speed': format((min_consume + max_consume) / 2, '.2f'),
            # 最小和最大速度之间的随机
            'isFence': 'Y',
            'isMock': False,
            "runMileage": distForthis,
            "runTime": timeForthis
          })
        split_points.append(new_split_point)
        # 最后一组被分成了 2 ~ 9 组
        self.task_count = self.task_count + 1
    elif len(split_point) == 1: # 直接把最后一个点扔进去
      split_points[-1][-1] = split_point[0] # 最后的最后点直接替换
    # 把任务列表加入
    self.task_list.append({
      'originPoint': point,
      'points': split_points
    })

  def start(self):
    if self.runFaceStatus == 'Y':
      # Registration/admission and an in-run comparison are separate results.
      admission = self.client.post_json(
        '/run/getRlStatus', json.dumps({'raRunArea': str(self.raRunArea)}),
        raise_on_business_code=True)
      adata = admission.get('data') if isinstance(admission, dict) else None
      if not isinstance(admission, dict) or admission.get('code') != 200 or not isinstance(adata, dict):
        raise FaceRequiredError('人脸登记准入响应无效，未发送 start；请在官方 App 核对审核状态。')
      status = adata.get('runFaceStudentStatus')
      if status != 'Y':
        meaning = {'N': '未登记', 'N0': '需重新采集', 'N1': '审核中'}.get(status, '未知状态') if isinstance(status, str) else '未知状态'
        raise FaceRequiredError(f'人脸登记准入未通过（{meaning}），未发送 start；请本人在官方 App 完成登记审核。')
    # APK startRun 走 HashMap<String,String>（:4821-4824）：三个字段都是字符串。
    data = {
      'raRunArea': str(self.raRunArea),
      'raType': str(self.raType),
      'raId': str(self.raId)
    }
    j = self.client.post_json('/run/start', json.dumps(data),
                 raise_on_business_code=True)
    d = j.get('data') or {}
    # APK 先保存 recordId/开始时间/人脸窗口参数，再判 canSport
    # （SportRunMapActivity.java:783-786）。即便后续拒绝/要求人脸，
    # “已经下发的 recordId”也必须留存在异常里，供处理已开始但不可继续的状态。
    self.crsRunRecordId = d.get('id')
    self.recordStartTime = d.get('recordStartTime')
    self.userName = d.get('studentId')

    # canSport 是客户端字符串状态："N"=拒绝（TextUtils.equals("N",…) :790）。
    # 业务拒绝与人脸要求是两类状态，分开抛出。
    can = str(d.get('canSport', '') or '').strip().upper()
    if can == 'N':
      raise RunNotPermittedError(
        "服务端拒绝开始跑步（canSport=N）：" + str(d.get('warnContent', '')),
        record_id=self.crsRunRecordId, raw=d)

    # ：faceTime 是窗口参数不是启用开关；开关只在 runFaceStatus。
    # APK 对 faceTime 有 10s 下界（:787-788）；Y 任务缺窗口参数不得默认通过。
    ft_raw = d.get('faceTime')
    try:
      self.faceTime = int(ft_raw) if ft_raw not in (None, '') else None
    except (TypeError, ValueError):
      self.faceTime = None
    if self.faceTime is not None and self.faceTime < yun_face.FACE_TIME_FLOOR:
      self.faceTime = yun_face.FACE_TIME_FLOOR
    self.randomList = d.get('randomList')

    if self.runFaceStatus == 'Y':
      if not self.randomList:
        raise FaceRequiredError(
          "runFaceStatus=Y 但 start 响应缺少 randomList 窗口参数，"
          "不默认放行，自动流程已停止。")
      if self.faceTime is None:
        # Rework R2：窗口必要参数缺失必须显式停止，不得按"无限预算"继续
        raise FaceRequiredError(
          "runFaceStatus=Y 但 start 响应缺少/非法 faceTime：窗口时长"
          "无法确定（截止无法设定），不默认放行，自动流程已停止。")
      self._face_trigger = yun_face.WindowTrigger(
        yun_face.windows_from_random_list(self.crsRunRecordId, self.randomList))
      print(f"云运动任务创建成功！（runFaceStatus=Y，"
         f"待触发人脸窗口 {len(self._face_trigger.windows)} 个）\n")
    else:
      if not self.runFaceStatus:
        print("提示：任务未下发 runFaceStatus 字段（按未启用人脸处理，"
           "与上游 3.6.6 分析一致；本流程不会发送人脸请求）")
      if self.faceTime not in (None, 0):
        print(f"提示：响应 faceTime={self.faceTime}，但任务 runFaceStatus="
           f"{self.runFaceStatus or '(缺失)'}；APK 逻辑人脸窗口不启用，"
           "本流程不会发送人脸请求")
      if self.faceTime is None:
        print("提示：响应未含 faceTime（任务开关为 N，按无人脸窗口处理；"
           "服务端行为未经线上验证）")
      print("云运动任务创建成功！\n")

  def _face_on_mileage(self, meters: float):
    """距离事件挂钩（返修 R1/R2）：调用方按轨迹点逐个推进，不能只喂批末里程。

    触发即执行整次人脸（语音引导→图像管线→上传状态机），全部预算共用
    单调时钟 deadline=faceTime+4s。expired/会话终止不即刻 raise，而是记入
    _face_block，由后续 split 前的守卫与 finish 检查点抛出
    FaceRunStopError——保证超时后“既不继续 split，也不 finish”；
    终端性 compare_failed 与结果未知的传输失败仍当场抛出。
    本函数对已阻断会话的后续事件只记录里程、不再处理（迟到回调丢弃）。
    APK 在人脸失败路径会走 checkRunState 自动提交（T1 :2798），本脚本
    不自动提交——该偏差显式记录。
    """
    self._max_mileage_m = max(int(getattr(self, "_max_mileage_m", 0)), int(meters))
    if getattr(self, "_face_block", None):
      return
    trigger = getattr(self, "_face_trigger", None)
    if trigger is None:
      return
    window = trigger.on_distance(meters / 1000.0)
    if window is None:
      return
    runner = self.face_runner
    if runner is None:
      raise FaceRequiredError(
        f"人脸核验窗口 {window.id_str} 已触发（runFaceStatus=Y），"
        "但未提供照片/视频源；自动流程停止，请使用官方 App 完成核验。")
    window.voice_second = self.client.now()
    print(f"[face] 窗口 {window.id_str} 触发 @ {meters / 1000.0:.3f} km")
    clock = getattr(self.client, "mono", None) or self.client.now
    outcome = runner.run_window(
      window, self.client, self.crsRunRecordId,
      session_terminated=lambda: bool(getattr(self, "_face_block", None)),
      clock=clock,
      window_seconds=float(self.faceTime or yun_face.FACE_TIME_FLOOR)
      + yun_face.WINDOW_LEAD_SECONDS)
    trigger.in_flight = False
    if outcome.state == "success":
      print("[face] 比对通过（data.status=Y），跑步继续")
    elif outcome.state == "compare_failed":
      # 策略决定（2026-10-06 与使用者确认后固定下来）：**不合格不提交**。
      # 真机 APK 会在这里 T1("faceFail") 自动提交成绩，并由客户端补上
      # appNoQualifiedReason + recordEndTime（SportRunMapActivity.java:2590-2595）。
      # 本脚本刻意不跟：宁可留一条半开记录，也不主动提交一条"人脸失败"的成绩。
      # 见 docs/ARCHITECTURE.md §7 已知偏差第 1 条——**这不是 bug，请勿顺手"修好"**。
      raise FaceRunStopError(
        f"人脸比对未通过（{outcome.msg or outcome.detail}）。"
        "APK 行为会自动提交本次跑步数据（T1/checkRunState），"
        "本脚本策略：不自动提交，保留现场由人工决定。")
    elif outcome.state == "expired":
      self._face_block = (f"{outcome.msg or outcome.detail}"
                "（窗口预算 faceTime+4s 耗尽，结果一律不采信）")
      print("[face] " + self._face_block + "；不再发送任何后续请求，也不允许 finish。")
    elif outcome.state == "session_terminated":
      self._face_block = "人脸会话已终止（迟到回调丢弃，自动流程终止）"
      print("[face] " + self._face_block)
    else:
      raise FaceRunStopError(
        f"人脸上传未确认成功（{outcome.msg or outcome.detail}）；"
        "结果未知，不要盲目重发，请先查询服务端记录。")

  def _face_guard(self, action: str):
    """返修 R2：任何后续网络动作前的阻断检查（expired/会话终止后不得继续）。"""
    blk = getattr(self, "_face_block", None)
    if blk:
      raise FaceRunStopError(f"拒绝执行『{action}』：{blk}")

  def _face_check_complete_before_finish(self):
    """返修 R1：结束前检查实际经过范围内所有必需窗口的状态；不得静默 finish。"""
    trigger = getattr(self, "_face_trigger", None)
    if trigger is None:
      return
    max_m = int(getattr(self, "_max_mileage_m", 0))
    bad = trigger.incomplete_within(max_m)
    if bad:
      desc = "; ".join(
        f"{w.id_str}({w.window_m}m: isShow={w.is_show}, upload={w.upload_success!r}, "
        f"compare={w.compare_success!r}, reason={w.reason!r})"
        for w in bad)
      raise FaceRunStopError(
        f"实际经过范围 {max_m / 1000:.3f} km 内存在 {len(bad)} 个未完成人脸窗口"
        f"（含被跳过/在途漏跨/比对未确认）：{desc}。拒绝发送 finish。")

  def _post_checked(self, router, json_text="", **kw):
    """业务 code 强制检查的统一发送口（返修 R3）。

    真机路线：YunClient.post_json(raise_on_business_code=True)。
    鸭子类型客户端：等价地自行
    解码并对 code!=200 抛 BusinessException——失败分支不因客户端形态
    而异，绝不允许“打印后继续”。
    """
    if hasattr(self.client, "post_json"):
      return self.client.post_json(router, json_text,
                     raise_on_business_code=True, **kw)
    text = self.client.post(router, json_text, **kw)
    try:
      obj = json.loads(text)
    except Exception as exc:
      raise DecodeException("响应无法解码为 JSON，业务检查无法完成") from exc
    if not isinstance(obj, dict):
      raise DecodeException("响应不是 JSON 对象，业务检查无法完成")
    if obj.get("code") != 200:
      raise BusinessException(obj.get("code"), str(obj.get("msg", "")), obj)
    return obj

  def split(self, points):
    data = _build_split_body(self.crsRunRecordId, self.userName, self.schoolId,
                 points, self.strides)
    # 特殊接口：gzip 后再 SM4（合工大抓包验证过；其他学校未知）
    # 返修 R3：HTTP 200 里的业务 code 同样解析——服务器拒绝立即停止，
    # 不再"打印后继续"。
    obj = self._post_checked("/run/splitPointCheating",
                 raw_bytes=yun_http.gzip_apk(json.dumps(data).encode("utf-8")))
    print(' ' + json.dumps({"code": obj.get("code"), "msg": obj.get("msg")},
                ensure_ascii=False))

  def do(self):
    sleep_time = self.now_time / (self.task_count + 1)
    print('等待' + format(sleep_time, '.2f') + '秒...')
    self.client.sleep(sleep_time) # 隔一段时间
    for task_index, task in enumerate(self.task_list):
      print('开始处理第' + str(task_index + 1) + '个点...') # 打卡点组
      for split_index, split in enumerate(task['points']): # 一组splitpoints （高德点10个一组）
        self.split(split) # 发送一组splitpoint （发送的高德点）
        print(' 第' + str(split_index + 1) + '次splitPoint发送成功！等待' + format(sleep_time, '.2f') + '秒...')
        self.client.sleep(sleep_time)
      print('第' + str(task_index + 1) + '个点处理完毕！')

  def do_by_points_map(self, path='./tasks', random_choose=False, isDrift=False, pick=None):
    """打表模式：把表格里的点按 split_count 分批上传。

    pick：直接指定表格文件名（无人值守调用用，例如机器人接入刚生成的那一条）。
    给定时不再交互选表；与旧参数兼容（都不给时行为不变）。
    """
    files = os.listdir(path)
    files.sort()
    if pick:
      # 只认目录内的文件名，避免越权读到目录外
      if os.path.basename(pick) != pick or pick not in files:
        raise FileNotFoundError(f"指定的表格不在目录内: {pick!r}")
      file = os.path.join(path, pick)
      print("指定表格：" + file)
    elif not random_choose:
      print("检测到可用表格：[输入-1随机选择，输入序号选择对应task]")
      print(files)
      choice = int(input("选择："))
      if choice == -1:
        file = os.path.join(path, random.choice(files))
        print("随机选择：" + file)
      else:
        file = os.path.join(path, files[choice])
    else:
      file = os.path.join(path, random.choice(files))
      print("随机选择：" + file)
    with open(file, 'r', encoding='utf-8') as f:
      self.task_map = json.loads(f.read())
    if isDrift:
      self.task_map = add_drift(self.task_map)
    points = []
    count = 0
    for point in tqdm(self.task_map['data']['pointsList'], leave=True):
      # ：loader 不得静默丢弃真机字段（runStep/ts 等），
      # 在保留原字段的基础上覆盖脚本可控项。
      point_changed = dict(point)
      point_changed.update({
        'runStatus': '1',
        'speed': point['speed'],
        # 打表，为了防止格式意外，来一个格式化
        'isFence': 'Y',
        'isMock': False,
        "runMileage": point['runMileage'],
        "runTime": point['runTime'],
        # ts 优先用点自带的时间戳（2 s 网格，与 runTime 对齐）。原来一律
        # 写成 int(time.time())，导致同一批 10 个点共享一个 ts、批间跳 20 s，
        # 与 runTime 100% 不同步——任何按 ts 交叉校验的地方都会命中。
        "ts": str(point.get("ts") or int(time.time()))
      })
      points.append(point_changed)
      count += 1
      if count == split_count:
        self._face_guard("splitPoint 批次上传")  # R2：人脸阻断后不再发任何请求
        self.split_by_points_map(points)
        self._last_confirmed_mileage_m = int(float(points[-1]['runMileage']))
        # 返修 R1：距离事件在批内逐轨迹点评估——时序仍与批次
        # 耦合（非按轨迹时间独立推进，见文档残留偏差）；
        # 首批/尾批跨窗不再依赖批末单点。
        for p in points:
          self._face_on_mileage(float(p['runMileage']))
        sleep_time = self.task_map['data']['duration'] / len(self.task_map['data']['pointsList']) * split_count
        print(f" 等待{sleep_time:.2f}秒.")
        self.client.sleep(sleep_time)
        count = 0
        points = []
    if count != 0:
      # 二返修 S1：尾批不再于批末无条件发送。APK 结束链：checkRunState
      # （API.java:343-344 映射 /run/isStandard）先发起
      # （SportRunMapActivity.java:2798 T1），b0 回调（:481-514）仅在
      # code=200 时决定 sendLastPoints 或 S1，S1（:2775）才 runToFinish。
      # 尾批在此缓存，由 finish 链在 isStandard 状态检查通过后补发。
      # 窗口距离事件照常逐点推进（时序仍与批次耦合，见文档偏差记录）。
      self._pending_tail_points = points
      for p in points:
        self._face_on_mileage(float(p['runMileage']))
      print(f"[尾批] 暂存尾部 {len(points)} 点，未在批末发送；"
         "将由 finish 链按『状态检查 → 尾批 → finish』顺序处理")

  def split_by_points_map(self, points):
    self._face_guard("splitPoint 批次上传")  # R2：任何入口直接调用同样被拦截
    data = _build_split_body(self.crsRunRecordId, self.userName, self.schoolId,
                 points, self.strides)
    # 返修 R3：业务 code 解析；服务器拒绝（如 code=500）→ BusinessException
    # 传播，do_by_points_map 立即停止，不再推进窗口/继续上传/finish。
    obj = self._post_checked("/run/splitPointCheating",
                 raw_bytes=yun_http.gzip_apk(json.dumps(data).encode("utf-8")))
    print(' ' + json.dumps({"code": obj.get("code"), "msg": obj.get("msg")},
                ensure_ascii=False))

  def finish_by_points_map(self):
    # 返修 R1/R2：finish 前必须过窗口完整性检查与会话阻断检查
    self._face_guard("finish")
    self._face_check_complete_before_finish()
    data = _build_finish_body(
      record_mileage_km=self.task_map['data']['recordMileage'],
      recode_cadence=self.task_map['data']['recodeCadence'],
      recode_pace=self.task_map['data']['recodePace'],
      recode_dislikes=self.task_map['data']['recodeDislikes'],
      manage_list=self.task_map['data']['manageList'],
      ra_run_area=self.raRunArea,
      ra_id=self.raId,
      ra_type=self.raType,
      record_id=self.crsRunRecordId,
      duration_s=self.task_map['data']['duration'],
      record_start_time=self.recordStartTime,
    )
    # 二返修 S1：状态检查（isStandard，同一 P1 体）→ 必要尾批 → finish，
    # 对齐 APK checkRunState 先行、b0 code=200 才 sendLastPoints/S1 的顺序；
    # 检查失败/未知/不支持分支 → 不发尾批也不发 finish（上方已 raise）。
    std = self._finish_state_check(data)
    tail = getattr(self, "_pending_tail_points", None)
    if tail:
      print(f"[finish链] 发送尾批 {len(tail)} 点（APK sendLastPoints 位置）...")
      self._face_guard("splitPoint 尾批上传（结束链）")
      # 先标记已消费再发送：原来在发送成功之后才清空，一旦这批抛异常（502/超时）
      # 被上层重试，同一批点会被**重复计入**（里程/步数/时间各多算一份）。
      # 漏发尾批比重复发安全：重复会污染记录且无法回收。
      self._pending_tail_points = []
      self.split_by_points_map(tail)
      self._last_confirmed_mileage_m = int(float(tail[-1]['runMileage']))
    print('发送结束信号...')
    obj = self._post_checked("/run/finish", json.dumps(data))
    print(' ' + json.dumps(redact(obj), ensure_ascii=False))
    print("[finish] 服务端已受理本次结束请求（code=200）。成绩有效性以上方 "
       "isStandard 预检呈报为准（判定语义未线上验证）")

  def finish(self):
    self._face_guard("finish")
    self._face_check_complete_before_finish()
    data = _build_finish_body(
      record_mileage_km=self.now_dist / 1000,
      recode_cadence=random.randint(self.raCadenceMin, self.raCadenceMax),
      recode_pace=self.now_time / 60 / (self.now_dist / 1000),
      recode_dislikes=self.myLikes,
      manage_list=self.manageList,
      ra_run_area=self.raRunArea,
      ra_id=self.raId,
      ra_type=self.raType,
      record_id=self.crsRunRecordId,
      duration_s=self.now_time,
      record_start_time=self.recordStartTime,
    )
    # 二返修 S1：状态检查（isStandard，同一 P1 体）→ 必要尾批 → finish，
    # 对齐 APK checkRunState 先行、b0 code=200 才 sendLastPoints/S1 的顺序；
    # 检查失败/未知/不支持分支 → 不发尾批也不发 finish（上方已 raise）。
    std = self._finish_state_check(data)
    tail = getattr(self, "_pending_tail_points", None)
    if tail:
      print(f"[finish链] 发送尾批 {len(tail)} 点（APK sendLastPoints 位置）...")
      self._face_guard("splitPoint 尾批上传（结束链）")
      # 先标记已消费再发送：原来在发送成功之后才清空，一旦这批抛异常（502/超时）
      # 被上层重试，同一批点会被**重复计入**（里程/步数/时间各多算一份）。
      # 漏发尾批比重复发安全：重复会污染记录且无法回收。
      self._pending_tail_points = []
      self.split_by_points_map(tail)
      self._last_confirmed_mileage_m = int(float(tail[-1]['runMileage']))
    print('发送结束信号...')
    obj = self._post_checked("/run/finish", json.dumps(data))
    print(' ' + json.dumps(redact(obj), ensure_ascii=False))
    print("[finish] 服务端已受理本次结束请求（code=200）。成绩有效性以上方 "
       "isStandard 预检呈报为准（判定语义未线上验证）")

  def _finish_state_check(self, data):
    """二返修 S1：结束链的 checkRunState → run/isStandard（API.java:343-344）
    在 finish 之前发起——SportRunMapActivity.java:2798（T1 先调 checkRunState）、
    b0 回调 :481-514（仅 code=200 才决定 sendLastPoints 或 S1）、S1 :2775
    （才 runToFinish）。

    处置：
    - HTTP 失败/解码失败/业务 code!=200 → 呈报停止信息并重抛：
     尾批与 finish 一律不再发送。
    - code=200 → RunStateBean 字段原样呈报（isStandard/isCheat/msg）。

    **关于 url/list（实测修正，2026-09-12）**：这里曾经把"url 或 list 非空"当成
    服务端要求后续处理的未知分支，直接停止。用真实请求打出来的响应证明那是误读：

      {"isStandard":"Y","msg":"本次跑步已满足要求",
       "url":"…/c146897….png",
       "list":[{"msg":"至少跑3.0公里","isStandard":"Y","url":"…png"},
           {"msg":"平均步频在90步/分钟和240步/分钟之间","isStandard":"Y","url":"…png"},
           {"msg":"配速应在3.3min/km和10.15min/km之间","isStandard":"Y","url":"…png"},
           {"msg":"踩点数至少3个点","isStandard":"Y","url":"…png"}]}

    也就是说 url/list 是**判定清单本身**（每项一条规则 + 一个图标 URL，APK 用它渲染
    结果对话框），服务端每次结束都会返回，并且 `list[i].isStandard` 就是该条规则的
    结论。把它当成未知分支会让每条记录都停在最后一步（尾批与 finish 都不发），
    真实发生过两次，成绩里什么都没留下。

    新的判定很窄，只有"确实是逐条全通过"才放行；任何一条为 N/缺失，或出现无法理解
    的清单形状，都照旧停止——不猜测、不把可疑结果当通过。APK 侧对应的
    `run/goonAfterStandard` 在本服务器上并不存在（实测 404），所以没有额外的确认接口
    需要调用。
    """
    try:
      obj = self._post_checked("/run/isStandard", json.dumps(data))
    except (HttpStatusException, DecodeException, BusinessException) as exc:
      print(f"[isStandard] 状态检查失败/未知（{type(exc).__name__}）："
         "不发送尾批、不发送 finish。本次未走结束链结束。")
      raise
    d = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(d, dict):
      raise FaceRunStopError("isStandard 缺少有效 data 对象；不发送尾批、不发送 finish。")
    print(f"[isStandard 预检] isStandard={d.get('isStandard')!r} "
       f"isCheat={d.get('isCheat')!r} msg={d.get('msg')!r}"
       "（字段=RunStateBean；服务端判定语义未线上验证，原样呈报）")

    if str(d.get("isCheat", "")).strip().upper() == "Y":
      raise FaceRunStopError("isStandard 报告 isCheat=Y；不发送尾批、不发送 finish。")
    if str(d.get("isStandard", "")).strip().upper() != "Y":
      raise FaceRunStopError("isStandard 未明确为 Y；不发送尾批、不发送 finish。")
    checks = d.get("list")
    if checks is not None:
      if not isinstance(checks, (list, tuple)):
        raise FaceRunStopError(
          f"isStandard 的 list 不是清单（{type(checks).__name__}），形状未知，"
          "不发送尾批、不发送 finish。")
      for index, item in enumerate(checks):
        if not isinstance(item, dict):
          raise FaceRunStopError(
            f"isStandard 清单第 {index} 项不是对象，形状未知，"
            "不发送尾批、不发送 finish。")
        verdict = str(item.get("isStandard", "")).strip().upper()
        print(f"  [{index}] {verdict or '?'} {item.get('msg')!r}")
        if verdict != "Y":
          raise FaceRunStopError(
            f"isStandard 清单第 {index} 项未通过"
            f"（isStandard={item.get('isStandard')!r} msg={item.get('msg')!r}）："
            "不发送尾批、不发送 finish。")
      if str(d.get("isCheat", "")).strip().upper() == "Y":
        raise FaceRunStopError(
          "isStandard 预检报告 isCheat=Y（服务端判定存在作弊特征）："
          "不发送尾批、不发送 finish。")
      print("[isStandard 预检] 判定清单逐条通过（含 isStandard=Y）；"
         "继续按 APK 结束链发送尾批与 finish。")
    return d


def dry_run_responder(home_fixture: dict, face_status: str = "Y",
           face_windows=(), face_compare_status: str = "Y"):
  """dry-run 假传输回包：只覆盖打表链路的只读/状态接口形态，绝不联网。"""
  def responder(router: str, envelope: dict) -> dict:
    if router.endswith("/run/getHomeRunInfo"):
      return home_fixture
    if router.endswith("/run/getRlStatus"):
      return {"code": 200, "msg": "dry-run", "data": {"runFaceStudentStatus": "Y"}}
    if router.endswith("/run/start"):
      return {"code": 200, "msg": "dry-run", "data": {
        "recordStartTime": "2000-01-01 00:00:00", "id": 900001,
        "studentId": "DRYRUN001",
        # APK 形态：canSport 为字符串（"N"=拒绝）；faceTime 为窗口参数秒
        "faceTime": 20 if face_windows else 0,
        "randomList": list(face_windows),
        "canSport": "Y"}}
    if router.endswith("/run/appFace/runFaceInfoComparison"):
      # 比对结果分层：外层 code + data.status（§二.3）
      return {"code": 200, "msg": "dry-run",
          "data": {"status": face_compare_status, "msg": "dry-run"}}
    if router.endswith("/run/finish"):
      return {"code": 200, "msg": "dry-run", "data": None}
    if router.endswith("/run/isStandard"):
      # 有效性查询（结束链）：fixture 为 RunStateBean 形态。字段值来自本地
      # 样本，不代表线上判定——run_dry 汇总行明确“服务端接受未验证”。
      return {"code": 200, "msg": "dry-run", "data": {
        "isStandard": "Y", "isCheat": "N", "msg": "dry-run", "url": None,
        "list": []}}
    # splitPointCheating 等默认成功
    return {"code": 200, "msg": "dry-run", "data": None}
  return responder


def _check_bind_apply(bundle, mirrored: bool):
  """Rework R6：标注必须声明坐标系约定，且与实际预处理一致。

  管线顺序是 EXIF 摆正 →（可选）水平镜像；标注坐标必须按处理后的图像声明，
  否则框/关键点会整体错位——不能拿处理前的标注冒充处理后的检测。
  """
  ba = bundle.bind_apply or {}
  if "after_exif" not in ba or "mirrored" not in ba:
    raise yun_face.FaceInputError(
      "标注缺少 bind_apply 坐标约定声明（需 {\"after_exif\":bool,"
      "\"mirrored\":bool}，描述相对摆正/镜像后图像的坐标系）")
  if bool(ba.get("after_exif")) is not True:
    raise yun_face.FaceInputError(
      "标注 bind_apply.after_exif=false：管线始终按 EXIF 摆正后处理，"
      "处理前坐标的标注无法安全换算，拒绝")
  if bool(ba.get("mirrored")) is not mirrored:
    raise yun_face.FaceInputError(
      f"标注 bind_apply.mirrored={ba.get('mirrored')!r} 与实际 --face-mirror="
      f"{mirrored!r} 不一致：坐标系矛盾，拒绝")


def build_face_runner(args, sleep=None, frame_provider=None, human_delay=None):
  """按 CLI 输入构建 yun_face.FaceRunner；未提供源返回 None。

  human_delay：窗口触发后、发出比对请求之前的人手反应延迟区间（秒）。
   默认 None 交给 FaceRunner 用模块默认（2.0~6.5s，随机）；dry-run 传 (0,0) 关闭。

  用 getattr 读取 face_* 参数，兼容旧调用方自造的 argparse.Namespace。

  Rework R6 + 二返修 S2（预检在任何真实网络请求之前——本函数在
  Yun 构造/start 前被调用，失败即未发出任何 start/split）：
  - 照片：标注必须按内容哈希（source_sha256）绑定该文件——路径字符串不是
   内容身份，仅 path 不算绑定；并声明 bind_apply 坐标约定。预检走完
   "解码→检测匹配→取景质量门→最终上传图像准备"，通过则缓存复用。
   无标注的照片输入直接预检失败（不再只打印提示放行到 start 之后）。
  - 视频：逐帧标注 frames + 按内容哈希绑定该视频文件（换视频沿用同帧号
   标注会被拒绝）。预检执行真实选帧链（解码→逐帧标注→质量门→压缩形态；
   注入 frame_provider 同样必经，不允许绕过预检）。人工逐帧标注模式，
   不声称自动检测（RetinaFace 未移植）。
  """
  face_photo = getattr(args, "face_photo", None)
  face_video = getattr(args, "face_video", None)
  face_mirror = bool(getattr(args, "face_mirror", False))
  if not (face_photo or face_video):
    if face_mirror:
      raise ValueError("--face-mirror 需要配合 --face-photo/--face-video 使用")
    return None
  if face_photo and face_video:
    raise ValueError("--face-photo 与 --face-video 二选一（同一时刻只有一个输入源）")
  bundle = None
  face_detection = getattr(args, "face_detection", None)
  if face_detection:
    bundle = yun_face.load_detection_bundle(resolve_cli_path(face_detection))
  else:
    raise yun_face.FaceInputError(
      "人脸源必须配合 --face-detection 标注：检测模型未移植，没有检测结果"
      "时取景质量门必然失败。二返修 S2：这种必然失败的输入不再推迟到 "
      "start 之后——预检失败，未发出任何请求")
  if face_video:
    vp = resolve_cli_path(face_video)
    if not os.path.isfile(vp):
      raise yun_face.FaceInputError(f"视频源文件不存在: {vp}（预检失败，未发出任何请求）")
    if bundle is None or not bundle.frames:
      raise yun_face.FaceInputError(
        "视频源需要逐帧检测标注（--face-detection JSON 的 frames 帧号映射）。"
        "检测模型未移植：没有逐帧标注能力时按预检失败停止，不用静态单标注"
        "冒充每帧检测。")
    _check_bind_apply(bundle, face_mirror)
    want = bundle.source_sha256
    if not want:
      raise yun_face.FaceInputError(
        "视频标注缺少 source_sha256（内容哈希）绑定：路径与帧号不是内容"
        "身份，换视频沿用同帧号标注必须被拒绝（二返修 S2）")
    got = yun_face.sha256_file(vp)
    if got != want:
      raise yun_face.FaceInputError(
        f"视频标注 source_sha256 与实际视频不符（{got[:12]}… != {want[:12]}…）："
        "错源视频标注，预检失败，未发出任何请求")
    if frame_provider is None:
      try:
        import cv2 # noqa: F401 —— 默认抽帧路径依赖，缺失必须预检失败
      except ImportError as exc:
        raise yun_face.FaceInputError(
          "视频源默认抽帧需要 opencv-python（未安装）：预检失败，未发出任何请求"
        ) from exc
    detection = dict(bundle.frames)
    source = yun_face.VideoFrameSource(vp, frame_provider=frame_provider,
                      mirror=face_mirror)
    runner = yun_face.FaceRunner(source, detection=detection,
                   sleep=sleep or time.sleep,
                   human_delay=human_delay)
    # 二返修 S2：真实选帧链预检（解码→逐帧标注匹配→取景门→最终压缩形态）。
    # 注入 provider 也必经这段预检，不允许绕过 build_face_runner 后宣称完成。
    try:
      v_img, v_meta = source.select(
        lambda img, idx: runner._gate_for_video(img, idx))
      v_face = runner.build_face_image(v_img, v_meta)
    except yun_face.FaceInputError as exc:
      raise yun_face.FaceInputError(
        f"视频源预检失败（不存在解码成功且标注/质量门通过的帧）：{exc} —— "
        "未发出任何请求") from exc
    runner.prepared = (v_img, v_meta, v_face)
    return runner
  else:
    pp = resolve_cli_path(face_photo)
    if not os.path.isfile(pp):
      raise yun_face.FaceInputError(f"照片源文件不存在: {pp}（预检失败，未发出任何请求）")
    with open(pp, "rb") as f:
      raw = f.read()
    yun_face.verify_photo_binding(bundle, raw, pp)
    _check_bind_apply(bundle, face_mirror)
    source = yun_face.PhotoSource(pp, mirror=face_mirror)
    runner = yun_face.FaceRunner(source, detection=bundle.static,
                   sleep=sleep or time.sleep,
                   human_delay=human_delay)
    # 二返修 S2：完整预检——解码/EXIF/镜像 → 标注匹配 → 取景质量门 →
    # 最终上传图像准备；任何一步失败都在 start 之前停止，通过则缓存复用。
    try:
      p_img, p_meta = source.load()
      p_face = runner.build_face_image(p_img, p_meta)
    except yun_face.FaceInputError as exc:
      raise yun_face.FaceInputError(
        f"照片源预检失败：{exc} —— 未发出任何请求") from exc
    runner.prepared = (p_img, p_meta, p_face)
    return runner


def run_dry(cfg_path: str, task_path: str, args):
  """离线演练：本地 fixture + FakeTransport；不登录、不探测学校、不调高德、不 sleep、不写配置。"""
  home_path = args.dry_home or (_CONF.get("Run", "dry_home", fallback="") or None)
  home_path = resolve_cli_path(home_path) if home_path else project_resource("dry_run_home.json")
  if not os.path.exists(home_path):
    raise FileNotFoundError(
      f"dry-run 需要本地 getHomeRunInfo fixture：{home_path}"
      "（可用 --dry-home 指定）。dry-run 禁止构造时联网，故不回退真实请求。")
  with open(home_path, 'r', encoding='utf-8') as f:
    home_fixture = json.load(f)

  # 人脸演练参数从 fixture 读取（脱敏离线样本自有字段）
  hdata = (home_fixture.get('data') or {})
  hstatus = ""
  try:
    hstatus = str(hdata['cralist'][0].get('runFaceStatus', '')).strip().upper()
  except (KeyError, IndexError, TypeError):
    pass
  face_windows = list(hdata.get('dry_face_windows') or []) if hstatus == 'Y' else []
  face_compare = str(hdata.get('dry_face_compare_status', 'Y'))

  profile = build_profile(_CONF)
  fake = FakeTransport(
    dry_run_responder(home_fixture, face_windows=face_windows,
             face_compare_status=face_compare),
    sm2box=SM2Box(PUBLIC_KEY, PRIVATE_KEY),
    fixed_pair=((profile.cipherkey_encrypted, profile.cipherkey)
          if profile.cipherkey_encrypted else None),
    # 仓库默认密钥对公私密钥不配对（config 注明“私钥失效”），本地解不开随机封装；
    # 配置了固定 cipherkey 时以其兜底构造回包，与线上“服务端持配对私钥”等效。
    fallback_key_b64=profile.cipherkey or None,
  )
  client = YunClient(profile, base_url=my_host, transport=fake,
            rng=random.Random(20260911), sleep=lambda s: None)
  # dry-run 里显式关掉"人手反应延迟"：它本来就是假 sleep（不耗时），但会取随机数、
  # 打日志，让演练输出带上与线上无关的抖动。演练要的是确定性。
  face_runner = build_face_runner(args, sleep=lambda s: None, human_delay=(0.0, 0.0))
  try:
    yun = Yun_For_New(auto_generate_task=False, client=client, home_info=None,
             face_runner=face_runner)
    yun.start()
    yun.do_by_points_map(path=task_path, random_choose=True, isDrift=args.drift)
    yun.finish_by_points_map()
  except FaceRequiredError as e:
    print("[dry-run 停止门] " + str(e))
    raise
  routers = [c["router"] for c in fake.calls]
  verified = sum(1 for c in fake.calls if c.get("envelope_verified"))
  undec = sum(1 for c in fake.calls if "_undecodable" in (c.get("business") or {}))
  print(f"[dry-run 完成] 共构造 {len(fake.calls)} 个请求，全部走假传输（无真实网络）：{routers}")
  print(f"[dry-run 信封] 可用持有私钥直接验证的信封 {verified}/{len(fake.calls)}；"
     f"业务体不可解码 {undec} 个（兜底 key 路线下按等效假设构造回包，不宣称信封已验证）")
  print("[dry-run 声明] 以上仅证明本地构造与静态证据一致；"
     "服务端是否接受、成绩是否有效、人脸是否通过均未验证。")
  return client # 供测试/审阅者检查 fake.calls


def main(run=True):
  args = parse_args()
  cfg_path = resolve_cli_path(args.config_path) if args.config_path else project_resource("config.ini")
  task_path = resolve_cli_path(args.task_path) if args.task_path else project_resource("tasks_fch")

  # 设置全局变量（快照）并构建统一客户端
  user_info = set_args(cfg_path)
  global my_token, my_device_id, my_device_name, my_uuid, my_sys_edition

  if args.dry_run:
    # dry-run 不登录、不探测、不 sleep、不写配置
    run_dry(cfg_path, task_path, args)
    return

  if not args.auto_run:
    if len(my_token) == 0:
      result = noTokenLogin(cfg_path)
      if result is None:
        return
      # 登录后同步内存客户端（含新学校地址），不只更新全局值
      apply_login_result(*result, conf_path=cfg_path)

    print("确定数据无误：")
  # A.4：默认输出脱敏（token/uuid/sign/map_key 掩码）
  print("Token: ".ljust(15) + mask_secret(my_token))
  print('deviceId: '.ljust(15) + my_device_id)
  print('deviceName: '.ljust(15) + my_device_name)
  print('utc: '.ljust(15) + my_utc)
  print('uuid: '.ljust(15) + mask_secret(my_uuid))
  print('sign: '.ljust(15) + mask_secret(my_sign))
  print('map_key: '.ljust(15) + mask_secret(my_key))

  if not run:
    return

  if args.auto_run:
    sure = 'y'
  else:
    sure = input("确认：[y/n]")
  Yun = None  # 返修 R3：异常分支需要报告 recordId/最后确认位置（未构造时保持 None）
  try:
    if sure == 'y':
      if args.auto_run:
        print_table = 'y'
      else:
        print_table = input("打表模式(固定路线，无需高德地图key)：[y/n]")
      if print_table == 'y':
        if not args.auto_run:
          print("warning:\n打表模式下\n跑步的步频、配速等信息受tasklist.json控制，不会读取map.json，config.ini的跑步信息失效")
          choice = input("请选择校区（1.翡翠湖校区,2.屯溪路校区,3.宣城校区,4.自定义(文件夹tasks_else)）")
          if (choice == '1'):
            path = "./tasks_fch"
          elif (choice == '2'):
            path = "./tasks_txl"
          elif (choice == '3'):
            path = "./tasks_xc"
          else:
            path = "./tasks_else"
          isDrift = input("是否为数据添加漂移：[y/n]")
          if isDrift == 'y':
            driftChoice = True
          else:
            driftChoice = False
          Yun = Yun_For_New(auto_generate_task=False,
                   face_runner=build_face_runner(args))
          Yun.start()
          Yun.do_by_points_map(path=path, isDrift=driftChoice)
          Yun.finish_by_points_map()
        else:
          path = task_path
          Yun = Yun_For_New(auto_generate_task=False,
                   face_runner=build_face_runner(args))
          Yun.start()
          Yun.do_by_points_map(path=path, random_choose=True, isDrift=args.drift)
          Yun.finish_by_points_map()
      else:
        quick_model = input("快速模式(瞬间跑完)：[y/n]")
        if quick_model == 'y':
          Yun = Yun_For_New()
          Yun.start()
          Yun.finish()
        else:
          Yun = Yun_For_New()
          print("起始点：[" + Yun.my_point + ']')
          Yun.start()
          Yun.do()
          Yun.finish()
    else:
      print("退出。")
  except RunNotPermittedError as e:
    # 业务拒绝 ≠ 人脸要求；recordId 已下发时明示，便于查服务端记录
    rid = f"（已下发 recordId={e.record_id}）" if e.record_id else "（无 recordId）"
    print(f"[业务拒绝] {e} {rid}")
    print("[业务拒绝] 请先在服务端确认该记录状态，不要盲目重发开始请求。")
  except FaceRunStopError as e:
    print("[人脸停止] " + str(e))
  except FaceRequiredError as e:
    print("[停止] " + str(e))
    print("[停止] A 阶段策略：识别人脸要求后不模拟、不猜测，请使用官方 App 完成核验。")
  except BusinessException as e:
    # 返修 R3：服务器明确拒绝（HTTP 200 业务 code!=200）= 终止性结果，
    # 后续 split/finish 不再发送；保留 recordId 与最后确认位置供查询。
    rid = getattr(Yun, "crsRunRecordId", None)
    pos = getattr(Yun, "_last_confirmed_mileage_m", None)
    pos_s = (f"最后确认位置≈{pos / 1000:.3f}km（末个已获 200 确认的批）"
         if isinstance(pos, int) else "尚无已确认批次位置")
    print(f"[业务拒绝] {e}")
    print(f"[业务拒绝] 后续上传/finish 均未发送。recordId={rid!r}，{pos_s}；"
       "请先查询服务端记录状态再决定下一步，不要盲目重发。")
  except (HttpStatusException, DecodeException) as e:
    # 状态未知时保留现场、报告，而不是继续 finish 或自动重发
    rid = getattr(Yun, "crsRunRecordId", None)
    pos = getattr(Yun, "_last_confirmed_mileage_m", None)
    pos_s = (f"最后确认位置≈{pos / 1000:.3f}km（末个已获确认的批）"
         if isinstance(pos, int) else "尚无已确认批次位置")
    print("[失败] " + str(e))
    print(f"[失败] 请求结果可能未定（状态未知），不要盲目重发；recordId={rid!r}，"
       f"{pos_s}；请先查询服务端记录再决定。")
  except Exception as e:
    print("跑步失败了，错误信息：")
    print(e)
    input()


if __name__ == '__main__':
  main()
