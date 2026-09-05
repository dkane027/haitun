# -*- coding: utf-8 -*-
"""
夜猫4K TVBox / 影视仓 点播源 (T5 python edition, 只依赖标准库)
==============================================================
真正的在线版: 主数据源 = 神马(smyyds)家族正式接口 (与夜猫4K App 完全同源)
  - 内容: xcms.zxyy123.top   (全站 12 万+ 条, list/detail/flitter 实时同步)
  - 解析: mf.smyyds.xyz      (注册设备→token→/Client/ 解析出 m3u8 直链)
兜底数据源 = rrmj 官方 API (人人, 外剧分类线)
================  神马链 (本次抓包逆向, 全链路实测通过)  ================
POST http://xcms.zxyy123.top//api.php/smtv/vod/?ac=list&class={cls}&page={n}
  可选: &year= &sort=Hotdesc/updatedesc &type= &area=
  分类: tvplay 电视剧 / tvshow 综艺 / dongman 动漫 / jilupian 纪录片 /
        dianying 电影(?) / 外据 外剧(人人,9582条) / ...
  响应: 明文 JSON 或 base64 → RC4(b64decode, MD5D) 解密
  flitter: ac=flitter&class={cls} → 类型/年份/地区筛选条件
POST ac=detail&ids={vid} → 明文 JSON (title/intro/actor/area/type/video_list[])
  video_list[].name = 线路名("极速丨奇"等), list[].url = 爱奇艺/腾讯/芒果网页 或 co_xxx
登录: POST mf.smyyds.xyz//api.php?app=1&act=user_reg + user_logon → {"token":...}
  data=RC4(plain, RC4KEY).hex (注意顺序: rc4(明文,key)); sign=md5(plain+"&"+SALT)
播放: GET mf.smyyds.xyz/Client/?url={网页或co_}&account={m}&token={token}&line={line}
  line=co(内链)/qq(爱奇艺)/mgtv(芒果)/rrmj(人人)...
  → {"code":200,"data":{"url":"https://cibn-edge-5g.1ljx.com/cloud/flv/...m3u8"}}
================  rrmj 在线链 (外剧兜底)  ================
搜索:  GET /search/comprehensive/precise-mixed?keywords={kw}&size=20 (fuzzySeasonList)
详情:  GET /drama/detail?dramaId={id}&isAgeLimit=0 → episodeList[].sid
播放:  GET /drama/detail?dramaId={id}&isAgeLimit=0&episodeSid={sid}
       → data.watchInfo.m3u8.url = 明文 mp4 直链
签名:  Base64(HmacSHA256("GET\\naliId:{did}\\nct:android\\ncv:5.27.7\\nt:{ms}\\n{url_sorted}",
                        "ES513W0B1CsdUrR13Qk5EgDAKPeeKZY"))
"""
import sys, os, json, time, random, hashlib, urllib.request, urllib.parse, ssl
import base64
import hmac
import threading
import gzip
import re

sys.path.append('..')
try:
    from base.spider import Spider
except ImportError:
    class Spider(object):
        def init(self, extend=""):
            pass

VERSION = "2026-09-06c"

# ============================== 神马在线链 (主) ==============================
XCMS = "xcms.zxyy123.top"
MF = "mf.smyyds.xyz"
RC4KEY = b"GN8ZGa4DmaHQrHhSTyQ3FwnhCQt68EXQ"
SALT = "d7563df33d41407f970361f176aece3b"
MD5D = "bea459dba4b048d72b626ad12c49fd59"
XDATA = "BG74O4gb2o4IxUXd5CCxllMV45eRMjPCnde3EEirPTzoJh1spv20WeUrfy8NYdYr"
XSIGN = "cjJ1cjJjclZVVGN3ZlNXSg==\n"
AUTH = "Basic c2hlbm1hOnNoZW5tYQ=="
UA = "Dalvik/2.1.0 (Linux; U; Android 12; S905L3A Build/STTC.220815.001)"
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36")
BAD_URLS = ("baidu.com/diaoxian", "diaoxian.m3u8")

# ============================== rrmj 在线链 (兜底) ==============================
RR_BASE = "https://api.rrmj.plus"
RR_SECRET = "ES513W0B1CsdUrR13Qk5EgDAKPeeKZY"
RR_CV = "5.27.7"
RR_CT = "android"
rr_did = "".join(random.choice("0123456789abcdef") for _ in range(16))

# 分类映射: xcms class 名 -> 展示(电影/电视剧/综艺/动漫/纪录片/外剧)
# 有效类目(实测 videonum): movie 27658 / tvplay 17981 / jilupian 14094 /
#   comic(动漫) 19060 / tvshow(综艺) 8119 / 外据(外剧) 9582
CLASSES = [
    {"type_id": "movie", "type_name": "电影"},
    {"type_id": "tvplay", "type_name": "电视剧"},
    {"type_id": "comic", "type_name": "动漫"},
    {"type_id": "tvshow", "type_name": "综艺"},
    {"type_id": "jilupian", "type_name": "纪录片"},
    {"type_id": "外据", "type_name": "外剧"},
]

# 部分条目的播放地址是各大站网页地址 -> mf /Client/ 解析 (line=站点); co_ 内链 line=co
LINE_MAP = (("youku.com", "youku"), ("v.qq.com", "qq"), ("iqiyi.com", "qq"),
            ("mgtv.com", "mgtv"), ("bilibili.com", "bilibili"), ("le.com", "letv"),
            ("sohu.com", "sohu"), ("tudou.com", "tudou"), ("pptv.com", "pptv"),
            ("wasu.cn", "wasu"), ("1905.com", "1905"), ("acfun.cn", "acfun"),
            ("m.yichengwlkj.com", "rrmj"), ("yichengwlkj.com", "rrmj"),
            ("rrmj", "rrmj"), ("drama", "rrmj"))


def line_of(u):
    u = (u or "").lower()
    if u.startswith("co_"):
        return "co"
    for h, ln in LINE_MAP:
        if h in u:
            return ln
    return ""


# 分类启发式(离线库兜底)
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
    {"type_id": "DIANYING", "type_name": "离线电影"},
    {"type_id": "DIANSHIJU", "type_name": "离线电视剧"},
    {"type_id": "ZONGYI", "type_name": "离线综艺"},
    {"type_id": "DONGMAN", "type_name": "离线动漫"},
    {"type_id": "SHAOER", "type_name": "离线少儿"},
    {"type_id": "JILUPIAN", "type_name": "离线纪录片"},
    {"type_id": "WAIJU", "type_name": "离线外剧"},
]

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE
_direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ============================== 基础工具 ==============================
def rc4(data, key):
    """标准 RC4 —— 注意参数顺序: rc4(明文, key)! data 在前 key 在后"""
    if isinstance(key, str):
        key = key.encode()
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) & 0xFF
        S[i], S[j] = S[j], S[i]
    out = bytearray()
    i = j = 0
    for b in data:
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out.append(b ^ S[(S[i] + S[j]) & 0xFF])
    return bytes(out)


def _hexid(n):
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


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


def http_req(url, data=None, host="", timeout=25, direct=True):
    hdr = {"Authorization": AUTH, "User-Agent": UA, "Accept-Encoding": "gzip"}
    if host:
        hdr["Host"] = host
    if data is not None:
        hdr["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET", headers=hdr)
    try:
        op = _direct_opener if direct else None
        if op is not None:
            r = op.open(req, timeout=timeout)
        else:
            r = urllib.request.urlopen(req, timeout=timeout, context=_ctx)
        with r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            return raw
    except Exception:
        return b""


# ============================== 神马 cms 接口 ==============================
def cms_call(query, timeout=25, retry=2):
    for _ in range(max(1, retry)):
        t = str(int(time.time()))
        key = rc4(t.encode(), t.encode()).hex()
        body = urllib.parse.urlencode(
            {"data": XDATA, "os": "31", "sign": XSIGN, "time": t, "key": key}).encode()
        raw = http_req("http://%s//api.php/smtv/vod/?%s" % (XCMS, query),
                       body, XCMS, timeout=timeout)
        if not raw:
            time.sleep(0.5)
            continue
        txt = raw.decode("utf-8", "replace").strip()
        try:
            if txt[:1] in ("{", "["):
                return jloads(txt)
            j = jloads(rc4(base64.b64decode(txt), MD5D).decode("utf-8", "replace"))
            return j
        except Exception:
            time.sleep(0.4)
    return None


def xcms_list(cls, page=1, year="", sort=""):
    q = "ac=list&class=%s&page=%d" % (urllib.parse.quote(cls), page)
    if year:
        q += "&year=%s" % urllib.parse.quote(year)
    if sort:
        q += "&sort=%s" % sort
    j = cms_call(q)
    if not isinstance(j, dict):
        return None
    data = j.get("data") or []
    return {"list": [v for v in (_xcms_vod(x) for x in data) if v],
            "total": int(j.get("videonum") or 0),
            "pagecount": max(1, (int(j.get("videonum") or 0) + 19) // 20)}


def xcms_detail(vid):
    j = cms_call("ac=detail&ids=%s" % vid)
    if not isinstance(j, dict) or not j.get("title"):
        return None
    return j


def _xcms_vod(it):
    """list 条目 -> vod"""
    vid = it.get("vod_id")
    if not vid:
        return None
    return {"vod_id": "xm_" + str(vid),
            "vod_name": it.get("title") or ("片源" + str(vid)),
            "vod_pic": it.get("pic") or "",
            "vod_remarks": it.get("state") or ""}


# ============================== 神马 mf 登录/解析 ==============================
_machine = None
_token = None


def mf_post(act, plain):
    fields = {"data": rc4(plain.encode(), RC4KEY).hex(),
              "sign": hashlib.md5((plain + "&" + SALT).encode()).hexdigest()}
    body = urllib.parse.urlencode(fields).encode()
    raw = http_req("http://%s//api.php?app=1&act=%s" % (MF, act), body, MF)
    return jloads(raw)


def mf_dec_msg(msg):
    try:
        s = rc4(bytes.fromhex(msg), RC4KEY).decode("latin1")
        return urllib.parse.unquote(s, encoding="utf-8", errors="replace")
    except Exception:
        return ""


def get_mf_token():
    """注册匿名设备 -> 登录取 token (进程内缓存)"""
    global _machine, _token
    if _token:
        return _token
    for _ in range(2):
        m = _hexid(16)
        t = str(int(time.time()))
        mf_post("user_reg", "user=%s&password=%s&markcode=%s&t=%s" % (m, m, m, t))
        t = str(int(time.time()))
        r = mf_post("user_logon", "account=%s&password=%s&markcode=%s&t=%s" % (m, m, m, t))
        if isinstance(r, dict) and r.get("code") == 200:
            tk = (jloads(mf_dec_msg(r.get("msg") or "")) or {}).get("token")
            if tk:
                _machine, _token = m, tk
                return tk
        time.sleep(0.4)
    return ""


def mf_resolve(u, line, vodname=""):
    """mf /Client/ 解析真实播放地址 (网页地址或 co_ 内链)"""
    tk = get_mf_token()
    if not tk:
        return ""
    m = _machine or _hexid(16)
    url = ("http://%s/Client/?url=%s&app=1&account=%s&password=%s&token=%s&machineid=%s"
           "&edition=1.0&vodname=%s&line=%s&new=1&_t=%d") % (
        MF, urllib.parse.quote(u), m, m, tk, m,
        urllib.parse.quote((vodname or "")[:24]), line, int(time.time() * 1000))
    raw = http_req(url, host=MF, timeout=40)
    j = jloads(raw)
    if isinstance(j, dict) and j.get("code") == 200:
        pu = ((j.get("data") or {}).get("url") or "").strip()
        if pu and not any(b in pu for b in BAD_URLS):
            return pu
    return ""


# ============================== rrmj 在线链 (兜底) ==============================
def rr_get(path, params, timeout=15, retry=2):
    global rr_did
    for _ in range(max(1, retry)):
        t = int(time.time() * 1000)
        qs = urllib.parse.urlencode(sorted(params.items()))
        full_url = RR_BASE + path + "?" + qs
        msg = "GET\naliId:%s\nct:android\ncv:5.27.7\nt:%d\n%s" % (rr_did, t, full_url)
        sign = base64.b64encode(
            hmac.new(RR_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()
        hdr = {"token": "", "clientVersion": "5.27.7", "clientType": "android",
               "cv": "5.27.7", "ct": "android", "deviceId": rr_did,
               "umid": rr_did, "aliId": rr_did, "uet": "9",
               "x-ca-sign": sign, "t": str(t), "User-Agent": UA,
               "Accept": "application/json, text/plain, */*"}
        req = urllib.request.Request(full_url, headers=hdr)
        try:
            with _direct_opener.open(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            j = jloads(raw)
            if isinstance(j, dict) and j.get("code") == "0000":
                return j.get("data")
        except Exception:
            pass
        time.sleep(0.3)
    return None


def rr_search(kw, size=20):
    d = rr_get("/search/comprehensive/precise-mixed",
               {"keywords": kw, "size": str(size)})
    if not isinstance(d, dict):
        return []
    out, seen = [], set()
    for it in (d.get("fuzzySeasonList") or []):
        did = it.get("id")
        if not did:
            continue
        vid = "rr_" + str(did)
        if vid in seen:
            continue
        seen.add(vid)
        out.append({"vod_id": vid,
                    "vod_name": it.get("title") or "",
                    "vod_pic": it.get("cover") or "",
                    "vod_remarks": str(it.get("year") or "") or "高清"})
    return out


def rr_detail_vod(vid):
    """rrmj 详情 -> (drama_id, title, eps)"""
    d = rr_get("/drama/detail", {"dramaId": vid, "isAgeLimit": "0"})
    if not isinstance(d, dict):
        return None
    info = d.get("dramaInfo") or d
    eps = d.get("episodeList") or []
    return {"did": vid, "title": info.get("title") or d.get("title") or "",
            "cover": info.get("cover") or d.get("cover") or "",
            "intro": info.get("description") or d.get("description") or "",
            "eps": eps}


def rr_play(drama_id, episode_sid):
    last = ""
    for _ in range(5):
        d = rr_get("/drama/detail",
                   {"dramaId": str(drama_id), "isAgeLimit": "0",
                    "episodeSid": str(episode_sid)})
        if not isinstance(d, dict):
            time.sleep(0.2)
            continue
        url = ((d.get("watchInfo") or {}).get("m3u8") or {}).get("url") or ""
        if url.startswith("http"):
            if "ali-cdn-video" in url:
                return url
            last = url
        time.sleep(0.15)
    return last


# ============================== 离线片库 (最终兜底) ==============================
_library = []
LIB_URLS = [
    "https://raw.githubusercontent.com/dkane027/haitun/refs/heads/main/yemao_library.json",
]


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
            return True
    cands = [s.strip() for s in src.split(";") if s.strip()] if src else []
    cands += ["yemao_library.json", "library.json"] + LIB_URLS
    for c in cands:
        raw = b""
        try:
            if c.startswith("http"):
                raw = http_req(c, timeout=25)
            else:
                p = os.path.join(os.path.dirname(os.path.abspath(__file__)), c)
                if os.path.exists(p):
                    raw = open(p, "rb").read()
        except Exception:
            raw = b""
        d = jloads(raw)
        if isinstance(d, list) and d:
            _library = [_norm(v) for v in d if v.get("id")]
            return True
    return False


def _lib_vod(v):
    return {"vod_id": str(v["id"]),
            "vod_name": v["title"] or ("片源" + str(v["id"])),
            "vod_pic": v["pic"],
            "vod_remarks": v["state"] or v["year"] or "高清"}


def build_filters():
    """按 xcms flitter 动态生成筛选项 (年份, 全部类别共用)"""
    out = {}
    try:
        j = cms_call("ac=flitter&class=tvplay")
        if isinstance(j, dict):
            for f in (j.get("flitter") or []):
                field = f.get("field") or ""
                values = f.get("values") or []
                if field in ("year",) and values:
                    out["tvplay"] = [{"key": "year", "name": f.get("name", "年份"),
                                      "value": [{"n": "全部", "v": ""}] +
                                               [{"n": str(y), "v": str(y)} for y in values]}]
                    break
    except Exception:
        pass
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
        hot = []
        r = xcms_list("movie", 1)
        if r:
            hot += r["list"][:8]
        r = xcms_list("tvplay", 1)
        if r:
            hot += r["list"][:8]
        r = xcms_list("外据", 1)
        if r:
            hot += r["list"][:4]
        if not hot:
            for v in _library[:24]:
                hot.append(_lib_vod(v))
        return {"class": list(CLASSES), "filters": build_filters(), "list": hot[:30]}

    def homeVideoContent(self):
        hot = []
        r = xcms_list("tvplay", 1)
        if r:
            hot = r["list"][:40]
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
        # 神马在线分类
        for c in CLASSES:
            if tid == c["type_id"]:
                r = xcms_list(tid, pg, year=str(ext.get("year") or ""))
                if r:
                    pc = max(1, (r["total"] + 19) // 20)
                    return {"page": pg, "pagecount": pc, "limit": 20,
                            "total": r["total"], "list": r["list"]}
                return {"page": pg, "pagecount": 1, "limit": 20, "total": 0, "list": []}
        # 离线分类 (DIANYING 等)
        year = str(ext.get("year") or "").strip()
        items = [v for v in _library if v["type"] == tid]
        if year:
            items = [v for v in items if str(v["year"]) == year]
        page = items[(pg - 1) * 24: pg * 24]
        total = len(items)
        return {"page": pg, "pagecount": max(1, (total + 23) // 24),
                "limit": 24, "total": total, "list": [_lib_vod(v) for v in page]}

    # ---------------- 详情 ----------------
    def detailContent(self, ids):
        vid = str(ids[0] if isinstance(ids, list) else ids).split(",")[0].strip()
        if vid.startswith("xm_"):       # 神马在线
            return self._xm_detail(vid[3:])
        if vid.startswith("rr_"):       # rrmj 在线(外剧)
            return self._rr_detail(vid[3:])
        return self._lib_detail(vid)    # 离线

    def _xm_detail(self, vid):
        j = xcms_detail(vid)
        if not j:
            return {"list": [{"vod_id": "xm_" + vid, "vod_name": "加载失败, 请重试",
                              "vod_play_from": "夜猫4K", "vod_play_url": "重试$0"}]}
        froms, urls = [], []
        for src in (j.get("video_list") or []):
            eps = src.get("list") or []
            arr = []
            for e in eps:
                t = (e.get("title") or "").replace("$", "").replace("#", "")
                u = e.get("url") or ""
                if not u:
                    continue
                # 外剧(rrmj网页 m.yichengwlkj.com/drama/{did}?episodeNo={n}) -> 走 rrmj 直链
                mj = re.search(r"/drama/(\d+)(?:\?[^ ]*episodeNo=(\d+))?", u)
                if mj and ("yichengwlkj" in u or "rrmj" in u):
                    did = mj.group(1)
                    epn = mj.group(2) or "1"
                    arr.append("%s$xrr_%s_%s" % (t or ("第%d集" % (len(arr) + 1)), did, epn))
                    continue
                arr.append("%s$xmp_%s_%s" % (t or ("第%d集" % (len(arr) + 1)), vid,
                                             urllib.parse.quote(u, safe="")))
            if arr:
                froms.append((src.get("name") or "线路").replace("$", ""))
                urls.append("#".join(arr))
        if not froms:
            froms, urls = ["夜猫4K"], ["暂无片源$0"]
        return {"list": [{
            "vod_id": "xm_" + vid,
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

    def _rr_detail(self, drama_id):
        d = rr_detail_vod(drama_id)
        if not d or not d.get("eps"):
            return {"list": [{"vod_id": "rr_" + str(drama_id), "vod_name": "加载失败, 请重试",
                              "vod_play_from": "人人视频", "vod_play_url": "重试$0"}]}
        arr = []
        for e in d["eps"]:
            t = (e.get("text") or e.get("title") or "").replace("$", "").replace("#", "")
            sid = e.get("sid")
            if sid:
                arr.append("%s$rrplay_%s_%s" % (t or ("第%s集" % e.get("episodeNo", len(arr) + 1)),
                                                drama_id, sid))
        return {"list": [{
            "vod_id": "rr_" + str(drama_id),
            "vod_name": d.get("title"),
            "vod_pic": d.get("cover") or "",
            "vod_remarks": "人人视频",
            "vod_content": d.get("intro") or "",
            "vod_play_from": "人人视频",
            "vod_play_url": "#".join(arr) if arr else "暂无片源$0",
        }]}

    def _lib_detail(self, vid):
        v = next((x for x in _library if str(x["id"]) == vid), None)
        if not v:
            return {"list": []}
        return {"list": [{"vod_id": vid, "vod_name": v["title"], "vod_pic": v["pic"],
                          "vod_remarks": v["state"],
                          "vod_content": "离线片库条目, 详情接口暂时不可用",
                          "vod_play_from": "夜猫4K", "vod_play_url": "重试$0"}]}

    # ---------------- 搜索 ----------------
    def searchContent(self, key, quick, pg="1"):
        key = (key or "").strip()
        if not key:
            return {"list": []}
        # 1) rrmj 在线搜索 (外剧/人人)
        out = rr_search(key)
        if out:
            # 2) 补神马 tvplay 前几页? 神马无搜索, 用 list 无意义 —— 只返 rrmj
            return {"list": out}
        # 兜底: 离线库
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
        # 外剧 rrmj 直链: xrr_{did}_{episodeNo}
        if pid.startswith("xrr_"):
            parts = pid[len("xrr_"):].split("_")
            if len(parts) >= 2:
                did = parts[0]
                try:
                    epn = int(parts[1])
                except Exception:
                    epn = 1
                d = rr_detail_vod(did)
                if d and d.get("eps") and 0 < epn <= len(d["eps"]):
                    sid = d["eps"][epn - 1].get("sid")
                    url = rr_play(did, sid)
                    if url:
                        return {"parse": 0, "playUrl": "", "url": url,
                                "header": {"User-Agent": UA}}
            return {"parse": 0, "playUrl": "", "url": "", "header": {}}
        # 神马: xmp_{vid}_{quote(url)}
        if pid.startswith("xmp_"):
            parts = pid[len("xmp_"):].split("_", 1)
            if len(parts) == 2:
                u = urllib.parse.unquote(parts[1])
                line = line_of(u)
                url = mf_resolve(u, line, "")
                if url:
                    return {"parse": 0, "playUrl": "", "url": url,
                            "header": {"User-Agent": UA}}
            return {"parse": 0, "playUrl": "", "url": "", "header": {}}
        # rrmj: rrplay_{did}_{sid}
        if pid.startswith("rrplay_"):
            parts = pid[len("rrplay_"):].split("_")
            if len(parts) >= 2:
                url = rr_play(parts[0], parts[1])
                if url:
                    return {"parse": 0, "playUrl": "", "url": url,
                            "header": {"User-Agent": UA}}
            return {"parse": 0, "playUrl": "", "url": "", "header": {}}
        if pid.startswith("http"):
            return {"parse": 0, "playUrl": "", "url": pid,
                    "header": {"User-Agent": WEB_UA}}
        return {"parse": 0, "playUrl": "", "url": "", "header": {}}

    def localProxy(self, param):
        return [200, "text/plain", {}, ""]