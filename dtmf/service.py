"""stdio JSON 服务：每行一个 JSON 请求，每行一个 JSON 响应。

仅做离线数值处理：读本地 PCM/WAV 或合成信号，返回识别结果或写出文件，
不包含任何播放器或界面。

请求示例见 examples/ 目录。支持的动作：
- decode     : {"action": "decode", "pcm_file": "a.pcm", "sample_rate": 8000,
                "config": {...可选 DetectorConfig 字段...}}
- synthesize : {"action": "synthesize", "keys": "159#", "out_file": "out.pcm",
                "config": {...可选 SynthConfig 字段...}}
- decode_synth: {"action": "decode_synth", "keys": "159#",
                 "synth": {...}, "detector": {...}}  # 合成后立即解码，便于自测
"""

from __future__ import annotations

import json
import sys
from dataclasses import fields

from .detector import DetectorConfig, DtmfDetector
from .pcmio import read_pcm, write_pcm
from .synth import SynthConfig, synthesize, to_int16


def _filter_kwargs(cls, data: dict) -> dict:
    valid = {f.name for f in fields(cls)}
    unknown = set(data) - valid
    if unknown:
        raise ValueError(f"{cls.__name__} 不支持字段: {sorted(unknown)}")
    return {k: v for k, v in data.items() if k in valid}


def handle_request(req: dict) -> dict:
    action = req.get("action")
    if action == "decode":
        samples, fs = read_pcm(req["pcm_file"], req.get("sample_rate"))
        cfg_dict = dict(req.get("config") or {})
        cfg_dict.setdefault("sample_rate", fs)
        detector = DtmfDetector(DetectorConfig(**_filter_kwargs(DetectorConfig, cfg_dict)))
        result = detector.decode(samples)
        return {"ok": True, "sample_rate": fs, "num_samples": int(len(samples)),
                **result.to_dict()}
    if action == "synthesize":
        cfg = SynthConfig(**_filter_kwargs(SynthConfig, req.get("config") or {}))
        signal, fs = synthesize(req["keys"], cfg)
        out = req["out_file"]
        write_pcm(out, to_int16(signal), fs)
        return {"ok": True, "out_file": out, "sample_rate": fs,
                "num_samples": int(len(signal))}
    if action == "decode_synth":
        synth_cfg = SynthConfig(**_filter_kwargs(SynthConfig, req.get("synth") or {}))
        signal, fs = synthesize(req["keys"], synth_cfg)
        det_dict = dict(req.get("detector") or {})
        det_dict.setdefault("sample_rate", fs)
        detector = DtmfDetector(DetectorConfig(**_filter_kwargs(DetectorConfig, det_dict)))
        result = detector.decode(to_int16(signal))
        return {"ok": True, "expected": req["keys"], "matched": result.digits == req["keys"],
                **result.to_dict()}
    raise ValueError(f"未知 action: {action!r}（支持 decode / synthesize / decode_synth）")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
            resp.setdefault("id", req.get("id"))
        except Exception as exc:  # 服务不中断，错误随响应返回
            resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            try:
                resp["id"] = json.loads(line).get("id")
            except Exception:
                resp["id"] = None
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
