#!/usr/bin/env python3
"""Compare CPU and GPU inference for the same OpenVINO image encoder.

The built-in demo is only a device smoke test. Supply the exported style image
encoder with --model before using the results to select a production device.
No images are uploaded and this command does not modify the MFU application.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("At least one timing is required")
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def static_shape(partial_shape: Any) -> tuple[int, ...]:
    dimensions = []
    for dimension in partial_shape:
        if dimension.is_dynamic:
            raise ValueError("The benchmark needs a static image-encoder shape")
        dimensions.append(int(dimension.get_length()))
    if not dimensions or any(value <= 0 for value in dimensions):
        raise ValueError("The benchmark needs positive input dimensions")
    return tuple(dimensions)


def demo_model(ov: Any) -> Any:
    """A small image-like network for device detection, not style accuracy."""
    from openvino import opset13

    image = opset13.parameter([1, 3, 224, 224], np.float32, name="image")
    weights = np.random.default_rng(0).normal(0, 0.04, (32, 3, 3, 3)).astype(np.float32)
    conv = opset13.convolution(image, opset13.constant(weights), [1, 1], [1, 1], [1, 1], [1, 1])
    activation = opset13.relu(conv)
    pool = opset13.avg_pool(activation, [1, 1], [0, 0], [0, 0], [4, 4], exclude_pad=True)
    return ov.Model([pool], [image], "mfu_style_device_demo")


def benchmark_device(core: Any, model: Any, device: str, sample: np.ndarray, warmup: int, runs: int) -> dict[str, Any]:
    started = time.perf_counter()
    compiled = core.compile_model(model, device)
    compile_ms = (time.perf_counter() - started) * 1000
    request = compiled.create_infer_request()
    input_name = compiled.input(0).get_any_name()
    for _ in range(warmup):
        request.infer({input_name: sample})
    elapsed_ms = []
    result = None
    for _ in range(runs):
        started = time.perf_counter()
        request.infer({input_name: sample})
        elapsed_ms.append((time.perf_counter() - started) * 1000)
        result = np.asarray(request.get_output_tensor(0).data).copy()
    return {
        "device": device,
        "status": "ok",
        "compile_ms": round(compile_ms, 2),
        "median_ms": round(statistics.median(elapsed_ms), 2),
        "p95_ms": round(percentile(elapsed_ms, 0.95), 2),
        "images_per_second": round(1000 / statistics.mean(elapsed_ms), 2),
        "output": result,
    }


def compare(model_path: Path | None, warmup: int, runs: int) -> dict[str, Any]:
    try:
        import openvino as ov
    except ImportError as exc:
        raise RuntimeError("OpenVINO is not installed in this Python environment") from exc

    core = ov.Core()
    model = core.read_model(str(model_path)) if model_path else demo_model(ov)
    if len(model.inputs) != 1:
        raise ValueError("Supply the image encoder alone (exactly one image input)")
    shape = static_shape(model.input(0).partial_shape)
    if len(shape) != 4 or shape[0] != 1:
        raise ValueError("Expected one image with NCHW input shape")
    sample = np.random.default_rng(42).random(shape, dtype=np.float32)
    report: dict[str, Any] = {
        "model": str(model_path) if model_path else "demo-device-smoke-test",
        "shape": shape,
        "warmup": warmup,
        "runs": runs,
        "available_devices": list(core.available_devices),
        "results": {},
    }
    cpu_output = None
    for device in ("CPU", "GPU"):
        if not any(name == device or name.startswith(f"{device}.") for name in core.available_devices):
            report["results"][device] = {"status": "unavailable", "reason": "device_not_detected"}
            continue
        try:
            result = benchmark_device(core, model, device, sample, warmup, runs)
            output = result.pop("output")
            if device == "CPU":
                cpu_output = output
            elif cpu_output is not None:
                result["max_absolute_difference_from_cpu"] = round(
                    float(np.max(np.abs(output.astype(np.float32) - cpu_output.astype(np.float32)))), 6
                )
            report["results"][device] = result
        except Exception as exc:
            report["results"][device] = {"status": "error", "reason": type(exc).__name__, "message": str(exc)[:400]}
    cpu = report["results"].get("CPU", {})
    gpu = report["results"].get("GPU", {})
    if cpu.get("status") == gpu.get("status") == "ok":
        report["gpu_speedup_vs_cpu"] = round(cpu["median_ms"] / max(gpu["median_ms"], 0.001), 2)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, help="OpenVINO IR/ONNX image encoder; omit for device smoke test")
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--runs", type=int, default=20)
    args = parser.parse_args()
    if args.model and not args.model.is_file():
        parser.error("--model must be an existing file")
    if args.warmup < 0 or args.runs < 1:
        parser.error("--warmup must be >= 0 and --runs must be >= 1")
    try:
        report = compare(args.model, args.warmup, args.runs)
    except (RuntimeError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["results"]["CPU"]["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
