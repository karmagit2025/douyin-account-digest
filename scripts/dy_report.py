#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dy_report.py —— 抖音账号内容采集与转写流水线（一条命令）

把「给定一个抖音账号 → 拿到最近 N 条视频的完整文字素材」这一段的活干完，
剩下的「读素材、写报告」交给 agent（agent 就是 LLM）。

用法：
    python dy_report.py <抖音主页链接 / sec_uid / v.douyin.com 短链>
    python dy_report.py <链接> --top 30          # 取最近 30 条（默认 20）
    python dy_report.py <链接> --sample 8        # 只精读 8 条，其余只取元数据（省钱）
    python dy_report.py <链接> --meta-only       # 不下载不转写，只要元数据
    python dy_report.py --login                  # 只做一次扫码登录，不采集
    python dy_report.py --check                  # 检查运行环境

产物（默认 ./dy_out/<账号后8位>/）：
    list.json      全量元数据（标题/时长/点赞/发布时间/视频直链）
    v/<id>.mp4     视频
    v/<id>.wav     16k 单声道音频
    v/<id>.txt     转写原文
    material.md    ⭐ 给 agent 读的唯一入口：账号画像 + 每条视频的元数据 + 转写全文

设计要点（踩过的坑）：
  ① 抖音服务端接口对无浏览器的客户端关死了（403 Uifid Not Found），必须走真浏览器
  ② 未登录只能看首页 21 条，翻页返回空 ⇒ 要 >21 条必须登录
  ③ 平台没有字幕 API（is_subtitled=0），内容只能 ASR
  ④ cookie 无法跨 profile 复制（应用绑定加密）⇒ 只能让用户扫码登录一次，profile 持久化复用
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------- 路径与常量 ----------
HOME = Path(os.environ.get("DSH_DY_HOME") or (Path.home() / ".dsh" / "dy-digest"))
PROFILE = HOME / "profile"          # 持久化浏览器 profile（登录态住在这儿）
HF_CACHE = HOME / "hf-cache"        # whisper 模型缓存
HOME.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0")
# 口播里高频的行业词，喂给 whisper 当提示，能明显减少专业词误听
ASR_PROMPT = ("以下是一段石材加工设备行业的中文讲解，涉及桥切机、四轴、五轴、伺服、"
              "水刀、岩板、石英石、台面、台中盆、测量仪、无尘安装、接缝、导角、铣刀、锯片等词。")

CHANNELS = ("msedge", "chrome")     # 依次尝试：系统 Edge / Chrome


def log(msg):
    print(msg, flush=True)


def die(msg, code=1):
    log("ERROR: " + msg)
    sys.exit(code)


# ---------- 环境检查 ----------
def check_env(verbose=True):
    ok = True
    problems = []

    if shutil.which("ffmpeg") is None:
        ok = False
        problems.append("缺 ffmpeg（音频抽轨要用）→ Windows: winget install Gyan.FFmpeg")

    try:
        import playwright  # noqa: F401
    except ImportError:
        ok = False
        problems.append("缺 playwright → python -m pip install playwright")

    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        ok = False
        problems.append("缺 faster-whisper → python -m pip install faster-whisper")

    if verbose:
        log("环境检查：%s" % ("通过" if ok else "有问题"))
        for p in problems:
            log("  ✗ " + p)
        log("  数据目录：%s" % HOME)
    return ok, problems


# ---------- 链接解析 ----------
def resolve_sec(target: str) -> str:
    """从主页链接 / 短链 / 裸 sec_uid 里取出 sec_uid"""
    t = target.strip()

    # 裸 sec_uid
    if re.fullmatch(r"MS4wLjABAAAA[A-Za-z0-9_\-]{10,}", t):
        return t

    # 短链 v.douyin.com/xxx → 跟随跳转
    if "v.douyin.com" in t:
        try:
            import requests
            r = requests.get(t if t.startswith("http") else "https://" + t,
                             headers={"User-Agent": UA}, timeout=20, allow_redirects=True)
            t = r.url
            log("短链已解析：%s" % t)
        except Exception as e:
            die("短链解析失败：%s" % e)

    m = re.search(r"/user/([A-Za-z0-9_\-]+)", t)
    if m:
        return m.group(1)
    die("认不出这个链接里的 sec_uid：%s\n（要形如 https://www.douyin.com/user/MS4wLjABAAAA...）" % target)


# ---------- 浏览器 ----------
def launch(channel_tried=None, headless=True, persistent=True):
    """起浏览器；返回 (playwright, context)。调用方负责 close。"""
    from playwright.sync_api import sync_playwright
    tried = []

    for ch in (channel_tried or CHANNELS):
        try:
            pw = sync_playwright().start()
            ctx = pw.chromium.launch_persistent_context(
                str(PROFILE), channel=ch, headless=headless, user_agent=UA,
                locale="zh-CN", viewport={"width": 1440, "height": 1000},
                args=["--no-first-run", "--disable-blink-features=AutomationControlled"],
            )
            log("浏览器：%s（%s）" % (ch, "无头" if headless else "有界面"))
            return pw, ctx
        except Exception as e:
            tried.append("%s: %s" % (ch, str(e)[:80]))
            try:
                pw.stop()
            except Exception:
                pass
    die("没有可用的浏览器。试过：\n  " + "\n  ".join(tried) +
        "\nWindows 装 Edge 即可；macOS/Linux 装 Chrome。")


def is_logged_in(ctx) -> bool:
    return any(c["name"].startswith("sessionid") for c in ctx.cookies())


def do_login():
    """开有界面窗口等用户扫码；成功后 profile 里就有登录态了"""
    log("正在打开扫码登录窗口…")
    pw, ctx = launch(headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    shot = HOME / "login_qr.png"

    try:
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        for sel in ("text=登录", "button:has-text('登录')"):
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    break
            except Exception:
                pass
        page.wait_for_timeout(3500)
        page.screenshot(path=str(shot))
        log("二维码已截图：%s" % shot)
        log("（窗口已经开在用户屏幕上，让他直接扫也行）")
        if is_logged_in(ctx):
            log("已经是登录状态，无需扫码。")
            return True
    except Exception as e:
        log("打开登录页出错：%s" % e)

    log("等扫码…（最长 5 分钟）")
    for i in range(150):
        if is_logged_in(ctx):
            log("登录成功（%ds）。" % (i * 2))
            return True
        time.sleep(2)
    log("没等到登录。可以再跑一次 --login。")
    return False


def ensure_login(auto=True):
    """确认有登录态；没有就尝试登录。返回是否已登录"""
    pw, ctx = launch(headless=True)
    try:
        if is_logged_in(ctx):
            log("登录态：有效")
            return True
    finally:
        try:
            ctx.close(); pw.stop()
        except Exception:
            pass

    log("登录态：无（只能看到首页 21 条，翻不了页）")
    if not auto:
        return False
    return do_login()


# ---------- 采集 ----------
def harvest(sec: str, want: int, out_dir: Path) -> list:
    """抓作品列表；拿到 >= want 条或翻不动为止"""
    pw, ctx = launch(headless=True)
    awemes, chunks = {}, []

    def on_resp(r):
        if "/aweme/v1/web/aweme/post/" not in r.url:
            return
        try:
            d = r.json()
        except Exception:
            return
        lst = d.get("aweme_list") or []
        chunks.append(len(lst))
        for a in lst:
            awemes[a["aweme_id"]] = a
        log("  翻页 +%d 条（累计 %d，has_more=%s）" % (len(lst), len(awemes), d.get("has_more")))

    page = ctx.new_page()
    page.on("response", on_resp)
    log("打开主页：%s" % sec[:24])
    page.goto("https://www.douyin.com/user/%s" % sec, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(7000)

    title = page.title()
    log("账号：%s" % title.replace(" - 抖音", ""))
    log("登录态：%s" % ("有" if is_logged_in(ctx) else "无"))

    stale = 0
    for i in range(40):
        if len(awemes) >= want:
            break
        before = len(awemes)
        try:
            page.eval_on_selector("div.route-scroll-container",
                                  "e => e.scrollTo(0, e.scrollHeight)")
        except Exception:
            page.mouse.wheel(0, 6000)
        page.wait_for_timeout(2400)
        if len(awemes) == before:
            stale += 1
            if stale >= 4:
                break
        else:
            stale = 0

    try:
        ctx.close(); pw.stop()
    except Exception:
        pass

    rows = []
    for a in awemes.values():
        v = a.get("video") or {}
        st = a.get("statistics") or {}
        rows.append({
            "aweme_id": a.get("aweme_id"),
            "desc": a.get("desc") or "",
            "create_time": a.get("create_time"),
            "duration_ms": v.get("duration"),
            "digg": st.get("digg_count"),
            "comment": st.get("comment_count"),
            "share": st.get("share_count"),
            "collect": st.get("collect_count"),
            "is_subtitled": a.get("is_subtitled"),
            "author": (a.get("author") or {}).get("nickname"),
            "play_addr": (v.get("play_addr") or {}).get("url_list") or [],
            "tag": [t.get("hashtag_name") for t in (a.get("text_extra") or []) if t.get("hashtag_name")],
        })
    rows.sort(key=lambda r: r["create_time"] or 0, reverse=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "list.json").write_text(
        json.dumps({"sec_uid": sec, "title": title, "chunks": chunks, "items": rows},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    log("列表共 %d 条 → %s" % (len(rows), out_dir / "list.json"))

    if len(rows) <= 21:
        log("⚠️ 只有 %d 条：未登录时抖音只给首页 21 条。要更多请先 `--login`。" % len(rows))
    return rows


# ---------- 下载 ----------
def download(rows, out_dir: Path, video_dir: Path):
    import requests
    video_dir.mkdir(parents=True, exist_ok=True)
    done = failed = 0

    for idx, r in enumerate(rows, 1):
        aid = r["aweme_id"]
        mp4 = video_dir / (aid + ".mp4")
        wav = video_dir / (aid + ".wav")
        if wav.exists() and wav.stat().st_size > 100_000:
            done += 1
            continue

        ok = False
        for u in (r["play_addr"] or [])[:3]:
            try:
                resp = requests.get(u, headers={"User-Agent": UA,
                                                "Referer": "https://www.douyin.com/"},
                                    timeout=60, stream=True)
                if resp.status_code != 200:
                    continue
                n = 0
                with open(mp4, "wb") as f:
                    for chunk in resp.iter_content(1 << 16):
                        f.write(chunk)
                        n += len(chunk)
                if n < 50_000:
                    continue
                ok = True
                break
            except Exception:
                continue

        if not ok:
            failed += 1
            log("  [%d/%d] 下载失败 %s" % (idx, len(rows), aid))
            continue

        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(mp4), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
                       check=False)
        if wav.exists():
            done += 1
            log("  [%d/%d] ok  %s  %.1fMB" % (idx, len(rows), aid, n / 1e6))
        else:
            failed += 1
    log("下载完成：成功 %d，失败 %d" % (done, failed))


# ---------- 转写 ----------
def transcribe(video_dir: Path, model_size="small", lang="zh"):
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HOME", str(HF_CACHE))

    wavs = sorted(video_dir.glob("*.wav"))
    todo = [w for w in wavs
            if not ((video_dir / (w.stem + ".txt")).exists()
                    and (video_dir / (w.stem + ".txt")).stat().st_size > 20)]
    if not todo:
        log("转写：全部已完成，跳过")
        return

    from faster_whisper import WhisperModel
    cached = (HF_CACHE / "hub").exists() and any(
        (HF_CACHE / "hub").glob("models--Systran--faster-whisper-%s" % model_size))
    log("转写：加载模型 %s（%s）…"
        % (model_size, "本地已缓存" if cached else "首次会下载约 460MB，走国内镜像"))
    t0 = time.time()
    model = WhisperModel(model_size, device="cpu", compute_type="int8",
                         cpu_threads=max(4, (os.cpu_count() or 8) - 4))
    log("模型就绪 %.1fs，待转写 %d 条" % (time.time() - t0, len(todo)))

    for i, w in enumerate(todo, 1):
        tp = video_dir / (w.stem + ".txt")
        t1 = time.time()
        try:
            segs, info = model.transcribe(w, language=lang, vad_filter=True,
                                          beam_size=5, initial_prompt=ASR_PROMPT)
            text = "".join(s.text for s in segs).strip()
        except Exception as e:
            log("  [%d/%d] 转写失败 %s：%s" % (i, len(todo), w.stem, str(e)[:80]))
            continue
        tp.write_text(text, encoding="utf-8")
        log("  [%d/%d] %s  %.1f分钟音频  %.0fs  %d字"
            % (i, len(todo), w.stem, info.duration / 60, time.time() - t1, len(text)))


# ---------- 汇总素材 ----------
def build_material(rows, out_dir: Path, video_dir: Path, want: int, sec: str = ""):
    """把 agent 要读的东西合成一个文件，省得它逐个打开"""
    import datetime as dt
    import statistics

    sel = rows[:want]
    lines = []
    lines.append("# 抖音账号素材：%s\n" % (rows[0]["author"] if rows else "?"))
    lines.append("- sec_uid: `%s`" % sec)
    lines.append("- 抓到的作品数: %d" % len(rows))
    lines.append("- 本文件精读: %d 条（最近 %d 条）" % (len(sel), len(sel)))

    if rows:
        ts = [r["create_time"] for r in rows if r["create_time"]]
        durs = [round((r["duration_ms"] or 0) / 1000) for r in sel if r["duration_ms"]]
        diggs = [r["digg"] or 0 for r in sel]
        lines.append("- 时间跨度: %s → %s" % (
            dt.datetime.fromtimestamp(min(ts)).strftime("%Y-%m-%d"),
            dt.datetime.fromtimestamp(max(ts)).strftime("%Y-%m-%d")))
        if durs:
            lines.append("- 精读集时长中位数: %ds，点赞中位数: %s"
                         % (statistics.median(durs), statistics.median(diggs)))
    lines.append("")

    # 元数据总表（含未精读的，让 agent 知道全貌）
    lines.append("## 全部 %d 条元数据\n" % len(rows))
    lines.append("| # | 日期 | 时长 | 赞 | 标题 |")
    lines.append("|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        d = dt.datetime.fromtimestamp(r["create_time"] or 0).strftime("%Y-%m-%d")
        mark = "**" if i <= len(sel) else ""
        lines.append("| %d | %s | %ds | %s | %s%s%s |" % (
            i, d, round((r["duration_ms"] or 0) / 1000), r["digg"],
            mark, (r["desc"] or "").replace("|", "/")[:60], mark))
    lines.append("")

    # 转写全文
    lines.append("## 精读转写原文\n")
    lines.append("> 以下是 ASR 输出，专业词有系统性误听，引用前请按上下文核对。\n")
    for i, r in enumerate(sel, 1):
        aid = r["aweme_id"]
        tp = video_dir / (aid + ".txt")
        text = tp.read_text(encoding="utf-8").strip() if tp.exists() else ""
        d = dt.datetime.fromtimestamp(r["create_time"] or 0).strftime("%Y-%m-%d")
        lines.append("### %d. %s" % (i, (r["desc"] or "")[:70]))
        lines.append("- %s · %ds · 赞%s · `%s`"
                     % (d, round((r["duration_ms"] or 0) / 1000), r["digg"], aid))
        if text:
            lines.append("\n" + text + "\n")
        else:
            lines.append("\n（无转写：可能是无口播视频，或下载/转写失败）\n")

    mp = out_dir / "material.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    size_kb = mp.stat().st_size / 1024
    log("素材已合成：%s（%.0f KB，约 %.0f 千字）" % (mp, size_kb, size_kb / 2))
    return mp


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser(description="抖音账号内容采集流水线")
    ap.add_argument("target", nargs="?", help="主页链接 / sec_uid / 短链")
    ap.add_argument("--top", type=int, default=20, help="精读最近几条（默认 20）")
    ap.add_argument("--meta-only", action="store_true", help="只取元数据，不下载不转写")
    ap.add_argument("--sample", type=int, default=None,
                    help="只精读这么几条，其余只要元数据（省钱档）")
    ap.add_argument("--asr-model", default="small", help="whisper 模型：tiny/base/small/medium")
    ap.add_argument("--login", action="store_true", help="只做登录，不采集")
    ap.add_argument("--check", action="store_true", help="只检查环境")
    ap.add_argument("--out", default=None, help="输出目录")
    args = ap.parse_args()

    if args.check:
        ok, _ = check_env()
        sys.exit(0 if ok else 2)

    if args.login:
        ok, probs = check_env(verbose=False)
        if not ok:
            die("先装依赖：\n  " + "\n  ".join(probs))
        sys.exit(0 if do_login() else 1)

    if not args.target:
        ap.error("要给一个抖音账号链接（或用 --login / --check）")

    ok, probs = check_env(verbose=False)
    if not ok and not args.meta_only:
        die("环境不全：\n  " + "\n  ".join(probs))

    sec = resolve_sec(args.target)
    out_dir = Path(args.out) if args.out else Path("dy_out") / sec[-8:]
    video_dir = out_dir / "v"

    log("=" * 60)
    log("账号 sec_uid：%s" % sec)
    log("输出目录：%s" % out_dir.resolve())
    log("=" * 60)

    if not args.meta_only and not ensure_login(auto=True):
        log("⚠️ 没有登录态，最多只能拿到 21 条。")

    # 采集池要足够大：即使只精读几条，元数据全貌也要拿全（报告的趋势分析靠它）
    pool = 60 if args.meta_only else max(args.top, args.sample or 0, 40)
    rows = harvest(sec, want=pool, out_dir=out_dir)

    if args.meta_only:
        log("\n完成（只取了元数据）。列表：%s" % (out_dir / "list.json"))
        return

    n = args.sample or args.top
    sel = rows[:n]
    log("\n下载 %d 条视频…" % len(sel))
    download(sel, out_dir, video_dir)

    log("\n转写…")
    transcribe(video_dir, model_size=args.asr_model)

    mp = build_material(rows, out_dir, video_dir, n, sec=sec)

    log("\n" + "=" * 60)
    log("完成。下一步：读 %s 写报告。" % mp)
    log("=" * 60)


if __name__ == "__main__":
    main()
