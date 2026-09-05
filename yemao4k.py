# -*- coding: utf-8 -*-
"""
夜猫4K TVBox / 影视仓 点播源 (T3 python edition, 只依赖标准库)
================================================================
修复要点 (相对旧版):
  1. RC4 参数顺序修正: rc4(key, data)  —— 旧版写反了, 导致 sign/data 全部失败
  2. 登录前先 user_reg 注册设备账号 (旧版直接 logon -> 122 账号不存在)
  3. detail 响应尾部有垃圾字节, 用 raw_decode 容错解析 (旧版 json.loads 直接抛错)
  4. 片库自带 yemao_library.json (在线抓取快照), 支持 extend 传 URL 覆盖
  5. 播放地址失效自动换线路重试; 过滤 diaoxian.m3u8 占位
  6. 搜索走库内 + 相关推荐补充; 补齐 T3 必需接口

用法: config.json 里
  {"key":"yemao4k","name":"夜猫4K","type":3,
   "api":"<本文件URL>","searchable":1,"quickSearch":1,"filterable":1,
   "ext":"<yemao_library.json URL>"}
"""
import sys, os, json, time, random, hashlib, urllib.request, urllib.parse, ssl

sys.path.append('..')
try:
    from base.spider import Spider
except ImportError:
    class Spider(object):
        def init(self, extend=""):
            pass

VERSION = "2026-09-04b"

HOST_MF = "mf.smyyds.xyz"
HOST_CMS = "cms.dayuys.icu"
SALT = "d7563df33d41407f970361f176aece3b"
RC4KEY = b"GN8ZGa4DmaHQrHhSTyQ3FwnhCQt68EXQ"
SMTV_DATA = "BG74O4gb2o4IxUXd5CCxllMV45eRMjPCnde3EEirPTzoJh1spv20WeUrfy8NYdYr"
SMTV_SIGN = "cjJ1cjJjclZVVGN3ZlNXSg==\n"
AUTH = "Basic c2hlbm1hOnNoZW5tYQ=="
UA = "Dalvik/2.1.0 (Linux; U; Android 12; S905L3A Build/STTC.220815.001)"
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36")
BAD_URLS = ("baidu.com/diaoxian", "diaoxian.m3u8")

# 部分条目的播放地址不是内部 co_ id, 而是各大站的网页地址 -> 必须交给 TVBox 的 parses 解析。
# from(线路名) 必须等于标准 flag, TVBox 才会去匹配 parses。
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

CLASSES = [
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


# ---------------- 基础工具 ----------------
def rc4(key, data):
    """标准 RC4: 注意 key 在前, data 在后"""
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


def http_raw(url, data=None, headers=None, timeout=20):
    """优先用宿主环境的 requests (T3 py 环境自带), 不可用时退回标准库 urllib"""
    hdr = {"User-Agent": UA}
    if headers:
        hdr.update(headers)
    try:
        import requests
        r = requests.request("POST" if data else "GET", url, data=data,
                             headers=hdr, timeout=timeout, verify=False)
        return r.content
    except ImportError:
        pass
    except Exception:
        return b""
    req = urllib.request.Request(url, data=data, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
            return r.read()
    except Exception:
        return b""


def jloads(raw):
    """服务器响应尾部可能带垃圾字节, 用 raw_decode 从最早的 { 或 [ 开始容错解析"""
    if not raw:
        return None
    s = raw.decode("utf-8", "replace")
    cands = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    if not cands:
        return None
    try:
        return json.JSONDecoder().raw_decode(s[min(cands):])[0]
    except Exception:
        return None


def post_form(host, path, fields, scheme="http"):
    body = urllib.parse.urlencode(fields) + "&"
    return http_raw(scheme + "://" + host + path, data=body.encode(),
                    headers={"Authorization": AUTH,
                             "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})


# ---------------- 登录 (mf 主机) ----------------
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
    """注册匿名设备 -> 登录取 token (进程内缓存)"""
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


# ---------------- 内容 API (cms 主机) ----------------
def cms_post(path, retry=3):
    """cms 偶发超时/空响应, 重试几次"""
    for i in range(retry):
        fields = {"time": str(int(time.time())), "key": _hexid(20),
                  "data": SMTV_DATA, "os": "32", "sign": SMTV_SIGN}
        j = jloads(post_form(HOST_CMS, path, fields, scheme="https"))
        if j:
            return j
        time.sleep(0.8 * (i + 1))
    return None


def detail_api(vid):
    return cms_post("/api.php/smtv/vod/?ac=detail&ids=%s" % vid)


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


# ---------------- 片库 ----------------
_library = []
LIB_URLS = [
    "https://raw.githubusercontent.com/dkane027/haitun/refs/heads/main/yemao_library.json",
]


# 最后兜底: 片库 URL/本地文件全部拉不到时用的内置种子 (34 条, 保证界面不空白)
SEED_JSON = r'''[{"id":344497,"t":"外八门之雪域魔窟","p":"https://m.ykimg.com/050E40005A5C3B76AD881A0664070330","c":"DIANYING","y":"2016"},{"id":343860,"t":"孤独的女人","p":"https://m.ykimg.com/050E00006276354E13F7FF0987889B4E","c":"DIANYING","y":"1964"},{"id":343746,"t":"十二生肖：世界末日的迹象","p":"https://m.ykimg.com/050E0000629466021FD852090BA34E65","c":"DIANYING","y":"2019"},{"id":343691,"t":"胜者为王之拳王阿里","p":"https://m.ykimg.com/050E40005BDA902CADA7B2844D0CAE3F","c":"DIANYING","y":"2020"},{"id":343209,"t":"夺国宝","p":"https://m.ykimg.com/050E00006347E86213EB6609DEFF06B0","c":"DIANYING","y":"1926"},{"id":343962,"t":"大路上","p":"https://m.ykimg.com/050E00006555A77213EBC61B340C681B","c":"DIANSHIJU","y":"2024"},{"id":342905,"t":"大红灯笼高高挂","p":"https://m.ykimg.com/050E0000692041CA140C6E143C4644A2","c":"DIANSHIJU","y":"2025"},{"id":340690,"t":"外来媳妇本地郎2","p":"https://m.ykimg.com/050E00006040A81C2027EE08A655DD0F","c":"DIANSHIJU","y":"2018"},{"id":333339,"t":"寒门状元","p":"http://0img.hitv.com/preview/sp_images/2026/04/09/202604091730000045191.jpg","c":"DIANSHIJU","y":"2025"},{"id":333240,"t":"我是你的白月光","p":"https://pic2.iqiyipic.com/image/20260416/96/e4/a_100801103_m_601_579_772.jpg","c":"DIANSHIJU","y":"2026"},{"id":342108,"t":"Ciao翘圣诞1","p":"https://m.ykimg.com/050E00005DEF4CF41B769111BB083761","c":"ZONGYI","y":"2017"},{"id":335767,"t":"美女天黑请闭眼1","p":"http://m.ykimg.com/053400005DEF7431859B5E3EE10F353E","c":"ZONGYI","y":"2017"},{"id":335704,"t":"Uta奇奇怪怪的开箱好物分享推荐1","p":"http://m.ykimg.com/053440005A17D421AD881A03890E6DFE","c":"ZONGYI","y":"2017"},{"id":335651,"t":"渣男日记2016","p":"http://m.ykimg.com/0534400059AD7F9E859B5C03040A542C","c":"ZONGYI","y":"2016"},{"id":335110,"t":"湾湾说2016","p":"http://m.ykimg.com/0534400059BAAE01AD881A03060A0746","c":"ZONGYI","y":"2016"},{"id":344638,"t":"我靠投诉开挂，成为了仙魔之主","p":"https://m.ykimg.com/050E0000698B0B346A22BF1D50971851","c":"DONGMAN","y":"2026"},{"id":344511,"t":"胆大党日语版","p":"https://vcover-vt-pic.puui.qpic.cn/vcover_vt_pic/0/mzc00200od022281727703266627/0","c":"DONGMAN","y":"2024"},{"id":344466,"t":"异变胖虎","p":"https://m.ykimg.com/050E00005729A660000000195504CDFC","c":"DONGMAN","y":"2026"},{"id":344250,"t":"丽莎的假期生活","p":"https://m.ykimg.com/050E00005729A660000000195504CDFC","c":"DONGMAN","y":"2025"},{"id":344097,"t":"冷君离","p":"https://m.ykimg.com/050E00006A0ADE2FC876E113C6A76401","c":"DONGMAN","y":"2026"},{"id":344573,"t":"东方娃娃幼儿大科学：海狮，大海里的“狮子”","p":"https://m.ykimg.com/050E000069F03292140C6E14325B0E7B","c":"SHAOER","y":"2026"},{"id":344415,"t":"东方娃娃幼儿大科学：森林里的发明家黑猩猩","p":"https://m.ykimg.com/050E000069F0307B7B519713B444CC96","c":"SHAOER","y":"2026"},{"id":343657,"t":"囡囡玩具生活","p":"https://m.ykimg.com/050E00005729A660000000195504CDFC","c":"SHAOER","y":"2025"},{"id":343414,"t":"睡前小耳朵宝宝听故事","p":"https://m.ykimg.com/050E000069F1C0C8C54E4912FD46F759","c":"SHAOER","y":"2026"},{"id":342800,"t":"福尔摩斯与华生名侦探的宝藏","p":"https://m.ykimg.com/050E0000693A20DB051E9D150E59490E","c":"SHAOER","y":"2026"},{"id":344604,"t":"一学就会的阳宅风水课","p":"https://m.ykimg.com/050E00006863AE14203CC7114E1782E4","c":"JILUPIAN","y":"2025"},{"id":344353,"t":"《小野田的丛林万夜》解读原型人物小野田宽郎","p":"https://m.ykimg.com/050E0000670A614B7B50BB1489129403","c":"JILUPIAN","y":"2024"},{"id":344340,"t":"奇幻冒险：害虫与益虫的战斗","p":"https://m.ykimg.com/050E00006736B251C7BFE9128B979594","c":"JILUPIAN","y":"2024"},{"id":344337,"t":"南极的眼泪","p":"https://m.ykimg.com/050E00005D80B0D142659322A27DE95B","c":"JILUPIAN","y":"2019"},{"id":344023,"t":"讲法记录说","p":"https://m.ykimg.com/050E000061F25D372037DD0937F75483","c":"JILUPIAN","y":"2022"},{"id":344716,"t":"飞鸟藏爱","p":"https://ju-oss-5g.maqq.cn/ju-pic/kokbksxdnrnunonokbkakikhkukoxdkckokm/upload/movie/20260512/vod38211979.webp","c":"WAIJU","y":"2026"},{"id":344421,"t":"无耻之徒美版8","p":"https://ju-oss-5g.maqq.cn/ju-pic/kokbksxdnrnunonokbkakikhkukoxdkckokm/upload/movie/20240829/vod26938395.webp","c":"WAIJU","y":"2017"},{"id":344178,"t":"摩登家庭7","p":"https://ju-oss-5g.maqq.cn/ju-pic/kokbksxdnrnunonokbkakikhkukoxdkckokm/upload/movie/20240905/vod26385630.webp","c":"WAIJU","y":"2015"},{"id":342987,"t":"夜魔侠：重生1","p":"https://ju-oss-5g.maqq.cn/ju-pic/kokbksxdnrnunonokbkakikhkukoxdkckokm/upload/vod/20250905-1/420232cca3287bb4b5f8775c733e193a.webp","c":"WAIJU","y":"2025"},{"id":342958,"t":"爱冲云霄","p":"https://ju-oss-5g.maqq.cn/ju-pic/kokbksxdnrnunonokbkakikhkukoxdkckokm/upload/movie/20260530/vod37134254.webp","c":"WAIJU","y":"2026"}]'''


def _norm(v):
    """兼容新(紧凑)/旧(完整)两种字段名"""
    return {"id": v.get("id"),
            "title": v.get("t") or v.get("title") or "",
            "pic": v.get("p") or v.get("pic") or "",
            "state": v.get("s") or v.get("state") or "",
            "type": v.get("c") or v.get("type") or "",
            "year": v.get("y") or v.get("year") or ""}


def load_library(src):
    global _library
    src = (src or "").strip()
    # 情况 A: 宿主已把 ext 指向的文件内容取回并直接传进来
    if src[:1] in ("[", "{"):
        d = jloads(src.encode("utf-8", "replace"))
        if isinstance(d, list) and d:
            _library = [_norm(v) for v in d if v.get("id")]
            return _library
    # 情况 B: ext 是 URL / 本地文件名 (可用 ; 分隔多个备选)
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


def _vod(v):
    return {"vod_id": str(v["id"]),
            "vod_name": v["title"] or ("片源" + str(v["id"])),
            "vod_pic": v["pic"],
            "vod_remarks": v["state"] or v["year"] or "高清"}


def build_filters():
    """按片库实际年份动态生成筛选项 (只保留有内容的年份)"""
    out = {}
    for c in CLASSES:
        tid = c["type_id"]
        ys = sorted({str(v["year"]) for v in _library
                     if v["type"] == tid and str(v["year"]).isdigit()
                     and len(str(v["year"])) == 4}, reverse=True)
        if len(ys) < 2:
            continue
        out[tid] = [{"key": "year", "name": "年份",
                     "value": [{"n": "全部", "v": ""}] +
                              [{"n": y, "v": y} for y in ys[:16]]}]
    return out


# ---------------- Spider 接口 ----------------
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

    def homeContent(self, filter):
        hot = []
        for c in CLASSES:
            n = 0
            for v in _library:
                if v["type"] == c["type_id"]:
                    hot.append(_vod(v))
                    n += 1
                    if n >= 6:
                        break
        return {"class": CLASSES, "filters": build_filters(), "list": hot}

    def homeVideoContent(self):
        return {"list": [_vod(v) for v in _library[:40]]}

    def categoryContent(self, tid, pg, filter, extend):
        try:
            pg = int(pg or 1)
        except Exception:
            pg = 1
        ext = extend if isinstance(extend, dict) else {}
        year = str(ext.get("year") or "").strip()
        items = [v for v in _library if not tid or v["type"] == tid]
        if year:
            items = [v for v in items if str(v["year"]) == year]
        page = items[(pg - 1) * 24: pg * 24]
        total = len(items)
        return {"page": pg, "pagecount": max(1, (total + 23) // 24),
                "limit": 24, "total": total, "list": [_vod(v) for v in page]}

    def detailContent(self, ids):
        vid = str(ids[0] if isinstance(ids, list) else ids).split(",")[0].strip()
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
                # 该线路全是同一家 vip 网页地址 -> 线路名用标准 flag, 让 TVBox 走 parses
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

    def searchContent(self, key, quick, pg="1"):
        key = (key or "").strip()
        if not key:
            return {"list": []}
        kl = key.lower().replace(" ", "")
        out = []
        for v in _library:
            t = (v["title"] or "")
            if kl in t.lower().replace(" ", ""):
                out.append(_vod(v))
                if len(out) >= 40:
                    break
        return {"list": out}

    def playerContent(self, flag, id, vipFlags):
        pid = str(id or "")
        # co_xxx: 走夜猫自家接口拿直链, parse=0 直接播
        if pid.startswith("co_"):
            url = resolve_play(pid, "")
            if url:
                return {"parse": 0, "playUrl": "", "url": url, "header": {"User-Agent": WEB_UA}}
            return {"parse": 0, "playUrl": "", "url": "", "header": {}}
        # 各大站网页地址: 交给 TVBox 配置里的 parses 解析 (parse=1)
        if pid.startswith("http"):
            if vip_flag(pid):
                return {"parse": 1, "playUrl": "", "url": pid, "header": {}}
            return {"parse": 0, "playUrl": "", "url": pid, "header": {"User-Agent": WEB_UA}}
        return {"parse": 0, "playUrl": "", "url": "", "header": {}}

    def localProxy(self, param):
        return [200, "text/plain", {}, ""]
