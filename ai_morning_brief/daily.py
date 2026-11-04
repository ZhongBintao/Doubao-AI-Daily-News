"""AI每日早报 v6 · single-entry daily orchestrator.

    python -m ai_morning_brief.daily --date YYYY-MM-DD           # run / resume
    python -m ai_morning_brief.daily --date YYYY-MM-DD --status  # show progress
    python -m ai_morning_brief.daily --date YYYY-MM-DD --check-plan
    python -m ai_morning_brief.daily --date YYYY-MM-DD --no-agent # fully automatic

Stages run in order and resume from the first unfinished one. Two stages can
hand off to an agent (LLM): ``director`` (write director_plan.json) and
``voice`` (Doubao voice clone, when --tts doubao). After a handoff the agent
re-runs the same command. Every agent stage has an automatic fallback, so an
edition always reaches a rendered MP4:

* director: invalid plan twice, or --no-agent -> deterministic fallback plan
* voice:    --no-agent or --tts edge -> Microsoft Edge neural TTS
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import config as C
from . import audio_mix, covers, director, newsroom, publish, render, selfcheck, timeline, voice
from .aihot_v2 import AIHotV2Client

STAGES = ("bootstrap", "fetch", "director", "voice", "mix", "render", "selfcheck", "cover", "publish", "report")
STAGE_LABELS = {
    "bootstrap": "环境自检", "fetch": "AIHOT 取数与选题", "director": "导演稿（写稿）", "voice": "逐句配音",
    "mix": "时间轴与混音", "render": "视频渲染", "selfcheck": "成片自检", "cover": "封面", "publish": "发布文案与整合包", "report": "完成汇总",
}
EXIT_OK, EXIT_FAILURE, EXIT_HANDOFF, EXIT_STOPPED = 0, 1, 3, 4
MAX_PLAN_ATTEMPTS = 2
SHANGHAI = timezone(timedelta(hours=8))


class StopEdition(Exception):
    """A deliberate, clean stop (e.g. fewer stories than --min-stories): nothing is made from old or invented news."""


class Handoff(Exception):
    """Raised by a stage that waits for an agent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class Run:
    def __init__(self, run_date: date, output_root: Path, *, tts: str, no_agent: bool, fixture_dir: Path | None = None,
                 tts_mode: str | None = None, min_stories: int = C.MIN_STORIES):
        self.date = run_date
        self.root = output_root
        self.dir = output_root / run_date.isoformat()
        self.art = self.dir / "artifacts"
        self.tts = tts
        self.no_agent = no_agent
        self.tts_mode = tts_mode or C.DOUBAO_TTS_MODE
        self.min_stories = min_stories
        self.fixture_dir = fixture_dir
        self.state_path = self.dir / "run_state.json"
        self.state = _read(self.state_path) or {"version": C.V6_VERSION, "date": run_date.isoformat(), "stages": {}}
        for name in STAGES:
            self.state["stages"].setdefault(name, {"status": "pending"})

    @property
    def ledger_path(self) -> Path:
        return self.root / "_state" / "aired_events.json"

    def save(self) -> None:
        _write(self.state_path, self.state)

    def stage(self, name: str) -> dict[str, Any]:
        return self.state["stages"][name]

    def client(self) -> AIHotV2Client:
        return AIHotV2Client(cache_dir=self.art / "aihot-cache", fixture_dir=self.fixture_dir)

    @property
    def video_path(self) -> Path:
        return self.dir / "renders" / f"ai-daily-news-{self.date.isoformat()}.mp4"


def _print_handoff(stage: str, lines: Sequence[str]) -> None:
    bar = "=" * 64
    print(f"\n{bar}\n  AGENT HANDOFF · {stage} · {STAGE_LABELS[stage]}\n{bar}")
    for line in lines:
        print(f"  {line}")
    print(f"{bar}\n  完成后重新运行同一条命令即可继续。\n{bar}\n")


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------
def stage_bootstrap(run: Run) -> dict[str, Any]:
    info: dict[str, Any] = {}
    local_bins = [C.REPO_ROOT / ".local" / "bin", C.REPO_ROOT / ".local" / "node" / "bin"]
    extra = [str(p) for p in local_bins if p.is_dir()]
    if extra:
        os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "")])
    for tool in ("ffmpeg", "ffprobe", "npx"):
        info[tool] = shutil.which(tool) or ""
    missing = [tool for tool in ("ffmpeg", "ffprobe", "npx") if not info[tool]]
    if missing:
        raise RuntimeError(f"缺少必要工具：{', '.join(missing)}。请先运行 bash scripts/bootstrap_env.sh")
    info["chrome"] = render.find_chrome() or ""
    font = render.ensure_font(C.FONT_CACHE_DIR)
    info["font"] = str(font) if font else ""
    try:
        import edge_tts  # noqa: F401
        info["edge_tts"] = True
    except ImportError:
        info["edge_tts"] = False
    info["warnings"] = [w for w, bad in (("未找到 Chrome：排版预处理和封面会跳过", not info["chrome"]),
                                         ("字体下载失败：将使用系统中文字体", not font),
                                         ("未安装 edge-tts：自动配音兜底不可用", not info["edge_tts"])) if bad]
    return info


def stage_fetch(run: Run) -> dict[str, Any]:
    client = run.client()
    snapshot = newsroom.enrich(client, newsroom.gather(client, run.date))
    _write(run.art / "aihot_snapshot.json", snapshot)
    aired = newsroom.load_aired(run.ledger_path)
    recent = [e for e in aired if str(e.get("date") or "") >= (run.date - timedelta(days=C.AIRED_LOOKBACK_DAYS)).isoformat()]
    reconstructed = False
    if not recent:
        aired = aired + newsroom.reconstruct_aired(client, run.date)
        reconstructed = True
    selection = newsroom.select_edition(snapshot, aired, run.date)
    selection["aired_ledger"] = {"path": str(run.ledger_path), "entries_used": len(aired), "reconstructed_from_dailies": reconstructed}
    _write(run.art / "selection.json", selection)
    count = len(selection["recommended"]["headlines"]) + len(selection["recommended"]["flashes"])
    if run.min_stories and count < run.min_stories:
        raise StopEdition(f"今天可播的新闻只有 {count} 条，少于要求的 {run.min_stories} 条，按要求终止，不用旧闻或虚构内容填充。"
                          f"候选与原因见 {run.art / 'selection.json'}")
    return {"mode": selection["mode"], "heavy_count": selection["heavy_count"], "pool": len(selection["pool"]),
            "recommended": selection["recommended"], "sources": selection["sources_used"], "errors": selection["errors"]}


def _plan_paths(run: Run) -> tuple[Path, Path]:
    return run.art / "director_plan.json", run.art / "director_task.json"


def check_plan(run: Run) -> tuple[bool, list[str]]:
    plan_path, _ = _plan_paths(run)
    selection = _read(run.art / "selection.json") or {}
    plan = _read(plan_path)
    if plan is None:
        return False, [f"找不到或无法解析 {plan_path}（必须是合法 JSON）。"]
    errors = director.validate_plan(plan, selection)
    _write(run.art / "director_check.json", {"status": "pass" if not errors else "fail", "errors": errors, "checked_at": _now()})
    return not errors, errors


def _no_news_plan() -> dict[str, Any]:
    return {"scenes": [
        {"scene_id": "hook", "kind": "hook", "category": "other", "section": "", "subject": "AIHOT", "logo": "",
         "hook": "今天 AI 圈【没有新的重磅】", "keyfact": "AIHOT 当前没有可播报的新资讯，本期不补旧闻。", "progress": None},
        {"scene_id": "outro", "kind": "outro", "text": C.OUTRO_TEXT, "progress": None}],
        "units": []}


def stage_director(run: Run) -> dict[str, Any]:
    selection = _read(run.art / "selection.json") or {}
    plan_path, task_path = _plan_paths(run)
    if selection.get("mode") == "no-news":
        from .writing import normalize_with_ledger
        final = {"version": C.V6_VERSION, "plan_source": "no-news", **_no_news_plan(), "picks": {"headlines": [], "flashes": []}, "pronunciation_ledger": []}
        for scene_id, text in (("hook", "今天 AI 圈没有新的重磅，本期不补旧闻。"), ("outro", C.OUTRO_TEXT)):
            n = normalize_with_ledger(text)
            final["units"].append({"unit_id": f"u{len(final['units']) + 1:02d}", "scene_id": scene_id, "display_text": n.display_text, "spoken_text": n.spoken_text, "card": None})
        _write(run.art / "director_final.json", final)
        return {"source": "no-news"}
    check_cmd = f"python -m ai_morning_brief.daily --date {run.date.isoformat()} --check-plan"
    task = director.build_task(run.date, selection, plan_path=plan_path, check_command=check_cmd)
    _write(task_path, task)
    stage = run.stage("director")
    source = "agent"
    if plan_path.is_file():
        ok, errors = check_plan(run)
        if not ok:
            stage["attempts"] = int(stage.get("attempts") or 0) + 1
            if stage["attempts"] < MAX_PLAN_ATTEMPTS and not run.no_agent:
                _print_handoff("director", [f"director_plan.json 校验未通过（第 {stage['attempts']} 次），请按下面的报错修改：", *errors[:12],
                                            f"校验命令：{check_cmd}"])
                raise Handoff("director")
            plan, source = director.fallback_plan(selection), "fallback-after-invalid"
            _write(run.art / "director_plan_rejected.json", {"errors": errors, "plan": _read(plan_path)})
        else:
            plan = _read(plan_path)
    elif run.no_agent:
        plan, source = director.fallback_plan(selection), "fallback-no-agent"
    else:
        _print_handoff("director", [f"读取任务文件：{task_path}", f"按其中 steps 写出：{plan_path}", f"校验命令：{check_cmd}",
                                    "写不出来也没关系：再运行一次带 --no-agent 的命令，程序会用兜底稿出片。"])
        raise Handoff("director")
    if source.startswith("fallback"):
        errors = director.validate_plan(plan, selection)
        if errors:
            raise RuntimeError("兜底稿未通过校验：" + "; ".join(errors[:5]))
        _write(run.art / "director_plan_fallback.json", plan)
    final = director.finalize(plan, selection, source=source)
    _write(run.art / "director_final.json", final)
    return {"source": source, "units": len(final["units"]), "scenes": [s["scene_id"] for s in final["scenes"]]}


def stage_voice(run: Run) -> dict[str, Any]:
    final = _read(run.art / "director_final.json")
    provider = "edge" if (run.no_agent and run.tts == "doubao") else run.tts
    manifest_path = run.art / "tts_manifest.json"
    manifest = _read(manifest_path)
    expected = [voice.spoken_for(u, provider) for u in final["units"]]
    mode = run.tts_mode if provider == "doubao" else None
    if (not manifest or manifest.get("provider") != provider or [u["spoken_text"] for u in manifest.get("units") or []] != expected
            or (manifest.get("doubao") or {}).get("mode") != mode):
        manifest = voice.build_manifest(run.dir, final, provider, mode=mode)
    if provider == "edge":
        failed = voice.synthesize_edge(run.dir, manifest)
        if failed:
            raise RuntimeError(f"edge-tts 合成失败：{', '.join(failed)}（检查网络/代理）")
    elif manifest.get("chunked"):
        missing = voice.missing_chunks(manifest)
        if missing:
            block = manifest.get("doubao") or {}
            how = ('逐组调用 text_to_audio_plus，请求 = request_template（voice 用固定音色描述，spoken_text 用该 chunk 的 text 全文），'
                   '每组一次调用，保存到该 chunk 的 output_path')
            _print_handoff("voice", [f"读取配音清单：{manifest_path}", how,
                                     f"按 doubao.instructions 执行：{block.get('instructions')}",
                                     f"还缺 {len(missing)} 组：{', '.join(missing)}",
                                     "豆包配音不可用时：加 --tts edge 重新运行，自动改用 edge-tts。"])
            raise Handoff("voice")
    else:
        missing = voice.missing_units(manifest)
        if missing:
            tool = (manifest.get("doubao") or {}).get("tool", "")
            how = (f"每条调用 text_to_audio_plus，请求：\"{C.DOUBAO_TTS_VOICE_PROFILE}\"。朗读以下文字：\"<该条 spoken_text>\"（英文和阿拉伯数字照原样读）"
                   if tool == "text_to_audio_plus" else "每条调用 audio_to_audio_plus，@音频1 = example-audio.mp3，prompt 见 doubao.prompt_template")
            _print_handoff("voice", [f"读取配音清单：{manifest_path}", how, "每条一句，保存到该条 output_path；按 doubao.instructions 执行。",
                                     f"还缺 {len(missing)} 条：{', '.join(missing)}",
                                     "豆包配音不可用时：加 --tts edge 重新运行，自动改用 edge-tts。"])
            raise Handoff("voice")
    result = voice.finalize(run.dir, manifest)
    return {"provider": provider, "units": len(result["units"]), "speech_seconds": result["total_speech_seconds"]}


def stage_mix(run: Run) -> dict[str, Any]:
    final = _read(run.art / "director_final.json")
    vm = _read(run.art / "voice_manifest.json")
    tl = timeline.build_timeline(run.dir, final, vm)
    report = audio_mix.mix(run.dir, tl, vm)
    return {"duration": tl["total_duration"], "mix": report["status"], "lufs": report["final"]["lufs"], "music_under_voice_db": report["music_under_voice_while_speaking_db"]}


def stage_render(run: Run) -> dict[str, Any]:
    final = _read(run.art / "director_final.json")
    tl = _read(run.art / "timeline.json")
    workspace = render.materialize(run.dir, final, tl, run.date)
    layout = render.layout_prepass(workspace)
    _write(run.art / "layout_report.json", layout)
    render.render(workspace, run.video_path)
    tempo_report = render.apply_final_cut_tempo(run.video_path, srt_path=run.dir / "assets" / "subtitles" / "subtitles.srt")
    _write(run.art / "tempo_report.json", tempo_report)
    probe = render.probe(run.video_path)
    _write(run.art / "video_probe.json", probe)
    return {"video": str(run.video_path.relative_to(run.dir)), "layout": layout.get("status"), "tempo": tempo_report.get("status"), **probe}


def stage_selfcheck(run: Run) -> dict[str, Any]:
    report = selfcheck.run(run.dir, run.video_path, _read(run.art / "video_probe.json") or {}, _read(run.art / "timeline.json") or {},
                           layout=_read(run.art / "layout_report.json"), mix=_read(run.art / "mix_report.json"))
    critical = {"resolution", "audio_stream", "first_frame_has_content"}
    failed = [c for c in report["checks"] if c["status"] != "pass"]
    if any(c["name"] in critical for c in failed):
        raise RuntimeError("成片自检未通过：" + "; ".join(f"{c['name']}: {c['detail']}" for c in failed))
    return {"status": report["status"], "warnings": [f"{c['name']}: {c['detail']}" for c in failed], "contact_sheet": report["contact_sheet"]}


def stage_cover(run: Run) -> dict[str, Any]:
    final = _read(run.art / "director_final.json")
    probe = _read(run.art / "video_probe.json") or {}
    manifest = covers.make_covers(run.dir, run.date, final, float(probe.get("duration") or 0))
    return {k: v.get("status") for k, v in manifest["covers"].items()}


def stage_publish(run: Run) -> dict[str, Any]:
    final = _read(run.art / "director_final.json")
    probe = _read(run.art / "video_probe.json") or {}
    package = publish.assemble(run.dir, run.date, final, run.video_path, float(probe.get("duration") or 0))
    selection = _read(run.art / "selection.json") or {}
    clusters = {c["lead"]["item_id"]: c for c in selection.get("clusters") or []}
    stories = []
    for scene in final.get("scenes") or []:
        if scene.get("kind") in {"story", "flash"}:
            cluster = clusters.get(str(scene.get("item_id")), {})
            # The ledger matches future editions by title, so it keeps AIHOT's factual title, never the hook-style one.
            title = (cluster.get("lead") or {}).get("title") or scene.get("aihot_title") or scene.get("title")
            stories.append({"item_ids": cluster.get("member_ids") or [scene.get("item_id")], "story_id": (cluster.get("lead") or {}).get("story_id", ""),
                            "title": title, "role": scene.get("kind")})
    newsroom.record_aired(run.ledger_path, run.date, stories)
    return {"package": package["path"], "xhs_title": package["copy"]["xiaohongshu"]["title"]}


def stage_report(run: Run) -> dict[str, Any]:
    probe = _read(run.art / "video_probe.json") or {}
    final = _read(run.art / "director_final.json") or {}
    stages = run.state["stages"]
    summary = {
        "date": run.date.isoformat(),
        "video": str(run.video_path),
        "duration": probe.get("duration"),
        "resolution": f"{probe.get('width')}x{probe.get('height')}",
        "plan_source": final.get("plan_source"),
        "tts": (stages.get("voice", {}).get("artifacts") or {}).get("provider"),
        "selfcheck": (stages.get("selfcheck", {}).get("artifacts") or {}).get("status"),
        "covers": stages.get("cover", {}).get("artifacts"),
        "package": (stages.get("publish", {}).get("artifacts") or {}).get("package"),
    }
    _write(run.dir / "run_report.json", {"status": "success", **summary})
    print("\n✅ AI每日早报 {date} 完成\n  视频：{video}\n  时长：{duration:.1f}s · {resolution}\n  稿件：{plan_source} · 配音：{tts} · 自检：{selfcheck}\n  发布包：{package}\n".format(
        **{**summary, "duration": float(summary["duration"] or 0)}))
    return summary


STAGE_FUNCS: dict[str, Callable[[Run], dict[str, Any]]] = {
    "bootstrap": stage_bootstrap, "fetch": stage_fetch, "director": stage_director, "voice": stage_voice, "mix": stage_mix,
    "render": stage_render, "selfcheck": stage_selfcheck, "cover": stage_cover, "publish": stage_publish, "report": stage_report,
}


def run_daily(run: Run, *, skip: set[str], from_stage: str | None, to_stage: str | None) -> int:
    if from_stage:
        reset = False
        for name in STAGES:
            reset = reset or name == from_stage
            if reset:
                run.state["stages"][name] = {"status": "pending"}
    run.save()
    for name in STAGES:
        stage = run.stage(name)
        if stage.get("status") == "done":
            continue
        if name in skip:
            stage.update(status="done", skipped=True, finished_at=_now())
            run.save()
            continue
        print(f"▶ {name} · {STAGE_LABELS[name]}")
        stage.update(status="running", started_at=_now(), error=None)
        run.save()
        try:
            artifacts = STAGE_FUNCS[name](run)
        except Handoff:
            stage.update(status="waiting_agent")
            run.save()
            return EXIT_HANDOFF
        except StopEdition as exc:
            stage.update(status="stopped", error=str(exc))
            run.save()
            print(f"■ {name}：{exc}", file=sys.stderr)
            return EXIT_STOPPED
        except Exception as exc:  # report and stop; artifacts stay for resume
            stage.update(status="failed", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc()[-2000:])
            run.save()
            print(f"✖ {name} 失败：{exc}", file=sys.stderr)
            return EXIT_FAILURE
        stage.update(status="done", finished_at=_now(), artifacts=artifacts)
        run.save()
        print(f"  ✓ {json.dumps(artifacts, ensure_ascii=False)[:300]}")
        if to_stage and name == to_stage:
            break
    return EXIT_OK


def print_status(run: Run) -> None:
    print(f"AI每日早报 {run.date.isoformat()} · {run.dir}")
    for name in STAGES:
        stage = run.stage(name)
        print(f"  {stage.get('status', 'pending'):14s} {name:10s} {STAGE_LABELS[name]}" + (f"  ← {stage['error']}" if stage.get("error") else ""))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ai_morning_brief.daily", description="AI每日早报 v6 每日编排器")
    parser.add_argument("--date", dest="run_date", help="期号日期 YYYY-MM-DD；默认北京时间今天")
    parser.add_argument("--output-root", type=Path, default=C.DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--tts", choices=C.TTS_PROVIDERS, default=C.DEFAULT_TTS_PROVIDER, help="配音：doubao（Agent 交接）或 edge（自动）")
    parser.add_argument("--tts-mode", choices=C.DOUBAO_TTS_MODES, default=C.DOUBAO_TTS_MODE,
                        help="豆包配音方式：text-to-audio（默认，text_to_audio_plus + 固定音色描述）或 voice-clone（audio_to_audio_plus + example-audio.mp3）")
    parser.add_argument("--min-stories", type=int, default=C.MIN_STORIES, help="可播新闻少于这个条数就终止（退出码 4），不用旧闻凑数；默认 0 不限制")
    parser.add_argument("--no-agent", action="store_true", help="不交接给 Agent：写稿用兜底稿，配音用 edge")
    parser.add_argument("--force", action="store_true", help="清除状态，从头重跑")
    parser.add_argument("--status", action="store_true", help="只看进度")
    parser.add_argument("--check-plan", action="store_true", help="只校验 director_plan.json")
    parser.add_argument("--skip", default="", help="跳过的阶段，逗号分隔（如 cover,publish）")
    parser.add_argument("--from", dest="from_stage", choices=STAGES, help="从指定阶段重跑")
    parser.add_argument("--to", dest="to_stage", choices=STAGES, help="执行到指定阶段为止")
    parser.add_argument("--fixture-dir", type=Path, help="离线测试：AIHOT JSON fixture 目录")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_date = date.fromisoformat(args.run_date) if args.run_date else datetime.now(SHANGHAI).date()
    run_dir = args.output_root / run_date.isoformat()
    if args.force and (run_dir / "run_state.json").is_file():
        (run_dir / "run_state.json").unlink()
    run = Run(run_date, args.output_root, tts=args.tts, no_agent=args.no_agent, fixture_dir=args.fixture_dir,
              tts_mode=args.tts_mode, min_stories=args.min_stories)
    if args.status:
        print_status(run)
        return EXIT_OK
    if args.check_plan:
        ok, errors = check_plan(run)
        print("校验通过 ✅" if ok else "校验未通过 ❌\n" + "\n".join(f"  - {e}" for e in errors))
        return EXIT_OK if ok else EXIT_FAILURE
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    return run_daily(run, skip=skip, from_stage=args.from_stage, to_stage=args.to_stage)


if __name__ == "__main__":
    raise SystemExit(main())
