# -*- coding: utf-8 -*-
"""
밴드 멀티캠 편집기 (Multicam Editor)

두 대의 카메라로 찍은 공연 영상을 하나로 합칩니다.
  - 영상에 녹음된 소리를 이용해 두 영상의 싱크를 자동으로 맞춤
  - 사용자가 원하는 위치에서 카메라 A / B 전환
  - 앞뒤 잘라내기(트리밍)

필요: Python 3.9+, numpy, pillow, ffmpeg/ffprobe
"""

import os
import sys
import re
import json
import math
import glob
import shutil
import subprocess
import threading
import queue
import time
import traceback
from io import BytesIO

APP_TITLE = "밴드 멀티캠 편집기"
VERSION = "1.0"

# ----------------------------------------------------------------------------
# 외부 프로그램(ffmpeg) 찾기
# ----------------------------------------------------------------------------

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _hidden_kwargs():
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = CREATE_NO_WINDOW
    return kw


def find_tool(name):
    """PATH 또는 흔한 설치 위치에서 ffmpeg/ffprobe 실행파일을 찾는다."""
    exe = name + (".exe" if os.name == "nt" else "")

    p = shutil.which(name)
    if p:
        return p

    here = os.path.dirname(os.path.abspath(__file__))
    patterns = [
        os.path.join(here, exe),
        os.path.join(here, "ffmpeg", "bin", exe),
        os.path.join(here, "bin", exe),
    ]
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        patterns += [
            os.path.join(local, "Microsoft", "WinGet", "Packages",
                         "Gyan.FFmpeg*", "*", "bin", exe),
            os.path.join(local, "Microsoft", "WinGet", "Links", exe),
            os.path.join(pf, "ffmpeg", "bin", exe),
            r"C:\ffmpeg\bin" + os.sep + exe,
        ]
    for pat in patterns:
        for hit in sorted(glob.glob(pat)):
            if os.path.isfile(hit):
                return hit
    return None


# ----------------------------------------------------------------------------
# 미디어 정보
# ----------------------------------------------------------------------------

class MediaInfo(object):
    def __init__(self, path):
        self.path = path
        self.duration = 0.0
        self.width = 0
        self.height = 0
        self.fps = 30.0
        self.fps_str = "30"
        self.has_audio = False
        self.vcodec = ""

    @property
    def name(self):
        return os.path.basename(self.path)


def probe(ffprobe, path):
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    out = subprocess.run(cmd, capture_output=True, **_hidden_kwargs())
    if out.returncode != 0:
        raise RuntimeError("영상 정보를 읽을 수 없습니다:\n" +
                           out.stderr.decode("utf-8", "replace")[:500])
    data = json.loads(out.stdout.decode("utf-8", "replace"))

    info = MediaInfo(path)
    try:
        info.duration = float(data.get("format", {}).get("duration", 0.0))
    except (TypeError, ValueError):
        info.duration = 0.0

    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and info.width == 0:
            info.width = int(st.get("width") or 0)
            info.height = int(st.get("height") or 0)
            info.vcodec = st.get("codec_name", "")
            rate = st.get("avg_frame_rate") or st.get("r_frame_rate") or "30/1"
            info.fps_str, info.fps = _parse_rate(rate)
            # 세로 촬영(회전 메타데이터) 반영
            rot = 0
            for sd in st.get("side_data_list", []) or []:
                if "rotation" in sd:
                    try:
                        rot = int(abs(float(sd["rotation"]))) % 180
                    except (TypeError, ValueError):
                        rot = 0
            if rot == 90:
                info.width, info.height = info.height, info.width
            if info.duration <= 0 and st.get("duration"):
                info.duration = float(st["duration"])
        elif st.get("codec_type") == "audio":
            info.has_audio = True

    if info.width == 0:
        raise RuntimeError("영상 트랙을 찾을 수 없습니다: " + os.path.basename(path))
    return info


# 표준 프레임률. 폰 촬영본은 프레임률이 들쭉날쭉(VFR)해서 평균이 59.354 같은
# 어정쩡한 값으로 나온다. 출력할 때는 가까운 표준값으로 맞춘다.
STD_FPS = [("24000/1001", 24000 / 1001.0), ("24", 24.0), ("25", 25.0),
           ("30000/1001", 30000 / 1001.0), ("30", 30.0), ("50", 50.0),
           ("60000/1001", 60000 / 1001.0), ("60", 60.0)]

FPS_CHOICES = {
    "24": ("24", 24.0),
    "25": ("25", 25.0),
    "29.97": ("30000/1001", 30000 / 1001.0),
    "30": ("30", 30.0),
    "50": ("50", 50.0),
    "59.94": ("60000/1001", 60000 / 1001.0),
    "60": ("60", 60.0),
}


# 카메라별 화면 보정 (각도 / 180 뒤집기 / 노출 / 대비 / 선명도 / 채도)
# rot 은 0 또는 180 만 쓴다. 90/270 은 가로세로가 바뀌어 출력 해상도까지
# 달라지므로 뺐다 — 기울기는 fine(1도 단위)으로 잡는다.
ADJUST_DEFAULT = {"rot": 0, "fine": 0.0, "expo": 0.0, "bright": 0.0,
                  "contrast": 1.0, "sharp": 0.0, "sat": 1.0,
                  # 화면 자르기 — 각 변에서 잘라낼 비율(%)
                  "cut_l": 0.0, "cut_r": 0.0, "cut_t": 0.0, "cut_b": 0.0}
CUT_LIMIT = 45.0                  # 한 변에서 잘라낼 수 있는 최대 비율(%)


def cut_filter(l, r, t, b):
    """네 변에서 지정한 비율(%)만큼 잘라내는 필터. 잘라낼 게 없으면 빈 문자열."""
    l, r = max(0.0, min(CUT_LIMIT, l)), max(0.0, min(CUT_LIMIT, r))
    t, b = max(0.0, min(CUT_LIMIT, t)), max(0.0, min(CUT_LIMIT, b))
    if l + r + t + b < 1e-6:
        return ""
    w, h = (100.0 - l - r) / 100.0, (100.0 - t - b) / 100.0
    return "crop=iw*%.6f:ih*%.6f:iw*%.6f:ih*%.6f" % (w, h, l / 100.0, t / 100.0)
FINE_LIMIT = 45.0                 # 미세 각도 범위 (90도 단위 회전과 합쳐 전 방향 가능)


def fine_zoom(deg, w, h):
    """
    화면을 deg 도 돌렸을 때, 검은 모서리가 안 보이게 하려면 몇 배로 확대해야 하는지.

    w×h 화면을 deg 만큼 돌린 안쪽에 들어가는 '같은 비율의 가장 큰 직사각형'을
    찾는 문제다. 되돌려 생각하면, 잘라낼 직사각형을 -deg 돌렸을 때의 외곽 상자가
    원본 안에 들어가야 한다:
        (w/z)·|cos| + (h/z)·|sin| <= w   그리고   (w/z)·|sin| + (h/z)·|cos| <= h

    가로/세로를 맞바꿔도 두 식이 서로 뒤바뀔 뿐이라 결과는 같다
    (그래서 90/270 회전을 먼저 했는지는 따질 필요가 없다).
    """
    a = math.radians(deg)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    w, h = float(w), float(h)
    return max((w * c + h * s) / w, (w * s + h * c) / h)


def cam_vf(rot=0, sharp=0.0, sat=1.0, fine=0.0, size=None,
           bright=0.0, contrast=1.0, expo=0.0,
           cut_l=0.0, cut_r=0.0, cut_t=0.0, cut_b=0.0):
    """
    카메라 한 대에 적용할 화면 보정 필터를 만든다.

    반드시 크기 조정(scale/pad) '앞'에 붙여야 한다 — 각도를 잡은 뒤 잘라내야
    하는데, 크기를 먼저 맞추면 잘라낸 만큼 화면이 찌그러진다.
    보정할 것이 없으면 빈 문자열을 돌려준다.

    size: 원본 (가로, 세로). 미세 각도의 확대 비율을 정하는 데만 쓴다.
    """
    f = []
    if rot == 180:
        f.append("hflip")                # transpose 두 번보다 싸다
        f.append("vflip")

    if abs(fine) > 1e-3:
        w, h = size or (16.0, 9.0)
        z = fine_zoom(fine, w, h)
        # 화면 크기는 그대로 두고 돌린 뒤(모서리는 검게 남음),
        # 가운데만 잘라내 검은 부분을 없앤다. 잘라낸 비율은 원본과 같다.
        f.append("rotate=%.6f:ow=iw:oh=ih:c=black" % math.radians(fine))
        f.append("crop=iw/%.6f:ih/%.6f" % (z, z))

    # 각도를 바로잡은 뒤에 자른다 (기울기 보정으로 잘린 가장자리를 또 자르지 않도록)
    cut = cut_filter(cut_l, cut_r, cut_t, cut_b)
    if cut:
        f.append(cut)

    # 노출은 배수(EV), 명도는 더하기 — 성격이 달라 필터도 따로다.
    if abs(expo) > 1e-3:
        f.append("exposure=exposure=%.3f" % expo)

    # 명도·대비·채도는 eq 하나로 처리한다 (필터를 세 번 태울 필요가 없다)
    eqp = []
    if abs(contrast - 1.0) > 1e-3:
        eqp.append("contrast=%.3f" % contrast)
    if abs(bright) > 1e-3:
        eqp.append("brightness=%.3f" % bright)
    if abs(sat - 1.0) > 1e-3:
        eqp.append("saturation=%.3f" % sat)
    if eqp:
        f.append("eq=" + ":".join(eqp))

    if abs(sharp) > 1e-3:
        # 밝기(luma)만 손댄다. 색까지 건드리면 색 경계에 테두리가 생긴다.
        # 돌리면 보간 때문에 살짝 흐려지므로 선명도는 맨 뒤에 둔다.
        f.append("unsharp=5:5:%.3f:5:5:0" % sharp)
    return ",".join(f)


def adjust_vf(adjust, cam, size=None):
    """{'A': {...}, 'B': {...}} 형태의 설정에서 한 카메라의 필터를 만든다."""
    a = dict(ADJUST_DEFAULT)
    a.update((adjust or {}).get(cam) or {})
    return cam_vf(int(a["rot"]), float(a["sharp"]), float(a["sat"]),
                  float(a["fine"]), size,
                  float(a["bright"]), float(a["contrast"]), float(a["expo"]),
                  float(a["cut_l"]), float(a["cut_r"]),
                  float(a["cut_t"]), float(a["cut_b"]))


def vf_join(pre, rest):
    """보정 필터를 나머지 필터 앞에 이어 붙인다(보정이 없으면 그대로)."""
    return (pre + "," + rest) if pre else rest


def frame_stats(np, img):
    """한 프레임의 (평균 밝기, 밝기 표준편차, 평균 채도). 모두 0~1 기준."""
    a = np.asarray(img).astype(np.float64) / 255.0
    y = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    chroma = a.max(axis=2) - a.min(axis=2)      # eq 의 saturation 에 정비례한다
    return float(y.mean()), float(y.std()), float(chroma.mean())


def solve_match(ref, tgt):
    """
    tgt 영상을 ref 영상처럼 보이게 하는 (명도, 대비, 채도)를 계산한다.

    eq 필터의 계산식을 그대로 뒤집어 푼 것이다:
        밝기_출력 = 대비 × (밝기_입력 − 0.5) + 0.5 + 명도
        채도_출력 = 채도계수 × 채도_입력
    대비는 표준편차(밝기가 퍼진 정도)를, 명도는 평균을, 채도는 평균 채도를 맞춘다.
    """
    rm, rs, rc = ref
    tm, ts, tc = tgt
    contrast = max(0.1, min(3.0, (rs / ts) if ts > 1e-4 else 1.0))
    bright = max(-1.0, min(1.0, rm - (contrast * (tm - 0.5) + 0.5)))
    sat = max(0.0, min(3.0, (rc / tc) if tc > 1e-4 else 1.0))
    return bright, contrast, sat


def snap_fps(v):
    """측정된 평균 프레임률을 가까운 표준 프레임률로 맞춘다(1.5% 이내일 때만)."""
    best = None
    for s, f in STD_FPS:
        d = abs(f - v) / f
        if d < 0.015 and (best is None or d < best[0]):
            best = (d, s, f)
    if best:
        return best[1], best[2]
    return "%.6f" % v, v


def _parse_rate(rate):
    """'30000/1001' -> ('30000/1001', 29.97)"""
    try:
        if "/" in rate:
            num, den = rate.split("/")
            num, den = float(num), float(den)
            if den == 0:
                return "30", 30.0
            val = num / den
        else:
            val = float(rate)
        if val <= 0 or val > 240:
            return "30", 30.0
        return rate, val
    except (TypeError, ValueError):
        return "30", 30.0


def fmt_time(t, fps=None):
    """초 -> 'MM:SS.mmm' (또는 프레임 표기)"""
    if t is None:
        return "--:--"
    neg = t < 0
    t = abs(float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    if h:
        txt = "%d:%02d:%06.3f" % (h, m, s)
    else:
        txt = "%02d:%06.3f" % (m, s)
    return ("-" if neg else "") + txt


def parse_time(text):
    """'1:23.5' / '83.5' -> 초"""
    text = (text or "").strip()
    if not text:
        raise ValueError("빈 값")
    sign = -1.0 if text.startswith("-") else 1.0
    text = text.lstrip("+-")
    parts = text.split(":")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return sign * total


# ----------------------------------------------------------------------------
# 오디오 싱크 (음성 파형 상호상관)
# ----------------------------------------------------------------------------

SR = 8000          # 분석용 샘플레이트
HOP = 80           # 10ms 단위 엔벌로프


class SyncResult(object):
    def __init__(self, offset, confidence, env_a, env_b, hop_sec):
        self.offset = offset            # B_시간 = A_시간 + offset
        self.confidence = confidence
        self.env_a = env_a
        self.env_b = env_b
        self.hop_sec = hop_sec


def _read_pcm_stream(ffmpeg, path, start=None, dur=None, sr=SR):
    """모노 float32 PCM 을 stdout 으로 뽑아내는 프로세스를 만든다."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start is not None:
        cmd += ["-ss", "%.6f" % start]
    cmd += ["-i", path]
    if dur is not None:
        cmd += ["-t", "%.6f" % dur]
    cmd += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, **_hidden_kwargs())


def load_envelope(ffmpeg, path, np, progress=None, duration=None):
    """전체 오디오의 10ms RMS 엔벌로프를 메모리 효율적으로 계산."""
    proc = _read_pcm_stream(ffmpeg, path)
    env = []
    leftover = b""
    read_samples = 0
    chunk_bytes = 4 * SR * 10  # 10초씩
    try:
        while True:
            buf = proc.stdout.read(chunk_bytes)
            if not buf:
                break
            buf = leftover + buf
            n = len(buf) // 4
            usable = (n // HOP) * HOP
            leftover = buf[usable * 4:]
            if usable == 0:
                continue
            arr = np.frombuffer(buf[:usable * 4], dtype="<f4")
            arr = arr.reshape(-1, HOP)
            env.append(np.sqrt((arr.astype(np.float64) ** 2).mean(axis=1)))
            read_samples += usable
            if progress and duration:
                progress(min(0.99, read_samples / float(SR) / duration))
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()
    if not env:
        return np.zeros(0, dtype=np.float64)
    return np.concatenate(env)


def load_raw(ffmpeg, path, start, dur, np, sr=SR):
    proc = _read_pcm_stream(ffmpeg, path, start, dur, sr)
    data = proc.stdout.read()
    proc.stdout.close()
    proc.wait()
    return np.frombuffer(data, dtype="<f4").astype(np.float64)


def _norm(x, np):
    x = x - x.mean()
    s = x.std()
    if s < 1e-12:
        return x
    return x / s


def _xcorr_lag(a, b, np, phat=True, guard=30):
    """
    corr[k] = sum a[i+k] * b[i] 의 최대 지점 k 를 찾는다.
    a[i+k] ≈ b[i] 이므로  b 의 i 번째가 a 의 i+k 번째와 같음.

    PHAT(위상 변환) 가중을 쓴다. 밴드 연주처럼 박자가 규칙적인 소리는
    일반 상관을 쓰면 한 박자/두 박자 어긋난 지점의 값이 정답과 거의 같아져
    엉뚱한 위치에 정렬되는 일이 잦다. PHAT 은 크기 스펙트럼을 평탄화해
    정답 지점만 뾰족하게 남긴다.

    반환: (lag, margin)
      margin = 피크 / (피크 주변 guard 밖의 최대값)
               1.0 이면 다른 후보와 구별이 안 되는 상태, 클수록 확실함.
    """
    n = len(a) + len(b)
    N = 1
    while N < n:
        N <<= 1
    R = np.fft.rfft(a, N) * np.conj(np.fft.rfft(b, N))
    if phat:
        R = R / (np.abs(R) + 1e-9)
    cc = np.fft.irfft(R, N)

    idx = int(np.argmax(cc))
    peak = float(cc[idx])
    lag = idx if idx <= N // 2 else idx - N

    masked = cc.copy()
    masked[np.arange(idx - guard, idx + guard + 1) % N] = -np.inf
    second = float(masked.max())
    margin = peak / second if second > 1e-12 else 99.0
    return lag, max(0.0, margin)


def estimate_offset(ffmpeg, path_a, path_b, np, dur_a=None, dur_b=None,
                    progress=None, refine=True):
    """
    두 영상의 시간 차이를 계산한다.
    반환 SyncResult.offset:  B_시간 = A_시간 + offset
    """
    def p(frac, msg):
        if progress:
            progress(frac, msg)

    p(0.02, "A 영상 소리 분석 중...")
    env_a = load_envelope(ffmpeg, path_a, np,
                          progress=lambda f: p(0.02 + 0.34 * f, "A 영상 소리 분석 중..."),
                          duration=dur_a)
    p(0.38, "B 영상 소리 분석 중...")
    env_b = load_envelope(ffmpeg, path_b, np,
                          progress=lambda f: p(0.38 + 0.34 * f, "B 영상 소리 분석 중..."),
                          duration=dur_b)

    if len(env_a) < 10 or len(env_b) < 10:
        raise RuntimeError("두 영상 모두에 소리가 있어야 자동 싱크가 가능합니다.")

    p(0.74, "파형 비교 중...")
    hop_sec = HOP / float(SR)
    a = _norm(env_a.copy(), np)
    b = _norm(env_b.copy(), np)
    lag, conf = _xcorr_lag(a, b, np, guard=int(0.3 / hop_sec))
    offset = -lag * hop_sec

    if refine:
        p(0.88, "정밀 보정 중...")
        try:
            offset2 = _refine(ffmpeg, path_a, path_b, offset, np,
                              dur_a or 0, dur_b or 0)
            if offset2 is not None and abs(offset2 - offset) < 0.25:
                offset = offset2
        except Exception:
            pass

    p(1.0, "완료")
    return SyncResult(offset, conf, env_a, env_b, hop_sec)


def _refine(ffmpeg, path_a, path_b, coarse, np, dur_a, dur_b, window=40.0):
    """거친 정렬 결과 주변에서 원본 파형(GCC-PHAT)으로 정밀 보정."""
    lo = max(0.0, -coarse)
    hi = min(dur_a, dur_b - coarse)
    if hi - lo < 5.0:
        return None
    start = lo + (hi - lo - min(window, hi - lo)) / 2.0
    dur = min(window, hi - lo)

    a = load_raw(ffmpeg, path_a, start, dur, np)
    b = load_raw(ffmpeg, path_b, start + coarse, dur, np)
    n = min(len(a), len(b))
    if n < SR * 2:
        return None
    a, b = _norm(a[:n], np), _norm(b[:n], np)
    lag, _ = _xcorr_lag(a, b, np, phat=True, guard=int(SR * 0.01))
    if abs(lag) > SR * 0.3:
        return None
    return coarse - lag / float(SR)


# ----------------------------------------------------------------------------
# 프레임 미리보기 워커
# ----------------------------------------------------------------------------

class FrameWorker(threading.Thread):
    """가장 최근 요청만 처리하는 프레임 추출 스레드."""

    def __init__(self, ffmpeg, on_frame):
        threading.Thread.__init__(self, daemon=True)
        self.ffmpeg = ffmpeg
        self.on_frame = on_frame
        self._cv = threading.Condition()
        self._req = None
        self._stop = False

    def request(self, path, t, width, token, vf_pre=""):
        with self._cv:
            self._req = (path, t, width, token, vf_pre)
            self._cv.notify()

    def stop(self):
        with self._cv:
            self._stop = True
            self._cv.notify()

    def run(self):
        while True:
            with self._cv:
                while self._req is None and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                req = self._req
                self._req = None
            path, t, width, token, vf_pre = req
            img = None
            try:
                img = grab_frame(self.ffmpeg, path, t, width, vf_pre)
            except Exception:
                img = None
            self.on_frame(token, img)


def grab_frame(ffmpeg, path, t, width, vf_pre="", lossless=False):
    """지정한 시각의 한 프레임을 PIL 이미지로 뽑는다. 실패하면 None.

    lossless=True 면 png 로 받는다(색을 재는 용도 — mjpeg 압축 오차를 피한다)."""
    from PIL import Image
    t = max(0.0, float(t))
    vf = vf_join(vf_pre, "scale=%d:-2" % max(160, int(width)))
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-ss", "%.3f" % t, "-i", path,
           "-frames:v", "1", "-vf", vf, "-f", "image2pipe"]
    cmd += ["-vcodec", "png"] if lossless else ["-vcodec", "mjpeg", "-q:v", "4"]
    cmd += ["-"]
    out = subprocess.run(cmd, capture_output=True, **_hidden_kwargs())
    if out.returncode != 0 or not out.stdout:
        return None
    return Image.open(BytesIO(out.stdout)).convert("RGB")


# ----------------------------------------------------------------------------
# 미리보기 재생 (소리 + 두 카메라 동시 재생)
# ----------------------------------------------------------------------------

class FrameBuf(object):
    """디코딩 스레드 -> 화면 사이의 프레임 버퍼. 가득 차면 디코딩을 잠시 멈춘다."""

    def __init__(self, maxlen):
        self.dq = []
        self.maxlen = maxlen
        self.cv = threading.Condition()
        self.closed = False

    def put(self, ts, buf, stop_evt):
        with self.cv:
            while len(self.dq) >= self.maxlen and not self.closed:
                if stop_evt.is_set():
                    return False
                self.cv.wait(0.1)
            if self.closed:
                return False
            self.dq.append((ts, buf))
            self.cv.notify_all()
            return True

    def take_until(self, now):
        """now 시각까지의 프레임 중 마지막 것만 남기고 버린다(늦으면 건너뜀)."""
        got = None
        with self.cv:
            while self.dq and self.dq[0][0] <= now + 1e-6:
                got = self.dq.pop(0)[1]
            self.cv.notify_all()
        return got

    def close(self):
        with self.cv:
            self.closed = True
            self.dq = []
            self.cv.notify_all()


class AudioOut(object):
    """ffmpeg 로 뽑은 PCM 을 사운드카드로 내보내고, 재생 위치를 알려준다."""

    def __init__(self, ffmpeg, path, start, stop_evt):
        """ffmpeg 디코딩만 먼저 띄운다. 실제 소리 출력은 open() 에서 시작.
        영상 디코딩이 준비되는 동안 오디오 디코딩도 같이 돌게 해서
        재생 버튼을 누른 뒤 기다리는 시간을 줄인다."""
        import sounddevice as sd
        self.sd = sd
        self.stop_evt = stop_evt
        self.ch = 2
        self.bpf = self.ch * 2                      # int16 스테레오
        self.lat = 0.0
        self.stream = None
        try:
            dev = sd.query_devices(kind="output")
            self.sr = int(dev["default_samplerate"]) or 48000
        except Exception:
            self.sr = 48000

        self.buf = bytearray()
        self.lock = threading.Lock()
        self.played = 0
        self.eof = False
        self.done = False

        self.proc = subprocess.Popen(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-ss", "%.6f" % max(0.0, start), "-i", path,
             "-vn", "-ac", str(self.ch), "-ar", str(self.sr), "-f", "s16le", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **_hidden_kwargs())
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def open(self):
        # 재생 시작 직후 끊기지 않도록 0.2초 정도 미리 채운다
        need = int(self.sr * 0.2) * self.bpf
        deadline = time.time() + 3.0
        while time.time() < deadline and not self.stop_evt.is_set():
            with self.lock:
                if len(self.buf) >= need:
                    break
            if self.eof:
                break
            time.sleep(0.01)

        # 소리를 한 바이트도 못 받았으면(코덱을 못 열거나, 오디오가 영상보다 짧아
        # 시작 지점에 이미 소리가 없는 경우) 소리 없이 재생하도록 실패를 알린다.
        # 그냥 두면 첫 콜백에서 done 이 서서 재생이 시작하자마자 멈춰 버린다.
        with self.lock:
            empty = not self.buf
        if empty and self.eof:
            raise RuntimeError("이 지점에서 읽을 소리가 없습니다")

        self.stream = self.sd.RawOutputStream(
            samplerate=self.sr, channels=self.ch, dtype="int16",
            latency="low", callback=self._cb)
        self.stream.start()
        try:
            self.lat = float(self.stream.latency or 0.0)
        except Exception:
            self.lat = 0.0

    def _read(self):
        chunk = self.sr * self.bpf // 4          # 0.25초씩
        try:
            while not self.stop_evt.is_set():
                data = self.proc.stdout.read(chunk)
                if not data:
                    break
                # 버퍼가 너무 커지지 않게 (약 4초 분량에서 대기)
                while not self.stop_evt.is_set():
                    with self.lock:
                        if len(self.buf) < self.sr * self.bpf * 4:
                            self.buf.extend(data)
                            break
                    time.sleep(0.02)
        except Exception:
            pass
        finally:
            self.eof = True

    def _cb(self, outdata, frames, time_info, status):
        need = frames * self.bpf
        with self.lock:
            n = min(need, len(self.buf))
            if n:
                outdata[:n] = bytes(self.buf[:n])
                del self.buf[:n]
            self.played += n // self.bpf
            empty = not self.buf
        if n < need:
            outdata[n:] = b"\x00" * (need - n)
        if empty and self.eof:
            self.done = True

    def position(self):
        """소리가 실제로 들리고 있는 지점(초). 이것이 재생의 기준 시계."""
        return max(0.0, self.played / float(self.sr) - self.lat)

    def alive(self):
        """소리 장치가 아직 살아 있는가. (재생 중 블루투스가 끊기면 False)"""
        try:
            return bool(self.stream is not None and self.stream.active)
        except Exception:
            return False

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
        _kill(self.proc)


def _kill(proc):
    if not proc:
        return
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    for f in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        try:
            if f:
                f.close()
        except Exception:
            pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


class Player(object):
    """
    미리보기 재생기.

    두 카메라 영상을 각각 하나의 ffmpeg 프로세스로 계속 디코딩해서 받아온다
    (프레임마다 ffmpeg 을 새로 띄우면 1080p 는 절대 따라가지 못한다).
    소리는 고른 카메라에서 재생하고, '소리가 어디까지 나갔는지'를 기준 시계로
    삼아 그 시각에 해당하는 영상 프레임을 보여준다.
    """

    def __init__(self, ffmpeg):
        self.ff = ffmpeg
        self.active = False
        self.audio = None
        self.audio_error = None
        self._audio_end = None       # 소리가 먼저 끝난 지점 (위치초, 그때의 벽시계)
        self._audio_seen = None      # 소리 위치가 마지막으로 움직인 기록 (멈춤 감지용)
        self.bufs = {}
        self.procs = {}
        self.stop_evt = threading.Event()
        self.t0 = 0.0
        self.box = (640, 360)
        self.vfps = 30.0
        self.wall0 = None
        self.last = {}

    # -- 시작/정지 ---------------------------------------------------------

    def start(self, sources, t0, box, vfps=30.0, audio=None, filters=None):
        """
        sources: {'A': (경로, 이 카메라에서 읽기 시작할 시각), ...}
                 시각이 음수면 0 으로 당기고 첫 프레임의 기준시각을 보정한다.
        audio:   (경로, 읽기 시작할 시각) 또는 None
        filters: {'A': '보정 필터 문자열', ...} — 크기 맞춤 앞에 붙는다
        """
        filters = filters or {}
        self.stop()
        self.stop_evt = threading.Event()
        self.t0 = t0
        self.box = box
        self.vfps = float(vfps)
        self.last = {}
        self.audio_error = None
        self._audio_end = None
        self._audio_seen = None
        self.frame_bytes = box[0] * box[1] * 3

        for cam, (path, src) in sources.items():
            used = max(0.0, src)
            ts0 = t0 + (used - src)          # 이 카메라 첫 프레임의 기준(A) 시각
            buf = FrameBuf(maxlen=int(self.vfps * 1.5))
            proc = subprocess.Popen(
                [self.ff, "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-ss", "%.6f" % used, "-i", path, "-an",
                 "-vf", vf_join(filters.get(cam) or "",
                                "scale=%d:%d:force_original_aspect_ratio=decrease,"
                                "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,fps=%s"
                                % (box[0], box[1], box[0], box[1],
                                   _fps_str(self.vfps))),
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **_hidden_kwargs())
            self.procs[cam] = proc
            self.bufs[cam] = buf
            threading.Thread(target=self._decode, args=(cam, proc, buf, ts0),
                             daemon=True).start()

        # 오디오 디코딩은 영상과 동시에 시작해 두고(대기 시간 절약),
        pending = None
        if audio:
            try:
                pending = AudioOut(self.ff, audio[0], audio[1], self.stop_evt)
            except ImportError:
                self.audio_error = "소리 재생 모듈(sounddevice)이 없습니다 — setup.bat 을 다시 실행하세요"
            except Exception as e:
                self.audio_error = str(e)

        # 영상 첫 프레임이 준비된 뒤에 소리를 튼다.
        # (소리가 기준 시계이므로, 먼저 틀면 시작 부분 영상이 건너뛰어진다)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if all(len(b.dq) > 0 for b in self.bufs.values()):
                break
            time.sleep(0.01)

        if pending is not None:
            try:
                pending.open()
                self.audio = pending
            except Exception as e:
                self.audio_error = str(e)
                pending.close()

        self.wall0 = time.time()
        self.active = True

    def _decode(self, cam, proc, buf, ts0):
        n = 0
        size = self.frame_bytes
        try:
            while not self.stop_evt.is_set():
                data = _read_exact(proc.stdout, size)
                if data is None:
                    break
                if not buf.put(ts0 + n / self.vfps, data, self.stop_evt):
                    break
                n += 1
        except Exception:
            pass

    def stop(self):
        self.active = False
        self.stop_evt.set()
        if self.audio:
            self.audio.close()
            self.audio = None
        for buf in self.bufs.values():
            buf.close()
        for proc in self.procs.values():
            _kill(proc)
        self.bufs = {}
        self.procs = {}
        self.last = {}

    # -- 조회 ---------------------------------------------------------------

    def _audio_is_clock(self):
        """소리가 아직 기준 시계 노릇을 하고 있는지."""
        a = self.audio
        if a.done:
            return False
        pos = a.position()
        if self._audio_seen is None or pos > self._audio_seen[0] + 1e-4:
            self._audio_seen = (pos, time.time())
            return True
        # 위치가 멈췄다. 장치까지 죽었으면(블루투스 끊김 등) 시계를 넘긴다.
        # 단순히 버퍼가 잠깐 빈 것일 수 있으므로 두 조건을 모두 본다.
        return not (time.time() - self._audio_seen[1] > 1.5 and not a.alive())

    def time(self):
        if self.audio is not None:
            if self._audio_end is None and self._audio_is_clock():
                return self.t0 + self.audio.position()
            # 소리가 영상보다 먼저 끝났거나 장치가 죽었다. 여기서 멈추면 남은
            # 영상을 못 보므로, 그 지점을 이어받아 벽시계로 계속 센다.
            if self._audio_end is None:
                self._audio_end = (self.audio.position(), time.time())
            pos, wall = self._audio_end
            return self.t0 + pos + (time.time() - wall)
        if self.wall0 is None:
            return self.t0
        return self.t0 + (time.time() - self.wall0)

    def frame(self, cam, now):
        """now 시각에 보여줄 프레임(bytes). 새 프레임이 없으면 직전 것을 준다."""
        buf = self.bufs.get(cam)
        if buf is None:
            return self.last.get(cam)
        got = buf.take_until(now)
        if got is not None:
            self.last[cam] = got
        return self.last.get(cam)


def _read_exact(stream, size):
    """rawvideo 파이프에서 정확히 한 프레임을 읽는다."""
    chunks = []
    left = size
    while left > 0:
        b = stream.read(left)
        if not b:
            return None
        chunks.append(b)
        left -= len(b)
    return b"".join(chunks) if len(chunks) > 1 else chunks[0]


def _fps_str(fps):
    if abs(fps - round(fps)) < 1e-6:
        return str(int(round(fps)))
    return "%.6f" % fps


def _fps_value(fps_str):
    """'60000/1001' 또는 '30' -> 실수"""
    try:
        if "/" in fps_str:
            a, b = fps_str.split("/")
            return float(a) / float(b)
        return float(fps_str)
    except (TypeError, ValueError, ZeroDivisionError):
        return 30.0


# ----------------------------------------------------------------------------
# 편집 계산 (GUI 와 분리된 순수 로직 — 자체 점검에서 그대로 사용)
# ----------------------------------------------------------------------------

def cam_ranges(dur_a, dur_b, offset):
    """A 기준 시간으로 각 카메라가 화면을 가진 범위. (B 시간 = A 시간 + offset)"""
    return {"A": (0.0, float(dur_a)), "B": (-offset, float(dur_b) - offset)}


def union_span(ranges):
    """
    두 카메라를 합쳐 쓸 수 있는 전체 시간 범위.
    한쪽만 찍힌 구간(늦게 켜거나 먼저 끈 구간)도 포함한다.
    아예 겹치는 데가 없으면 이어 붙일 수 없으므로 긴 쪽만 쓴다.
    """
    a, b = ranges["A"], ranges["B"]
    if a[1] < b[0] or b[1] < a[0]:
        return a if (a[1] - a[0]) >= (b[1] - b[0]) else b
    return (min(a[0], b[0]), max(a[1], b[1]))


def apply_coverage(segs, ranges, eps=1e-6):
    """
    지정된 카메라에 그 시각의 화면이 없으면 있는 쪽 카메라로 바꾼다.

    구간을 버리지는 않는다 — 버리면 뒤 장면들의 시각이 통째로 밀리기 때문이다.
    둘 다 없으면 원래 카메라를 그대로 둔다(호출 쪽에서 범위를 제한해 막는다).
    """
    edges = set()
    for lo, hi in ranges.values():
        edges.add(lo)
        edges.add(hi)

    def pick(cam, t):
        other = "B" if cam == "A" else "A"
        for c in (cam, other):
            lo, hi = ranges[c]
            if lo - eps <= t <= hi + eps:
                return c
        return cam

    out = []
    for s, e, cam in segs:
        pts = sorted(set([s, e] + [x for x in edges if s + eps < x < e - eps]))
        for i in range(len(pts) - 1):
            a0, a1 = pts[i], pts[i + 1]
            if a1 - a0 < eps:
                continue
            out.append((a0, a1, pick(cam, (a0 + a1) / 2.0)))

    merged = []
    for s, e, c in out:
        if merged and merged[-1][2] == c and abs(merged[-1][1] - s) < eps:
            merged[-1] = (merged[-1][0], e, c)
        else:
            merged.append((s, e, c))
    return merged


def compute_segments(trim_start, trim_end, cuts, start_cam):
    """
    트림 구간을 카메라별 장면으로 쪼갠다.
    cuts: [(시간, 'A'|'B')]  — 그 시점부터 해당 카메라로 전환
    반환: [(시작, 끝, 카메라)] — 빈틈/겹침 없이 구간 전체를 덮는다.
    """
    s, e = trim_start, trim_end
    if e - s <= 0:
        return []
    cuts = sorted(cuts, key=lambda c: c[0])

    def cam_at(t):
        cam = start_cam
        for ct, cc in cuts:
            if ct <= t + 1e-9:
                cam = cc
            else:
                break
        return cam

    pts = [s] + [t for t, _ in cuts if s + 1e-6 < t < e - 1e-6] + [e]
    segs = []
    for i in range(len(pts) - 1):
        a0, a1 = pts[i], pts[i + 1]
        if a1 - a0 < 1e-6:
            continue
        segs.append((a0, a1, cam_at(a0 + 1e-6)))

    merged = []
    for seg in segs:
        if merged and merged[-1][2] == seg[2]:
            merged[-1] = (merged[-1][0], seg[1], seg[2])
        else:
            merged.append(seg)
    return merged


def transition_frames(idx, fps, want_sec, mode):
    """
    각 장면 경계에서 실제로 쓸 전환 길이(프레임)를 정한다.
    양옆 장면의 절반을 넘지 않게 줄여서, 짧은 장면이 전환에 다 먹히지 않게 한다.
    반환: [0, nd_1, nd_2, ..., 0]  (양 끝 = 바깥이라 전환 없음)
    """
    n = len(idx) - 1                       # 장면 개수
    out = [0] * (n + 1)
    if mode == "none" or want_sec <= 0 or n < 2:
        return out
    want = int(round(want_sec * fps))
    if want < 2:
        return out
    for b in range(1, n):
        left = idx[b] - idx[b - 1]
        right = idx[b + 1] - idx[b]
        nd = min(want, left // 2, right // 2)
        out[b] = nd if nd >= 2 else 0
    return out


def wave_index(x, width, t0, dur, hop):
    """
    타임라인 x 좌표에 그릴 소리 엔벌로프의 위치. 눈금(x_of)과 같은 좌표계라서 어긋나지 않는다.
    타임라인이 소리보다 넓으면(한쪽 카메라만 찍힌 구간) 범위 밖 값이 나온다 — 음수이거나
    엔벌로프 길이 이상이면 그 자리에는 그릴 소리가 없다.
    """
    t = t0 + (x - 1) / float(max(1, width - 2)) * dur
    return int(math.floor(t / hop))


def build_plan(segs, path_a, path_b, offset, trim_start, trim_end,
               width, height, fps_str, fps, vcodec_args, audio_cam, output,
               tmpdir=None, fade_in=0.0, fade_out=0.0,
               trans_mode="none", trans_sec=0.0, adjust=None, sizes=None):
    """
    내보내기 작업 명세를 만든다. B 카메라 시간 = A 시간 + offset.

    fade_in / fade_out : 영상 시작·끝의 검정 페이드 길이(초). 소리도 같이 페이드.
    trans_mode : "none"(하드컷) / "dissolve"(두 화면이 겹치며 전환) / "black"(어두워졌다 밝아짐)
    trans_sec  : 전환 길이(초)
    adjust     : 카메라별 화면 보정 {'A': {'rot':0,'fine':0,'sharp':0.0,'sat':1.0}, ...}
    sizes      : 카메라별 원본 크기 {'A': (가로, 세로), ...} — 미세 각도 확대율 계산용
    """
    paths = {"A": path_a, "B": path_b}
    sizes = sizes or {}
    pre = dict((c, adjust_vf(adjust, c, sizes.get(c))) for c in ("A", "B"))

    # 장면 경계를 '트림 시작 기준 프레임 번호'로 고정한다.
    # 장면마다 길이를 따로 반올림하면 오차가 누적되어(전환이 많을수록)
    # 영상이 소리보다 점점 밀린다. 경계를 프레임 번호로 잡으면 어긋나지 않는다.
    bounds = [segs[0][0]] + [e for _, e, _ in segs]
    idx = [int(round((t - trim_start) * fps)) for t in bounds]
    for i in range(1, len(idx)):
        if idx[i] <= idx[i - 1]:
            idx[i] = idx[i - 1] + 1

    nd = transition_frames(idx, fps, trans_sec, trans_mode)
    # 전환은 경계를 가운데 두고 앞뒤로 나눠 가진다
    head = [x // 2 for x in nd]            # 경계 왼쪽 장면의 끝에서 가져갈 프레임
    tail = [x - x // 2 for x in nd]        # 경계 오른쪽 장면의 앞에서 가져갈 프레임

    def src_time(cam, frame):
        t = trim_start + frame / float(fps) + (offset if cam == "B" else 0.0)
        return max(0.0, t)

    items = []
    for i, (s, e, cam) in enumerate(segs):
        if trans_mode == "dissolve":
            f0 = idx[i] + tail[i]              # 앞쪽 전환에 내준 만큼 뒤로
            f1 = idx[i + 1] - head[i + 1]      # 뒤쪽 전환에 내줄 만큼 앞에서 끝
        else:
            f0, f1 = idx[i], idx[i + 1]
        item = {"kind": "clip", "path": paths[cam], "cam": cam,
                "src_start": src_time(cam, f0), "frames": f1 - f0,
                "vf_pre": pre[cam]}
        if trans_mode == "black":
            # 장면 안에서 어두워졌다 밝아지게 (앞뒤로 나눠 가진 길이만큼)
            if tail[i]:
                item["fade_in"] = tail[i] / float(fps)
            if head[i + 1]:
                item["fade_out"] = head[i + 1] / float(fps)
        items.append(item)

        if trans_mode == "dissolve" and i + 1 < len(segs) and nd[i + 1]:
            b = i + 1
            nxt = segs[b][2]
            items.append({
                "kind": "dissolve",
                "path": paths[cam], "src_start": src_time(cam, idx[b] - head[b]),
                "path2": paths[nxt], "src2_start": src_time(nxt, idx[b] - head[b]),
                "cam": "%s>%s" % (cam, nxt),
                "frames": nd[b],
                "vf_pre": pre[cam], "vf_pre2": pre[nxt],
            })

    total_frames = idx[-1] - idx[0]

    # 시작/끝 검정 페이드 — 첫/마지막 조각 안에서 처리
    if fade_in > 0 and items:
        d = min(fade_in, items[0]["frames"] / float(fps))
        if d > 0:
            items[0]["fade_in"] = max(items[0].get("fade_in", 0.0), d)
    if fade_out > 0 and items:
        d = min(fade_out, items[-1]["frames"] / float(fps))
        if d > 0:
            items[-1]["fade_out"] = max(items[-1].get("fade_out", 0.0), d)

    a_off = offset if audio_cam == "B" else 0.0
    return {
        "segments": items,
        "width": (width // 2) * 2, "height": (height // 2) * 2,
        "fps_str": fps_str,
        "vcodec_args": list(vcodec_args),
        "audio_path": paths[audio_cam],
        "audio_start": max(0.0, trim_start + a_off),
        "audio_fade_in": max(0.0, fade_in),
        "audio_fade_out": max(0.0, fade_out),
        "total_duration": total_frames / float(fps),
        "output": output,
        "tmpdir": tmpdir or os.path.join(os.path.dirname(output) or ".",
                                         "._multicam_tmp_%d" % int(time.time() * 1000)),
    }


# ----------------------------------------------------------------------------
# 내보내기 (세그먼트 인코딩 -> 이어붙이기 -> 오디오 합치기)
# ----------------------------------------------------------------------------

class ExportJob(object):
    def __init__(self, ffmpeg, plan, on_progress, on_done):
        self.ffmpeg = ffmpeg
        self.plan = plan
        self.on_progress = on_progress
        self.on_done = on_done
        self.proc = None
        self.cancelled = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    # -- 내부 --------------------------------------------------------------

    def _run(self, ):
        p = self.plan
        tmpdir = p["tmpdir"]
        try:
            os.makedirs(tmpdir, exist_ok=True)
            parts = []
            total_frames = sum(s["frames"] for s in p["segments"]) or 1
            done_frames = 0

            for i, seg in enumerate(p["segments"]):
                if self.cancelled:
                    raise KeyboardInterrupt
                out = os.path.join(tmpdir, "seg_%04d.mp4" % i)
                self._encode_segment(seg, out, p, i, len(p["segments"]),
                                     done_frames, total_frames)
                parts.append(out)
                done_frames += seg["frames"]

            if self.cancelled:
                raise KeyboardInterrupt

            self.on_progress(0.93, "장면 이어붙이는 중...")
            listfile = os.path.join(tmpdir, "list.txt")
            with open(listfile, "w", encoding="utf-8") as f:
                for part in parts:
                    f.write("file '%s'\n" % part.replace("\\", "/").replace("'", "'\\''"))
            silent = os.path.join(tmpdir, "video_only.mp4")
            self._run_ff([self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                          "-nostdin", "-f", "concat", "-safe", "0", "-i", listfile,
                          "-c", "copy", silent])

            if self.cancelled:
                raise KeyboardInterrupt

            self.on_progress(0.96, "소리 입히는 중...")
            cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-i", silent,
                   "-ss", "%.6f" % p["audio_start"], "-i", p["audio_path"],
                   "-map", "0:v:0", "-map", "1:a:0",
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-ac", "2",
                   "-t", "%.6f" % p["total_duration"]]
            # 화면이 검정에서 밝아지는 동안 소리도 같이 커지고, 끝에서 같이 잦아든다
            af = []
            total = p["total_duration"]
            if p.get("audio_fade_in", 0) > 0:
                af.append("afade=t=in:st=0:d=%.6f" % min(p["audio_fade_in"], total))
            if p.get("audio_fade_out", 0) > 0:
                d = min(p["audio_fade_out"], total)
                af.append("afade=t=out:st=%.6f:d=%.6f" % (max(0.0, total - d), d))
            if af:
                cmd += ["-af", ",".join(af)]
            cmd += ["-movflags", "+faststart", p["output"]]
            self._run_ff(cmd)

            self.on_progress(1.0, "완료")
            self.on_done(True, p["output"])
        except KeyboardInterrupt:
            self.on_done(False, "사용자가 취소했습니다.")
        except Exception as e:
            self.on_done(False, str(e))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def _vf_base(p, pre=""):
        """카메라 보정(pre) -> 크기 맞춤 순서. 회전이 크기 맞춤보다 먼저여야 한다."""
        return vf_join(pre,
                       "scale=%d:%d:force_original_aspect_ratio=decrease,"
                       "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=%s,format=yuv420p"
                       % (p["width"], p["height"], p["width"], p["height"],
                          p["fps_str"]))

    def _encode_segment(self, seg, out, p, idx, count, done_frames, total_frames):
        base = self._vf_base(p, seg.get("vf_pre", ""))
        dur = seg["frames"] / _fps_value(p["fps_str"])

        if seg.get("kind") == "dissolve":
            # 두 카메라를 같은 순간에서 각각 읽어와 겹쳐 넘긴다
            # (카메라마다 보정이 다를 수 있으므로 각자 다른 필터를 태운다)
            base2 = self._vf_base(p, seg.get("vf_pre2", ""))
            fc = ("[0:v]%s,setpts=PTS-STARTPTS[a];"
                  "[1:v]%s,setpts=PTS-STARTPTS[b];"
                  "[a][b]xfade=transition=fade:duration=%.6f:offset=0,"
                  "format=yuv420p[v]" % (base, base2, dur))
            cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-ss", "%.6f" % seg["src_start"], "-t", "%.6f" % (dur + 1.0),
                   "-i", seg["path"],
                   "-ss", "%.6f" % seg["src2_start"], "-t", "%.6f" % (dur + 1.0),
                   "-i", seg["path2"],
                   "-filter_complex", fc, "-map", "[v]",
                   "-an", "-frames:v", str(seg["frames"])]
        else:
            vf = base
            fi, fo = seg.get("fade_in", 0.0), seg.get("fade_out", 0.0)
            if fi > 0:
                vf += ",fade=t=in:st=0:d=%.6f" % fi
            if fo > 0:
                vf += ",fade=t=out:st=%.6f:d=%.6f" % (max(0.0, dur - fo), fo)
            cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-ss", "%.6f" % seg["src_start"], "-i", seg["path"],
                   "-an", "-vf", vf, "-frames:v", str(seg["frames"])]

        cmd += p["vcodec_args"]
        cmd += ["-r", p["fps_str"], "-progress", "pipe:1", "-nostats", out]

        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, **_hidden_kwargs())
        seg_done = 0
        for line in self.proc.stdout:
            if self.cancelled:
                break
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("frame="):
                try:
                    seg_done = int(line.split("=", 1)[1])
                except ValueError:
                    seg_done = 0
                frac = 0.90 * (done_frames + min(seg_done, seg["frames"])) / float(total_frames)
                self.on_progress(frac, "장면 %d/%d 변환 중 (카메라 %s)"
                                 % (idx + 1, count, seg["cam"]))
        err = self.proc.stderr.read().decode("utf-8", "replace")
        self.proc.stdout.close()
        self.proc.stderr.close()
        rc = self.proc.wait()
        if self.cancelled:
            raise KeyboardInterrupt
        if rc != 0:
            raise RuntimeError("장면 %d 변환 실패:\n%s" % (idx + 1, err[-800:]))

    def _run_ff(self, cmd):
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, **_hidden_kwargs())
        _, err = self.proc.communicate()
        if self.cancelled:
            raise KeyboardInterrupt
        if self.proc.returncode != 0:
            raise RuntimeError(err.decode("utf-8", "replace")[-800:])


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

COL_A = "#3a7bd5"
COL_B = "#e8743b"
COL_BG = "#1e1f24"
COL_PANEL = "#2a2c33"
COL_TEXT = "#e8e8ea"
COL_MUTED = "#9aa0a6"
COL_WAVE = "#4b5058"
COL_TRIM = "#3d4250"

TRANS_MODES = [("없음 (바로 전환)", "none"),
               ("디졸브 (겹치며 전환)", "dissolve"),
               ("검정 페이드", "black")]
TRANS_LABELS = [t[0] for t in TRANS_MODES]
TRANS_BY_LABEL = dict(TRANS_MODES)
TRANS_BY_MODE = dict((m, l) for l, m in TRANS_MODES)


class App(tk.Tk):
    def __init__(self, ffmpeg, ffprobe, np):
        tk.Tk.__init__(self)
        self.ff = ffmpeg
        self.fp = ffprobe
        self.np = np

        self.title("%s v%s" % (APP_TITLE, VERSION))

        # DPI 인식을 켜 두었으므로 화면 좌표가 실제 픽셀이다.
        # 글꼴은 pt 단위라 Tk 가 알아서 키우지만, 코드에 박힌 픽셀값은 직접 환산해야 한다.
        try:
            self._ui = max(1.0, self.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            self._ui = 1.0

        # 미리보기는 남는 세로 공간을 받는 구조라, 창이 작으면 제일 먼저 찌그러진다.
        # 그래서 화면을 최대한 쓰도록 크게 잡고 최대화 상태로 시작한다.
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = max(self.px(1000), sw - self.px(80))
        h = max(self.px(700), sh - self.px(100))
        self.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 3)))
        self.minsize(self.px(1000), self.px(640))
        try:
            self.state("zoomed")
        except tk.TclError:
            pass
        self.configure(bg=COL_BG)

        # 상태
        self.info = {"A": None, "B": None}
        self.offset = 0.0            # B_시간 = A_시간 + offset
        self.sync_conf = None
        self.env = {"A": None, "B": None}
        self.hop_sec = HOP / float(SR)
        self.trim_start = 0.0
        self.trim_end = 0.0
        self.playhead = 0.0
        self.start_cam = "A"
        self.cuts = []               # [(시간, 'A'|'B')]
        self.project_path = None

        self._photo = {"A": None, "B": None}
        self._token = {"A": 0, "B": 0}
        self._uiq = queue.Queue()
        self._export = None
        self._playing = False
        self._play_after = None
        self._player = Player(self.ff)
        self._play_photo = {}
        self._play_item = {}
        self._play_gap = {"A": None, "B": None}   # 프레임이 안 오기 시작한 시각
        self._play_blank = set()                 # "영상 없음" 을 띄워 둔 카메라
        self._play_box = None
        self._play_redraw = 0
        self._ph_items = None
        self._big_preview = False    # 편집/내보내기 패널을 접어 미리보기를 키운 상태
        self._adj_after = None       # 화면 보정 값 변경을 몰아서 반영하기 위한 예약
        self._matching = False       # 두 화면 맞추기 분석 중
        self._can_expose = self._has_filter("exposure")

        self.workers = {
            "A": FrameWorker(self.ff, lambda tok, img: self._uiq.put(("frame", "A", tok, img))),
            "B": FrameWorker(self.ff, lambda tok, img: self._uiq.put(("frame", "B", tok, img))),
        }
        for w in self.workers.values():
            w.start()

        self._style()
        self._build()
        # 최소 폭은 실제로 필요한 폭에 맞춘다. 고정값으로 두면 편집 패널의
        # 버튼이 잘려 안 보인다(화면이 아주 작을 때를 위해 상한은 둔다).
        self.update_idletasks()
        need_w = self._root_frame.winfo_reqwidth() + self.px(20)   # 스크롤바 자리
        # 화면보다 넓은 최소 크기를 요구하면 창을 화면 밖으로 밀어내므로 한 번 더 제한
        cap = min(self.px(1400), self.winfo_screenwidth() - self.px(40))
        self.minsize(max(self.px(1000), min(need_w, cap)), self.px(640))
        self._bind_keys()
        self.after(40, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def px(self, n):
        """96dpi 기준으로 적힌 픽셀값을 현재 화면 배율로 환산한다."""
        return int(round(n * self._ui))

    # -- 스타일 ------------------------------------------------------------

    def _style(self):
        fam = "Malgun Gothic" if os.name == "nt" else "Sans"
        self.font = (fam, 10)
        self.font_b = (fam, 10, "bold")
        self.font_s = (fam, 9)
        self.option_add("*Font", self.font)

        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=COL_BG, foreground=COL_TEXT,
                     fieldbackground=COL_PANEL, font=self.font)
        st.configure("TFrame", background=COL_BG)
        st.configure("Panel.TFrame", background=COL_PANEL)
        st.configure("TLabel", background=COL_BG, foreground=COL_TEXT)
        st.configure("Muted.TLabel", foreground=COL_MUTED, font=self.font_s)
        st.configure("Head.TLabel", font=(fam, 11, "bold"))
        st.configure("A.TLabel", background=COL_BG, foreground=COL_A, font=self.font_b)
        st.configure("B.TLabel", background=COL_BG, foreground=COL_B, font=self.font_b)
        st.configure("TLabelframe", background=COL_BG, foreground=COL_TEXT,
                     bordercolor="#3a3d47")
        st.configure("TLabelframe.Label", background=COL_BG, foreground=COL_MUTED,
                     font=self.font_s)
        st.configure("TButton", padding=(self.px(10), self.px(5)), background="#3a3d47",
                     foreground=COL_TEXT, borderwidth=0)
        st.map("TButton", background=[("active", "#4a4e5a"), ("disabled", "#2a2c33")],
               foreground=[("disabled", "#666")])
        st.configure("A.TButton", background=COL_A)
        st.map("A.TButton", background=[("active", "#4d8ce8")])
        st.configure("B.TButton", background=COL_B)
        st.map("B.TButton", background=[("active", "#f28650")])
        st.configure("Go.TButton", background="#2e9e5b", font=self.font_b,
                     padding=(self.px(14), self.px(9)))
        st.map("Go.TButton", background=[("active", "#37b56a"), ("disabled", "#2a2c33")])
        st.configure("TEntry", fieldbackground=COL_PANEL, foreground=COL_TEXT,
                     bordercolor="#3a3d47", insertcolor=COL_TEXT)
        # readonly 콤보박스는 선택 상태로 그려져서 글씨가 묻힌다 -> 선택색을 배경과 맞춤
        st.configure("TCombobox", fieldbackground=COL_PANEL, foreground=COL_TEXT,
                     background="#3a3d47", arrowcolor=COL_TEXT, bordercolor="#3a3d47")
        st.map("TCombobox",
               fieldbackground=[("readonly", COL_PANEL), ("disabled", COL_BG)],
               selectbackground=[("readonly", COL_PANEL), ("!readonly", COL_PANEL)],
               selectforeground=[("readonly", COL_TEXT), ("!readonly", COL_TEXT)],
               foreground=[("readonly", COL_TEXT), ("disabled", "#666")],
               background=[("active", "#4a4e5a")])
        self.option_add("*TCombobox*Listbox.background", COL_PANEL)
        self.option_add("*TCombobox*Listbox.foreground", COL_TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", "#4a5570")
        self.option_add("*TCombobox*Listbox.selectForeground", COL_TEXT)

        st.configure("Treeview", background=COL_PANEL, fieldbackground=COL_PANEL,
                     foreground=COL_TEXT, borderwidth=0, rowheight=self.px(24))
        st.configure("Treeview.Heading", background="#3a3d47", foreground=COL_TEXT,
                     borderwidth=0)
        st.map("Treeview", background=[("selected", "#4a5570")])
        for pb in ("TProgressbar", "Horizontal.TProgressbar"):
            st.configure(pb, background="#2e9e5b", troughcolor="#22242a",
                         bordercolor="#22242a", lightcolor="#2e9e5b",
                         darkcolor="#2e9e5b", borderwidth=0)
        st.configure("TScrollbar", background="#3a3d47", troughcolor="#22242a",
                     bordercolor="#22242a", arrowcolor=COL_TEXT, borderwidth=0,
                     arrowsize=self.px(14))
        st.map("TScrollbar", background=[("active", "#4a4e5a")])
        # 화살표/체크 표시는 픽셀 고정이라 고배율 화면에서 혼자 작아진다
        st.configure("TCombobox", arrowsize=self.px(14))
        st.configure("TSpinbox", arrowsize=self.px(12))
        for cb in ("TRadiobutton", "TCheckbutton"):
            st.configure(cb, background=COL_BG, foreground=COL_TEXT,
                         indicatorsize=self.px(10), indicatormargin=self.px(3))
        # 기본 표시는 안 고른 쪽이 흰 원으로 칠해져, 어두운 화면에서 선택이 반대로 읽힌다
        st.configure("TRadiobutton", upperbordercolor=COL_MUTED, lowerbordercolor=COL_MUTED)
        st.map("TRadiobutton",
               indicatorbackground=[("selected", COL_A), ("!selected", COL_BG)],
               indicatorforeground=[("selected", "#ffffff")])

    # -- 화면 구성 ----------------------------------------------------------

    def _build(self):
        P = self.px                      # 화면 배율 환산 (아래 픽셀값은 모두 96dpi 기준)

        # 전체를 스크롤 가능한 판 위에 올린다. 미리보기를 크게 키우면 아래 메뉴가
        # 창 밖으로 밀려나는데, 그때 스크롤해서 볼 수 있어야 한다.
        self._vport = tk.Canvas(self, bg=COL_BG, highlightthickness=0)
        self._vsb = ttk.Scrollbar(self, orient="vertical", command=self._vport.yview)
        self._vport.configure(yscrollcommand=self._vsb.set)
        self._vport.pack(side="left", fill="both", expand=True)

        root = ttk.Frame(self._vport, padding=P(10))
        self._root_frame = root
        self._root_win = self._vport.create_window(0, 0, window=root, anchor="nw")
        root.bind("<Configure>", lambda ev: self._sync_scroll())
        self._vport.bind("<Configure>", self._on_vport_resize)
        self.bind("<MouseWheel>", self._on_wheel)

        # 1) 파일 + 싱크
        top = ttk.Frame(root)
        top.pack(fill="x")
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)

        self.file_lbl = {}
        for col, cam in enumerate(("A", "B")):
            box = ttk.Labelframe(top, text="카메라 %s %s" % (cam, "(기준 영상)" if cam == "A" else ""),
                                 padding=P(8))
            box.grid(row=0, column=col, sticky="ew",
                     padx=(0, P(6)) if col == 0 else (P(6), 0))
            box.columnconfigure(1, weight=1)
            ttk.Button(box, text="영상 열기...",
                       command=lambda c=cam: self.open_video(c)).grid(row=0, column=0)
            lbl = ttk.Label(box, text="(선택 안 됨)", style="Muted.TLabel", anchor="w")
            lbl.grid(row=0, column=1, sticky="ew", padx=P(8))
            self.file_lbl[cam] = lbl

        sync = ttk.Frame(root)
        sync.pack(fill="x", pady=(P(8), 0))
        self.btn_sync = ttk.Button(sync, text="🔊  소리로 자동 싱크 맞추기",
                                   command=self.do_sync, state="disabled")
        self.btn_sync.pack(side="left")
        ttk.Label(sync, text="시간차 B−A(초)").pack(side="left", padx=(P(14), P(4)))
        self.var_offset = tk.StringVar(value="0.000")
        e = ttk.Entry(sync, textvariable=self.var_offset, width=11, justify="right")
        e.pack(side="left")
        e.bind("<Return>", lambda ev: self.apply_manual_offset())
        for txt, d in (("−0.1", -0.1), ("−1f", None), ("+1f", None), ("+0.1", 0.1)):
            ttk.Button(sync, text=txt, width=5,
                       command=lambda t=txt: self.nudge_offset(t)).pack(side="left", padx=P(1))
        self.sync_lbl = ttk.Label(sync, text="", style="Muted.TLabel")
        self.sync_lbl.pack(side="left", padx=P(12))

        # clam 테마는 진행 바 두께(thickness) 설정을 무시하므로, 높이를 고정한
        # 상자에 넣어 늘린다. (안 그러면 고배율 화면에서 실처럼 얇아진다)
        pbox = tk.Frame(root, height=P(18), bg=COL_BG)
        pbox.pack(fill="x", pady=(P(8), 0))
        pbox.pack_propagate(False)
        self.prog = ttk.Progressbar(pbox, mode="determinate", maximum=1000)
        self.prog.pack(fill="both", expand=True)
        self.prog_lbl = ttk.Label(root, text="영상 두 개를 열어 주세요.", style="Muted.TLabel")
        self.prog_lbl.pack(fill="x")

        # 2) 미리보기 — 남는 공간을 모두 쓰되, 아래 고정 패널들보다 나중에 배치한다.
        #    (먼저 배치하면 창이 작을 때 '내보내기' 패널이 화면 밖으로 밀려남)
        prev = ttk.Frame(root)
        prev.columnconfigure(0, weight=1)
        prev.columnconfigure(1, weight=1)
        prev.rowconfigure(1, weight=1)

        self.cam_title = {}
        self.canvas_prev = {}
        for col, cam in enumerate(("A", "B")):
            head = ttk.Label(prev, text="카메라 %s" % cam, style="Head.TLabel", anchor="center")
            head.grid(row=0, column=col, sticky="ew", pady=(0, P(4)))
            self.cam_title[cam] = head
            cv = tk.Canvas(prev, bg="#000", highlightthickness=P(2),
                           highlightbackground="#333", height=P(200))
            cv.grid(row=1, column=col, sticky="nsew",
                    padx=(0, P(5)) if col == 0 else (P(5), 0))
            cv.bind("<Configure>", lambda ev, c=cam: self._on_prev_resize(c))
            cv.bind("<Button-1>", lambda ev, c=cam: self.add_cut(c))
            self.canvas_prev[cam] = cv

        # 3) 타임라인
        tl = ttk.Labelframe(root, text="타임라인 (기준: 카메라 A 시간)", padding=P(6))
        self.tl_canvas = tk.Canvas(tl, height=P(104), bg=COL_PANEL, highlightthickness=0)
        self.tl_canvas.pack(fill="x")
        self.tl_canvas.bind("<Button-1>", self._tl_click)
        self.tl_canvas.bind("<B1-Motion>", self._tl_click)
        self.tl_canvas.bind("<Configure>", lambda ev: self.redraw_timeline())

        # 4) 이동 컨트롤
        nav = ttk.Frame(root)
        self.btn_play = ttk.Button(nav, text="▶ 재생", width=8, command=self.toggle_play)
        self.btn_play.pack(side="left")
        for txt, d in (("⏮ -10초", -10), ("-1초", -1), ("◀ 프레임", -1e-9),
                       ("프레임 ▶", 1e-9), ("+1초", 1), ("+10초 ⏭", 10)):
            ttk.Button(nav, text=txt,
                       command=lambda dd=d: self.seek_by(dd)).pack(side="left", padx=P(2))
        ttk.Label(nav, text="현재 위치").pack(side="left", padx=(P(14), P(4)))
        self.var_pos = tk.StringVar(value="00:00.000")
        pe = ttk.Entry(nav, textvariable=self.var_pos, width=12, justify="right")
        pe.pack(side="left")
        pe.bind("<Return>", lambda ev: self.apply_pos_entry())
        self.pos_lbl = ttk.Label(nav, text="", style="Muted.TLabel")
        self.pos_lbl.pack(side="left", padx=P(10))
        self.btn_bigprev = ttk.Button(nav, text="🔍 미리보기 크게 (F)",
                                      command=self.toggle_big_preview)
        self.btn_bigprev.pack(side="right")

        # 5) 편집 도구 + 컷 목록
        mid = ttk.Frame(root)
        mid.columnconfigure(0, weight=3)     # 편집
        mid.columnconfigure(1, weight=0)     # 화면 보정 (내용만큼만)
        mid.columnconfigure(2, weight=2)     # 전환점 목록

        edit = ttk.Labelframe(mid, text="편집", padding=P(8))
        edit.grid(row=0, column=0, sticky="nsew", padx=(0, P(6)))

        r1 = ttk.Frame(edit)
        r1.pack(fill="x")
        ttk.Label(r1, text="자르기:").pack(side="left")
        ttk.Button(r1, text="[ 시작점 지정 (I)", command=self.set_in).pack(side="left", padx=P(3))
        ttk.Button(r1, text="끝점 지정 ] (O)", command=self.set_out).pack(side="left", padx=P(3))
        ttk.Button(r1, text="전체", command=self.reset_trim).pack(side="left", padx=P(3))
        self.trim_lbl = ttk.Label(edit, text="", style="Muted.TLabel")
        self.trim_lbl.pack(fill="x", pady=(P(4), P(8)))

        r2 = ttk.Frame(edit)
        r2.pack(fill="x")
        ttk.Label(r2, text="전환점:").pack(side="left")
        ttk.Button(r2, text="여기서 A 로 (1)", style="A.TButton",
                   command=lambda: self.add_cut("A")).pack(side="left", padx=P(3))
        ttk.Button(r2, text="여기서 B 로 (2)", style="B.TButton",
                   command=lambda: self.add_cut("B")).pack(side="left", padx=P(3))
        ttk.Button(r2, text="선택 삭제", command=self.del_cut).pack(side="left", padx=P(3))
        ttk.Button(r2, text="전체 삭제", command=self.clear_cuts).pack(side="left", padx=P(3))

        r3 = ttk.Frame(edit)
        r3.pack(fill="x", pady=(P(8), 0))
        ttk.Label(r3, text="시작 카메라").pack(side="left")
        self.var_startcam = tk.StringVar(value="A")
        for cam in ("A", "B"):
            ttk.Radiobutton(r3, text=cam, value=cam, variable=self.var_startcam,
                            command=self.on_startcam).pack(side="left", padx=P(4))
        ttk.Label(r3, text="  |  미리보기 화면을 클릭해도 전환점이 추가됩니다.",
                  style="Muted.TLabel").pack(side="left")

        # 페이드 / 전환 효과
        r4 = ttk.Frame(edit)
        r4.pack(fill="x", pady=(P(10), 0))

        def sec_box(parent, var, lo, hi, inc):
            sp = ttk.Spinbox(parent, from_=lo, to=hi, increment=inc, width=5,
                             textvariable=var, command=self.update_ui)
            sp.pack(side="left")
            ttk.Label(parent, text="초", style="Muted.TLabel").pack(side="left", padx=(P(2), 0))
            return sp

        ttk.Label(r4, text="페이드:").pack(side="left")
        ttk.Label(r4, text="시작", style="Muted.TLabel").pack(side="left", padx=(P(8), P(3)))
        self.var_fin = tk.StringVar(value="1.0")
        sec_box(r4, self.var_fin, 0, 10, 0.5)
        ttk.Label(r4, text="끝", style="Muted.TLabel").pack(side="left", padx=(P(12), P(3)))
        self.var_fout = tk.StringVar(value="1.5")
        sec_box(r4, self.var_fout, 0, 10, 0.5)

        ttk.Label(r4, text="   |   전환:").pack(side="left")
        self.var_trans = tk.StringVar(value=TRANS_LABELS[1])
        cbt = ttk.Combobox(r4, textvariable=self.var_trans, width=17, state="readonly",
                           values=TRANS_LABELS)
        cbt.pack(side="left", padx=(P(6), P(6)))
        cbt.bind("<<ComboboxSelected>>", lambda ev: (self.focus_set(), self.update_ui()))
        self.var_tsec = tk.StringVar(value="0.5")
        sec_box(r4, self.var_tsec, 0.1, 5, 0.1)

        for var in (self.var_fin, self.var_fout, self.var_tsec):
            var.trace_add("write", lambda *a: self.update_ui())

        # 카메라별 화면 보정 (회전 / 선명도 / 채도)
        # 편집 패널 아래에 붙이면 세로를 먹어 미리보기가 줄어든다 -> 옆 칸에 둔다.
        adjbox = ttk.Labelframe(mid, text="화면 보정 (카메라별)", padding=P(6))
        adjbox.grid(row=0, column=1, sticky="nsew", padx=P(6))

        # 카메라 한 대가 한 줄인 표.
        self.var_fine, self.var_expo, self.var_bright = {}, {}, {}
        self.var_contrast, self.var_sharp, self.var_sat = {}, {}, {}
        self.var_flip = {}

        # (제목, 최소, 최대, 증감, 키, 칸 너비)
        cols = (("각도°", -FINE_LIMIT, FINE_LIMIT, 0.5, "fine", 5),
                ("노출", -3.0, 3.0, 0.05, "expo", 5),
                ("명도", -1.0, 1.0, 0.01, "bright", 5),
                ("대비", 0.1, 3.0, 0.02, "contrast", 4),
                ("채도", 0.0, 3.0, 0.02, "sat", 4),
                ("선명", 0.0, 3.0, 0.05, "sharp", 4))
        self._adj_cols = cols
        for i, col in enumerate(cols):
            ttk.Label(adjbox, text=col[0], style="Muted.TLabel",
                      anchor="center").grid(row=0, column=i + 1, padx=P(1))

        holders = {"fine": self.var_fine, "expo": self.var_expo,
                   "bright": self.var_bright, "contrast": self.var_contrast,
                   "sharp": self.var_sharp, "sat": self.var_sat}
        self._spin_expo = {}
        self.var_cut = {}          # {cam: {'cut_l': var, ...}}
        self.btn_match = {}
        ncol = len(cols) + 3
        for i, cam in enumerate(("A", "B")):
            row = 1 + i * 2
            ttk.Label(adjbox, text=cam, style="%s.TLabel" % cam,
                      width=2).grid(row=row, column=0, pady=P(1))
            for j, (_t, lo, hi, inc, key, wide) in enumerate(cols):
                var = tk.StringVar(value=("%g" % ADJUST_DEFAULT[key]))
                holders[key][cam] = var
                sp = ttk.Spinbox(adjbox, from_=lo, to=hi, increment=inc, width=wide,
                                 textvariable=var, command=self.on_adjust)
                sp.grid(row=row, column=j + 1, padx=P(1))
                if key == "expo":
                    self._spin_expo[cam] = sp
                var.trace_add("write", lambda *a: self.on_adjust())
            self.var_flip[cam] = tk.BooleanVar(value=False)
            ttk.Checkbutton(adjbox, text="180°", variable=self.var_flip[cam],
                            command=self.on_adjust).grid(row=row, column=len(cols) + 1,
                                                         padx=(P(6), 0))
            ttk.Button(adjbox, text="↺", width=3,
                       command=lambda c=cam: self.reset_adjust(c)
                       ).grid(row=row, column=len(cols) + 2, padx=(P(2), 0))

            # 둘째 줄: 화면 자르기 + 맞추기 버튼
            crow = ttk.Frame(adjbox)
            crow.grid(row=row + 1, column=0, columnspan=ncol, sticky="ew",
                      pady=(0, P(4)))
            ttk.Label(crow, text="자르기%", style="Muted.TLabel").pack(side="left")
            self.var_cut[cam] = {}
            for key, name in (("cut_l", "좌"), ("cut_r", "우"),
                              ("cut_t", "상"), ("cut_b", "하")):
                ttk.Label(crow, text=name, style="Muted.TLabel").pack(side="left",
                                                                     padx=(P(6), P(1)))
                v = tk.StringVar(value="0")
                self.var_cut[cam][key] = v
                ttk.Spinbox(crow, from_=0, to=CUT_LIMIT, increment=1, width=3,
                            textvariable=v, command=self.on_adjust).pack(side="left")
                v.trace_add("write", lambda *a: self.on_adjust())
            other = "B" if cam == "A" else "A"
            btn = ttk.Button(crow, text="🎯 %s에 맞추기" % other,
                             command=lambda c=cam, o=other: self.match_cams(c, o))
            btn.pack(side="left", padx=(P(10), 0))
            self.btn_match[cam] = btn

        mrow = ttk.Frame(adjbox)
        mrow.grid(row=5, column=0, columnspan=ncol, sticky="ew")
        # 결과 문구는 폭이 들쭉날쭉해 패널을 밀어내므로 위쪽 상태줄에 띄운다
        if not self._can_expose:
            for sp in self._spin_expo.values():
                sp.state(["disabled"])
            ttk.Label(mrow, text="(이 ffmpeg 은 노출 조절 미지원)",
                      style="Muted.TLabel").pack(side="left", padx=P(4))

        cutbox = ttk.Labelframe(mid, text="전환점 목록", padding=P(6))
        cutbox.grid(row=0, column=2, sticky="nsew", padx=(P(6), 0))
        self.tree = ttk.Treeview(cutbox, columns=("t", "cam"), show="headings", height=4)
        self.tree.heading("t", text="시간")
        self.tree.heading("cam", text="카메라")
        self.tree.column("t", width=P(120), anchor="center")
        self.tree.column("cam", width=P(80), anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(cutbox, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", self._tree_goto)
        self.tree.bind("<Delete>", lambda ev: self.del_cut())

        # 6) 내보내기
        exp = ttk.Labelframe(root, text="내보내기", padding=P(8))
        row = ttk.Frame(exp)
        row.pack(fill="x")

        def combo(label, var, width, values):
            ttk.Label(row, text=label).pack(side="left")
            cb = ttk.Combobox(row, textvariable=var, width=width, state="readonly",
                              values=values)
            cb.pack(side="left", padx=(P(4), P(12)))
            # 고른 뒤 포커스를 놓아 선택 강조가 남지 않게 하고, 요약 정보를 갱신한다
            cb.bind("<<ComboboxSelected>>", lambda ev: (self.focus_set(), self.update_ui()))
            return cb

        self.var_res = tk.StringVar(value="1920x1080 (FHD)")
        combo("해상도", self.var_res, 18,
              ["A 영상과 동일", "1920x1080 (FHD)", "1280x720 (HD)",
               "2560x1440 (QHD)", "3840x2160 (4K)"])

        self.var_fps = tk.StringVar(value="A 영상과 동일")
        combo("프레임", self.var_fps, 14,
              ["A 영상과 동일", "24", "25", "29.97", "30", "50", "59.94", "60"])

        self.var_audio = tk.StringVar(value="A")
        combo("소리", self.var_audio, 12, ["A", "B"])

        self.var_q = tk.StringVar(value="표준")
        combo("화질", self.var_q, 12, ["고화질(느림)", "표준", "가벼움(빠름)"])

        self.var_gpu = tk.BooleanVar(value=False)
        self.chk_gpu = ttk.Checkbutton(row, text="GPU 가속", variable=self.var_gpu)
        self.chk_gpu.pack(side="left")
        if not self._has_nvenc():
            self.chk_gpu.state(["disabled"])

        row2 = ttk.Frame(exp)
        row2.pack(fill="x", pady=(P(8), 0))
        self.btn_export = ttk.Button(row2, text="💾  영상 만들기", style="Go.TButton",
                                     command=self.do_export, state="disabled")
        self.btn_export.pack(side="left")
        self.btn_cancel = ttk.Button(row2, text="취소", command=self.cancel_export,
                                     state="disabled")
        self.btn_cancel.pack(side="left", padx=P(6))
        ttk.Button(row2, text="프로젝트 저장", command=self.save_project).pack(side="right")
        ttk.Button(row2, text="프로젝트 불러오기",
                   command=self.load_project).pack(side="right", padx=P(6))
        self.est_lbl = ttk.Label(row2, text="", style="Muted.TLabel")
        self.est_lbl.pack(side="left", padx=P(14))

        # 위에서 아래로 순서대로. 미리보기 높이는 _fit_preview 가 정한다.
        prev.pack(side="top", fill="x", pady=(P(8), 0))
        tl.pack(side="top", fill="x", pady=(P(10), 0))
        nav.pack(side="top", fill="x", pady=(P(8), 0))
        mid.pack(side="top", fill="x", pady=(P(10), 0))
        exp.pack(side="top", fill="x", pady=(P(10), 0))

        self.update_ui()

    # -- 스크롤 / 미리보기 크기 ------------------------------------------------

    def _sync_scroll(self):
        """내용 높이에 맞춰 스크롤 범위를 갱신하고, 필요할 때만 스크롤바를 보인다."""
        need = self._root_frame.winfo_reqheight()
        self._vport.configure(scrollregion=(0, 0, 0, need))
        if need > self._vport.winfo_height() + 2:
            if not self._vsb.winfo_ismapped():
                self._vsb.pack(side="right", fill="y", before=self._vport)
        elif self._vsb.winfo_ismapped():
            self._vsb.pack_forget()
            self._vport.yview_moveto(0)

    def _on_vport_resize(self, ev):
        # 안쪽 내용의 가로 폭을 보이는 영역에 맞춘다
        self._vport.itemconfigure(self._root_win, width=ev.width)
        self._fit_preview()
        self._sync_scroll()

    def _on_wheel(self, ev):
        # 값을 바꾸는 위젯(스핀박스/콤보박스)과 자체 스크롤이 있는 목록은 건드리지 않는다
        if isinstance(ev.widget, (ttk.Spinbox, ttk.Combobox, ttk.Treeview)):
            return
        if self._vsb.winfo_ismapped():
            self._vport.yview_scroll(-1 if ev.delta > 0 else 1, "units")

    def _fit_preview(self):
        """
        '작게' 면 창에 딱 맞게(스크롤 없이), '크게' 면 16:9 를 꽉 채우는 높이로.
        크게 하면 아래 메뉴가 창 밖으로 밀리지만 스크롤해서 볼 수 있다.
        """
        cv = self.canvas_prev["A"]
        w = max(1, cv.winfo_width())
        if self._big_preview:
            h = int(w * 9 / 16.0)
        else:
            # 칸이 실제로 요구하는 높이 = 그림 높이 + 테두리(highlightthickness).
            # 테두리를 빼지 않으면 딱 그만큼 넘쳐서 스크롤바가 괜히 생긴다.
            border = cv.winfo_reqheight() - int(float(cv.cget("height")))
            chrome = self._root_frame.winfo_reqheight() - cv.winfo_reqheight()
            h = self._vport.winfo_height() - chrome - border - self.px(2)
        h = max(self.px(110), h)
        if abs(cv.winfo_reqheight() - h) <= 2:
            return
        for c in ("A", "B"):
            self.canvas_prev[c].configure(height=h)

    def toggle_big_preview(self):
        self._big_preview = not self._big_preview
        self.btn_bigprev.configure(
            text="🔍 미리보기 작게 (F)" if self._big_preview else "🔍 미리보기 크게 (F)")
        was_playing, at = self._playing, self.playhead
        if was_playing:
            self.stop_play()
        self._fit_preview()
        self.update_idletasks()
        self._sync_scroll()
        self._vport.yview_moveto(0)      # 커진 미리보기가 먼저 보이게
        if was_playing:
            # 새 칸 크기가 확정된 뒤에 디코딩 크기를 다시 정해야 재생 화면도 같이 커진다
            self.seek(at)
            self.start_play()

    def _bind_keys(self):
        def guard(fn):
            def wrapper(ev):
                w = self.focus_get()
                if isinstance(w, (ttk.Entry, tk.Entry, ttk.Combobox)):
                    return
                fn()
                return "break"
            return wrapper

        self.bind("<Left>", guard(lambda: self.seek_by(-1e-9)))
        self.bind("<Right>", guard(lambda: self.seek_by(1e-9)))
        self.bind("<Shift-Left>", guard(lambda: self.seek_by(-1)))
        self.bind("<Shift-Right>", guard(lambda: self.seek_by(1)))
        self.bind("<Control-Left>", guard(lambda: self.seek_by(-10)))
        self.bind("<Control-Right>", guard(lambda: self.seek_by(10)))
        self.bind("<space>", guard(self.toggle_play))
        for k in ("1", "a", "A"):
            self.bind("<KeyPress-%s>" % k, guard(lambda: self.add_cut("A")))
        for k in ("2", "b", "B"):
            self.bind("<KeyPress-%s>" % k, guard(lambda: self.add_cut("B")))
        for k in ("i", "I"):
            self.bind("<KeyPress-%s>" % k, guard(self.set_in))
        for k in ("o", "O"):
            self.bind("<KeyPress-%s>" % k, guard(self.set_out))
        for k in ("f", "F"):
            self.bind("<KeyPress-%s>" % k, guard(self.toggle_big_preview))
        self.bind("<Control-s>", lambda ev: self.save_project())

    # -- 유틸 ---------------------------------------------------------------

    def _has_filter(self, name):
        """이 ffmpeg 빌드에 해당 필터가 있는지. (exposure 는 5.0 이후에만 있다)"""
        try:
            out = subprocess.run([self.ff, "-hide_banner", "-filters"],
                                 capture_output=True, **_hidden_kwargs())
            return (" %s " % name) in out.stdout.decode("utf-8", "replace")
        except Exception:
            return False

    def _has_nvenc(self):
        try:
            out = subprocess.run([self.ff, "-hide_banner", "-encoders"],
                                 capture_output=True, **_hidden_kwargs())
            return b"h264_nvenc" in out.stdout
        except Exception:
            return False

    @property
    def ready(self):
        return self.info["A"] is not None and self.info["B"] is not None

    def out_fps(self):
        v = self.var_fps.get()
        if v in FPS_CHOICES:
            return FPS_CHOICES[v]
        if self.info["A"]:
            return snap_fps(self.info["A"].fps)
        return "30", 30.0

    def out_size(self):
        v = self.var_res.get()
        if v.startswith("A ") and self.info["A"]:
            w, h = self.info["A"].width, self.info["A"].height
        else:
            m = re.match(r"(\d+)x(\d+)", v)
            w, h = (int(m.group(1)), int(m.group(2))) if m else (1920, 1080)
        return (w // 2) * 2, (h // 2) * 2

    def frame_dur(self):
        return 1.0 / self.out_fps()[1]

    def _fnum(self, var, default=0.0):
        try:
            return max(0.0, float(var.get()))
        except (TypeError, ValueError, tk.TclError):
            return default

    def _snum(self, var, default=0.0):
        """음수도 허용하는 숫자 읽기 (미세 각도는 반대로도 돌려야 한다)."""
        try:
            return float(var.get())
        except (TypeError, ValueError, tk.TclError):
            return default

    def fade_settings(self):
        """(시작 페이드초, 끝 페이드초, 전환모드, 전환길이초)"""
        return (self._fnum(self.var_fin), self._fnum(self.var_fout),
                TRANS_BY_LABEL.get(self.var_trans.get(), "none"),
                self._fnum(self.var_tsec))

    # -- 카메라별 화면 보정 ---------------------------------------------------

    def cam_adjust(self, cam):
        """{'rot', 'fine', 'expo', 'bright', 'contrast', 'sharp', 'sat'}"""
        expo = max(-3.0, min(3.0, self._snum(self.var_expo[cam], 0.0)))
        return {"rot": 180 if self.var_flip[cam].get() else 0,
                "fine": max(-FINE_LIMIT, min(FINE_LIMIT,
                                             self._snum(self.var_fine[cam], 0.0))),
                "expo": expo if self._can_expose else 0.0,
                "bright": max(-1.0, min(1.0, self._snum(self.var_bright[cam], 0.0))),
                "contrast": max(0.1, min(3.0, self._fnum(self.var_contrast[cam], 1.0))),
                "sharp": min(3.0, self._fnum(self.var_sharp[cam], 0.0)),
                "sat": min(3.0, self._fnum(self.var_sat[cam], 1.0)),
                "cut_l": self._cut(cam, "cut_l"), "cut_r": self._cut(cam, "cut_r"),
                "cut_t": self._cut(cam, "cut_t"), "cut_b": self._cut(cam, "cut_b")}

    def _cut(self, cam, key):
        return max(0.0, min(CUT_LIMIT, self._fnum(self.var_cut[cam][key], 0.0)))

    def cam_adjust_nocolor(self, cam):
        """명도·대비·채도만 기본값으로 되돌린 설정 (맞추기 계산의 기준선)."""
        a = self.cam_adjust(cam)
        a.update(bright=0.0, contrast=1.0, sat=1.0)
        return a

    def adjust_all(self):
        return dict((c, self.cam_adjust(c)) for c in ("A", "B"))

    def cam_size(self, cam):
        info = self.info[cam]
        return (info.width, info.height) if info and info.width else None

    def cam_filters(self):
        return dict((c, cam_vf(size=self.cam_size(c), **self.cam_adjust(c)))
                    for c in ("A", "B"))

    def cam_sizes(self):
        return dict((c, self.cam_size(c)) for c in ("A", "B"))

    def on_adjust(self, *a):
        """스핀박스를 누르고 있는 동안 매번 다시 그리지 않도록 잠깐 모아서 반영."""
        if self._adj_after:
            self.after_cancel(self._adj_after)
        self._adj_after = self.after(400, self._apply_adjust)
        self.update_ui()

    def _apply_adjust(self):
        self._adj_after = None
        if self._playing:
            # 디코딩 필터가 바뀌므로 지금 위치에서 재생을 다시 건다
            at = self.playhead
            self.stop_play()
            self.seek(at)
            self.start_play()
        else:
            self.refresh_preview()

    def _adj_vars(self):
        return (("fine", self.var_fine), ("expo", self.var_expo),
                ("bright", self.var_bright), ("contrast", self.var_contrast),
                ("sharp", self.var_sharp), ("sat", self.var_sat))

    def reset_adjust(self, cam):
        self.var_flip[cam].set(False)
        for key, holder in self._adj_vars():
            holder[cam].set("%g" % ADJUST_DEFAULT[key])
        for key, v in self.var_cut[cam].items():
            v.set("%g" % ADJUST_DEFAULT[key])
        self.on_adjust()

    # -- 두 영상 화면 맞추기 ---------------------------------------------------

    def match_cams(self, target, ref):
        """ref 카메라 화면을 기준으로 target 의 명도·대비·채도를 계산해 넣는다."""
        if not self.ready:
            messagebox.showinfo(APP_TITLE, "영상 두 개를 먼저 열어 주세요.")
            return
        if self._matching:
            return
        lo, hi = self.overlap()
        s = max(lo, min(self.trim_start, hi))
        e = max(lo, min(self.trim_end, hi))
        if e - s < 0.05:
            s, e = lo, hi
        if e - s < 0.05:
            messagebox.showinfo(APP_TITLE, "두 영상이 겹치는 구간이 없어 맞출 수 없습니다.")
            return

        n = 5
        times = [s + (e - s) * (i + 0.5) / n for i in range(n)]
        jobs = {
            ref: (self.info[ref].path, cam_vf(size=self.cam_size(ref),
                                              **self.cam_adjust(ref)),
                  self.offset if ref == "B" else 0.0),
            target: (self.info[target].path,
                     cam_vf(size=self.cam_size(target),
                            **self.cam_adjust_nocolor(target)),
                     self.offset if target == "B" else 0.0),
        }
        self._matching = True
        for b in self.btn_match.values():
            b.state(["disabled"])
        self.prog_lbl.configure(text="두 화면을 분석하는 중...")
        threading.Thread(target=self._match_worker,
                         args=(target, ref, times, jobs), daemon=True).start()

    def _match_worker(self, target, ref, times, jobs):
        try:
            stats = {}
            for cam, (path, vf, off) in jobs.items():
                acc = []
                for t in times:
                    img = grab_frame(self.ff, path, t + off, 320, vf, lossless=True)
                    if img is not None:
                        acc.append(frame_stats(self.np, img))
                if not acc:
                    raise RuntimeError("%s 카메라에서 화면을 읽지 못했습니다" % cam)
                stats[cam] = tuple(sum(v[i] for v in acc) / len(acc) for i in range(3))
            res = solve_match(stats[ref], stats[target])
            self._uiq.put(("match", target, ref, res, None))
        except Exception as e:
            self._uiq.put(("match", target, ref, None, str(e)))

    def _on_match_done(self, target, ref, res, err):
        self._matching = False
        for b in self.btn_match.values():
            b.state(["!disabled"])
        if err:
            self.prog_lbl.configure(text="맞추기 실패: " + err[:80])
            return
        bright, contrast, sat = res
        self.var_bright[target].set("%.2f" % bright)
        self.var_contrast[target].set("%.2f" % contrast)
        self.var_sat[target].set("%.2f" % sat)
        self.prog_lbl.configure(
            text="카메라 %s 를 %s 에 맞췄습니다 — 명도 %.2f · 대비 %.2f · 채도 %.2f"
                 % (target, ref, bright, contrast, sat))
        self.on_adjust()

    def transition_spans(self):
        """타임라인에 그릴 전환 구간 [(시작초, 끝초)] — 실제 내보내기와 같은 계산."""
        fi, fo, mode, tsec = self.fade_settings()
        segs = self.segments()
        if mode == "none" or tsec <= 0 or len(segs) < 2:
            return []
        fps = self.out_fps()[1]
        idx = [int(round((t - self.trim_start) * fps))
               for t in [segs[0][0]] + [e for _, e, _ in segs]]
        for i in range(1, len(idx)):
            if idx[i] <= idx[i - 1]:
                idx[i] = idx[i - 1] + 1
        nd = transition_frames(idx, fps, tsec, mode)
        spans = []
        for b in range(1, len(segs)):
            if nd[b]:
                s = self.trim_start + (idx[b] - nd[b] // 2) / fps
                spans.append((s, s + nd[b] / fps))
        return spans

    def snap(self, t):
        """출력 프레임 격자에 맞춰 시간을 정렬."""
        fd = self.frame_dur()
        base = self.trim_start
        return base + round((t - base) / fd) * fd

    def overlap(self):
        """두 영상이 모두 존재하는 A 기준 시간 범위."""
        if not self.ready:
            return 0.0, 0.0
        a, b = self.info["A"], self.info["B"]
        lo = max(0.0, -self.offset)
        hi = min(a.duration, b.duration - self.offset)
        if hi <= lo:
            return 0.0, a.duration
        return lo, hi

    def ranges(self):
        """A 기준 시간으로 각 카메라가 화면을 가진 범위."""
        if not self.ready:
            return {"A": (0.0, 0.0), "B": (0.0, 0.0)}
        return cam_ranges(self.info["A"].duration, self.info["B"].duration,
                          self.offset)

    def span(self):
        """편집에 쓸 수 있는 전체 범위 — 한쪽만 찍힌 구간까지 포함한다."""
        if not self.ready:
            return 0.0, 0.0
        return union_span(self.ranges())

    def cam_has(self, cam, t):
        """그 시각에 이 카메라의 화면이 있는지."""
        lo, hi = self.ranges()[cam]
        return lo - 1e-6 <= t <= hi + 1e-6

    def cam_at(self, t):
        cam = self.start_cam
        for ct, cc in self.cuts:
            if ct <= t + 1e-9:
                cam = cc
            else:
                break
        return cam

    def segments(self):
        """[(시작, 끝, 카메라)] — 트림 구간을 빈틈없이 채운다.
        한 대만 찍힌 구간은 그 카메라로 자동 대체한다."""
        segs = compute_segments(self.trim_start, self.trim_end,
                                self.cuts, self.start_cam)
        return apply_coverage(segs, self.ranges()) if self.ready else segs

    # -- 파일 열기 -----------------------------------------------------------

    def open_video(self, cam):
        path = filedialog.askopenfilename(
            title="카메라 %s 영상 선택" % cam,
            filetypes=[("영상 파일", "*.mp4 *.mov *.mkv *.avi *.m4v *.mts *.m2ts *.wmv *.webm"),
                       ("모든 파일", "*.*")])
        if not path:
            return
        try:
            info = probe(self.fp, path)
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self.info[cam] = info
        self.file_lbl[cam].configure(
            text="%s   ·   %dx%d  %.2ffps  %s"
                 % (info.name, info.width, info.height, info.fps, fmt_time(info.duration)))
        self.env[cam] = None
        if cam == "A":
            self.var_res.set("A 영상과 동일" if info.width >= 1280 else "1920x1080 (FHD)")
        if self.ready:
            self.reset_trim()
            if not info.has_audio or not self.info["A" if cam == "B" else "B"].has_audio:
                self.sync_lbl.configure(text="한쪽에 소리가 없어 자동 싱크 불가 — 직접 입력하세요")
        self.update_ui()
        self.refresh_preview()

    # -- 싱크 ---------------------------------------------------------------

    def do_sync(self):
        if not self.ready:
            return
        if self._playing:
            self.stop_play()
        if not (self.info["A"].has_audio and self.info["B"].has_audio):
            messagebox.showwarning(APP_TITLE, "두 영상 모두 소리가 있어야 자동 싱크가 가능합니다.")
            return
        self.btn_sync.state(["disabled"])
        self.btn_export.state(["disabled"])

        def work():
            try:
                res = estimate_offset(
                    self.ff, self.info["A"].path, self.info["B"].path, self.np,
                    dur_a=self.info["A"].duration, dur_b=self.info["B"].duration,
                    progress=lambda f, m: self._uiq.put(("prog", f, m)))
                self._uiq.put(("sync", res, None))
            except Exception as e:
                self._uiq.put(("sync", None, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_sync(self, res, err):
        self.btn_sync.state(["!disabled"])
        if err:
            self.prog["value"] = 0
            self.prog_lbl.configure(text="")
            messagebox.showerror(APP_TITLE, "자동 싱크 실패:\n" + err)
            self.update_ui()
            return
        self.offset = res.offset
        self.sync_conf = res.confidence
        self.env["A"], self.env["B"] = res.env_a, res.env_b
        self.hop_sec = res.hop_sec
        self.var_offset.set("%.3f" % self.offset)

        if res.confidence >= 2.5:
            quality, color = "확실함", "#7fe08a"
        elif res.confidence >= 1.6:
            quality, color = "양호", "#7fe08a"
        elif res.confidence >= 1.25:
            quality, color = "보통 — 미리보기로 확인 권장", "#ffe066"
        else:
            quality, color = "불확실 — 직접 맞춰야 할 수 있음", "#ff8a80"
        self.sync_lbl.configure(
            text="B 영상이 A 보다 %s %s 시작  (신뢰도 %.2f · %s)"
                 % (fmt_time(abs(self.offset)), "먼저" if self.offset > 0 else "늦게",
                    res.confidence, quality),
            foreground=color)
        self.reset_trim()
        self.prog["value"] = 0
        self.prog_lbl.configure(text="싱크 완료. 전환점을 지정하세요.")
        self.update_ui()
        self.refresh_preview()

    def apply_manual_offset(self):
        try:
            self.offset = float(self.var_offset.get())
        except ValueError:
            self.var_offset.set("%.3f" % self.offset)
            return
        self.sync_lbl.configure(text="직접 입력한 시간차 적용됨")
        self.reset_trim()
        self.refresh_preview()
        self.update_ui()

    def nudge_offset(self, label):
        step = self.frame_dur() if "f" in label else 0.1
        sign = -1 if label.startswith("−") else 1
        self.offset += sign * step
        self.var_offset.set("%.3f" % self.offset)
        self.apply_manual_offset()

    # -- 재생 위치 -----------------------------------------------------------

    def _apply_pos(self, t):
        """재생 위치에 딸린 표시들(시간, 출력 카메라, 테두리)만 갱신."""
        self.playhead = t
        self.var_pos.set(fmt_time(t))
        cam = self.cam_at(t)
        inside = self.trim_start - 1e-6 <= t <= self.trim_end + 1e-6
        self.pos_lbl.configure(
            text="출력 카메라: %s%s" % (cam, "" if inside else "   (잘려나가는 구간)"))
        for c in ("A", "B"):
            active = (c == cam) and inside
            self.canvas_prev[c].configure(
                highlightbackground=(COL_A if c == "A" else COL_B) if active else "#333")
            self.cam_title[c].configure(
                text="카메라 %s%s" % (c, "   ▶ 출력 중" if active else ""))

    def seek(self, t, redraw=True):
        if self.ready:
            lo, hi = self.span()
            t = max(lo, min(t, hi))
        self._apply_pos(t)
        if redraw:
            self.redraw_timeline()
        self.refresh_preview()

    def seek_by(self, d):
        if abs(d) < 1e-6:
            d = math.copysign(self.frame_dur(), d)
        was = self._playing
        if was:
            self.stop_play()
        self.seek(self.playhead + d)

    def apply_pos_entry(self):
        try:
            self.seek(parse_time(self.var_pos.get()))
        except ValueError:
            self.var_pos.set(fmt_time(self.playhead))

    # -- 재생 ---------------------------------------------------------------

    def toggle_play(self):
        if not self.ready:
            return
        if self._playing:
            self.stop_play()
        else:
            self.start_play()

    def start_play(self):
        t0 = self.playhead
        if not (self.trim_start <= t0 < self.trim_end - 0.05):
            t0 = self.trim_start
        # 미리보기 칸 크기에 맞춰 디코딩 -> 파이썬에서 크기를 다시 줄일 필요가 없다
        cv = self.canvas_prev["A"]
        # 상한이 낮으면 큰 모니터에서 재생 화면만 칸보다 작게 그려진다
        # (_blit 은 디코딩된 크기 그대로 올린다).
        bw = max(320, min(1600, cv.winfo_width() - 4))
        bh = max(180, min(900, cv.winfo_height() - 4))
        bw, bh = (bw // 2) * 2, (bh // 2) * 2
        self._play_box = (bw, bh)
        self._play_photo = {}
        self._play_item = {}
        self._play_gap = {"A": None, "B": None}
        self._play_blank = set()
        for c in ("A", "B"):        # 아직 오지 않은 정지화면 요청을 무효화
            self._token[c] += 1

        acam = self.var_audio.get()
        audio = None
        if self.info[acam].has_audio:
            aoff = self.offset if acam == "B" else 0.0
            audio = (self.info[acam].path, t0 + aoff)

        try:
            self._player.start(
                {"A": (self.info["A"].path, t0),
                 "B": (self.info["B"].path, t0 + self.offset)},
                t0, (bw, bh), vfps=30.0, audio=audio, filters=self.cam_filters())
        except Exception as e:
            messagebox.showerror(APP_TITLE, "재생을 시작할 수 없습니다:\n" + str(e))
            return

        self._playing = True
        self.btn_play.configure(text="⏸ 정지")
        if self._player.audio_error:
            self.prog_lbl.configure(
                text="소리 없이 화면만 재생합니다 — %s" % self._player.audio_error[:70])
        elif self._player.audio is None:
            self.prog_lbl.configure(text="카메라 %s 에 소리가 없어 화면만 재생합니다." % acam)
        else:
            self.prog_lbl.configure(text="재생 중 — 1/2 키로 바로 전환점을 찍을 수 있습니다.")
        self._play_after = self.after(20, self._tick_play)

    def stop_play(self):
        self._playing = False
        if self._play_after:
            self.after_cancel(self._play_after)
            self._play_after = None
        self._player.stop()
        self._play_photo = {}
        self._play_item = {}
        self._play_gap = {"A": None, "B": None}
        self._play_blank = set()
        self.btn_play.configure(text="▶ 재생")
        self.seek(self.playhead)

    def _tick_play(self):
        if not self._playing:
            return
        # 여기서 예외가 새어 나가면 다음 차례가 예약되지 않아, 소리는 계속 나는데
        # 화면만 멈춰 버린다. 무슨 일이 있어도 다시 예약한다.
        try:
            now = self._player.time()
            if now >= self.trim_end:
                self._apply_pos(min(now, self.trim_end))
                self.stop_play()
                return

            self._apply_pos(now)
            for cam in ("A", "B"):
                self._show_play_frame(cam, now)
            self._move_playhead(now)
        except Exception:
            traceback.print_exc()
        self._play_after = self.after(20, self._tick_play)

    def _show_play_frame(self, cam, now):
        buf = self._player.frame(cam, now)
        if buf is not None:
            self._play_gap[cam] = None
            self._play_blank.discard(cam)
            self._blit(cam, buf)
            return
        # 이 카메라에서 프레임이 안 온다. 잠깐이면 디코딩이 늦는 것이지만,
        # 계속 안 오면 이 시점에 영상이 없는 것이므로 옛 화면을 남겨 두지 않는다.
        if self._play_gap[cam] is None:
            self._play_gap[cam] = time.time()
        elif time.time() - self._play_gap[cam] > 0.5 and cam not in self._play_blank:
            self._play_blank.add(cam)
            self._play_photo.pop(cam, None)
            self._play_item.pop(cam, None)
            self._draw_placeholder(cam, "이 시점에는 영상이 없습니다")

    def _blit(self, cam, buf):
        """디코딩된 원본 픽셀을 미리보기 칸에 그대로 올린다."""
        from PIL import Image, ImageTk
        w, h = self._play_box
        try:
            img = Image.frombytes("RGB", (w, h), buf)
        except ValueError:
            return
        photo = self._play_photo.get(cam)
        cv = self.canvas_prev[cam]
        if photo is None:
            photo = ImageTk.PhotoImage(img)
            self._play_photo[cam] = photo
            cv.delete("all")
            self._play_item[cam] = cv.create_image(
                cv.winfo_width() // 2, cv.winfo_height() // 2, image=photo)
        else:
            photo.paste(img)          # 새 PhotoImage 를 만들지 않아 훨씬 빠르다

    # -- 편집 ---------------------------------------------------------------

    def reset_trim(self):
        lo, hi = self.span()
        self.trim_start, self.trim_end = lo, hi
        self.cuts = [c for c in self.cuts if lo <= c[0] <= hi]
        self.seek(max(lo, min(self.playhead, hi)))
        self.update_ui()

    def set_in(self):
        lo, hi = self.span()
        t = max(lo, min(self.playhead, self.trim_end - self.frame_dur()))
        self.trim_start = t
        self.seek(t)
        self.update_ui()

    def set_out(self):
        lo, hi = self.span()
        t = min(hi, max(self.playhead, self.trim_start + self.frame_dur()))
        self.trim_end = t
        self.seek(t)
        self.update_ui()

    def add_cut(self, cam):
        if not self.ready:
            return
        t = self.snap(self.playhead)
        if not (self.trim_start < t < self.trim_end):
            messagebox.showinfo(APP_TITLE, "전환점은 자르기 구간 안에만 넣을 수 있습니다.")
            return
        self.cuts = [c for c in self.cuts if abs(c[0] - t) > self.frame_dur() / 2]
        self.cuts.append((t, cam))
        self.cuts.sort(key=lambda c: c[0])
        if not self.cam_has(cam, t):
            self.prog_lbl.configure(
                text="이 시점에는 카메라 %s 영상이 없어, 있는 쪽 화면이 대신 나갑니다." % cam)
        self.refresh_cutlist()
        self.seek(self.playhead)
        self.update_ui()

    def del_cut(self):
        sel = self.tree.selection()
        if not sel:
            return
        idxs = sorted((int(self.tree.item(s, "text") or self.tree.index(s)) for s in sel),
                      reverse=True)
        for i in idxs:
            if 0 <= i < len(self.cuts):
                del self.cuts[i]
        self.refresh_cutlist()
        self.seek(self.playhead)
        self.update_ui()

    def clear_cuts(self):
        if self.cuts and messagebox.askyesno(APP_TITLE, "전환점을 모두 지울까요?"):
            self.cuts = []
            self.refresh_cutlist()
            self.seek(self.playhead)
            self.update_ui()

    def on_startcam(self):
        self.start_cam = self.var_startcam.get()
        self.seek(self.playhead)
        self.update_ui()

    def refresh_cutlist(self):
        self.tree.delete(*self.tree.get_children())
        for i, (t, cam) in enumerate(self.cuts):
            self.tree.insert("", "end", iid=str(i), text=str(i),
                             values=(fmt_time(t), cam))

    def _tree_goto(self, ev):
        sel = self.tree.selection()
        if sel:
            i = int(sel[0])
            if 0 <= i < len(self.cuts):
                self.seek(self.cuts[i][0])

    # -- 미리보기 -----------------------------------------------------------

    def refresh_preview(self, only=None):
        if not self.ready or self._playing:
            return
        for cam in (("A", "B") if only is None else (only,)):
            info = self.info[cam]
            if info is None:
                continue
            t = self.playhead + (self.offset if cam == "B" else 0.0)
            cv = self.canvas_prev[cam]
            w = max(160, cv.winfo_width())
            if t < 0 or t > info.duration:
                self._token[cam] += 1
                self._draw_placeholder(cam, "이 시점에는 영상이 없습니다")
                continue
            self._token[cam] += 1
            self.workers[cam].request(info.path, t, w, self._token[cam],
                                      cam_vf(size=self.cam_size(cam),
                                             **self.cam_adjust(cam)))

    def _on_prev_resize(self, cam):
        if self._playing:
            # 재생 중 창 크기가 바뀌면 그림을 다시 만들어 가운데로 맞춘다
            self._play_photo.pop(cam, None)
            self._play_item.pop(cam, None)
            self._play_blank.discard(cam)   # 안내 문구도 새 크기로 다시 그리게
            return
        self.refresh_preview(cam)

    def _draw_placeholder(self, cam, msg):
        cv = self.canvas_prev[cam]
        cv.delete("all")
        cv.create_text(cv.winfo_width() // 2, cv.winfo_height() // 2,
                       text=msg, fill=COL_MUTED, font=self.font)

    def _show_frame(self, cam, tok, img):
        # 재생 중에는 정지화면을 그리면 안 된다. 재생 직전에 걸어 둔 요청이 뒤늦게
        # 도착해 cv.delete("all") 로 재생 화면을 지워 버리면, 그 뒤로 _blit 은
        # 사라진 이미지에 paste 만 하게 되어 화면이 영영 멈춘다.
        if self._playing or tok != self._token[cam]:
            return
        cv = self.canvas_prev[cam]
        cw, ch = max(1, cv.winfo_width()), max(1, cv.winfo_height())
        if img is None:
            self._draw_placeholder(cam, "미리보기를 만들 수 없습니다")
            return
        from PIL import Image, ImageTk
        iw, ih = img.size
        scale = min(cw / float(iw), ch / float(ih))
        nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
        img = img.resize((nw, nh), Image.BILINEAR)
        photo = ImageTk.PhotoImage(img)
        self._photo[cam] = photo
        cv.delete("all")
        cv.create_image(cw // 2, ch // 2, image=photo)

    # -- 타임라인 그리기 -----------------------------------------------------

    def redraw_timeline(self):
        P = self.px
        cv = self.tl_canvas
        cv.delete("all")
        self._ph_items = None
        W = max(1, cv.winfo_width())
        H = P(104)
        if not self.ready:
            cv.create_text(W // 2, H // 2, text="영상을 두 개 열면 타임라인이 표시됩니다.",
                           fill=COL_MUTED, font=self.font)
            return

        t0, t1 = self.span()
        dur = max(0.001, t1 - t0)

        def x_of(t):
            return int((t - t0) / dur * (W - 2)) + 1

        # 파형
        wave_top, wave_h = P(4), P(34)
        env = self.env["A"]
        if env is not None and len(env) > 1:
            mx = float(env.max()) or 1.0
            step = max(1, int(dur / self.hop_sec) // max(1, W))
            pts = []
            for x in range(W):
                i0 = wave_index(x, W, t0, dur, self.hop_sec)
                seg = env[i0:i0 + step] if i0 >= 0 else env[:0]
                v = float(seg.max()) / mx if len(seg) else 0.0
                pts.append(v)
            for x, v in enumerate(pts):
                h = int(v * wave_h)
                if h > 0:
                    cv.create_line(x, wave_top + wave_h - h, x, wave_top + wave_h,
                                   fill=COL_WAVE)

        # 각 카메라가 화면을 가진 구간 (한쪽만 찍힌 데를 한눈에 보이게)
        rng = self.ranges()
        for i, cam in enumerate(("A", "B")):
            lo, hi = rng[cam]
            y = P(38) + i * P(3)
            cv.create_rectangle(0, y, W, y + P(2), fill="#2f333c", width=0)
            cv.create_rectangle(x_of(max(lo, t0)), y, x_of(min(hi, t1)), y + P(2),
                                fill=COL_A if cam == "A" else COL_B, width=0)

        # 카메라 구간
        band_top, band_h = P(44), P(26)
        cv.create_rectangle(0, band_top, W, band_top + band_h, fill=COL_TRIM, width=0)
        for s, e, cam in self.segments():
            x0, x1 = x_of(s), x_of(e)
            cv.create_rectangle(x0, band_top, max(x0 + 1, x1), band_top + band_h,
                                fill=COL_A if cam == "A" else COL_B, width=0)
            if x1 - x0 > P(18):
                cv.create_text((x0 + x1) // 2, band_top + band_h // 2, text=cam,
                               fill="#fff", font=self.font_b)

        # 트림 바깥 어둡게
        for a0, a1 in ((t0, self.trim_start), (self.trim_end, t1)):
            if a1 > a0:
                cv.create_rectangle(x_of(a0), 0, x_of(a1), band_top + band_h + P(2),
                                    fill="#000", stipple="gray50", width=0)
        for t, col in ((self.trim_start, "#7fe08a"), (self.trim_end, "#7fe08a")):
            x = x_of(t)
            cv.create_line(x, 0, x, band_top + band_h + P(2), fill=col, width=P(2))

        # 전환 구간(디졸브/검정 페이드)을 띠 위에 겹쳐 표시
        for s, e in self.transition_spans():
            x0, x1 = x_of(s), x_of(e)
            cv.create_rectangle(x0, band_top, max(x0 + 2, x1), band_top + band_h,
                                fill="#ffffff", stipple="gray50", width=0)

        # 시작/끝 페이드 구간은 어둡게
        fi, fo, _, _ = self.fade_settings()
        for a0, a1 in ((self.trim_start, min(self.trim_end, self.trim_start + fi)),
                       (max(self.trim_start, self.trim_end - fo), self.trim_end)):
            if a1 > a0:
                cv.create_rectangle(x_of(a0), band_top, x_of(a1), band_top + band_h,
                                    fill="#000000", stipple="gray50", width=0)

        # 전환점 표시
        for t, cam in self.cuts:
            if self.trim_start <= t <= self.trim_end:
                x = x_of(t)
                cv.create_line(x, band_top - P(6), x, band_top + band_h + P(6),
                               fill="#ffffff", width=max(1, P(1)))

        # 눈금
        ruler_y = band_top + band_h + P(8)
        step = self._ruler_step(dur, W)
        t = math.ceil(t0 / step) * step      # 범위가 음수에서 시작할 수도 있다
        while t <= t1:
            x = x_of(t)
            cv.create_line(x, ruler_y, x, ruler_y + P(5), fill=COL_MUTED)
            cv.create_text(x + P(2), ruler_y + P(7), text=fmt_time(t).split(".")[0],
                           anchor="nw", fill=COL_MUTED, font=self.font_s)
            t += step

        # 재생 헤드 — 재생 중에는 이 두 개만 움직여서 다시 그리는 비용을 줄인다
        x = x_of(self.playhead)
        line = cv.create_line(x, 0, x, ruler_y + P(4), fill="#ffe066", width=P(2))
        tri = cv.create_polygon(x - P(5), 0, x + P(5), 0, x, P(8), fill="#ffe066")
        self._ph_items = (line, tri, ruler_y + P(4), t0, dur, W)

    def _move_playhead(self, t):
        if not self._ph_items:
            self.redraw_timeline()
            return
        line, tri, bottom, t0, dur, W = self._ph_items
        cv = self.tl_canvas
        if W != max(1, cv.winfo_width()):
            self.redraw_timeline()
            return
        x = int((t - t0) / dur * (W - 2)) + 1
        try:
            cv.coords(line, x, 0, x, bottom)
            cv.coords(tri, x - self.px(5), 0, x + self.px(5), 0, x, self.px(8))
        except tk.TclError:
            self.redraw_timeline()

    def _ruler_step(self, dur, W):
        lbl_w = self.px(46)          # 눈금 글씨 하나가 차지하는 폭
        for s in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
            if dur / s * lbl_w < W:
                return s
        return 3600

    def _tl_click(self, ev):
        if not self.ready:
            return
        if self._playing:
            self.stop_play()
        W = max(1, self.tl_canvas.winfo_width())
        t0, t1 = self.span()
        dur = max(0.001, t1 - t0)
        t = max(t0, min(t1, t0 + (ev.x - 1) / float(W - 2) * dur))
        self.seek(t)

    # -- 상태 갱신 -----------------------------------------------------------

    def update_ui(self):
        if not hasattr(self, "est_lbl"):     # 화면 구성 도중 호출될 수 있음
            return
        ok = self.ready
        self.btn_sync.state(["!disabled"] if ok else ["disabled"])
        segs = self.segments()
        total = self.trim_end - self.trim_start
        can = ok and total > 0.2 and len(segs) > 0
        self.btn_export.state(["!disabled"] if can and not self._export else ["disabled"])

        if ok:
            lo, hi = self.overlap()
            self.trim_lbl.configure(
                text="사용 구간  %s  →  %s   (길이 %s)   ·   두 영상이 겹치는 범위 %s ~ %s"
                     % (fmt_time(self.trim_start), fmt_time(self.trim_end),
                        fmt_time(total), fmt_time(lo), fmt_time(hi)))
            w, h = self.out_size()
            fi, fo, mode, tsec = self.fade_settings()
            extra = ""
            if mode != "none":
                spans = self.transition_spans()
                extra = " · 전환 %s %.1f초(%d곳)" % (
                    "디졸브" if mode == "dissolve" else "검정 페이드", tsec, len(spans))
                if spans and len(spans) < len(segs) - 1:
                    extra += " ※짧은 장면은 자동 단축"
            if fi > 0 or fo > 0:
                extra += " · 페이드 %.1f/%.1f초" % (fi, fo)
            self.est_lbl.configure(
                text="장면 %d개 · 길이 %s · %dx%d @ %.2ffps%s"
                     % (len(segs), fmt_time(total), w, h, self.out_fps()[1], extra))
        self.redraw_timeline()

    # -- 내보내기 -----------------------------------------------------------

    def do_export(self):
        segs = self.segments()
        if not segs:
            return
        if self._playing:
            self.stop_play()
        acam = self.var_audio.get()
        if not self.info[acam].has_audio:
            messagebox.showerror(APP_TITLE, "카메라 %s 영상에는 소리가 없습니다." % acam)
            return

        default = "합본_%s.mp4" % time.strftime("%m%d_%H%M")
        out = filedialog.asksaveasfilename(
            title="저장할 파일 이름", defaultextension=".mp4",
            initialfile=default, filetypes=[("MP4 영상", "*.mp4")])
        if not out:
            return
        for cam in ("A", "B"):
            if os.path.abspath(out) == os.path.abspath(self.info[cam].path):
                messagebox.showerror(APP_TITLE, "원본 영상을 덮어쓸 수 없습니다.")
                return

        fps_str, fps = self.out_fps()
        w, h = self.out_size()
        crf = {"고화질(느림)": 18, "표준": 21, "가벼움(빠름)": 24}[self.var_q.get()]
        preset = {"고화질(느림)": "slow", "표준": "medium", "가벼움(빠름)": "veryfast"}[self.var_q.get()]
        if self.var_gpu.get():
            vargs = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                     "-cq", str(crf), "-b:v", "0", "-pix_fmt", "yuv420p"]
        else:
            vargs = ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                     "-pix_fmt", "yuv420p"]

        fi, fo, tmode, tsec = self.fade_settings()
        plan = build_plan(segs, self.info["A"].path, self.info["B"].path,
                          self.offset, self.trim_start, self.trim_end,
                          w, h, fps_str, fps, vargs, acam, out,
                          fade_in=fi, fade_out=fo,
                          trans_mode=tmode, trans_sec=tsec,
                          adjust=self.adjust_all(), sizes=self.cam_sizes())

        self._export = ExportJob(
            self.ff, plan,
            lambda f, m: self._uiq.put(("prog", f, m)),
            lambda ok_, msg: self._uiq.put(("export_done", ok_, msg)))
        self.btn_export.state(["disabled"])
        self.btn_cancel.state(["!disabled"])
        self._export.start()

    def cancel_export(self):
        if self._export:
            self._export.cancel()

    def _on_export_done(self, ok, msg):
        self._export = None
        self.btn_cancel.state(["disabled"])
        self.prog["value"] = 0
        if ok:
            self.prog_lbl.configure(text="저장 완료: " + msg)
            if messagebox.askyesno(APP_TITLE, "완성됐습니다!\n\n%s\n\n폴더를 열까요?" % msg):
                try:
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(msg)])
                except Exception:
                    pass
        else:
            self.prog_lbl.configure(text="")
            messagebox.showerror(APP_TITLE, "내보내기 실패:\n\n" + msg)
        self.update_ui()

    # -- 프로젝트 -----------------------------------------------------------

    def save_project(self):
        if not self.ready:
            return
        path = filedialog.asksaveasfilename(
            title="프로젝트 저장", defaultextension=".mcp",
            filetypes=[("멀티캠 프로젝트", "*.mcp")])
        if not path:
            return
        data = {
            "version": VERSION,
            "video_a": self.info["A"].path,
            "video_b": self.info["B"].path,
            "offset": self.offset,
            "trim_start": self.trim_start,
            "trim_end": self.trim_end,
            "start_cam": self.start_cam,
            "cuts": [[t, c] for t, c in self.cuts],
            "fade_in": self._fnum(self.var_fin),
            "fade_out": self._fnum(self.var_fout),
            "trans_mode": TRANS_BY_LABEL.get(self.var_trans.get(), "none"),
            "trans_sec": self._fnum(self.var_tsec),
            "adjust": self.adjust_all(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self.prog_lbl.configure(text="프로젝트 저장됨: " + os.path.basename(path))

    def load_project(self):
        path = filedialog.askopenfilename(
            title="프로젝트 불러오기", filetypes=[("멀티캠 프로젝트", "*.mcp"),
                                          ("모든 파일", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for cam, key in (("A", "video_a"), ("B", "video_b")):
                p = data[key]
                if not os.path.exists(p):
                    messagebox.showerror(APP_TITLE, "영상 파일을 찾을 수 없습니다:\n" + p)
                    return
                info = probe(self.fp, p)
                self.info[cam] = info
                self.file_lbl[cam].configure(
                    text="%s   ·   %dx%d  %.2ffps  %s"
                         % (info.name, info.width, info.height, info.fps,
                            fmt_time(info.duration)))
            self.offset = float(data.get("offset", 0.0))
            self.var_offset.set("%.3f" % self.offset)
            self.trim_start = float(data.get("trim_start", 0.0))
            self.trim_end = float(data.get("trim_end", 0.0))
            self.start_cam = data.get("start_cam", "A")
            self.var_startcam.set(self.start_cam)
            self.cuts = [(float(t), c) for t, c in data.get("cuts", [])]
            self.var_fin.set("%.1f" % float(data.get("fade_in", 0.0)))
            self.var_fout.set("%.1f" % float(data.get("fade_out", 0.0)))
            self.var_trans.set(TRANS_BY_MODE.get(data.get("trans_mode", "none"),
                                                 TRANS_LABELS[0]))
            self.var_tsec.set("%.1f" % float(data.get("trans_sec", 0.5)))
            adj = data.get("adjust") or {}
            for cam in ("A", "B"):
                a = dict(ADJUST_DEFAULT)
                a.update(adj.get(cam) or {})
                self.var_flip[cam].set(int(a["rot"]) == 180)
                for key, holder in self._adj_vars():
                    holder[cam].set("%g" % float(a[key]))
                for key, v in self.var_cut[cam].items():
                    v.set("%g" % float(a[key]))
            self.env = {"A": None, "B": None}
            self.refresh_cutlist()
            self.seek(self.trim_start)
            self.update_ui()
            self.prog_lbl.configure(text="프로젝트 불러옴: " + os.path.basename(path))
        except Exception as e:
            messagebox.showerror(APP_TITLE, "불러오기 실패:\n" + str(e))

    # -- 이벤트 펌프 ---------------------------------------------------------

    def _handle_ui(self, msg):
        kind = msg[0]
        if kind == "frame":
            self._show_frame(msg[1], msg[2], msg[3])
        elif kind == "prog":
            self.prog["value"] = int(msg[1] * 1000)
            self.prog_lbl.configure(text=msg[2])
        elif kind == "sync":
            self._on_sync(msg[1], msg[2])
        elif kind == "export_done":
            self._on_export_done(msg[1], msg[2])
        elif kind == "match":
            self._on_match_done(msg[1], msg[2], msg[3], msg[4])

    def _pump(self):
        # 한 건이 실패해도 나머지를 계속 처리하고, 무슨 일이 있어도 다음 차례를
        # 다시 예약한다. 여기서 예외가 새어 나가면 미리보기가 영영 멈춘다.
        try:
            while True:
                try:
                    msg = self._uiq.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_ui(msg)
                except Exception:
                    traceback.print_exc()
        finally:
            self.after(40, self._pump)

    def _on_close(self):
        if self._export:
            if not messagebox.askyesno(APP_TITLE, "변환 중입니다. 정말 종료할까요?"):
                return
            self._export.cancel()
        self._playing = False
        self._player.stop()
        for w in self.workers.values():
            w.stop()
        self.destroy()


# ----------------------------------------------------------------------------
# 자체 점검 (python multicam_editor.py --selftest)
# ----------------------------------------------------------------------------

def selftest():
    import numpy as np
    ok = True

    def check(name, cond, detail=""):
        print(("  통과  " if cond else "  실패  ") + name + ("   " + detail if detail else ""))
        return cond

    # 1) 상호상관 부호 검증
    rng = np.random.default_rng(0)
    base = rng.standard_normal(5000)
    a = np.zeros(6000); a[300:300 + 5000] = base       # A 안에서 300 위치
    b = np.zeros(6000); b[500:500 + 5000] = base       # B 안에서 500 위치
    lag, margin = _xcorr_lag(_norm(a, np), _norm(b, np), np)
    # a[i+lag] == b[i] 이어야 하므로 lag = 300-500 = -200
    ok &= check("상호상관 부호 (lag=-200)", lag == -200, "lag=%d, 여유 x%.1f" % (lag, margin))
    # offset 부호: B시간 = A시간 + offset. 같은 사건이 A 300, B 500 -> offset=+200
    ok &= check("offset 부호 (+200)", abs(-lag * 1.0 - 200) < 1e-9)

    # 2) 규칙적인 박자에서도 정답을 찾는지 (PHAT 가중의 존재 이유)
    #    0.5초 간격 펄스 + 무작위 세기 = 밴드 연주 엔벌로프 흉내
    hop = 0.01
    n = 6000
    pulses = np.zeros(n)
    amps = rng.random(120) * 0.8 + 0.2
    shape = np.exp(-np.linspace(0, 9, 25))
    for i, amp in enumerate(amps):
        s = int(i * 0.5 / hop)
        if s + len(shape) <= n:
            pulses[s:s + len(shape)] += amp * shape
    # x[i]=pulses[500+i], y[i]=pulses[637+i]  ->  x[i+137]=y[i] 이므로 정답 lag=+137
    true_lag = 137
    x = pulses[500:500 + 4000]
    y = pulses[500 + true_lag:500 + true_lag + 4000]
    lag_phat, m_phat = _xcorr_lag(_norm(x, np), _norm(y, np), np, phat=True, guard=30)
    lag_plain, m_plain = _xcorr_lag(_norm(x, np), _norm(y, np), np, phat=False, guard=30)
    ok &= check("박자 반복 신호 정렬 (PHAT)", lag_phat == true_lag,
                "lag=%d, 여유 x%.2f  /  PHAT 없으면 lag=%d 여유 x%.2f"
                % (lag_phat, m_phat, lag_plain, m_plain))
    ok &= check("PHAT 이 사이드로브 여유를 키움", m_phat > m_plain,
                "x%.2f > x%.2f" % (m_phat, m_plain))

    # 3) 장면 계산
    segs = compute_segments(5.0, 30.0, [(10.0, "B"), (18.0, "A"), (24.0, "B")], "A")
    ok &= check("장면 분할", [c for _, _, c in segs] == ["A", "B", "A", "B"], str(segs))
    ok &= check("빈틈 없음",
                abs(segs[0][0] - 5.0) < 1e-9 and abs(segs[-1][1] - 30.0) < 1e-9 and
                all(abs(segs[i][1] - segs[i + 1][0]) < 1e-9 for i in range(len(segs) - 1)))
    ok &= check("중복 전환 병합",
                compute_segments(0, 10, [(3, "A"), (6, "A")], "A") == [(0, 10, "A")])
    # 트림 시작보다 앞선 전환점은 계속 유효(이미 B 로 바뀐 상태),
    # 트림 끝보다 뒤인 전환점은 영향 없음
    ok &= check("트림 앞뒤 전환점 처리",
                compute_segments(5, 10, [(1, "B"), (20, "A")], "A") == [(5, 10, "B")])
    ok &= check("빈 구간", compute_segments(5, 5, [], "A") == [])

    # 4) 계획 생성 — B 장면에만 시간차가 더해져야 함
    plan = build_plan(segs, "a.mp4", "b.mp4", 2.5, 5.0, 30.0, 640, 360, "30", 30.0,
                      [], "A", "out.mp4", tmpdir="tmp")
    ok &= check("B 장면 소스 시각 = 장면시각 + 시간차",
                abs(plan["segments"][1]["src_start"] - 12.5) < 1e-9)
    ok &= check("A 장면 소스 시각 = 장면시각 그대로",
                abs(plan["segments"][0]["src_start"] - 5.0) < 1e-9)
    ok &= check("총 길이 = 25초", abs(plan["total_duration"] - 25.0) < 1e-9)
    plan_b = build_plan(segs, "a.mp4", "b.mp4", 2.5, 5.0, 30.0, 640, 360, "30", 30.0,
                        [], "B", "out.mp4", tmpdir="tmp")
    ok &= check("소리를 B 로 쓰면 오디오 시작에도 시간차 반영",
                abs(plan_b["audio_start"] - 7.5) < 1e-9)
    ok &= check("홀수 해상도는 짝수로 보정",
                build_plan(segs, "a", "b", 0, 5, 30, 641, 361, "30", 30.0, [], "A",
                           "o.mp4", tmpdir="t")["width"] == 640)

    # 5) 전환이 많고 프레임 격자에 딱 안 맞는 시각이어도 영상/소리가 밀리지 않아야 함
    fps = 30000 / 1001.0                      # 29.97
    many = [(i * 3.137 + 2.0, "B" if i % 2 else "A") for i in range(1, 40)]
    segs2 = compute_segments(2.0, 130.0, many, "A")
    p2 = build_plan(segs2, "a.mp4", "b.mp4", 1.234, 2.0, 130.0, 1920, 1080,
                    "30000/1001", fps, [], "A", "o.mp4", tmpdir="t")
    drift = 0.0
    acc = 0
    for sg in p2["segments"]:
        want = 2.0 + acc / fps + (1.234 if sg["cam"] == "B" else 0.0)
        drift = max(drift, abs(sg["src_start"] - want))
        acc += sg["frames"]
    ok &= check("전환 %d회에도 누적 밀림 없음" % len(segs2), drift < 1e-9,
                "최대 어긋남 %.3g초" % drift)
    ok &= check("총 길이 = 프레임수/fps",
                abs(p2["total_duration"] - acc / fps) < 1e-9)
    ok &= check("장면 길이 합 = 전체 프레임수", acc == int(round(128.0 * fps)),
                "%d vs %d" % (acc, int(round(128.0 * fps))))

    # 6) 페이드 / 전환 효과
    def walk(plan, fps_, offset_, trim_s):
        """조각들이 빈틈없이 이어지는지 + 각 조각이 올바른 원본 시각에서 오는지"""
        acc_, worst = 0, 0.0
        for it in plan["segments"]:
            base = trim_s + acc_ / fps_
            if it["kind"] == "dissolve":
                worst = max(worst, abs(it["src_start"] - (base + (offset_ if it["cam"][0] == "B" else 0))))
                worst = max(worst, abs(it["src2_start"] - (base + (offset_ if it["cam"][-1] == "B" else 0))))
            else:
                worst = max(worst, abs(it["src_start"] - (base + (offset_ if it["cam"] == "B" else 0))))
            acc_ += it["frames"]
        return acc_, worst

    segs3 = compute_segments(0.0, 40.0, [(10.0, "B"), (20.0, "A"), (30.0, "B")], "A")
    common = dict(tmpdir="t", fade_in=1.0, fade_out=2.0)
    hard = build_plan(segs3, "a.mp4", "b.mp4", 3.0, 0.0, 40.0, 640, 360, "30", 30.0,
                      [], "A", "o.mp4", trans_mode="none", **common)
    diss = build_plan(segs3, "a.mp4", "b.mp4", 3.0, 0.0, 40.0, 640, 360, "30", 30.0,
                      [], "A", "o.mp4", trans_mode="dissolve", trans_sec=0.6, **common)
    blk = build_plan(segs3, "a.mp4", "b.mp4", 3.0, 0.0, 40.0, 640, 360, "30", 30.0,
                     [], "A", "o.mp4", trans_mode="black", trans_sec=0.6, **common)

    ok &= check("하드컷: 장면 4개", len(hard["segments"]) == 4)
    ok &= check("디졸브: 장면 4개 + 전환 3개",
                len(diss["segments"]) == 7 and
                sum(1 for x in diss["segments"] if x["kind"] == "dissolve") == 3,
                str([x["kind"] for x in diss["segments"]]))

    for name, plan in (("하드컷", hard), ("디졸브", diss), ("검정", blk)):
        tot, worst = walk(plan, 30.0, 3.0, 0.0)
        ok &= check("%s: 길이 보존 + 원본 시각 정확" % name,
                    tot == 1200 and worst < 1e-9,
                    "총 %d프레임, 최대 어긋남 %.3g초" % (tot, worst))
        ok &= check("%s: 총 길이 40초" % name,
                    abs(plan["total_duration"] - 40.0) < 1e-9)

    d0 = [x for x in diss["segments"] if x["kind"] == "dissolve"][0]
    ok &= check("디졸브 길이 = 0.6초", d0["frames"] == 18, "%d프레임" % d0["frames"])
    ok &= check("디졸브는 두 카메라를 같은 순간에서 읽음",
                abs((d0["src2_start"] - d0["src_start"]) - 3.0) < 1e-9,
                "차이 %.3f초" % (d0["src2_start"] - d0["src_start"]))

    ok &= check("검정 페이드: 전환 지점 앞뒤로 페이드가 붙음",
                bool(blk["segments"][0].get("fade_out")) and
                bool(blk["segments"][1].get("fade_in")),
                str([(round(x.get("fade_in", 0), 3), round(x.get("fade_out", 0), 3))
                     for x in blk["segments"]]))

    ok &= check("시작/끝 페이드 적용",
                abs(diss["segments"][0].get("fade_in", 0) - 1.0) < 1e-9 and
                abs(diss["segments"][-1].get("fade_out", 0) - 2.0) < 1e-9)
    ok &= check("소리도 같이 페이드",
                diss["audio_fade_in"] == 1.0 and diss["audio_fade_out"] == 2.0)

    # 짧은 장면은 전환에 다 먹히면 안 됨
    short = compute_segments(0.0, 10.0, [(4.0, "B"), (4.4, "A"), (7.0, "B")], "A")
    sp = build_plan(short, "a", "b", 0.0, 0.0, 10.0, 640, 360, "30", 30.0, [], "A",
                    "o.mp4", tmpdir="t", trans_mode="dissolve", trans_sec=3.0)
    tot, worst = walk(sp, 30.0, 0.0, 0.0)
    ok &= check("짧은 장면에서도 길이 보존", tot == 300 and worst < 1e-9,
                "%d프레임" % tot)
    ok &= check("모든 조각 길이 >= 1프레임",
                all(x["frames"] >= 1 for x in sp["segments"]),
                str([x["frames"] for x in sp["segments"]]))
    tf = transition_frames([0, 120, 132, 210, 300], 30.0, 3.0, "dissolve")
    ok &= check("전환 길이가 짧은 장면의 절반 이하로 줄어듦", tf == [0, 6, 6, 39, 0],
                str(tf))

    # 6) 카메라별 화면 보정 (뒤집기 / 노출 / 대비 / 선명도 / 채도)
    ok &= check("보정이 없으면 필터도 없음", cam_vf() == "")
    ok &= check("180도는 뒤집기 두 번", cam_vf(rot=180) == "hflip,vflip")
    ok &= check("90/270 은 더 이상 돌리지 않음",
                cam_vf(rot=90) == "" and cam_vf(rot=270) == "")
    ok &= check("기본값이면 필터를 만들지 않음",
                cam_vf(sat=1.0) == "" and cam_vf(sharp=0.0) == "" and
                cam_vf(bright=0.0) == "" and cam_vf(contrast=1.0) == "")
    ok &= check("명도/대비/채도는 eq 하나로 묶임",
                cam_vf(bright=0.1, contrast=1.2, sat=1.5) ==
                "eq=contrast=1.200:brightness=0.100:saturation=1.500")
    ok &= check("명도만 바꿔도 동작", cam_vf(bright=-0.2) == "eq=brightness=-0.200")
    ok &= check("대비만 바꿔도 동작", cam_vf(contrast=0.6) == "eq=contrast=0.600")
    ok &= check("노출은 별도 필터(배수)", cam_vf(expo=0.5) == "exposure=exposure=0.500")
    ok &= check("노출 0 은 필터를 만들지 않음", cam_vf(expo=0.0) == "")
    ok &= check("노출은 명도보다 앞",
                cam_vf(expo=0.4, bright=0.1) ==
                "exposure=exposure=0.400,eq=brightness=0.100")
    ok &= check("보정 순서: 뒤집기 -> 색보정 -> 선명도",
                cam_vf(rot=180, sharp=1.0, sat=1.5) ==
                "hflip,vflip,eq=saturation=1.500,unsharp=5:5:1.000:5:5:0")

    # 두 화면 맞추기 계산 (eq 수식을 그대로 뒤집은 값이 나와야 한다)
    ok &= check("같은 화면이면 보정값은 기본값",
                solve_match((0.5, 0.2, 0.3), (0.5, 0.2, 0.3)) == (0.0, 1.0, 1.0))
    b_, c_, s_ = solve_match((0.5, 0.2, 0.30), (0.4, 0.1, 0.15))
    ok &= check("대비=표준편차 비, 채도=채도 비",
                abs(c_ - 2.0) < 1e-9 and abs(s_ - 2.0) < 1e-9,
                "대비 %.3f 채도 %.3f" % (c_, s_))
    ok &= check("명도는 대비 적용 뒤 평균을 맞춘다",
                abs(c_ * (0.4 - 0.5) + 0.5 + b_ - 0.5) < 1e-9,
                "명도 %.3f" % b_)
    ok &= check("맞추기 결과는 조절 범위 안으로 제한",
                solve_match((0.9, 9.0, 9.0), (0.1, 0.01, 0.01)) == (1.0, 3.0, 3.0))

    # 화면 자르기
    ok &= check("안 자르면 필터도 없음", cut_filter(0, 0, 0, 0) == "")
    ok &= check("네 변 자르기 식",
                cut_filter(10, 20, 5, 15) ==
                "crop=iw*0.700000:ih*0.800000:iw*0.100000:ih*0.050000")
    ok &= check("자르기는 한 변 %d%% 로 제한" % int(CUT_LIMIT),
                cut_filter(90, 0, 0, 0) == cut_filter(CUT_LIMIT, 0, 0, 0))
    ok &= check("자르기도 크기 맞춤보다 앞",
                cam_vf(cut_l=10, sat=1.2).startswith("crop=") and
                "eq=" in cam_vf(cut_l=10, sat=1.2))
    ok &= check("각도 보정 뒤에 자른다",
                cam_vf(fine=3, cut_l=10, size=(1920, 1080)).index("rotate=") <
                cam_vf(fine=3, cut_l=10, size=(1920, 1080)).rindex("crop="))

    # 7) 한쪽 카메라만 찍힌 구간도 쓸 수 있어야 한다
    rg = cam_ranges(100.0, 60.0, 5.0)        # B 는 A 보다 5초 늦게 시작, 60초짜리
    ok &= check("카메라별 촬영 범위", rg == {"A": (0.0, 100.0), "B": (-5.0, 55.0)})
    ok &= check("합친 범위는 한쪽만 찍힌 구간까지", union_span(rg) == (-5.0, 100.0))
    ok &= check("아예 안 겹치면 긴 쪽만",
                union_span(cam_ranges(10.0, 5.0, -100.0)) == (0.0, 10.0))

    cov = apply_coverage([(0.0, 100.0, "B")], rg)
    ok &= check("B 가 없는 뒷부분은 A 로 바뀜",
                cov == [(0.0, 55.0, "B"), (55.0, 100.0, "A")], str(cov))
    cov2 = apply_coverage([(-5.0, 100.0, "A")], rg)
    ok &= check("A 가 없는 앞부분은 B 로 바뀜",
                cov2 == [(-5.0, 0.0, "B"), (0.0, 100.0, "A")], str(cov2))
    ok &= check("둘 다 있는 구간은 그대로",
                apply_coverage([(10.0, 40.0, "B")], rg) == [(10.0, 40.0, "B")])
    ok &= check("같은 카메라로 이어지면 합친다",
                apply_coverage([(60.0, 70.0, "B"), (70.0, 80.0, "B")], rg) ==
                [(60.0, 80.0, "A")])
    ok &= check("장면이 빠지지 않아 전체 길이가 보존됨",
                abs(sum(e - s for s, e, _ in cov) - 100.0) < 1e-9)

    covp = build_plan(apply_coverage(compute_segments(0.0, 100.0, [(50.0, "B")], "A"), rg),
                      "a.mp4", "b.mp4", 5.0, 0.0, 100.0, 640, 360, "30", 30.0,
                      [], "A", "o.mp4", tmpdir="t")
    ok &= check("계획에도 대체된 카메라가 반영됨",
                [x["cam"] for x in covp["segments"]] == ["A", "B", "A"],
                str([x["cam"] for x in covp["segments"]]))
    ok &= check("대체해도 총 길이 100초", abs(covp["total_duration"] - 100.0) < 1e-6)

    adj = {"A": {"rot": 180, "sharp": 1.0}, "B": {"bright": 0.1, "sat": 1.5}}
    pa = build_plan(segs3, "a.mp4", "b.mp4", 0.0, 0.0, 40.0, 640, 360, "30", 30.0,
                    [], "A", "o.mp4", tmpdir="t", trans_mode="dissolve",
                    trans_sec=0.6, adjust=adj)
    clips = [x for x in pa["segments"] if x["kind"] == "clip"]
    diss2 = [x for x in pa["segments"] if x["kind"] == "dissolve"]
    ok &= check("장면마다 자기 카메라 보정이 실림",
                all(x["vf_pre"] == ("hflip,vflip,unsharp=5:5:1.000:5:5:0"
                                    if x["cam"] == "A"
                                    else "eq=brightness=0.100:saturation=1.500")
                    for x in clips))
    ok &= check("디졸브는 두 카메라 보정을 따로 실음",
                all(d["vf_pre"] != d["vf_pre2"] for d in diss2),
                str([(d["cam"], d["vf_pre"][:12], d["vf_pre2"][:12]) for d in diss2]))
    ok &= check("보정은 크기 맞춤보다 앞에 붙는다",
                ExportJob._vf_base(pa, clips[0]["vf_pre"]).index("hflip") <
                ExportJob._vf_base(pa, clips[0]["vf_pre"]).index("scale="))
    ok &= check("보정이 없으면 필터 문자열이 예전 그대로",
                ExportJob._vf_base(hard, hard["segments"][0]["vf_pre"]) ==
                ExportJob._vf_base(hard))
    ok &= check("설정 일부만 있어도 나머지는 기본값",
                adjust_vf({"B": {"sat": 1.5}}, "B") == "eq=saturation=1.500" and
                adjust_vf({}, "A") == "")

    # 7) 미세 각도 (1도 단위, 해상도 유지)
    ok &= check("0도는 필터를 만들지 않음", cam_vf(fine=0.0, size=(1920, 1080)) == "")
    ok &= check("미세 각도는 돌리고 잘라낸다",
                "rotate=" in cam_vf(fine=3, size=(1920, 1080)) and
                "crop=" in cam_vf(fine=3, size=(1920, 1080)))
    ok &= check("확대율은 좌우 대칭",
                abs(fine_zoom(7, 1920, 1080) - fine_zoom(-7, 1920, 1080)) < 1e-12)
    ok &= check("확대율은 가로세로를 바꿔도 같음",
                abs(fine_zoom(7, 1920, 1080) - fine_zoom(7, 1080, 1920)) < 1e-12)
    ok &= check("각도가 클수록 더 확대",
                all(fine_zoom(d, 1920, 1080) < fine_zoom(d + 1, 1920, 1080)
                    for d in range(0, 45)))

    def corner_free(deg, w, h):
        """잘라낸 직사각형이 돌린 원본 안에 완전히 들어가는가."""
        z = fine_zoom(deg, w, h)
        a = math.radians(deg)
        c, s = abs(math.cos(a)), abs(math.sin(a))
        cw, ch = w / z, h / z
        return cw * c + ch * s <= w + 1e-9 and cw * s + ch * c <= h + 1e-9

    ok &= check("잘라낸 영역에 검은 모서리가 안 들어감",
                all(corner_free(d, w, h)
                    for d in range(-45, 46)
                    for w, h in ((1920, 1080), (1080, 1920), (1000, 1000))))

    pf = build_plan(segs3, "a.mp4", "b.mp4", 0.0, 0.0, 40.0, 640, 360, "30", 30.0,
                    [], "A", "o.mp4", tmpdir="t",
                    adjust={"A": {"fine": 3.0}}, sizes={"A": (1920, 1080)})
    a_clip = [x for x in pf["segments"] if x["cam"] == "A"][0]
    ok &= check("계획에 미세 각도가 실림", "rotate=" in a_clip["vf_pre"])
    ok &= check("미세 각도도 크기 맞춤보다 앞",
                ExportJob._vf_base(pf, a_clip["vf_pre"]).index("rotate=") <
                ExportJob._vf_base(pf, a_clip["vf_pre"]).index("scale="))
    ok &= check("원본 크기를 안 알려줘도 동작(16:9 가정)",
                "rotate=" in cam_vf(fine=3))

    # 8) 타임라인 파형은 눈금과 맞아야 한다 (A 60초, B 70초, 시간차 +2 -> 범위 -2 ~ 68초)
    w_lo, w_hi = union_span(cam_ranges(60.0, 70.0, 2.0))
    w_dur, w_px, w_hop = w_hi - w_lo, 1000, 0.01

    def w_x(t):
        return int((t - w_lo) / w_dur * (w_px - 2)) + 1

    def w_sec(t):
        return wave_index(w_x(t), w_px, w_lo, w_dur, w_hop) * w_hop

    ok &= check("파형: 합친 범위가 A 보다 넓은 경우", (w_lo, w_hi) == (-2.0, 68.0),
                str((w_lo, w_hi)))
    one_px = w_dur / (w_px - 2) + w_hop
    ok &= check("파형: 눈금 52초 자리에 소리 52초가 그려짐", abs(w_sec(52.0) - 52.0) <= one_px,
                "%.3f초" % w_sec(52.0))
    ok &= check("파형: A 전 구간에서 눈금과 1픽셀 안으로 일치",
                all(abs(w_sec(t) - t) <= one_px for t in (0.0, 8.0, 12.34, 33.3, 59.9)))
    ok &= check("파형: A 가 시작하기 전에는 그릴 소리가 없음",
                wave_index(w_x(-1.0), w_px, w_lo, w_dur, w_hop) < 0)
    ok &= check("파형: A 가 끝난 뒤에는 그릴 소리가 없음",
                wave_index(w_x(65.0), w_px, w_lo, w_dur, w_hop) >= int(60.0 / w_hop))
    ok &= check("파형: 범위가 A 와 같으면 전체를 빠짐없이 덮음",
                wave_index(1, 1000, 0.0, 60.0, w_hop) == 0 and
                abs(wave_index(999, 1000, 0.0, 60.0, w_hop) - 6000) <= 1)

    print("\n자체 점검:", "전체 통과" if ok else "실패 있음")
    return 0 if ok else 1


# ----------------------------------------------------------------------------

def _enable_dpi_awareness():
    """
    고해상도 화면(배율 125%/150%/200%)에서 Windows 가 창 전체를 비트맵째 늘리는 것을 막는다.
    이걸 켜지 않으면 미리보기 영상도 같이 늘어나서 흐릿해진다.
    반드시 Tk 를 만들기 전에 호출해야 한다.

    단, 시스템 배율까지만 인식한다(Per-Monitor 아님). Tk 는 창을 배율이 다른 모니터로
    옮겼을 때 스스로 다시 그리지 못하는데, 시스템 배율 기준이면 그런 모니터에서는
    Windows 가 대신 늘려 줘서 크기라도 맞는다. Per-Monitor 로 켜면 그 경우 글씨와
    버튼이 절반 크기로 쪼그라들어 아예 못 쓰게 된다.
    """
    if os.name != "nt":
        return
    import ctypes
    try:    # DPI_AWARENESS_CONTEXT_SYSTEM_AWARE (Win10 1703+)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-2)):
            return
    except Exception:
        pass
    try:    # PROCESS_SYSTEM_DPI_AWARE (Win8.1+)
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:    # Vista+
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def main():
    if "--selftest" in sys.argv:
        return selftest()

    try:
        import numpy as np
    except ImportError:
        _fatal("numpy 가 설치되어 있지 않습니다.\n\n"
               "명령 프롬프트에서 아래를 실행하세요:\n"
               "    pip install numpy pillow")
        return 1
    try:
        import PIL  # noqa: F401
    except ImportError:
        _fatal("pillow 가 설치되어 있지 않습니다.\n\n"
               "명령 프롬프트에서 아래를 실행하세요:\n"
               "    pip install numpy pillow")
        return 1

    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        _fatal("ffmpeg 을 찾을 수 없습니다.\n\n"
               "관리자 권한 없이 아래 명령으로 설치할 수 있습니다:\n"
               "    winget install Gyan.FFmpeg\n\n"
               "설치 후 창을 모두 닫았다가 다시 실행하세요.")
        return 1

    _enable_dpi_awareness()
    app = App(ffmpeg, ffprobe, np)
    app.mainloop()
    return 0


def _fatal(msg):
    print(msg)
    # 자체 점검은 콘솔에서 돌리는 것이므로 대화상자를 띄우면 안 된다(입력을 기다려 멈춤)
    if "--selftest" in sys.argv:
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(APP_TITLE, msg)
        r.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        _fatal("예상치 못한 오류가 발생했습니다.\n\n" + traceback.format_exc()[-1200:])
        sys.exit(1)
