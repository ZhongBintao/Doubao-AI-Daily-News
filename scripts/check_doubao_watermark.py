"""Check where the Doubao tag ("内容由豆包AI生成") would be cut in real Doubao audio files.

    python3 scripts/check_doubao_watermark.py raw1.wav raw2.mp3 ...
    python3 scripts/check_doubao_watermark.py --save-cut DIR raw1.wav   # also write the audio without the tag

Prints, per file: duration, first voiced frame, the pause after the tag and the cut point
(same rule the pipeline uses, see voice.watermark_scan). Nothing is uploaded anywhere.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_morning_brief import voice  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--save-cut", type=Path, help="write <name>.notag.wav (the audio from the cut on) into this folder")
    args = parser.parse_args()
    missing = 0
    for path in args.files:
        samples = voice.decode_mono(path)
        scan = voice.watermark_scan(samples)
        seconds = len(samples) / voice.SAMPLE_RATE
        if scan["status"] == "cut":
            print(f"{path.name}: {seconds:.2f}s  有声起点 {scan['onset_seconds']:.2f}s  标签后停顿 "
                  f"{scan['pause_start_seconds']:.2f}–{scan['pause_end_seconds']:.2f}s  → 从 {scan['cut'] / voice.SAMPLE_RATE:.2f}s 开始是正文")
            if args.save_cut:
                args.save_cut.mkdir(parents=True, exist_ok=True)
                voice.write_wav(args.save_cut / f"{path.stem}.notag.wav", samples[scan["cut"]:])
        else:
            missing += 1
            print(f"{path.name}: {seconds:.2f}s  没有找到水印：{scan.get('reason')}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
