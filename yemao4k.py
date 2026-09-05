# -*- coding: utf-8 -*-
"""
夜猫4K TVBox / 影视仓 点播源 (T4 python edition, 只依赖标准库)
==============================================================
在线版: 主数据源 = 人人视频 (rrmj) 官方 API (签名直连, 内容与 App 实时同步)
        兜底数据源 = smyyds 离线片库 (yemao_library.json)
================  rrmj 在线链 (已全链路验证)  ================
签名: Base64(HmacSHA256("GET\\naliId:{did}\\nct:android\\ncv:5.27.7\\nt:{ms}\\n{url_sorted}",
                        "ES513W0B1CsdUrR13Qk5EgDAKPeeKZY"))
首页:  GET /m-station/app/page           → sections[].sectionContents[] (全站热播/新剧/电影)
榜单:  GET /m-station/top/drama/list?topId={id}&pageNum={n}&pageSize={s}  (60个榜单, 每榜50条)
搜索:  GET /search/comprehensive/precise-mixed?keywords={kw}&size=20     (fuzzySeasonList)
详情:  GET /drama/detail?dramaId={id}&isAgeLimit=0          → dramaInfo + episodeList[].sid
播放:  GET /drama/detail?dramaId={id}&isAgeLimit=0&episodeSid={sid}
        → data.watchInfo.m3u8.url = 明文 mp4 CDN 直链 (免解密, Range 206 验证通过)
免费权限: SD(高清) 可播; HD(超清)/AI_OD(4K/AI原画) 需 VIP

用法: config.json 里
  {"key":"yemao4k","name":"夜猫4K","type":3,
   "api":"<本文件URL>","searchable":1,"quickSearch":1,"filterable":1,
   "ext":"<yemao_library.json URL>"}
"""
import sys, os, json, time, random, hashlib, urllib.request, urllib.parse, ssl
import base64
import hmac
import threading
import gzip

sys.path.append('..')
try:
    from base.spider import Spider
except ImportError:
    class Spider(object):
        def init(self, extend=""):
            pass

VERSION = "2026-09-06a"

# ============================== rrmj 在线链 ==============================
RR_BASE = "https://api.rrmj.plus"
RR_SECRET = "ES513W0B1CsdUrR13Qk5EgDAKPeeKZY"
RR_CV = "5.27.7"          # android 白名单版本 (来自官方 package-url APK 文件名)
RR_CT = "android"
RR_UA = ("Dalvik/2.1.0 (Linux; U; Android 12; S905L3A Build/STTC.220815.001)")
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36")

# 榜单 topId 映射 (来自 /m-station/top/home containerList)
# 分类设计: 榜单 tab 呈现, 每个含子筛选 (全部/电影/美剧/韩剧/日剧/英剧/泰剧)
RANKS = [
    {"type_id": "rank_9",  "type_name": "热播榜"},
    {"type_id": "rank_1",  "type_name": "高分榜"},
    {"type_id": "rank_156", "type_name": "热搜榜"},
    {"type_id": "rank_213", "type_name": "飙升榜"},
    {"type_id": "rank_278", "type_name": "经典榜"},
    {"type_id": "rank_254", "type_name": "期待榜"},
    {"type_id": "rank_220", "type_name": "冷门佳作"},
    {"type_id": "rank_313", "type_name": "短剧榜"},
]
# 榜单子分类 (全部/电影/美剧/韩剧/日剧/英剧/泰剧) —— topId 由 榜单基id+偏移 计算
# 实测各榜子分类 topId (top/home 已枚举全部): 直接用完整映射表
RANK_SUBS = {
    # 高分榜: 1 全部, 3 日剧, 4 韩剧, 5 电影, 7 英剧, 8 美剧
    "rank_1":   [("全部", 1), ("日剧", 3), ("韩剧", 4), ("电影", 5), ("英剧", 7), ("美剧", 8)],
    # 热播榜: 9 全部, 11 日剧, 12 韩剧, 13 电影, 14 泰剧, 15 英剧, 16 美剧
    "rank_9":   [("全部", 9), ("日剧", 11), ("韩剧", 12), ("电影", 13), ("泰剧", 14), ("英剧", 15), ("美剧", 16)],
    # 热搜榜: 156 全部, 157 美剧, 158 韩剧, 159 日剧, 160 泰剧, 161 英剧, 169 电影
    "rank_156": [("全部", 156), ("美剧", 157), ("韩剧", 158), ("日剧", 159), ("泰剧", 160), ("英剧", 161), ("电影", 169)],
    # 飙升榜: 213 全部, 214 电影, 215 美剧, 216 韩剧, 217 泰剧, 218 日剧, 219 英剧
    "rank_213": [("全部", 213), ("电影", 214), ("美剧", 215), ("韩剧", 216), ("泰剧", 217), ("日剧", 218), ("英剧", 219)],
    # 冷门佳作榜: 220 全部, 221 美剧, 222 电影, 223 韩剧, 224 日剧, 226 英剧
    "rank_220": [("全部", 220), ("美剧", 221), ("电影", 222), ("韩剧", 223), ("日剧", 224), ("英剧", 226)],
    # 经典榜: 278 全部, 279 美剧, 280 电影, 281 韩剧, 282 日剧, 283 泰剧, 284 英剧
    "rank_278": [("全部", 278), ("美剧", 279), ("电影", 280), ("韩剧", 281), ("日剧", 282), ("泰剧", 283), ("英剧", 284)],
    # 期待榜: 254 全部, 255 电影, 256 日剧, 257 韩剧, 258 美剧, 259 泰剧
    "rank_254": [("全部", 254), ("电影", 255), ("日剧", 256), ("韩剧", 257), ("美剧", 258), ("泰剧", 259)],
    # 短剧榜: 313 总榜, 314 真人, 315 AI短剧, 316 漫剧
    "rank_313": [("总榜", 313), ("真人", 314), ("AI短剧", 315), ("漫剧", 316)],
}
# 季度榜合并进期待榜后面, 不单列 (避免分类过多)

# rrmj dramaType -> 展示名
DRAMA_TYPE_NAME = {"TV": "电视剧", "MOVIE": "电影", "PLAYLET": "短剧", "COMIC": "漫剧",
                   "VARIETY": "综艺", "DOCUMENTARY": "纪录片"}

# ============================== smyyds 离线链 ==============================
HOST_MF = "mf.smyyds.xyz"
HOST_CMS = "cms.dayuys.icu"
SALT = "d7563df33d41407f970361f176aece3b"
RC4KEY = b"GN8ZGa4DmaHQrHhSTyQ3FwnhCQt68EXQ"
SMTV_DATA = "BG74O4gb2o4IxUXd5CCxllMV45eRMjPCnde3EEirPTzoJh1spv20WeUrfy8NYdYr"
SMTV_SIGN = "cjJ1cjJjclZVVGx3ZlNXSg==\n"
AUTH = "Basic c2hlbm1hOnNoZW5tYQ=="
UA = RR_UA
BAD_URLS = ("baidu.com/diaoxian", "diaoxian.m3u8")

# 部分条目的播放地址不是内部 co_ id, 而是各大站的网页地址 -> 必须交给 TVBox 的 parses 解析。
VIP_HOSTS = (("youku.com", "youku"), ("v.qq.com", "qq"), ("iqiyi.com", "iqiyi"),
             ("mgtv.com", "mgtv"), ("bilibili.com", "bilibili"), ("le.com", "letv"),
             ("sohu.com", "sohu"), ("tudou.com", "tudou"), ("pptv.com", "pptv"),
             ("wasu.cn", "wasu"), ("1905.com", "1905"), ("acfun.cn", "acfun"))


def vip_flag(u):
    u = (u or "").lower()
    if not u.startswith("http"):
        return ""
    for h, f in VIP_HOSTS:
        if h in u:
            return f
    return ""


# 分类启发式 (离线库)
_KID = ("少儿", "0-3岁", "4-6岁", "7-10岁", "11-14岁", "早教", "儿歌", "亲子")
_DOC = ("纪录", "记录片", "纪实")
_CARTOON = ("动漫", "动画", "国漫", "日漫", "番剧")
_VARIETY = ("综艺", "真人秀", "脱口秀", "选秀", "访谈", "晚会")
_CN_AREA = ("内地", "大陆", "中国", "国产", "中国大陆", "香港", "台湾", "澳门",
            "中国香港", "中国台湾")


def classify(j):
    ty = ",".join(j.get("type") or [])
    area = ",".join(j.get("area") or [])
    try:
        ep = int(j.get("cur_episode") or 1)
    except Exception:
        ep = 1
    if any(k in ty for k in _KID):
        return "SHAOER"
    if any(k in ty for k in _DOC):
        return "JILUPIAN"
    if any(k in ty for k in _CARTOON):
        return "DONGMAN"
    if any(k in ty for k in _VARIETY):
        return "ZONGYI"
    if ep > 1:
        return "DIANSHIJU" if (any(k in area for k in _CN_AREA) or "国产" in ty) else "WAIJU"
    return "DIANYING"


OFF_CLASSES = [
    {"type_id": "DIANYING", "type_name": "电影"},
    {"type_id": "DIANSHIJU", "type_name": "电视剧"},
    {"type_id": "ZONGYI", "type_name": "综艺"},
    {"type_id": "DONGMAN", "type_name": "动漫"},
    {"type_id": "SHAOER", "type_name": "少儿"},
    {"type_id": "JILUPIAN", "type_name": "纪录片"},
    {"type_id": "WAIJU", "type_name": "外剧"},
]

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


# ============================== 基础工具 ==============================
def rc4(key, data):
    """标准 RC4: key 在前, data 在后"""
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) % 256
        S[i], S[j] = S[j], S[i]
    i = j = 0
    out = bytearray()
    for c in data:
        i = (i + 1) % 256
        j = (j + S[i]) % 256
        S[i], S[j] = S[j], S[i]
        out.append(c ^ S[(S[i] + S[j]) % 256])
    return bytes(out)


def _hexid(n):
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


# 系统代理往往会阻断 rrtv CDN —— 统一绕过代理直连
_direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_raw(url, data=None, headers=None, timeout=20, direct=False):
    """GET/POST; direct=True 绕过系统代理 (rrmj 域名在系统代理下常 502/超时)"""
    hdr = {"User-Agent": UA}
    if headers:
        hdr.update(headers)
    op = _direct_opener if direct else None

    def _do():
        req = urllib.request.Request(url, data=data, headers=hdr)
        if op is not None:
            return op.open(req, timeout=timeout).read()
        return urllib.request.urlopen(req, timeout=timeout, context=_ctx).read()

    try:
        import requests
        r = requests.request("POST" if data else "GET", url, data=data,
                             headers=hdr, timeout=timeout, verify=False,
                             proxies={"http": None, "https": None} if direct else None)
        return r.content
    except ImportError:
        pass
    except Exception:
        return b""
    try:
        return _do()
    except Exception:
        return b""


def _http_direct(url, headers=None, timeout=20):
    """直连 GET, 自动解压 gzip, 返回 (status, bytes)"""
    hdr = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
           "Accept-Encoding": "gzip"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, headers=hdr)
    try:
        with _direct_opener.open(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        if e.headers and e.headers.get("Content-Encoding") == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        return e.code, raw
    except Exception:
        return -1, b""


def jloads(raw):
    if not raw:
        return None
    s = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    cands = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    if not cands:
        return None
    try:
        return json.JSONDecoder().raw_decode(s[min(cands):])[0]
    except Exception:
        return None


def post_form(host, path, fields, scheme="http", timeout=20):
    body = urllib.parse.urlencode(fields) + "&"
    return http_raw(scheme + "://" + host + path, data=body.encode(),
                    headers={"Authorization": AUTH,
                             "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                    timeout=timeout)


# ============================== rrmj 签名请求 ==============================
_RR_DID = _hexid(16)


def rr_sign(path, params, ct=RR_CT, cv=RR_CV):
    """构造签名 headers (GET)"""
    t = int(time.time() * 1000)
    qs = urllib.parse.urlencode(sorted(params.items()))
    full_url = RR_BASE + path + "?" + qs
    msg = "GET\naliId:%s\nct:%s\ncv:%s\nt:%d\n%s" % (_RR_DID, ct, cv, t, full_url)
    sign = base64.b64encode(
        hmac.new(RR_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    return full_url, {
        "token": "", "clientVersion": cv, "clientType": ct, "cv": cv, "ct": ct,
        "deviceId": _RR_DID, "umid": _RR_DID, "aliId": _RR_DID, "uet": "9",
        "x-ca-sign": sign, "t": str(t), "User-Agent": RR_UA,
    }


def rr_get(path, params, timeout=15, retry=2):
    """rrmj 签名 GET -> data (dict/list) or None"""
    for _ in range(max(1, retry)):
        url, headers = rr_sign(path, params)
        st, raw = _http_direct(url, headers=headers, timeout=timeout)
        if st == 200 and raw:
            j = jloads(raw)
            if isinstance(j, dict) and j.get("code") == "0000":
                return j.get("data")
            # 版本过期等场景: 重置 device id 再试
            if isinstance(j, dict) and j.get("code") in ("0001",):
                global _RR_DID
                _RR_DID = _hexid(16)
        time.sleep(0.4)
    return None


# ------------------------------ rrmj 数据适配 ------------------------------
def _rr_vod(it):
    """榜单/首页条目 -> TVBox vod"""
    did = it.get("dramaId") or it.get("id")
    if not did:
        return None
    title = it.get("title") or it.get("name") or ""
    cover = it.get("cover") or it.get("coverUrl") or it.get("cover3Url") or ""
    if isinstance(cover, list) and cover:
        cover = cover[0]
    remark = []
    yr = str(it.get("year") or "")
    if yr:
        remark.append(yr)
    sc = it.get("score")
    if sc:
        try:
            if float(sc) > 0:
                remark.append(str(sc))
        except Exception:
            pass
    tot = it.get("total") or it.get("episodeCount") or it.get("count")
    if tot:
        remark.append("全%s集" % tot if str(tot).isdigit() else str(tot))
    corner = it.get("cornerMark")
    if corner:
        remark.append(str(corner))
    return {"vod_id": "rr_" + str(did),
            "vod_name": title or ("剧集" + str(did)),
            "vod_pic": cover,
            "vod_remarks": " ".join(remark) or "高清"}


def _rr_vod_from_search(it):
    """搜索结果条目 (fuzzySeasonList 元素) -> TVBox vod"""
    did = it.get("id") or it.get("dramaId")
    if not did:
        return None
    remark = []
    for k in ("year",):
        v = str(it.get(k) or "")
        if v:
            remark.append(v)
    if it.get("score"):
        try:
            if float(it.get("score")) > 0:
                remark.append(str(it.get("score")))
        except Exception:
            pass
    fee = it.get("feeMode")
    if fee == "vip":
        remark.append("VIP")
    cls = it.get("classify")
    if cls:
        remark.append(str(cls))
    return {"vod_id": "rr_" + str(did),
            "vod_name": it.get("title") or "",
            "vod_pic": it.get("cover") or "",
            "vod_remarks": " ".join(remark) or "高清"}


# ------------------------------ rrmj 在线接口 ------------------------------
def rr_home_hot():
    """首页 sections 里的剧集 (全站热播/近期开播新剧/近期上线电影)"""
    d = rr_get("/m-station/app/page", {})
    if not isinstance(d, dict):
        return []
    out, seen = [], set()
    for sec in (d.get("sections") or []):
        for c in (sec.get("sectionContents") or []):
            did = c.get("dramaId")
            if did and str(did) not in seen:
                seen.add(str(did))
                v = _rr_vod(c)
                if v:
                    out.append(v)
    return out


def rr_rank_list(top_id, page, size=20):
    """榜单分页列表"""
    d = rr_get("/m-station/top/drama/list",
               {"topId": str(top_id), "pageNum": str(page), "pageSize": str(size)})
    if not isinstance(d, dict):
        return None
    content = d.get("content") or []
    return {"list": [v for v in (_rr_vod(c) for c in content) if v],
            "total": int(d.get("total") or 0),
            "isEnd": bool(d.get("isEnd"))}


def rr_search(kw, size=20):
    """在线搜索 (fuzzySeasonList 为主, seasonList 精确匹配在前)"""
    d = rr_get("/search/comprehensive/precise-mixed",
               {"keywords": kw, "size": str(size)})
    if not isinstance(d, dict):
        return []
    out, seen = [], set()

    def _add(items):
        for it in items:
            v = _rr_vod_from_search(it)
            if v and v["vod_name"] and v["vod_id"] not in seen:
                seen.add(v["vod_id"])
                out.append(v)

    _add(d.get("seasonList") or [])      # 精确匹配
    _add(d.get("fuzzySeasonList") or [])  # 模糊匹配
    # 系列剧: 展开 seasonList (各季)
    for ser in (d.get("seriesList") or []):
        for se in (ser.get("seasonList") or []):
            v = _rr_vod_from_search({
                "id": se.get("id"), "title": "%s 第%d季" % (ser.get("name") or "", se.get("seasonNo") or 1),
                "cover": se.get("coverUrl"), "year": "", "score": se.get("score"),
                "feeMode": se.get("feeMode"), "classify": DRAMA_TYPE_NAME.get(se.get("dramaType"), ""),
            })
            if v and v["vod_id"] not in seen:
                seen.add(v["vod_id"])
                out.append(v)
    return out


def rr_detail(drama_id):
    """rrmj 详情 + 剧集列表"""
    d = rr_get("/drama/detail", {"dramaId": str(drama_id), "isAgeLimit": "0"})
    if not isinstance(d, dict):
        return None
    info = d.get("dramaInfo") or {}
    eps = d.get("episodeList") or []
    return {"info": info, "episodes": eps,
            "title": info.get("title") or d.get("title") or "",
            "intro": info.get("description") or d.get("description") or "",
            "cover": info.get("cover") or d.get("cover") or "",
            "type": info.get("dramaType") or d.get("dramaType") or "",
            "year": info.get("year") or d.get("year") or "",
            "area": info.get("producerRegion") or d.get("producerRegion") or "",
            "score": info.get("score") or d.get("score") or "",
            "playRestricted": d.get("playRestricted") or info.get("playRestricted") or 0}


def rr_play(drama_id, episode_sid):
    """rrmj 取播放直链 (明文 mp4)。
    实测: ali-cdn-video.rrtv.vip 主机稳定(206 OK);
          qn-302-cdn-local.rrtv.vip 在国内网络经常超时/460。
    所以多取几次直到拿到 ali 主机的链接 (服务端随机分配)。"""
    last = ""
    for _ in range(6):
        d = rr_get("/drama/detail",
                   {"dramaId": str(drama_id), "isAgeLimit": "0",
                    "episodeSid": str(episode_sid)})
        if not isinstance(d, dict):
            time.sleep(0.3)
            continue
        wi = d.get("watchInfo") or {}
        url = ((wi.get("m3u8") or {}).get("url") or "").strip()
        if url.startswith("http"):
            if "ali-cdn-video" in url:
                return url
            last = url
        time.sleep(0.2)
    return last


# ============================== smyyds 离线链 ==============================
_token = None
_machine = None


def mf_call(act, plain):
    fields = {"data": rc4(RC4KEY, plain.encode()).hex(),
              "sign": hashlib.md5((plain + "&" + SALT).encode()).hexdigest()}
    return jloads(post_form(HOST_MF, "//api.php?app=1&act=" + act, fields))


def mf_msg(msg):
    """msg = hex(RC4(urlencoded utf-8))"""
    try:
        s = rc4(RC4KEY, bytes.fromhex(msg)).decode("latin1")
        return urllib.parse.unquote(s, encoding="utf-8", errors="replace")
    except Exception:
        return ""


def server_time():
    j = mf_call("notice", "t=" + str(int(time.time())))
    if isinstance(j, dict) and j.get("time"):
        return str(j["time"])
    return str(int(time.time()))


def get_token():
    global _token, _machine
    if _token:
        return _token
    for _ in range(2):
        try:
            m = _hexid(16)
            st = server_time()
            mf_call("user_reg", "user=%s&password=%s&markcode=%s&t=%s" % (m, m, m, st))
            st = server_time()
            j = mf_call("user_logon", "account=%s&password=%s&markcode=%s&t=%s" % (m, m, m, st))
            if isinstance(j, dict) and j.get("code") == 200:
                tk = (json.loads(mf_msg(j.get("msg", ""))) or {}).get("token")
                if tk:
                    _token, _machine = tk, m
                    return _token
        except Exception:
            pass
        time.sleep(0.5)
    return ""


def cms_post(path, retry=3, timeout=20):
    for i in range(retry):
        fields = {"time": str(int(time.time())), "key": _hexid(20),
                  "data": SMTV_DATA, "os": "32", "sign": SMTV_SIGN}
        j = jloads(post_form(HOST_CMS, path, fields, scheme="https", timeout=timeout))
        if j:
            return j
        time.sleep(0.8 * (i + 1))
    return None


def detail_api(vid, timeout=20, retry=3):
    return cms_post("/api.php/smtv/vod/?ac=detail&ids=%s" % vid,
                    retry=retry, timeout=timeout)


def resolve_play(co_id, vod_name=""):
    """co_xxx -> 真实播放地址"""
    tk = get_token()
    if not tk:
        return ""
    m = _machine or _hexid(16)
    url = ("http://%s/Client/?url=%s&app=1&account=%s&password=%s&token=%s"
           "&machineid=%s&edition=1.0&vodname=%s&line=co&new=1&_t=%d") % (
        HOST_MF, urllib.parse.quote(co_id), m, m, tk, m,
        urllib.parse.quote(vod_name or ""), int(time.time() * 1000))
    j = jloads(http_raw(url))
    if isinstance(j, dict) and j.get("code") == 200:
        u = ((j.get("data") or {}).get("url") or "").strip()
        if u and not any(b in u for b in BAD_URLS):
            return u
    return ""


# ============================== 离线片库 ==============================
_library = []
LIB_URLS = [
    "https://raw.githubusercontent.com/dkane027/haitun/refs/heads/main/yemao_library.json",
]

SEED_JSON = r'''[{"id":344497,"t":"外八门之雪域魔窟","p":"https://m.ykimg.com/050E40005A5C3B76AD881A0664070330","c":"DIANYING","y":"2016"}]'''


def _norm(v):
    return {"id": v.get("id"),
            "title": v.get("t") or v.get("title") or "",
            "pic": v.get("p") or v.get("pic") or "",
            "state": v.get("s") or v.get("state") or "",
            "type": v.get("c") or v.get("type") or "",
            "year": v.get("y") or v.get("year") or ""}


def load_library(src):
    global _library
    src = (src or "").strip()
    if src[:1] in ("[", "{"):
        d = jloads(src.encode("utf-8", "replace"))
        if isinstance(d, list) and d:
            _library = [_norm(v) for v in d if v.get("id")]
            return _library
    cands = [s.strip() for s in src.split(";") if s.strip()] if src else []
    cands += ["yemao_library.json", "library.json"] + LIB_URLS
    for c in cands:
        raw = b""
        try:
            if c.startswith("http"):
                raw = http_raw(c, timeout=25)
            else:
                p = os.path.join(os.path.dirname(os.path.abspath(__file__)), c)
                if os.path.exists(p):
                    raw = open(p, "rb").read()
        except Exception:
            raw = b""
        d = jloads(raw)
        if isinstance(d, list) and d:
            _library = [_norm(v) for v in d if v.get("id")]
            return _library
    d = jloads(SEED_JSON.encode("utf-8"))
    _library = [_norm(v) for v in (d or []) if v.get("id")]
    return _library


def _lib_vod(v):
    return {"vod_id": str(v["id"]),
            "vod_name": v["title"] or ("片源" + str(v["id"])),
            "vod_pic": v["pic"],
            "vod_remarks": v["state"] or v["year"] or "高清"}


def build_filters():
    out = {}
    for c in OFF_CLASSES:
        tid = c["type_id"]
        ys = sorted({str(v["year"]) for v in _library
                     if v["type"] == tid and str(v["year"]).isdigit()
                     and len(str(v["year"])) == 4}, reverse=True)
        if len(ys) < 2:
            continue
        out[tid] = [{"key": "year", "name": "年份",
                     "value": [{"n": "全部", "v": ""}] +
                               [{"n": y, "v": y} for y in ys[:16]]}]
    # rrmj 榜单子分类筛选
    for r in RANKS:
        tid = r["type_id"]
        subs = RANK_SUBS.get(tid) or []
        if len(subs) > 1:
            out[tid] = [{"key": "sub", "name": "分类",
                         "value": [{"n": n, "v": str(i)} for n, i in subs]}]
    return out


# ============================== Spider 接口 ==============================
class Spider(Spider):

    def getName(self):
        return "夜猫4K"

    def getDependence(self):
        return []

    def init(self, extend=""):
        if isinstance(extend, dict):
            extend = extend.get("lib") or extend.get("site") or ""
        load_library(extend or "")
        return ""

    def isVideoFormat(self, url):
        u = (url or "").lower()
        return any(x in u for x in (".m3u8", ".mp4", ".flv", ".mkv", ".ts"))

    def manualVideoCheck(self):
        return False

    def destroy(self):
        return ""

    # ---------------- 首页 ----------------
    def homeContent(self, filter):
        hot = rr_home_hot()[:24]     # rrmj 在线首页 (与 App 同步)
        if not hot:
            # rrmj 不可用时: 用热播榜 + 离线库前 24 条兜底
            r = rr_rank_list(9, 1)
            hot = (r or {}).get("list") or []
            if not hot:
                hot = [_lib_vod(v) for v in _library[:24]]
        classes = [{"type_id": "home", "type_name": "首页"}] + RANKS + OFF_CLASSES
        return {"class": classes, "filters": build_filters(), "list": hot}

    def homeVideoContent(self):
        hot = rr_home_hot()[:40]
        if not hot:
            r = rr_rank_list(9, 1)
            hot = (r or {}).get("list") or []
        if not hot:
            hot = [_lib_vod(v) for v in _library[:40]]
        return {"list": hot}

    # ---------------- 分类页 ----------------
    def categoryContent(self, tid, pg, filter, extend):
        try:
            pg = int(pg or 1)
        except Exception:
            pg = 1
        ext = extend if isinstance(extend, dict) else {}

        # rrmj 榜单
        if tid.startswith("rank_"):
            subs = RANK_SUBS.get(tid) or [("全部", int(tid[5:]))]
            sub_id = ext.get("sub")
            top_id = int(sub_id) if sub_id else subs[0][1]
            r = rr_rank_list(top_id, pg, size=20)
            if r:
                pagecount = max(1, (r["total"] + 19) // 20)
                return {"page": pg, "pagecount": pagecount,
                        "limit": 20, "total": r["total"], "list": r["list"]}
            return {"page": pg, "pagecount": 1, "limit": 20, "total": 0, "list": []}

        # 离线库分类 (DIANYING 等)
        year = str(ext.get("year") or "").strip()
        items = [v for v in _library if not tid or v["type"] == tid]
        if year:
            items = [v for v in items if str(v["year"]) == year]
        page = items[(pg - 1) * 24: pg * 24]
        total = len(items)
        return {"page": pg, "pagecount": max(1, (total + 23) // 24),
                "limit": 24, "total": total, "list": [_lib_vod(v) for v in page]}

    # ---------------- 详情 ----------------
    def detailContent(self, ids):
        vid = str(ids[0] if isinstance(ids, list) else ids).split(",")[0].strip()
        # rrmj 在线条目
        if vid.startswith("rr_"):
            return self._rr_detail(vid[3:])
        # 离线条目
        return self._lib_detail(vid)

    def _rr_detail(self, drama_id):
        d = rr_detail(drama_id)
        if not d or not d.get("title"):
            return {"list": [{"vod_id": "rr_" + str(drama_id),
                              "vod_name": "加载失败, 请重试",
                              "vod_play_from": "夜猫4K",
                              "vod_play_url": "重试$0"}]}
        info, eps = d["info"], d["episodes"]
        arr = []
        for e in eps:
            t = (e.get("text") or e.get("title") or e.get("episodeName") or "").replace("$", "").replace("#", "")
            sid = e.get("sid")
            if sid:
                arr.append("%s$rrplay_%s_%s" % (t or ("第%s集" % e.get("episodeNo", len(arr) + 1)),
                                                drama_id, sid))
        if not arr:
            # 无剧集 (下架/区域限制): 提示而不是报错
            return {"list": [{"vod_id": "rr_" + str(drama_id),
                              "vod_name": d.get("title"),
                              "vod_pic": d.get("cover") or "",
                              "type_name": DRAMA_TYPE_NAME.get(d.get("type"), ""),
                              "vod_remarks": "暂无片源",
                              "vod_content": (d.get("intro") or "").strip() + "\n\n该内容暂无可播剧集(可能已下架或区域限制)",
                              "vod_play_from": "人人视频",
                              "vod_play_url": "暂无片源$0"}]}
        # vip 剧给提示
        fee = info.get("feeMode") or ""
        intro = (d.get("intro") or "").strip()
        if fee == "vip":
            intro = ("【免费可看高清(480P), 超清及以上需官方VIP】\n" + intro).strip()
        type_name = DRAMA_TYPE_NAME.get(d.get("type"), d.get("type") or "")
        remark = []
        if d.get("year"):
            remark.append(str(d["year"]))
        if d.get("score"):
            remark.append(str(d["score"]))
        if info.get("totalEpisode"):
            remark.append("全%s集" % info.get("totalEpisode"))
        play_url = "#".join(arr) if arr else "暂无片源$0"
        return {"list": [{
            "vod_id": "rr_" + str(drama_id),
            "vod_name": d.get("title") or ("剧集" + str(drama_id)),
            "vod_pic": d.get("cover") or "",
            "type_name": type_name,
            "vod_year": str(d.get("year") or ""),
            "vod_area": str(d.get("area") or ""),
            "vod_actor": ", ".join(info.get("actorList") or []) if isinstance(info.get("actorList"), list) else str(info.get("actorList") or ""),
            "vod_director": str(info.get("director") or ""),
            "vod_remarks": " ".join(remark) or "高清",
            "vod_content": intro,
            "vod_play_from": "人人视频",
            "vod_play_url": play_url,
        }]}

    def _lib_detail(self, vid):
        j = detail_api(vid)
        if not isinstance(j, dict) or not j.get("title"):
            v = next((x for x in _library if str(x["id"]) == vid), None)
            if not v:
                return {"list": []}
            return {"list": [{"vod_id": vid, "vod_name": v["title"], "vod_pic": v["pic"],
                              "vod_remarks": v["state"], "vod_content": "接口暂时不可用, 请稍后重试",
                              "vod_play_from": "夜猫4K", "vod_play_url": "重试$0"}]}
        froms, urls = [], []
        for src in (j.get("video_list") or []):
            eps = src.get("list") or []
            arr, flags = [], []
            for e in eps:
                t = (e.get("title") or "").replace("$", "").replace("#", "")
                u = e.get("url") or ""
                if u:
                    arr.append("%s$%s" % (t or ("第%d集" % (len(arr) + 1)), u))
                    f = vip_flag(u)
                    if f:
                        flags.append(f)
            if arr:
                nm = (src.get("name") or "线路").replace("$", "")
                if flags and len(set(flags)) == 1 and len(flags) == len(arr):
                    nm = flags[0]
                froms.append(nm)
                urls.append("#".join(arr))
        if not froms:
            froms, urls = ["夜猫4K"], ["暂无播放源$0"]
        return {"list": [{
            "vod_id": vid,
            "vod_name": j.get("title", ""),
            "vod_pic": j.get("img_url", ""),
            "type_name": ",".join(j.get("type") or []),
            "vod_year": str(j.get("pubtime") or ""),
            "vod_area": ",".join(j.get("area") or []),
            "vod_actor": ",".join(j.get("actor") or []),
            "vod_director": ",".join(j.get("director") or []),
            "vod_remarks": j.get("trunk", ""),
            "vod_content": j.get("intro", ""),
            "vod_play_from": "$$$".join(froms),
            "vod_play_url": "$$$".join(urls),
        }]}

    # ---------------- 搜索 ----------------
    def searchContent(self, key, quick, pg="1"):
        key = (key or "").strip()
        if not key:
            return {"list": []}
        # rrmj 在线搜索 (与 App 同步)
        out = rr_search(key, size=20)
        if out:
            return {"list": out}
        # 兜底: 离线库搜索
        kl = key.lower().replace(" ", "")
        lib_out = []
        for v in _library:
            t = (v["title"] or "")
            if kl in t.lower().replace(" ", ""):
                lib_out.append(_lib_vod(v))
                if len(lib_out) >= 40:
                    break
        return {"list": lib_out}

    # ---------------- 播放 ----------------
    def playerContent(self, flag, id, vipFlags):
        pid = str(id or "")
        # rrmj 在线播放: rrplay_{dramaId}_{sid}
        if pid.startswith("rrplay_"):
            parts = pid[len("rrplay_"):].split("_")
            if len(parts) >= 2:
                url = rr_play(parts[0], parts[1])
                if url:
                    return {"parse": 0, "playUrl": "", "url": url,
                            "header": {"User-Agent": RR_UA}}
            return {"parse": 0, "playUrl": "", "url": "", "header": {}}
        # 离线链: co_xxx 走夜猫自家接口拿直链
        if pid.startswith("co_"):
            url = resolve_play(pid, "")
            if url:
                return {"parse": 0, "playUrl": "", "url": url,
                        "header": {"User-Agent": WEB_UA}}
            return {"parse": 0, "playUrl": "", "url": "", "header": {}}
        # 各大站网页地址: 交给 TVBox 配置里的 parses 解析 (parse=1)
        if pid.startswith("http"):
            if vip_flag(pid):
                return {"parse": 1, "playUrl": "", "url": pid, "header": {}}
            return {"parse": 0, "playUrl": "", "url": pid,
                    "header": {"User-Agent": WEB_UA}}
        return {"parse": 0, "playUrl": "", "url": "", "header": {}}

    def localProxy(self, param):
        return [200, "text/plain", {}, ""]


# ============================== 自测入口 ==============================
if __name__ == "__main__":
    s = Spider()
    s.init("")
    print("== homeContent ==")
    home = s.homeContent(True)
    print("classes:", [c["type_name"] for c in home["class"]])
    print("hot items:", len(home.get("list") or []))
    for v in (home.get("list") or [])[:5]:
        print("  -", v["vod_id"], v["vod_name"], v["vod_remarks"])

    print()
    print("== categoryContent rank_9 (热播榜) ==")
    cat = s.categoryContent("rank_9", 1, None, {})
    print("total:", cat.get("total"), "items:", len(cat.get("list") or []))
    for v in (cat.get("list") or [])[:5]:
        print("  -", v["vod_id"], v["vod_name"], v["vod_remarks"])

    print()
    print("== searchContent 老友记 ==")
    sr = s.searchContent("老友记", False)
    print("items:", len(sr.get("list") or []))
    for v in (sr.get("list") or [])[:5]:
        print("  -", v["vod_id"], v["vod_name"], v["vod_remarks"])

    print()
    print("== detail + play 链路 (取热播榜第一个 rr_ 条目) ==")
    first_rr = next((v["vod_id"] for v in (cat.get("list") or []) if v["vod_id"].startswith("rr_")), None)
    if not first_rr:
        first_rr = next((v["vod_id"] for v in (home.get("list") or []) if v["vod_id"].startswith("rr_")), None)
    if first_rr:
        did = first_rr[3:]
        det = s.detailContent([first_rr])
        v0 = (det.get("list") or [{}])[0]
        print("title:", v0.get("vod_name"), "| type:", v0.get("type_name"), "| year:", v0.get("vod_year"))
        print("play_from:", v0.get("vod_play_from"))
        pu = (v0.get("vod_play_url") or "").split("#")
        print("episodes:", len(pu))
        if pu and pu[0] and "$" in pu[0]:
            ep1_name, ep1_id = pu[0].split("$", 1)
            print("ep1:", ep1_name, "->", ep1_id)
            pc = s.playerContent("人人视频", ep1_id, None)
            url = pc.get("url") or ""
            print("play url:", url[:100] or "(空)")
            if url:
                st, raw = _http_direct(url, headers={"User-Agent": RR_UA, "Range": "bytes=0-1023"})
                print("probe:", st, "bytes:", len(raw), "box:", raw[4:8] if len(raw) > 12 else b"")
    print()
    print("ALL DONE")
