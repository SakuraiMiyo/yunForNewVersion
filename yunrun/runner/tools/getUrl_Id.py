# -*- coding: utf-8 -*-
"""学校目录查询（旧路线 http://sports.aiyyd.com:9001/api/app/schoolList）。

修订要点：
- 模块导入时不再读取 config.ini（去除导入副作用）；缺省配置按项目根解析，可用 conf_path 覆盖。
- 头部构造、签名、信封与响应解码统一走 yun_http；不再各处复制实现。
- uuid 不再硬编码抓包值：配置 [User] uuid 非空则沿用，为空则每请求随机大写（与 3.6.6 证据一致）。
- 该端点属于“公共目录”，与学校业务服务、登录路由分开；A 阶段不改默认端点值。
"""
import configparser
import hashlib
import json
import os
import sys
from base64 import b64decode, b64encode

# 允许 `python tools/getUrl_Id.py` 直接从 tools 目录运行。
# runner/tools/ 下需要 runner/ 才能 import yun_http；config.ini 在项目根（再上一层）。
_RUNNER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _RUNNER_DIR not in sys.path:
    sys.path.insert(0, _RUNNER_DIR)
from paths import PROJECT_ROOT as _REPO_ROOT, ensure_importable  # noqa: E402

ensure_importable()

import yun_http
from yun_http import YunClient, join_url
from yun_http import encrypt_sm4, decrypt_sm4  # 兼容旧导出名

DEFAULT_CONF = os.path.join(str(_REPO_ROOT), "config.ini")

# 学校目录有两条路线（解包 3.6.6 核对，2026-10-18）：
#   新路线（当前 App 实际使用）：POST https://sports.aiyyd.com:9011/api/app/lisshtcool
#     —— API.java:639-641 声明端点；SelectUserPresenter/SelectSchoolActivity 经
#     JTRetrofitService 调用。JT 服务**不加密**：明文 JSON 体 + sign 头
#     （JTRetrofitService.java:55-90：platform/utc/uuid 树序 + md5key 签名，无 token）。
#   旧路线（早期 App 版本，上游脚本一直用的）：POST http://sports.aiyyd.com:9001/api/app/schoolList
#     —— SM4 固定信封（cipherKey=配置里的 cipherkeyencrypted）。
# 两条都试：新路线优先（与在用 App 一致），失败回退旧路线。
JT_SCHOOL_LIST_BASE = "https://sports.aiyyd.com:9011/api"
JT_SCHOOL_LIST_PATH = "/app/lisshtcool"
SCHOOL_LIST_BASE = "http://sports.aiyyd.com:9001/api"


def md5_encryption(data):
    md5 = hashlib.md5()
    md5.update(data.encode('utf-8'))
    return md5.hexdigest()


def load_conf(conf_path=None, *, required=True):
    """读取 config.ini。

    ``required=False`` 时找不到配置文件不报错，转而读同目录的
    ``config.example.ini``——学校目录这类「登录前」查询只用协议常量，
    全新 clone（还没有 config.ini）也该能列出学校。
    """
    path = conf_path or DEFAULT_CONF
    if not os.path.exists(path):
        if required:
            raise FileNotFoundError(f"配置文件不存在: {path}")
        example = os.path.join(os.path.dirname(path), "config.example.ini")
        conf = configparser.ConfigParser(interpolation=None)
        if os.path.exists(example):
            conf.read(example, encoding="utf-8-sig")
        return conf
    conf = configparser.ConfigParser(interpolation=None)
    conf.read(path, encoding="utf-8-sig")
    return conf


def _jt_school_list(conf, transport=None):
    """新路线请求（JT 明文体 + 签名头）。返回响应 dict 或 None（失败时）。

    头部集合与 JTRetrofitService 拦截器逐一对齐（无 token 头——选校发生在登录前）；
    签名公式与主协议一致：md5("platform=..&utc=..&uuid=..&appsecret=<md5key>")。
    transport 仅用于测试注入（fn(url, data, headers, timeout) -> response）。
    """
    import time
    import uuid as _uuid

    profile = yun_http.profile_from_conf(conf)
    rid = profile.uuid or str(_uuid.uuid4()).upper()
    utc = str(int(time.time()))
    sign = yun_http.md5_sign(profile.platform, utc, rid, profile.md5key)
    headers = {
        "Content-Type": "application/json",
        "isApp": "app",
        "deviceId": profile.device_id,
        "deviceName": profile.device_name,
        "version": profile.app_edition,
        "sysVersion": profile.sys_version,
        "platform": profile.platform,
        "uuid": rid,
        "utc": utc,
        "sign": sign,
    }
    url = join_url(JT_SCHOOL_LIST_BASE, JT_SCHOOL_LIST_PATH)
    try:
        if transport is not None:
            resp = transport(url=url, data="{}", headers=headers,
                             timeout=yun_http.DEFAULT_TIMEOUT)
        else:
            resp = yun_http._get_session().post(
                url=url, data="{}", headers=headers, timeout=yun_http.DEFAULT_TIMEOUT)
        if resp.status_code != 200:
            print(f"[学校目录·新路线] HTTP {resp.status_code}，回退旧路线")
            return None
        import json as _json
        return _json.loads(resp.text)
    except Exception as exc:  # noqa: BLE001 - 目录不可达不是错误：回退旧路线
        print(f"[学校目录·新路线] 不可达（{type(exc).__name__}），回退旧路线")
        return None


def _load_cached_schools():
    """读离线缓存 data/schools.json（live 拉取失败时的兜底，公共目录数据）。"""
    import json as _json
    path = os.path.join(str(_REPO_ROOT), "data", "schools.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            cache = _json.load(handle)
        schools = cache.get("schools") or []
        if isinstance(schools, list) and schools:
            return schools, str(cache.get("fetched_at") or "")
    except (OSError, ValueError):
        pass
    return None, ""


def list_schools(conf_path=None, transport=None, rng=None):
    """全量学校目录（不预设任何学校）。

    返回 (schools, source)：source ∈ {"live-jt", "live-legacy", "cache"}。
    live 双路线都不可达时回退离线缓存（data/schools.json）。
    schools 项至少含 schoolName/schoolUrl/schoolId。
    """
    conf = load_conf(conf_path, required=False)

    infojson = _jt_school_list(conf, transport=transport)
    source = "live-jt"
    if not isinstance(infojson, dict) or infojson.get('code') != 200:
        profile = yun_http.profile_from_conf(conf)
        client = YunClient(profile, base_url=SCHOOL_LIST_BASE, transport=transport, rng=rng)
        infojson = client.post_json("/app/schoolList", "", fixed_envelope=True)
        source = "live-legacy"
    if isinstance(infojson, dict) and infojson.get('code') == 200:
        schools = infojson.get('data') or []
        if schools:
            return schools, source
    cached, fetched_at = _load_cached_schools()
    if cached:
        print(f"[学校目录] live 不可达，使用离线缓存（{fetched_at}）")
        return cached, "cache"
    raise RuntimeError("学校目录不可达且没有本地缓存（data/schools.json）")


def getschool_Url_Id(schoolName, conf_path=None, transport=None, rng=None):
    """按学校名精确查 (schoolUrl, schoolId)。transport 可注入假传输（测试/dry-run）。

    先走当前 App 的新路线（app/lisshtcool，明文+签名头），失败回退旧路线
    （app/schoolList，SM4 固定信封），再不行用离线缓存。名称需与目录完全一致
    （管理前台提供全量列表点选，避免手输不一致）。
    """
    schools, _source = list_schools(conf_path=conf_path, transport=transport, rng=rng)
    for school in schools:
        if school.get('schoolName') == schoolName:
            schoolUrl = (school.get('schoolUrl') or '').rstrip('/')
            schoolId = school.get('schoolId')
            return schoolUrl, schoolId
    print("未找到匹配的学校名称")
    return None, None


def writeUrlToConfig(schoolUrl, schoolId, conf_path=None):
    path = conf_path or DEFAULT_CONF
    config = load_conf(path)
    current_school_host = config.get("Yun", "school_host", fallback="")
    current_school_id = config.get("Yun", "school_id", fallback="")
    if schoolUrl != current_school_host or str(schoolId) != current_school_id:
        print("schoolUrl:", schoolUrl)
        print("schoolId:", schoolId)
        config.set("Yun", "school_host", schoolUrl or "")
        config.set("Yun", "school_id", str(schoolId))
        with open(path, 'w', encoding='utf-8') as configfile:
            config.write(configfile)
    else:
        print("当前学校URL和ID与配置文件中的一致，无需更新。")


if __name__ == '__main__':
    schoolName = input("请输入学校名称：")
    url, schoolId = getschool_Url_Id(schoolName)
    if url is not None:
        writeUrlToConfig(url, schoolId)
