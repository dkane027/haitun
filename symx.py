#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
山有木兮影视 (film.symx.club) Python Spider
兼容 FongMi/TV & WebHomeTV/PeekPro

验证码识别: 纯Python实现(无外部依赖) + cv2/PIL可选加速
"""

import base64, hashlib, hmac, json, random, time, struct, zlib, math, sys

# ==================== FongMi/TV 基类兼容 ====================
sys.path.append('..')
try:
    from base.spider import Spider as _BaseSpider
except ImportError:
    try:
        import requests as _rq
        class _BaseSpider:
            def fetch(self, url, headers=None, timeout=15, **kw):
                kw.pop('timeout', None)
                return _rq.get(url, headers=headers, timeout=15, **kw)
            def post(self, url, json=None, headers=None, timeout=15, **kw):
                return _rq.post(url, json=json, headers=headers, timeout=15, **kw)
    except ImportError:
        _BaseSpider = object

try:
    import requests
except ImportError:
    requests = None

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    from PIL import Image
    import io as _io
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ==================== 纯Python图像解码器 ====================
# JPEG zigzag 扫描顺序
_ZIGZAG = [
    (0,0),(0,1),(1,0),(2,0),(1,1),(0,2),(0,3),(1,2),
    (2,1),(3,0),(4,0),(3,1),(2,2),(1,3),(0,4),(0,5),
    (1,4),(2,3),(3,2),(4,1),(5,0),(6,0),(5,1),(4,2),
    (3,3),(2,4),(1,5),(0,6),(0,7),(1,6),(2,5),(3,4),
    (4,3),(5,2),(6,1),(7,0),(7,1),(6,2),(5,3),(4,4),
    (3,5),(2,6),(1,7),(2,7),(3,6),(4,5),(5,4),(6,3),
    (7,2),(7,3),(6,4),(5,5),(4,6),(3,7),(4,7),(5,6),
    (6,5),(7,4),(7,5),(6,6),(5,7),(6,7),(7,6),(7,7)
]

# IDCT 预计算矩阵
_IDCT_M = []
for _i in range(8):
    _row = []
    _ci = 1.0 / math.sqrt(2) if _i == 0 else 1.0
    for _j in range(8):
        _row.append(_ci * math.cos((2 * _j + 1) * _i * math.pi / 16) * 0.5)
    _IDCT_M.append(_row)


class _BitReader:
    def __init__(self, data):
        self.data, self.pos, self.bit = data, 0, 0
    def read_bit(self):
        if self.pos >= len(self.data):
            return 0
        b = (self.data[self.pos] >> (7 - self.bit)) & 1
        self.bit += 1
        if self.bit == 8:
            self.bit, self.pos = 0, self.pos + 1
        return b
    def read_bits(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | self.read_bit()
        return v


def _build_huffman(counts, symbols):
    table, code, idx = {}, 0, 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            table[(length, code)] = symbols[idx]
            code, idx = code + 1, idx + 1
        code <<= 1
    return table


def _decode_huffman(br, ht):
    code = 0
    for length in range(1, 17):
        code = (code << 1) | br.read_bit()
        if (length, code) in ht:
            return ht[(length, code)]
    return 0


def _extend_sign(val, bits):
    if bits == 0:
        return 0
    return val - (1 << bits) + 1 if val < (1 << (bits - 1)) else val


def _idct_8x8(block):
    """8x8 IDCT: result = M^T * block * M"""
    temp = [[0.0] * 8 for _ in range(8)]
    for i in range(8):
        for j in range(8):
            temp[i][j] = sum(block[i][k] * _IDCT_M[k][j] for k in range(8))
    result = [[0] * 8 for _ in range(8)]
    for i in range(8):
        for j in range(8):
            s = sum(_IDCT_M[k][i] * temp[k][j] for k in range(8))
            result[i][j] = max(0, min(255, int(round(s + 128))))
    return result


def _decode_jpeg(data):
    """纯Python JPEG解码器, 返回 (width, height, gray_2d_list)"""
    pos = 0
    qt, hdc, hac = {}, {}, {}
    frame = scan = None
    while pos < len(data) - 1:
        if data[pos] != 0xFF:
            pos += 1
            continue
        m = data[pos + 1]
        pos += 2
        if m == 0xD8:
            continue
        elif m == 0xD9:
            break
        elif m in (0xC0, 0xC1):
            ln = struct.unpack('>H', data[pos:pos+2])[0]
            h = struct.unpack('>H', data[pos+3:pos+5])[0]
            w = struct.unpack('>H', data[pos+5:pos+7])[0]
            nc = data[pos+7]
            comps = []
            for c in range(nc):
                b = pos + 8 + c * 3
                comps.append({'id': data[b], 'h': (data[b+1]>>4)&0xF, 'v': data[b+1]&0xF, 'qt': data[b+2]})
            frame = {'w': w, 'h': h, 'c': comps}
            pos += ln
        elif m == 0xDB:
            ln = struct.unpack('>H', data[pos:pos+2])[0]
            p = pos + 2
            while p < pos + ln:
                tq = data[p] & 0xF
                pq = data[p] >> 4
                p += 1
                t = []
                if pq == 0:
                    t = list(data[p:p+64])
                    p += 64
                else:
                    t = [struct.unpack('>H', data[p+i*2:p+i*2+2])[0] for i in range(64)]
                    p += 128
                qt[tq] = t
            pos += ln
        elif m == 0xC4:
            ln = struct.unpack('>H', data[pos:pos+2])[0]
            p = pos + 2
            while p < pos + ln:
                tc, th = (data[p] >> 4) & 0xF, data[p] & 0xF
                p += 1
                counts = list(data[p:p+16])
                p += 16
                total = sum(counts)
                syms = list(data[p:p+total])
                p += total
                tbl = _build_huffman(counts, syms)
                if tc == 0:
                    hdc[th] = tbl
                else:
                    hac[th] = tbl
            pos += ln
        elif m == 0xDA:
            ln = struct.unpack('>H', data[pos:pos+2])[0]
            ns = data[pos+2]
            sc = []
            for s in range(ns):
                b = pos + 3 + s * 2
                sc.append({'id': data[b], 'td': (data[b+1]>>4)&0xF, 'ta': data[b+1]&0xF})
            pos += ln
            scan = sc
            # 提取熵编码数据 (去除 0xFF00 填充)
            ed = bytearray()
            i = pos
            while i < len(data) - 1:
                if data[i] == 0xFF:
                    if data[i+1] == 0x00:
                        ed.append(0xFF)
                        i += 2
                    elif data[i+1] == 0xD9:
                        break
                    else:
                        i += 2
                else:
                    ed.append(data[i])
                    i += 1
            br = _BitReader(bytes(ed))
            comps = frame['c']
            mh = max(c['h'] for c in comps)
            mv = max(c['v'] for c in comps)
            mx = (frame['w'] + mh * 8 - 1) // (mh * 8)
            my = (frame['h'] + mv * 8 - 1) // (mv * 8)
            cmap = {}
            for s in scan:
                for f in comps:
                    if f['id'] == s['id']:
                        cmap[s['id']] = {'h': f['h'], 'v': f['v'], 'qt': f['qt'], 'td': s['td'], 'ta': s['ta']}
            dcp = {k: 0 for k in cmap}
            yblocks = []
            for yy in range(my):
                for xx in range(mx):
                    for cid, ci in cmap.items():
                        for vy in range(ci['v']):
                            for hx in range(ci['h']):
                                blk = [[0]*8 for _ in range(8)]
                                ssss = _decode_huffman(br, hdc.get(ci['td'], {}))
                                bits = br.read_bits(ssss) if ssss else 0
                                diff = _extend_sign(bits, ssss)
                                dcp[cid] += diff
                                blk[0][0] = dcp[cid]
                                k = 1
                                while k < 64:
                                    rs = _decode_huffman(br, hac.get(ci['ta'], {}))
                                    if rs == 0:
                                        break
                                    if rs == 0xF0:
                                        k += 16
                                        continue
                                    rrrr, ssss2 = (rs >> 4) & 0xF, rs & 0xF
                                    k += rrrr
                                    if k >= 64:
                                        break
                                    bits2 = br.read_bits(ssss2) if ssss2 else 0
                                    r, c = _ZIGZAG[k]
                                    blk[r][c] = _extend_sign(bits2, ssss2)
                                    k += 1
                                q = qt.get(ci['qt'], [1]*64)
                                for kk in range(64):
                                    r, c = _ZIGZAG[kk]
                                    blk[r][c] *= q[kk]
                                px = _idct_8x8(blk)
                                if cid == 1:
                                    bx = (xx * mh + hx) * 8
                                    by = (yy * mv + vy) * 8
                                    yblocks.append((bx, by, px))
            W, H = frame['w'], frame['h']
            gray = [[128]*W for _ in range(H)]
            for bx, by, px in yblocks:
                for r in range(8):
                    for c in range(8):
                        x, y = bx + c, by + r
                        if 0 <= x < W and 0 <= y < H:
                            gray[y][x] = px[r][c]
            return W, H, gray
        else:
            if pos + 1 < len(data):
                pos += struct.unpack('>H', data[pos:pos+2])[0]
            else:
                break
    return None


def _decode_png(data):
    """纯Python PNG解码器, 返回 (width, height, gray_2d_list, alpha_2d_or_None)"""
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        return None
    pos, w, h, bd, ct = 8, 0, 0, 0, 0
    idat = b''
    while pos < len(data):
        cl = struct.unpack('>I', data[pos:pos+4])[0]
        ct_name = data[pos+4:pos+8]
        cd = data[pos+8:pos+8+cl]
        if ct_name == b'IHDR':
            w, h, bd, ct = struct.unpack('>IIBB', cd[:10])
        elif ct_name == b'IDAT':
            idat += cd
        elif ct_name == b'IEND':
            break
        pos += 12 + cl
    raw = zlib.decompress(idat)
    ch = {0:1, 2:3, 3:1, 4:2, 6:4}.get(ct, 1)
    bpp = max(1, ch * (bd // 8))
    stride = w * bpp
    pixels, alpha, prev = [], [], [0]*stride
    p = 0
    for y in range(h):
        ft = raw[p]
        p += 1
        row = list(raw[p:p+stride])
        p += stride
        if ft == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i-bpp]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                a = row[i-bpp] if i >= bpp else 0
                row[i] = (row[i] + (a + prev[i]) // 2) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = row[i-bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i-bpp] if i >= bpp else 0
                pp = a + b - c
                pa, pb, pc = abs(pp-a), abs(pp-b), abs(pp-c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (row[i] + pred) & 0xFF
        prev = row
        gr, ar = [], []
        for x in range(w):
            if ct == 0:
                gr.append(row[x * bpp])
            elif ct == 2:
                idx = x * 3
                gr.append((row[idx]*299 + row[idx+1]*587 + row[idx+2]*114) // 1000)
            elif ct == 4:
                gr.append(row[x*2])
                ar.append(row[x*2+1])
            elif ct == 6:
                idx = x * 4
                gr.append((row[idx]*299 + row[idx+1]*587 + row[idx+2]*114) // 1000)
                ar.append(row[idx+3])
            else:
                gr.append(row[x])
        pixels.append(gr)
        if ar:
            alpha.append(ar)
    return w, h, pixels, (alpha if alpha else None)


# ==================== 纯Python验证码求解 ====================

def _solve_slider_pure(bg_b64, tpl_b64):
    """SLIDER: 找缺口位置 (纯Python)"""
    bg_raw = base64.b64decode(bg_b64.split(',')[1] if ',' in bg_b64 else bg_b64)
    tpl_raw = base64.b64decode(tpl_b64.split(',')[1] if ',' in tpl_b64 else tpl_b64)
    
    bg = _decode_jpeg(bg_raw) if bg_raw[:2] == b'\xff\xd8' else _decode_png(bg_raw)
    tpl = _decode_png(tpl_raw) if tpl_raw[:4] == b'\x89PNG' else _decode_jpeg(tpl_raw)
    if not bg or not tpl:
        return None
    
    bw, bh, bgray = bg
    tw, th, tgray, talpha = tpl
    # 模板有效宽度
    tpl_w = tw
    if talpha:
        min_x, max_x = tw, 0
        for y in range(th):
            for x in range(tw):
                if talpha[y][x] > 128:
                    if x < min_x: min_x = x
                    if x > max_x: max_x = x
        tpl_w = max_x - min_x + 1 if max_x > min_x else tw
    
    # 方法1: 列边缘强度
    col_edge = [0] * bw
    for x in range(1, bw):
        s = 0
        for y in range(bh):
            s += abs(bgray[y][x] - bgray[y][x-1])
        col_edge[x] = s
    best_x1 = max(range(50, bw - tpl_w), key=lambda x: col_edge[x])
    
    # 方法2: 列亮度 (缺口较暗)
    col_bright = [sum(bgray[y][x] for y in range(bh)) / bh for x in range(bw)]
    best_x2 = min(range(50, bw - tpl_w), key=lambda x: sum(col_bright[x:x+tpl_w]) / tpl_w)
    
    # 方法3: 阈值法找暗列
    best_x3 = None
    for x in range(50, bw - 50):
        dark = sum(1 for y in range(bh) if bgray[y][x] < 60)
        if dark > bh * 0.5:
            best_x3 = x
            break
    
    # 选择最佳
    candidates = [(best_x1, col_edge[best_x1])]
    candidates.append((best_x2, 1000))
    if best_x3 is not None:
        candidates.append((best_x3, 800))
    result = max(candidates, key=lambda c: c[1])[0]
    if result < 20 and best_x3 is not None:
        result = best_x3
    return int(result)


def _solve_rotate_pure(bg_b64, tpl_b64):
    """ROTATE: 旋转模板匹配 (纯Python)"""
    bg_raw = base64.b64decode(bg_b64.split(',')[1] if ',' in bg_b64 else bg_b64)
    tpl_raw = base64.b64decode(tpl_b64.split(',')[1] if ',' in tpl_b64 else tpl_b64)
    
    bg = _decode_jpeg(bg_raw) if bg_raw[:2] == b'\xff\xd8' else _decode_png(bg_raw)
    tpl = _decode_png(tpl_raw) if tpl_raw[:4] == b'\x89PNG' else _decode_jpeg(tpl_raw)
    if not bg or not tpl:
        return None
    
    bw, bh, bgray = bg
    tw, th, tgray, talpha = tpl
    
    # 缩小到 60x60 加速
    N = 60
    bg_small = [[0]*N for _ in range(N)]
    tpl_small = [[0]*N for _ in range(N)]
    
    for y in range(N):
        for x in range(N):
            bx, by = int(x * bw / N), int(y * bh / N)
            bg_small[y][x] = bgray[by][bx] if by < bh and bx < bw else 128
            tx, ty = int(x * tw / N), int(y * th / N)
            tpl_small[y][x] = tgray[ty][tx] if ty < th and tx < tw else 128
    
    # 圆形遮罩索引
    cx = cy = N // 2
    radius = N // 2 - 2
    mask = []
    for y in range(N):
        for x in range(N):
            if (x - cx)**2 + (y - cy)**2 <= radius * radius:
                mask.append(y * N + x)
    
    bg_vals = [bg_small[i // N][i % N] for i in mask]
    n = len(mask)
    bg_mean = sum(bg_vals) / n
    bg_norm = [v - bg_mean for v in bg_vals]
    
    # 粗搜索 (每3度)
    best_angle, best_score = 0, -1
    for angle in range(0, 360, 3):
        rad = math.radians(angle)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        rot_vals = []
        for idx in mask:
            py, px = idx // N, idx % N
            dx, dy = px - cx, py - cy
            sx = cos_a * dx - sin_a * dy + cx
            sy = sin_a * dx + cos_a * dy + cy
            ix, iy = int(round(sx)), int(round(sy))
            if 0 <= ix < N and 0 <= iy < N:
                rot_vals.append(tpl_small[iy][ix])
            else:
                rot_vals.append(128)
        rot_mean = sum(rot_vals) / n
        rot_norm = [v - rot_mean for v in rot_vals]
        num = sum(bg_norm[i] * rot_norm[i] for i in range(n))
        den = (sum(v*v for v in bg_norm) * sum(v*v for v in rot_norm)) ** 0.5
        score = num / den if den > 0 else 0
        if score > best_score:
            best_score = score
            best_angle = angle
    
    # 精细搜索 (±3度, 每1度)
    for angle in range(max(0, best_angle - 3), min(360, best_angle + 4)):
        rad = math.radians(angle)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        rot_vals = []
        for idx in mask:
            py, px = idx // N, idx % N
            dx, dy = px - cx, py - cy
            sx = cos_a * dx - sin_a * dy + cx
            sy = sin_a * dx + cos_a * dy + cy
            ix, iy = int(round(sx)), int(round(sy))
            if 0 <= ix < N and 0 <= iy < N:
                rot_vals.append(tpl_small[iy][ix])
            else:
                rot_vals.append(128)
        rot_mean = sum(rot_vals) / n
        rot_norm = [v - rot_mean for v in rot_vals]
        num = sum(bg_norm[i] * rot_norm[i] for i in range(n))
        den = (sum(v*v for v in bg_norm) * sum(v*v for v in rot_norm)) ** 0.5
        score = num / den if den > 0 else 0
        if score > best_score:
            best_score = score
            best_angle = angle
    
    return int(best_angle)


def _solve_concat_pure(bg_b64):
    """CONCAT: 找拼接位置 (纯Python)"""
    bg_raw = base64.b64decode(bg_b64.split(',')[1] if ',' in bg_b64 else bg_b64)
    bg = _decode_jpeg(bg_raw) if bg_raw[:2] == b'\xff\xd8' else _decode_png(bg_raw)
    if not bg:
        return None
    w, h, gray = bg
    col_diff = [0.0] * w
    for x in range(1, w):
        s = 0
        for y in range(h):
            s += abs(gray[y][x] - gray[y][x-1])
        col_diff[x] = s / h
    margin = 30
    best_x = margin
    best_val = -1
    for x in range(margin, w - margin):
        if col_diff[x] > best_val:
            best_val = col_diff[x]
            best_x = x
    return int(w - best_x)


# ==================== 主 Spider ====================
class Spider(_BaseSpider):

    def init(self, extend=""):
        if isinstance(extend, list):
            extend = ""
        self.extend = extend or ""
        self.host = "https://film.symx.club"
        self.header = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 11; KB2000 Build/RP1A.201005.001) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.7827.91 Mobile Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "X-Platform": "web",
        }
        self.session = requests.Session() if requests else None
        if self.session:
            self.session.headers.update(self.header)
        self.security = {}
        self.client_id = self._gen_client_id()
        self.verify_token = ""
        self._init_security()
        self._do_verify()

    # ==================== 安全/签名 ====================

    def _gen_client_id(self):
        return "".join(format(random.randint(0, 15), "x") for _ in range(32))

    def _kw(self, hex_str):
        key = "0x1A2B3C4D5E6F7A8B9C"
        result = ""
        for i in range(0, len(hex_str), 2):
            byte_val = int(hex_str[i:i + 2], 16)
            result += chr(byte_val ^ ord(key[(i // 2) % len(key)]))
        return result

    def _init_security(self):
        try:
            resp = self.session.get(f"{self.host}/api/system/config", timeout=10)
            data = resp.json()["data"]
            self.security = {
                "reportId": self._kw(data["reportId"]),
                "traceId": self._kw(data["traceId"]),
                "session": self._kw(data["session"]),
            }
        except Exception:
            self.security = {"reportId": "X-Sign-X", "traceId": "tsp", "session": ""}

    def _gen_timestamp(self):
        ts = str(int(time.time() * 1000))
        tsw = ts[:-1]
        return tsw + str(sum(int(d) for d in tsw) % 10)

    def _sign(self, url, timestamp):
        path = url.split("?")[0]
        a = "symx_" + self.security["session"]
        s = {"p": path, "t": timestamp, "s": a}
        template = self.security["traceId"]
        u = "".join(s[c] for c in template)
        u = u.replace("1", "i").replace("0", "o").replace("5", "s")
        return hmac.new(self.security["session"].encode(), u.encode(), hashlib.sha256).hexdigest()

    def _headers(self, url):
        ts = self._gen_timestamp()
        sign = self._sign(url, ts)
        return {"X-Timestamp": ts, self.security["reportId"]: sign,
                "X-Verify-Token": self.verify_token, "X-Client-Id": self.client_id}

    # ==================== API 请求 ====================

    def _get(self, path, params=None):
        url = path
        if params:
            url = f"{path}?{'&'.join(f'{k}={v}' for k,v in params.items())}"
        try:
            resp = self.session.get(f"{self.host}/api{url}", headers=self._headers(url), timeout=10)
            return resp.json()
        except Exception:
            return {"code": -1, "message": "error", "data": None}

    def _post(self, path, data=None):
        url = path
        headers = self._headers(url)
        headers["Content-Type"] = "application/json;charset=utf-8"
        try:
            resp = self.session.post(f"{self.host}/api{url}", json=data, headers=headers, timeout=10)
            return resp.json()
        except Exception:
            return {"code": -1, "message": "error", "data": None}

    # ==================== 验证码处理 ====================

    def _do_verify(self, max_attempts=15):
        for attempt in range(max_attempts):
            result = self._post("/auth/verify/generate")
            if result.get("code") != 200 or not result.get("data"):
                time.sleep(0.3)
                continue

            captcha = result["data"]
            captcha_id = captcha["id"]
            ctype = captcha.get("type", "")
            bg_w = int(captcha.get("backgroundImageWidth") or 600)
            bg_h = int(captcha.get("backgroundImageHeight") or 380)
            tpl_w = int(captcha.get("templateImageWidth") or 0)
            tpl_h = int(captcha.get("templateImageHeight") or 0)
            bg_b64 = captcha.get("backgroundImage", "")
            tpl_b64 = captcha.get("templateImage", "") or ""

            if ctype == "SLIDER":
                end = bg_w - tpl_w if tpl_w else 490
            elif ctype in ("ROTATE", "ROTATE_DEGREE"):
                end = 360
            elif ctype == "CONCAT":
                end = bg_w
            else:
                end = bg_w

            # 求解验证码 (优先 cv2 > PIL > 纯Python)
            move_x = None
            if ctype == "SLIDER":
                if HAS_CV2:
                    move_x = self._solve_slider_cv2(bg_b64, tpl_b64)
                move_x = move_x if move_x is not None else _solve_slider_pure(bg_b64, tpl_b64)
            elif ctype in ("ROTATE", "ROTATE_DEGREE"):
                if HAS_CV2:
                    move_x = self._solve_rotate_cv2(bg_b64, tpl_b64, end)
                move_x = move_x if move_x is not None else _solve_rotate_pure(bg_b64, tpl_b64)
            elif ctype == "CONCAT":
                move_x = _solve_concat_pure(bg_b64)

            if move_x is None or move_x < 0:
                time.sleep(0.3)
                continue

            move_x = max(0, min(int(move_x), end))
            track_list, start_time, stop_time = self._gen_track(move_x)

            verify_data = self._to_native({
                "startTime": start_time, "stopTime": stop_time,
                "trackList": track_list,
                "movePercent": round(move_x / end, 4) if end > 0 else 0,
                "clickCount": 0, "bgImageWidth": bg_w, "bgImageHeight": bg_h,
                "templateImageWidth": tpl_w, "templateImageHeight": tpl_h,
                "end": end, "moveX": move_x,
                "startX": track_list[0]["x"], "startY": track_list[0]["y"],
            })

            result = self._post("/auth/verify", {"id": captcha_id, "data": verify_data})
            if result.get("code") == 200 and result.get("data"):
                token = result["data"].get("token", "") if isinstance(result["data"], dict) else str(result["data"])
                if not token and isinstance(result["data"], str):
                    token = result["data"]
                self.verify_token = token
                return True
            time.sleep(0.3)
        return False

    def _to_native(self, obj):
        if isinstance(obj, dict):
            return {k: self._to_native(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._to_native(v) for v in obj]
        elif hasattr(obj, 'item'):
            return obj.item()
        return obj

    # cv2 后端 (可选)
    def _solve_slider_cv2(self, bg_b64, tpl_b64):
        try:
            bg = self._cv2_decode(bg_b64)
            tpl = self._cv2_decode(tpl_b64)
            if bg is None or tpl is None:
                return None
            bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY) if len(bg.shape)==3 else bg.copy()
            if len(tpl.shape)==3 and tpl.shape[2]==4:
                tpl_gray = cv2.cvtColor(tpl, cv2.COLOR_BGRA2GRAY)
                alpha = tpl[:,:,3]
                cols = np.any(alpha>0, axis=0)
                cmin, cmax = np.where(cols)[0][[0,-1]]
                tpl_c = tpl_gray[:, cmin:cmax+1]
            else:
                tpl_c = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY) if len(tpl.shape)==3 else tpl.copy()
            bg_edges = cv2.Canny(bg_gray, 100, 200)
            tpl_edges = cv2.Canny(tpl_c, 100, 200)
            res = cv2.matchTemplate(bg_edges, tpl_edges, cv2.TM_CCOEFF_NORMED)
            _, _, _, loc = cv2.minMaxLoc(res)
            return int(loc[0])
        except Exception:
            return None

    def _solve_rotate_cv2(self, bg_b64, tpl_b64, end=360):
        try:
            bg = self._cv2_decode(bg_b64)
            tpl = self._cv2_decode(tpl_b64)
            if bg is None or tpl is None:
                return None
            bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY) if len(bg.shape)==3 else bg.copy()
            tpl_gray = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY) if len(tpl.shape)==3 else tpl.copy()
            bh, bw = bg_gray.shape
            th, tw = tpl_gray.shape
            bmask = np.zeros((bh,bw), np.uint8)
            cv2.circle(bmask, (bw//2,bh//2), min(bw,bh)//2-5, 255, -1)
            tmask = np.zeros((th,tw), np.uint8)
            cv2.circle(tmask, (tw//2,th//2), min(tw,th)//2-5, 255, -1)
            be = cv2.Canny(cv2.bitwise_and(bg_gray,bg_gray,mask=bmask), 100, 200)
            te = cv2.Canny(cv2.bitwise_and(tpl_gray,tpl_gray,mask=tmask), 100, 200)
            best_a, best_s = 0, -1
            for a in range(0, 360, 2):
                M = cv2.getRotationMatrix2D((tw//2,th//2), a, 1)
                rot = cv2.warpAffine(te, M, (tw,th))
                res = cv2.matchTemplate(be, rot, cv2.TM_CCOEFF_NORMED)
                _, v, _, _ = cv2.minMaxLoc(res)
                if v > best_s:
                    best_s, best_a = v, a
            for a in range(max(0,best_a-2), min(360,best_a+3)):
                M = cv2.getRotationMatrix2D((tw//2,th//2), a, 1)
                rot = cv2.warpAffine(te, M, (tw,th))
                res = cv2.matchTemplate(be, rot, cv2.TM_CCOEFF_NORMED)
                _, v, _, _ = cv2.minMaxLoc(res)
                if v > best_s:
                    best_s, best_a = v, a
            return int(round(best_a / 360 * end))
        except Exception:
            return None

    def _cv2_decode(self, b64):
        raw = b64.split(",")[1] if "," in b64 else b64
        return cv2.imdecode(np.frombuffer(base64.b64decode(raw), np.uint8), cv2.IMREAD_UNCHANGED)

    def _gen_track(self, move_x, start_x=100, start_y=200, duration_ms=None):
        if duration_ms is None:
            duration_ms = random.randint(800, 1500)
        track = [{"x": start_x, "y": start_y, "type": "down", "t": 0}]
        steps = max(25, abs(move_x) // 3)
        for i in range(1, steps + 1):
            p = i / steps
            eased = 2*p*p if p < 0.5 else 1 - (-2*p+2)**2/2
            track.append({"x": round(start_x + move_x * eased + random.uniform(-1.5, 1.5)),
                          "y": round(start_y + random.uniform(-2, 2)),
                          "type": "move", "t": round(duration_ms * p)})
        fx = start_x + move_x
        os = random.randint(2, 6)
        track.append({"x": fx + os, "y": start_y, "type": "move", "t": duration_ms + random.randint(50,100)})
        track.append({"x": fx, "y": start_y, "type": "move", "t": duration_ms + random.randint(150,250)})
        track.append({"x": fx, "y": start_y, "type": "up", "t": duration_ms + random.randint(260,350)})
        return track, int(time.time()*1000), int(time.time()*1000) + duration_ms + 350

    # ==================== 首页 ====================

    def homeContent(self, filter):
        result = {}
        classes = []
        resp = self._get("/film/category")
        if resp.get("code") == 200 and resp.get("data"):
            for cat in resp["data"]:
                classes.append({"type_id": str(cat["categoryId"]), "type_name": cat["categoryName"]})
        result["class"] = classes

        filters = {}
        for cat in classes:
            resp = self._get("/film/category/filter", {"categoryId": cat["type_id"]})
            if resp.get("code") == 200 and resp.get("data"):
                cat_filters = []
                fd = resp["data"]
                if isinstance(fd, dict):
                    for key, values in fd.items():
                        if isinstance(values, list) and values:
                            cat_filters.append({
                                "key": key,
                                "name": values[0].get("groupName", key) if isinstance(values[0], dict) else key,
                                "value": [{"n": v.get("name", str(v.get("id",""))), "v": str(v.get("id",""))}
                                          if isinstance(v, dict) else {"n": str(v), "v": str(v)} for v in values]
                            })
                if cat_filters:
                    filters[cat["type_id"]] = cat_filters
        if filter:
            result["filters"] = filters

        result["list"] = self.homeVideoContent().get("list", [])
        return result

    def homeVideoContent(self):
        videos = []
        resp = self._get("/film/category")
        if resp.get("code") == 200 and resp.get("data"):
            for cat in resp["data"]:
                for film in cat.get("filmList", [])[:6]:
                    videos.append({"vod_id": str(film["id"]), "vod_name": film.get("name",""),
                                   "vod_pic": film.get("cover",""), "vod_remarks": film.get("updateStatus","")})
        return {"list": videos[:30]}

    # ==================== 分类列表 ====================

    def categoryContent(self, tid, pg, filter, extend):
        page = int(pg) if pg else 1
        params = {"categoryId": str(tid), "pageNum": str(page), "pageSize": "20"}
        if extend and isinstance(extend, dict):
            for k, v in extend.items():
                if v: params[k] = str(v)
        elif extend and isinstance(extend, str) and extend:
            try:
                for k, v in json.loads(extend).items():
                    if v: params[k] = str(v)
            except Exception:
                pass

        resp = self._get("/film/category/list", params)
        result = {"list": [], "page": str(page), "pagecount": "1", "limit": "20", "total": "0"}
        if resp.get("code") == 200 and resp.get("data"):
            data = resp["data"]
            if isinstance(data, dict):
                total = data.get("total", 0)
                result["total"] = str(total)
                result["pagecount"] = str(max(1, (total + 19) // 20))
                for item in data.get("list", []):
                    result["list"].append({"vod_id": str(item.get("id","")), "vod_name": item.get("name",""),
                                           "vod_pic": item.get("cover",""), "vod_remarks": item.get("updateStatus",""),
                                           "vod_year": item.get("year","")})
            elif isinstance(data, list):
                result["total"] = str(len(data))
                for item in data:
                    result["list"].append({"vod_id": str(item.get("id","")), "vod_name": item.get("name",""),
                                           "vod_pic": item.get("cover",""), "vod_remarks": item.get("updateStatus","")})
        return result

    # ==================== 详情页 ====================

    def detailContent(self, ids):
        if isinstance(ids, str):
            ids = [ids]
        fid = ids[0] if ids else ""

        resp = self._get("/film/detail", {"id": str(fid)})
        if resp.get("code") != 200 or not resp.get("data"):
            if resp.get("code") == 1004:
                if self._do_verify():
                    resp = self._get("/film/detail", {"id": str(fid)})

        if resp.get("code") != 200 or not resp.get("data"):
            return {"list": []}

        d = resp["data"]
        play_from, play_url = [], []
        for line in d.get("playLineList", []):
            play_from.append(line.get("playerName", ""))
            eps = []
            for ep in line.get("lines", []):
                eps.append(f'{ep.get("name", str(ep.get("index","")))}${ep.get("id","")}')
            play_url.append("#".join(eps))

        return {"list": [{
            "vod_id": str(d.get("id","")), "vod_name": d.get("name",""),
            "vod_pic": d.get("cover",""), "vod_year": d.get("year",""),
            "vod_area": d.get("other",""), "vod_actor": d.get("actor",""),
            "vod_director": d.get("director",""), "vod_content": d.get("blurb",""),
            "vod_remarks": d.get("updateStatus",""), "vod_score": d.get("doubanScore",""),
            "vod_play_from": "$$$".join(play_from), "vod_play_url": "$$$".join(play_url),
        }]}

    # ==================== 搜索 ====================

    def searchContent(self, key, quick, pg="1"):
        page = int(pg) if pg else 1
        resp = self._get("/film/search", {"keyword": key, "pageNum": str(page), "pageSize": "20"})
        if resp.get("code") != 200:
            if resp.get("code") == 1004:
                if self._do_verify():
                    resp = self._get("/film/search", {"keyword": key, "pageNum": str(page), "pageSize": "20"})
        if resp.get("code") != 200 or not resp.get("data"):
            return {"list": []}
        data = resp["data"]
        videos = []
        items = data.get("list", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        for item in items:
            videos.append({"vod_id": str(item.get("id","")), "vod_name": item.get("name",""),
                           "vod_pic": item.get("cover",""), "vod_remarks": item.get("updateStatus","")})
        return {"list": videos}

    # ==================== 播放解析 ====================

    def playerContent(self, flag, id, vipFlags):
        line_id = str(id)
        resp = self._get("/line/play/parse", {"lineId": line_id})
        if resp.get("code") != 200:
            if resp.get("code") == 1004:
                if self._do_verify():
                    resp = self._get("/line/play/parse", {"lineId": line_id})

        url = ""
        if resp.get("code") == 200 and resp.get("data"):
            pd = resp["data"]
            if isinstance(pd, str):
                url = pd
            elif isinstance(pd, dict):
                url = pd.get("url") or pd.get("playUrl") or ""

        # 降级: /line/play
        if not url:
            resp2 = self._get("/line/play", {"lineId": line_id})
            if resp2.get("code") == 200 and resp2.get("data"):
                d = resp2["data"]
                if isinstance(d, dict):
                    url = d.get("playUrl") or d.get("url") or ""

        return {
            "parse": 0,
            "playUrl": "",
            "url": url,
            "header": {"User-Agent": self.header["User-Agent"], "Referer": self.host},
            "format": "application/x-mpegURL" if ".m3u8" in url else "",
        }

    def isVideoFormat(self, url):
        if not url:
            return False
        return any(e in url.lower() for e in (".m3u8", ".mp4", ".flv", ".avi", ".mkv", ".mov", ".wmv", ".ts"))


# ==================== 模块级函数 (FongMi/TV) ====================
_spider = None

def init(extend=""):
    global _spider
    if _spider is None:
        _spider = Spider()
    _spider.init(extend)

def getName():
    return "山有木兮影视"

def isVideoFormat(url):
    return _spider.isVideoFormat(url) if _spider else False

def homeContent(filter):
    return _spider.homeContent(filter) if _spider else {"class": [], "list": []}

def homeVideoContent():
    return _spider.homeVideoContent() if _spider else {"list": []}

def categoryContent(tid, pg, filter, extend):
    return _spider.categoryContent(tid, pg, filter, extend) if _spider else {"list": [], "page": "1", "pagecount": "1", "limit": "20", "total": "0"}

def detailContent(ids):
    return _spider.detailContent(ids) if _spider else {"list": []}

def searchContent(key, quick, pg="1"):
    return _spider.searchContent(key, quick, pg) if _spider else {"list": []}

def playerContent(flag, id, vipFlags):
    return _spider.playerContent(flag, id, vipFlags) if _spider else {"parse": 0, "url": "", "header": {}}
