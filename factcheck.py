#!/usr/bin/env python3
"""Fact-check a short video (Instagram Reel, YouTube Short, TikTok or a local file).

    python factcheck.py "https://www.instagram.com/reel/XXXX/"
    python factcheck.py clip.mp4 --report-lang French
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import wave
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
PROMPTS_DIR = ROOT / "prompts"
DEFAULT_API_MODEL = "claude-sonnet-5-5"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# --- video, audio, frames ---------------------------------------------------

def fetch_video(source: str, workdir: Path, cookies_browser: str | None) -> tuple[Path, dict]:
    local = Path(source).expanduser()
    if local.exists():
        dst = workdir / f"video{local.suffix}"
        shutil.copy(local, dst)
        return dst, {"source": str(local)}

    import yt_dlp

    opts = {
        "outtmpl": str(workdir / "video.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(source, download=True)

    videos = sorted(workdir.glob("video.*"))
    if not videos:
        raise RuntimeError("download failed, no video file found")

    meta = {
        "source": source,
        "title": info.get("title") or "",
        "author": info.get("uploader") or info.get("channel") or "",
        "upload_date": info.get("upload_date") or "",
        "caption": (info.get("description") or "").strip(),
    }
    return videos[0], meta


def extract_audio(video: Path, workdir: Path) -> Path | None:
    audio = workdir / "audio.wav"
    try:
        run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio)])
        return audio
    except subprocess.CalledProcessError:
        return None  # no audio track


def transcribe(audio: Path, model_size: str, language: str | None) -> tuple[str, str]:
    import numpy as np
    from faster_whisper import WhisperModel

    # Pass raw samples instead of the file path: faster-whisper decodes files with PyAV,
    # and recent PyAV releases break its decode_audio().
    with wave.open(str(audio), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    samples = pcm.astype(np.float32) / 32768.0

    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, info = model.transcribe(samples, language=language, vad_filter=True)
    lines = [f"[{s.start:06.1f}s] {s.text.strip()}" for s in segments]
    return "\n".join(lines), info.language


def video_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def extract_frames(video: Path, workdir: Path, n: int) -> list[tuple[float, Path]]:
    duration = video_duration(video)
    # longest side 1024px: enough to read on-screen text
    scale = "scale='if(gt(iw,ih),1024,-2)':'if(gt(iw,ih),-2,1024)'"
    frames = []
    for i in range(n):
        t = duration * (i + 0.5) / n
        path = workdir / f"frame_{i:02d}.jpg"
        run(["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(video),
             "-frames:v", "1", "-vf", scale, "-q:v", "4", str(path)])
        frames.append((t, path))
    return frames


# --- Claude -------------------------------------------------------------------
# "claude-code" runs `claude -p`, which uses your Claude subscription.
# "api" calls the Anthropic API directly and needs ANTHROPIC_API_KEY.

def parse_json(text: str) -> dict:
    blocks = re.findall(r"```json\s*(.*?)```", text, re.S)
    raw = blocks[-1] if blocks else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(raw)


def ask_claude_code(system: str, text: str, frames, web: bool, model: str | None,
                    workdir: Path) -> tuple[str, list[dict]]:
    if not shutil.which("claude"):
        sys.exit("Claude Code not found. Install it and run `claude` once to log in (see README).")

    if frames:
        text += "\n\nFRAMES FROM THE VIDEO (open each one with the Read tool, in order):\n"
        text += "\n".join(f"- {p.resolve()} (at {t:.1f}s)" for t, p in frames)

    tools = ["Read"] + (["WebSearch", "WebFetch"] if web else [])
    cmd = ["claude", "-p", "--output-format", "json",
           "--append-system-prompt", system,
           "--allowedTools", ",".join(tools)]
    if model:
        cmd += ["--model", model]

    # If ANTHROPIC_API_KEY is set, Claude Code bills the API instead of the subscription.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = subprocess.run(cmd, input=text, capture_output=True, text=True,
                          cwd=workdir, env=env, timeout=1200)
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.exit(f"Claude Code failed:\n{proc.stdout}\n{proc.stderr}")
    if out.get("is_error"):
        sys.exit(f"Claude Code error: {out.get('result')}")
    return out.get("result", ""), []


def ask_api(system: str, text: str, frames, web: bool, model: str | None,
            max_searches: int) -> tuple[str, list[dict]]:
    import anthropic

    client = anthropic.Anthropic()
    content: list = [{"type": "text", "text": text}]
    for t, path in frames:
        content.append({"type": "text", "text": f"Frame at {t:.1f}s:"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.b64encode(path.read_bytes()).decode()}})

    messages = [{"role": "user", "content": content}]
    kwargs = {"model": model or DEFAULT_API_MODEL, "max_tokens": 16000,
              "system": system, "messages": messages}
    if web:
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search",
                            "max_uses": max_searches}]

    text_parts, citations = [], []
    for _ in range(10):
        resp = client.messages.create(**kwargs)
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
                for c in getattr(block, "citations", None) or []:
                    if getattr(c, "url", None):
                        citations.append({"url": c.url, "title": getattr(c, "title", "")})
        if resp.stop_reason != "pause_turn":
            break
        # long searches can pause the turn; send it back as-is to continue
        messages.append({"role": "assistant", "content": resp.content})
    return "".join(text_parts), citations


def ask(args, system: str, text: str, frames=(), web: bool = False,
        workdir: Path = Path(".")) -> tuple[dict, list[dict]]:
    if args.backend == "api":
        out, cites = ask_api(system, text, frames, web, args.model, args.max_searches)
    else:
        if web:
            system += (f"\n\nUse at most {args.max_searches} web searches in total. "
                       "Do not use any tool other than Read, WebSearch and WebFetch.")
        out, cites = ask_claude_code(system, text, frames, web, args.model, workdir)
    return parse_json(out), cites


def extract_claims(args, transcript, frames, meta, workdir) -> dict:
    system = (PROMPTS_DIR / "extract_claims.md").read_text(encoding="utf-8")
    text = (
        f"POST METADATA:\n{json.dumps(meta, ensure_ascii=False, indent=2)}\n\n"
        f"TRANSCRIPT (timestamped):\n{transcript or '(no audio track)'}\n\n"
        f"Maximum number of claims: {args.max_claims}"
    )
    result, _ = ask(args, system, text, frames=frames, workdir=workdir)
    return result


def verify_claims(args, extraction, meta, workdir) -> tuple[dict, list]:
    system = (PROMPTS_DIR / "verify_claims.md").read_text(encoding="utf-8")
    system = system.replace("{{REPORT_LANGUAGE}}", args.report_lang)
    system = system.replace("{{TODAY}}", datetime.now().strftime("%Y-%m-%d"))
    text = (
        f"POST METADATA:\n{json.dumps(meta, ensure_ascii=False, indent=2)}\n\n"
        f"VIDEO ANALYSIS AND CLAIMS TO CHECK:\n{json.dumps(extraction, ensure_ascii=False, indent=2)}"
    )
    return ask(args, system, text, web=True, workdir=workdir)


# --- report -------------------------------------------------------------------

def render_markdown(meta: dict, extraction: dict, result: dict, citations: list) -> str:
    o = result.get("overall", {})
    score = o.get("score")
    out = [
        "# Fact-check report",
        "",
        f"Source: {meta.get('source', '')}  ",
        f"Author: {meta.get('author') or '-'}  ",
        f"Checked: {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "## Summary",
        extraction.get("summary", ""),
        "",
        f"Main point: {extraction.get('main_point', '')}",
        "",
        "## Overall",
        f"**Reliability: {score if score is not None else '-'}/100** - {o.get('verdict', '')}",
        "",
        o.get("summary", ""),
        "",
    ]
    if o.get("issues"):
        out += ["Main issues:"] + [f"- {i}" for i in o["issues"]] + [""]
    if o.get("missing_context"):
        out += [f"Missing context: {o['missing_context']}", ""]

    if result.get("checks"):
        out.append("## Claims")
    for c in result.get("checks", []):
        ts = f" ({c['timestamp']})" if c.get("timestamp") else ""
        out += [
            "",
            f"### {c.get('id')}. {c.get('claim', '')}",
            f"**{c.get('verdict', '')}**, confidence {c.get('confidence', '?')}/100{ts}",
            "",
            c.get("explanation", ""),
        ]
        if c.get("correction"):
            out += ["", f"Correction: {c['correction']}"]
        if c.get("sources"):
            out += [""] + [
                f"- [{s.get('title') or s.get('url')}]({s.get('url')})"
                + (f" ({s['reliability']})" if s.get("reliability") else "")
                for s in c["sources"]
            ]

    seen = set()
    extra = [c for c in citations if not (c["url"] in seen or seen.add(c["url"]))]
    if extra:
        out += ["", "## All sources consulted"]
        out += [f"- [{c['title'] or c['url']}]({c['url']})" for c in extra]

    out += ["", "---", "*Generated automatically and may contain mistakes. Check the sources.*"]
    return "\n".join(out)


def main() -> None:
    load_dotenv(ROOT / ".env")

    ap = argparse.ArgumentParser(description="Fact-check a short video.")
    ap.add_argument("source", help="video URL (Instagram, YouTube, TikTok...) or local file")
    ap.add_argument("--frames", type=int, default=8, help="frames sent to the model (default: 8)")
    ap.add_argument("--max-claims", type=int, default=8, help="max claims to check (default: 8)")
    ap.add_argument("--max-searches", type=int, default=15, help="max web searches (default: 15)")
    ap.add_argument("--whisper-model", default=os.getenv("WHISPER_MODEL", "small"),
                    help="tiny, base, small, medium or large-v3 (default: small)")
    ap.add_argument("--audio-lang", default=None, help="spoken language, e.g. en or fr (default: auto)")
    ap.add_argument("--report-lang", default=os.getenv("REPORT_LANG", "English"),
                    help="language of the report (default: English)")
    ap.add_argument("--backend", choices=["claude-code", "api"],
                    default=os.getenv("BACKEND", "claude-code"),
                    help="claude-code uses your Claude subscription, api uses API credits")
    ap.add_argument("--model", default=os.getenv("CLAUDE_MODEL"),
                    help="e.g. sonnet or opus (claude-code), claude-sonnet-5-5 (api)")
    ap.add_argument("--cookies-from-browser", default=os.getenv("COOKIES_BROWSER"),
                    help="chrome, safari, firefox... (Instagram sometimes requires a login)")
    ap.add_argument("--out", default=str(ROOT / "reports"), help="output folder (default: reports/)")
    ap.add_argument("--keep-video", action="store_true", help="keep the downloaded video")
    args = ap.parse_args()

    if args.backend == "api" and not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set (copy .env.example to .env).")

    outdir = Path(args.out) / datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir.mkdir(parents=True, exist_ok=True)

    log("Downloading video...")
    video, meta = fetch_video(args.source, outdir, args.cookies_from_browser)

    log("Transcribing audio...")
    audio = extract_audio(video, outdir)
    transcript, lang = "", None
    if audio:
        transcript, lang = transcribe(audio, args.whisper_model, args.audio_lang)
        audio.unlink(missing_ok=True)
    meta["detected_language"] = lang
    (outdir / "transcript.txt").write_text(transcript, encoding="utf-8")

    log(f"Extracting {args.frames} frames...")
    frames = extract_frames(video, outdir, args.frames)
    if not args.keep_video:
        video.unlink(missing_ok=True)

    log("Extracting claims...")
    extraction = extract_claims(args, transcript, frames, meta, outdir)
    (outdir / "claims.json").write_text(json.dumps(extraction, ensure_ascii=False, indent=2),
                                        encoding="utf-8")

    if extraction.get("claims"):
        n = len(extraction["claims"])
        log(f"Checking {n} claim{'s' if n > 1 else ''} (this can take a few minutes)...")
        result, citations = verify_claims(args, extraction, meta, outdir)
    else:
        log("No checkable claims found.")
        result = {"checks": [], "overall": {
            "verdict": "No checkable factual claims in this video.",
            "summary": "The video is mostly opinion, advice or entertainment."}}
        citations = []

    report = {"meta": meta, "extraction": extraction, "result": result, "citations": citations}
    (outdir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = render_markdown(meta, extraction, result, citations)
    (outdir / "report.md").write_text(md, encoding="utf-8")

    print(md)
    log(f"Saved to {outdir}/")


if __name__ == "__main__":
    main()
