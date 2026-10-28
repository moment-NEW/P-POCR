#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P-POCR Video: 视频字幕 OCR 工具
视频 → ffmpeg 抽帧 + 裁切下半部分 → PaddleOCR API → 字幕 SRT/TXT

流程:
  1. ffmpeg 按 --fps 抽帧, 裁切画面底部 --crop-bottom 比例作为 OCR 源
  2. mpdecimate 滤镜丢弃重复帧 (字幕未变化), 大幅减少 API 调用
  3. 每张唯一帧提交 PaddleOCR API (异步任务), 并发处理, 结果缓存到本地
  4. 按时间轴合并相邻相同文本, 生成 SRT 字幕文件和纯文本 TXT
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    print("错误: 请先安装 requests 库")
    print("运行: pip install requests")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("错误: 请先安装 PyYAML 库")
    print("运行: pip install PyYAML")
    sys.exit(1)

# 复用 pocr.py 的配置加载 (API token / job_url)
from pocr import Config

# Windows 控制台中文输出保护
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

VIDEO_DEFAULTS = {
    "fps": 2,              # 每秒抽帧数
    "crop_bottom": 0.5,    # 裁取画面底部比例 (0.5 = 下半部分)
    "model": "PP-OCRv5",   # 字幕识别模型 (纯 OCR, 快且准)
    "workers": 6,          # 并发请求数
    "batch": 12,           # 每批提交的任务数
    "interval": 2,         # 轮询间隔 (秒)
    "timeout": 300,        # 单帧任务超时 (秒)
    "min_score": 0.3,      # 文本置信度阈值 (低于则丢弃)
    "dedup": True,         # 是否做条带去重
    "dedup_strip": 0.15,   # 去重检测条带: 画面底部比例 (只看字幕区, 背景动不影响)
    "merge_similarity": 0.7,  # 相邻字幕文本相似度 >= 此值时合并 (0 关闭)
    "local_det": True,     # 本地文字检测预筛: 无字的帧不调 API (需 rapidocr-onnxruntime)
    "output": "output",
}

VIDEO_EXTENSIONS = [".mp4", ".mkv", ".avi", ".mov", ".flv", ".ts", ".webm", ".m4v", ".wmv"]


class VideoOCRError(Exception):
    """视频 OCR 流程错误"""


# ---------------- ffmpeg 相关 ----------------

def find_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise VideoOCRError(
            f"未找到 {name}, 请确认已安装 ffmpeg 并加入 PATH 环境变量"
        )
    return path


def probe_video(video: str, ffprobe: str) -> Dict[str, Any]:
    """获取视频时长/分辨率"""
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "format=duration:stream=width,height,duration",
        "-of", "json", video,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise VideoOCRError(f"ffprobe 读取视频信息失败: {proc.stderr[:300]}")
    info = json.loads(proc.stdout or "{}")
    duration = None
    try:
        duration = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        for stream in info.get("streams", []):
            try:
                duration = float(stream["duration"])
                break
            except (KeyError, TypeError, ValueError):
                pass
    width = height = None
    if info.get("streams"):
        width = info["streams"][0].get("width")
        height = info["streams"][0].get("height")
    return {"duration": duration, "width": width, "height": height}


def md5_of(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_change_times(video: str, ffmpeg: str, opts: Dict[str, Any]) -> List[float]:
    """第一遍: 只在字幕条带上跑 mpdecimate, 找出字幕区发生变化的时刻 (不写图片, 快)"""
    fps = float(opts["fps"])
    strip = float(opts["dedup_strip"])
    chain = f"fps={fps},crop=iw:ih*{strip}:0:ih*(1-{strip}),mpdecimate,showinfo"

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "info", "-an", "-sn"]
    if opts.get("start"):
        cmd += ["-ss", str(opts["start"])]
    if opts.get("duration"):
        cmd += ["-t", str(opts["duration"])]
    cmd += ["-i", video, "-vf", chain, "-f", "null", "-"]

    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise VideoOCRError(f"ffmpeg 条带分析失败:\n{proc.stderr[-600:]}")

    times = [round(float(x) + float(opts.get("start") or 0), 3)
             for x in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", proc.stderr)]
    return times


def grab_frame(video: str, t: float, out_path: Path, crop_chain: str, ffmpeg: str) -> bool:
    """第二遍: 对单个时刻精确抽帧并裁切 OCR 区域"""
    proc = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{t:.3f}", "-i", video, "-vf", crop_chain,
         "-frames:v", "1", "-q:v", "2", str(out_path)],
        capture_output=True, text=True, errors="replace",
    )
    return proc.returncode == 0 and out_path.exists()


def extract_frames(video: str, frames_dir: Path, ffmpeg: str, opts: Dict[str, Any]) -> List[Dict[str, Any]]:
    """抽帧 + 裁切底部 + 去重, 返回 [{file, t, md5}] (t 为相对视频开头的秒数)"""
    frames_dir.mkdir(parents=True, exist_ok=True)
    # 清掉上一次抽帧的旧文件, 避免与本次帧数混在一起
    for old in frames_dir.glob("f_*.jpg"):
        old.unlink()

    frac = float(opts["crop_bottom"])
    crop_chain = (
        f"crop=iw:ih*{frac}:0:ih*(1-{frac}),"
        "scale=w='min(iw,1920)':h=-2"
    )
    fps = float(opts["fps"])
    offset = float(opts.get("start") or 0)

    print(f"🎬 正在抽帧: {video}")

    if opts["dedup"]:
        # 第一遍: 找出字幕条带有变化的时间点 (背景运动不影响判断)
        times = probe_change_times(video, ffmpeg, opts)
        print(f"   条带去重 (底部 {opts['dedup_strip']:.0%}): "
              f"字幕区变化点 {len(times)} 个, 其余采样点已跳过")
    else:
        # 不去重: 抽取整个窗口的等间隔帧
        window = float(opts.get("duration") or 0)
        if window <= 0:
            info = probe_video(video, find_binary("ffprobe"))
            window = (info["duration"] or 0) - offset
        times = [round(offset + k / fps, 3) for k in range(int(window * fps) + 1)]
        print(f"   未去重模式: 计划抽取 {len(times)} 帧")

    # 第二遍: 并发对每个时间点精确抽帧裁切
    ok_frames, failed = [], 0

    def grab(job):
        i, t = job
        out = frames_dir / f"f_{i:06d}.jpg"
        return t, out, grab_frame(video, t, out, crop_chain, ffmpeg)

    with ThreadPoolExecutor(max_workers=4) as ex:
        for t, out, ok in ex.map(grab, enumerate(times)):
            if ok:
                ok_frames.append({"file": str(out), "t": t, "md5": md5_of(str(out))})
            else:
                failed += 1
    if failed:
        print(f"⚠️  有 {failed} 个时间点抽帧失败 (多为超出视频末尾, 可忽略)")

    ok_frames.sort(key=lambda f: f["t"])
    print(f"✅ 抽帧完成: 共保留 {len(ok_frames)} 帧")
    return ok_frames


# ---------------- PaddleOCR API ----------------

def extract_texts(result: Any) -> List[tuple]:
    """从 API 返回的 result 中提取 (文本, 置信度) 列表, 兼容多种模型结构"""
    out: List[tuple] = []

    def add(t, s):
        if t is not None and str(t).strip():
            out.append((str(t), s))

    if isinstance(result, dict) and "ocrResults" in result:
        for r in result["ocrResults"]:
            pr = r.get("prunedResult") or {}
            texts = pr.get("rec_texts") or []
            scores = pr.get("rec_scores") or []
            for i, t in enumerate(texts):
                add(t, scores[i] if i < len(scores) else None)
    elif isinstance(result, dict) and "layoutParsingResults" in result:
        # PaddleOCR-VL / PP-StructureV3 返回 Markdown
        for r in result["layoutParsingResults"]:
            md = (r.get("markdown") or {}).get("text") or ""
            add(md, None)
    else:
        def walk(o):
            if isinstance(o, dict):
                if "rec_texts" in o:
                    scores = o.get("rec_scores") or []
                    for i, t in enumerate(o["rec_texts"]):
                        add(t, scores[i] if i < len(scores) else None)
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(result)
    return out


def ocr_file(api: Dict[str, Any], model: str, file_path: str,
             interval: float, timeout: float, min_score: float) -> Dict[str, Any]:
    """提交单帧 OCR 任务并等待完成, 返回 {status, text}"""
    headers = {"Authorization": f"bearer {api['token']}"}
    payload = {"useDocOrientationClassify": False, "useDocUnwarping": False}
    data = {"model": model, "optionalPayload": json.dumps(payload)}

    session = get_session()
    with open(file_path, "rb") as f:
        resp = session.post(api["job_url"], headers=headers, data=data,
                            files={"file": f}, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"提交失败 [{resp.status_code}]: {resp.text[:200]}")
    job_id = resp.json()["data"]["jobId"]

    deadline = time.time() + timeout
    while True:
        r = session.get(f"{api['job_url']}/{job_id}", headers=headers, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"查询失败 [{r.status_code}]: {r.text[:200]}")
        job = r.json()["data"]
        state = job.get("state")
        if state == "done":
            break
        if state == "failed":
            raise RuntimeError(f"任务失败: {job.get('errorMsg', '未知错误')}")
        if time.time() > deadline:
            raise TimeoutError(f"任务超时 (>{timeout}s)")
        time.sleep(interval)

    json_url = (job.get("resultUrl") or {}).get("jsonUrl")
    if not json_url:
        raise RuntimeError("结果中缺少 jsonUrl")
    jsonl = requests.get(json_url, timeout=60).text

    pieces = []
    for line in jsonl.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        result = json.loads(line).get("result", {})
        for text, score in extract_texts(result):
            if score is None or score >= min_score:
                pieces.append(text)
    text = " ".join(pieces).strip()
    return {"status": "ok" if text else "empty", "text": text}


def ocr_with_retry(api, model, file_path, interval, timeout, min_score) -> Dict[str, Any]:
    last = None
    for attempt in (1, 2):
        try:
            return ocr_file(api, model, file_path, interval, timeout, min_score)
        except Exception as e:  # 网络抖动等自动重试一次
            last = e
            if attempt == 1:
                time.sleep(2)
    return {"status": "failed", "text": "", "error": str(last)}


# 每线程一个 Session: 复用 TCP 连接, 避免每次请求都重新做 DNS 解析
# (裸 requests 每次新建连接, DNS 一抖动整个批量任务就会挂死在域名解析上)
_TLS = threading.local()


def get_session() -> "requests.Session":
    if not hasattr(_TLS, "session"):
        _TLS.session = requests.Session()
    return _TLS.session


def load_cache(cache_path: Path) -> Dict[str, Any]:
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"results": {}}


def save_cache(cache_path: Path, cache: Dict[str, Any]):
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


# 本地文字检测引擎 (可选依赖 rapidocr-onnxruntime, 即 PaddleOCR 模型的 ONNX 版)
_DET_ENGINE = None


def get_det_engine():
    global _DET_ENGINE
    if _DET_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            return None
        _DET_ENGINE = RapidOCR()
    return _DET_ENGINE


def local_det_filter(pending: List[Dict[str, Any]], cache: Dict[str, Any],
                     cache_path: Path, enabled: bool) -> List[Dict[str, Any]]:
    """本地检测帧内是否真有文字, 没有文字的帧直接标记跳过, 不消耗 API"""
    if not enabled or not pending:
        return pending
    engine = get_det_engine()
    if engine is None:
        print("ℹ️  未安装 rapidocr-onnxruntime, 跳过本地预筛 "
              "(安装后可自动跳过无字幕帧: pip install rapidocr-onnxruntime)")
        return pending

    kept = []
    print(f"🔎 本地文字检测: {len(pending)} 帧 ...")
    t0 = time.time()
    for i, fr in enumerate(pending, 1):
        try:
            result, _ = engine(fr["file"])
            has_text = bool(result)
        except Exception:
            has_text = True  # 检测出错时宁可多调一次 API, 不漏字幕
        if has_text:
            kept.append(fr)
        else:
            cache["results"][fr["md5"]] = {"status": "local_empty", "text": ""}
        if i % 50 == 0 or i == len(pending):
            done = i - len(kept)
            print(f"   [{i}/{len(pending)}] 已排除无字幕帧 {done} 个, "
                  f"待 OCR {len(kept)} 个 (耗时 {time.time()-t0:.0f}s)")
        if i % 100 == 0:
            save_cache(cache_path, cache)  # 周期落盘, 中断不白检
    save_cache(cache_path, cache)
    print(f"✅ 本地预筛完成: {len(pending)} 帧 -> {len(kept)} 帧需要 OCR "
          f"(排除 {len(pending) - len(kept)} 帧, 省去对应 API 调用)")
    return kept


def ocr_frames(frames: List[Dict[str, Any]], cache: Dict[str, Any],
               cache_path: Path, opts: Dict[str, Any], api: Dict[str, Any]):
    """对所有唯一帧 (按 md5 去重) 做 OCR, 结果写入缓存"""
    pending, seen = [], set()
    for fr in frames:
        m = fr["md5"]
        if m in seen:
            continue
        seen.add(m)
        rec = cache["results"].get(m)
        if rec is None:
            pending.append(fr)
        elif rec.get("status") == "local_empty" and not opts["local_det"]:
            # --no-local-det 时允许重新送 API 复核被本地排除的帧
            pending.append(fr)

    unique_total = len(seen)
    cached = unique_total - len(pending)
    if cached:
        print(f"📦 缓存命中 {cached}/{unique_total} 帧, 跳过已处理内容")
    if not pending:
        return

    # 本地文字检测预筛: 无字幕帧不调 API
    pending = local_det_filter(pending, cache, cache_path, opts["local_det"])
    save_cache(cache_path, cache)
    if not pending:
        print("✅ 所有帧经本地检测均无文字, 无需调用 API")
        return

    print(f"🔍 开始 OCR: {len(pending)} 帧待识别 (模型: {opts['model']}, 并发: {opts['workers']})")
    done = 0
    total = len(pending)
    batch = max(1, opts["batch"])

    for i in range(0, total, batch):
        chunk = pending[i:i + batch]
        with ThreadPoolExecutor(max_workers=opts["workers"]) as ex:
            futures = {
                ex.submit(ocr_with_retry, api, opts["model"], fr["file"],
                          opts["interval"], opts["timeout"], opts["min_score"]): fr
                for fr in chunk
            }
            for fut in as_completed(futures):
                fr = futures[fut]
                cache["results"][fr["md5"]] = fut.result()
                done += 1
                status = cache["results"][fr["md5"]]["status"]
                mark = {"ok": "✅", "empty": "⚪", "failed": "❌"}.get(status, "·")
                print(f"  [{done}/{total}] {mark} {Path(fr['file']).name} (t={fr['t']}s)")
        save_cache(cache_path, cache)  # 每批落盘, 中断可续

    failed = sum(1 for v in cache["results"].values() if v.get("status") == "failed")
    if failed:
        print(f"⚠️  有 {failed} 帧识别失败, 重跑本命令可自动重试 (已成功的不会重复调用 API)")


# ---------------- 字幕组装 ----------------

def fmt_srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def merge_runs(entries: List[tuple], interval: float, end_limit: Optional[float],
               sim_thr: float = 0.0) -> List[List]:
    """合并时间轴上相邻的相同/高度相似文本 → [start, end, text]"""
    runs: List[List] = []
    for t, text in entries:
        if not text:
            continue
        if runs and runs[-1][2] == text:
            runs[-1][1] = t  # 延长到下一帧出现的时刻
        else:
            if runs:
                runs[-1][1] = t
            runs.append([t, t + interval, text])

    # 相似合并: 字幕淡入淡出/运动模糊时 OCR 会输出同一句的残缺版本,
    # 与前一条足够相似则视为同一句, 保留更完整的文本
    if sim_thr > 0 and len(runs) > 1:
        merged = [runs[0]]
        for r in runs[1:]:
            prev = merged[-1]
            if SequenceMatcher(None, prev[2], r[2]).ratio() >= sim_thr:
                if len(r[2]) > len(prev[2]):
                    prev[2] = r[2]
                prev[1] = r[1]
            else:
                merged.append(r)
        runs = merged

    if runs:
        # 最后一条字幕持续到采样窗口结束 (视频结尾或 --duration 截止处)
        last = runs[-1]
        if end_limit and end_limit > last[0]:
            last[1] = end_limit
        # 保证时间长度不为 0
        for r in runs:
            if r[1] <= r[0]:
                r[1] = r[0] + interval
    return runs


def write_outputs(frames: List[Dict[str, Any]], cache: Dict[str, Any],
                  outdir: Path, stem: str, interval: float, end_limit: Optional[float],
                  sim_thr: float = 0.0):
    entries = []
    for fr in frames:
        rec = cache["results"].get(fr["md5"], {})
        text = ""
        if rec.get("status") == "ok":
            text = " ".join(str(rec.get("text", "")).split())
        entries.append((fr["t"], text))

    runs = merge_runs(entries, interval, end_limit, sim_thr)

    srt_path = outdir / f"{stem}.srt"
    with open(srt_path, "w", encoding="utf-8-sig") as f:
        for i, (start, end, text) in enumerate(runs, 1):
            f.write(f"{i}\n{fmt_srt_time(start)} --> {fmt_srt_time(end)}\n{text}\n\n")

    txt_path = outdir / f"{stem}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        for _, _, text in runs:
            f.write(text + "\n")

    # 带时间戳的文本版 (阅读/对照用): [00:01:23] 字幕内容
    timed_path = outdir / f"{stem}_timed.txt"
    with open(timed_path, "w", encoding="utf-8") as f:
        for start, _, text in runs:
            f.write(f"[{fmt_srt_time(start).split(',')[0]}] {text}\n")

    return srt_path, txt_path, timed_path, len(runs)


# ---------------- 主流程 ----------------

def process_video(video: str, opts: Dict[str, Any], api: Dict[str, Any], ffprobe: str, ffmpeg: str):
    stem = Path(video).stem
    outdir = Path(opts["output"]) / stem
    frames_dir = outdir / "frames"
    cache_path = outdir / "ocr_cache.json"
    os.makedirs(outdir, exist_ok=True)

    info = probe_video(video, ffprobe)
    dur_str = f"{info['duration']:.1f}s" if info["duration"] else "未知"
    print(f"\n{'=' * 60}")
    print(f"🎬 处理视频: {video}")
    print(f"   分辨率: {info['width']}x{info['height']}  时长: {dur_str}")
    print(f"{'=' * 60}")

    frames = extract_frames(video, frames_dir, ffmpeg, opts)
    if not frames:
        raise VideoOCRError("未抽到任何帧, 请检查视频文件或增大 --fps")

    if opts["frames_only"]:
        print(f"🖼️  仅抽帧模式, 帧已保存到: {frames_dir}")
        print("   确认裁切区域无误后, 去掉 --frames-only 重跑即可开始识别")
        return

    cache = load_cache(cache_path)
    ocr_frames(frames, cache, cache_path, opts, api)

    interval = 1.0 / float(opts["fps"])
    # 字幕覆盖范围截止: 视频结尾, 或 --start/--duration 指定的采样窗口末尾
    end_limit = info["duration"]
    if opts.get("duration"):
        seg_end = float(opts["start"] or 0) + float(opts["duration"])
        end_limit = seg_end if not end_limit else min(end_limit, seg_end)
    srt_path, txt_path, timed_path, n_runs = write_outputs(
        frames, cache, outdir, stem, interval, end_limit,
        float(opts.get("merge_similarity") or 0))

    ok = sum(1 for v in cache["results"].values() if v.get("status") == "ok")
    empty = sum(1 for v in cache["results"].values() if v.get("status") == "empty")
    local_empty = sum(1 for v in cache["results"].values() if v.get("status") == "local_empty")
    failed = sum(1 for v in cache["results"].values() if v.get("status") == "failed")
    print(f"\n🎉 完成! 识别出 {n_runs} 条字幕 (API 识别 {ok} 帧 / 本地排除无字幕 {local_empty} 帧 / "
          f"API 空文本 {empty} 帧 / 失败 {failed} 帧)")
    print(f"   字幕文件:   {srt_path}")
    print(f"   纯文本:     {txt_path}")
    print(f"   带时间戳:   {timed_path}")
    print(f"   帧缓存:     {frames_dir} (可删除, 保留则重跑不重复调 API)")


def collect_videos(input_path: str) -> List[str]:
    p = Path(input_path)
    if p.is_file():
        return [str(p)]
    if p.is_dir():
        videos = [str(f) for f in sorted(p.iterdir())
                  if f.suffix.lower() in VIDEO_EXTENSIONS]
        if not videos:
            raise VideoOCRError(
                f"目录中未找到视频文件 (支持: {', '.join(VIDEO_EXTENSIONS)})")
        return videos
    raise VideoOCRError(f"路径不存在: {input_path}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="P-POCR Video: 视频字幕 OCR 工具 (ffmpeg 抽帧 + PaddleOCR API)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 整个视频转字幕 (默认: 2fps 抽帧, 裁下半部分, PP-OCRv5)
  python video_ocr.py video.mp4

  # 只处理前 60 秒, 快速验证效果
  python video_ocr.py video.mp4 --duration 60

  # 字幕在更底部时, 只裁下面 30% 画面
  python video_ocr.py video.mp4 --crop-bottom 0.3

  # 只抽帧不调 API (检查裁切区域是否正确)
  python video_ocr.py video.mp4 --frames-only

  # 批量处理目录下所有视频
  python video_ocr.py ./Videos/
        """,
    )
    parser.add_argument("input", help="视频文件路径或目录")
    parser.add_argument("--config", "-c", help="配置文件路径 (默认: config.yaml)")
    parser.add_argument("--output", "-o", help="输出目录 (默认: output)")
    parser.add_argument("--fps", type=float, help="每秒抽帧数 (默认 2, 越高时间轴越准但调 API 越多)")
    parser.add_argument("--crop-bottom", type=float, help="裁取画面底部比例, 0.5=下半部分 (默认 0.5)")
    parser.add_argument("--model", "-m", help="OCR 模型 (默认 PP-OCRv5, 也可用 PaddleOCR-VL-1.6)")
    parser.add_argument("--workers", type=int, help="并发请求数 (默认 4)")
    parser.add_argument("--batch", type=int, help="每批提交任务数 (默认 8)")
    parser.add_argument("--interval", type=float, help="轮询间隔秒数 (默认 2)")
    parser.add_argument("--timeout", type=float, help="单帧任务超时秒数 (默认 300)")
    parser.add_argument("--min-score", type=float, help="文本置信度阈值 (默认 0.3)")
    parser.add_argument("--no-dedup", action="store_true", help="不去重, 保留所有抽帧")
    parser.add_argument("--dedup-strip", type=float,
                        help="去重检测条带高度, 画面底部比例 (默认 0.15, 只看字幕区变化)")
    parser.add_argument("--merge-similarity", type=float,
                        help="相邻字幕相似度 >= 该值时合并为一条 (默认 0.7, 0 关闭)")
    parser.add_argument("--no-local-det", action="store_true",
                        help="关闭本地文字检测预筛, 所有帧都送 API")
    parser.add_argument("--frames-only", action="store_true", help="只抽帧裁切, 不调用 OCR API")
    parser.add_argument("--start", type=float, help="起始秒数 (从此处开始处理)")
    parser.add_argument("--duration", type=float, help="只处理指定时长秒数")
    return parser.parse_args()


def main():
    print("=" * 60)
    print("  P-POCR Video: 视频字幕 OCR 工具")
    print("=" * 60)

    args = parse_args()

    opts = dict(VIDEO_DEFAULTS)
    config_path = args.config or "config.yaml"
    config = Config(config_path if os.path.exists(config_path) else None)
    video_cfg = config.get("video")
    if isinstance(video_cfg, dict):
        opts.update({k: v for k, v in video_cfg.items() if v is not None})

    # 命令行参数优先
    for key, arg_val in [
        ("fps", args.fps), ("crop_bottom", args.crop_bottom), ("model", args.model),
        ("workers", args.workers), ("batch", args.batch), ("interval", args.interval),
        ("timeout", args.timeout), ("min_score", args.min_score), ("output", args.output),
        ("dedup_strip", args.dedup_strip), ("merge_similarity", args.merge_similarity),
    ]:
        if arg_val is not None:
            opts[key] = arg_val
    if args.no_dedup:
        opts["dedup"] = False
    if args.no_local_det:
        opts["local_det"] = False
    opts["start"] = args.start
    opts["duration"] = args.duration
    opts["frames_only"] = args.frames_only

    if not (0 < float(opts["crop_bottom"]) <= 1):
        raise VideoOCRError("--crop-bottom 必须在 (0, 1] 范围内")
    if not (0 < float(opts["dedup_strip"]) <= 1):
        raise VideoOCRError("--dedup-strip 必须在 (0, 1] 范围内")
    if float(opts["fps"]) <= 0:
        raise VideoOCRError("--fps 必须大于 0")

    api = {"token": config.get("api", "token"), "job_url": config.get("api", "job_url")}
    if not api.get("token"):
        raise VideoOCRError("配置中缺少 api.token")

    ffmpeg = find_binary("ffmpeg")
    ffprobe = find_binary("ffprobe")

    videos = collect_videos(args.input)
    success = 0
    try:
        for i, video in enumerate(videos, 1):
            if len(videos) > 1:
                print(f"\n[{i}/{len(videos)}]")
            try:
                process_video(video, opts, api, ffprobe, ffmpeg)
                success += 1
            except VideoOCRError as e:
                print(f"❌ 跳过: {e}")
        if len(videos) > 1:
            print(f"\n✨ 批处理完成: 成功 {success}/{len(videos)}")
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断 (已完成的部分已保存, 重跑可续)")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except VideoOCRError as e:
        print(f"\n❌ 错误: {e}")
        sys.exit(1)
