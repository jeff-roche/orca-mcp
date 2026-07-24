"""Parsing of OrcaSlicer G-code output: header estimates and layer analysis.

OrcaSlicer (like its Prusa/Bambu ancestors) writes rich metadata as comments:

  ; estimated printing time (normal mode) = 1h 23m 45s
  ; total filament used [g] = 34.56
  ; total filament length [mm] : 11504.32
  ; filament_type = TPU
  ...full config dump at the end of the file...

Layer boundaries are marked with ``; CHANGE_LAYER`` / ``; Z_HEIGHT:`` /
``;LAYER_CHANGE`` depending on version, and feature types with
``; FEATURE:`` lines. We parse defensively across variants.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

_TIME_RE = re.compile(
    r";\s*estimated printing time.*?=\s*(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?",
    re.IGNORECASE,
)
_KV_RE = re.compile(r";\s*([\w\[\] .%-]+?)\s*[=:]\s*(.+?)\s*$")

_LAYER_MARKERS = (";LAYER_CHANGE", "; CHANGE_LAYER", ";LAYER:")
_Z_RE = re.compile(r";\s*Z(?:_HEIGHT)?\s*[:=]\s*([\d.]+)", re.IGNORECASE)
_HEIGHT_RE = re.compile(r";\s*(?:LAYER_)?HEIGHT\s*[:=]\s*([\d.]+)", re.IGNORECASE)
_FEATURE_RE = re.compile(r";\s*(?:FEATURE|TYPE)\s*:\s*(.+?)\s*$", re.IGNORECASE)
_FAN_RE = re.compile(r"^M106(?:\s+P\d+)?\s+S(\d+)")
_FAN_OFF_RE = re.compile(r"^M107")
_MOVE_E_RE = re.compile(r"^G1\s+(?=[^;]*E[\d.]+)[^;]*F(\d+(?:\.\d+)?)")


@dataclass
class LayerStats:
    index: int
    z: float | None = None
    layer_height: float | None = None
    line_start: int = 0
    line_end: int = 0
    features: list[str] = field(default_factory=list)
    fan_speed_pct: float | None = None      # last fan speed set in this layer
    min_print_speed_mms: float | None = None
    max_print_speed_mms: float | None = None
    extrusion_line_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def parse_estimates(gcode_path: str | Path) -> dict:
    """Extract headline estimates and key config values from a G-code file."""
    path = Path(gcode_path)
    text_head = ""
    text_tail = ""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()

    head = lines[:400]
    tail = lines[-1200:] if len(lines) > 1200 else lines
    text_head = "".join(head)
    text_tail = "".join(tail)
    blob = text_head + text_tail

    result: dict = {"gcode_path": str(path), "size_bytes": path.stat().st_size}

    m = _TIME_RE.search(blob)
    if m:
        d, h, mi, s = (int(g) if g else 0 for g in m.groups())
        total = ((d * 24 + h) * 60 + mi) * 60 + s
        result["estimated_time_seconds"] = total
        result["estimated_time_pretty"] = _pretty_time(total)

    wanted_keys = {
        "total filament used [g]": "filament_used_g",
        "total filament length [mm]": "filament_used_mm",
        "total filament volume [cm^3]": "filament_used_cm3",
        "filament used [g]": "filament_used_g",
        "filament used [mm]": "filament_used_mm",
        "filament_type": "filament_type",
        "nozzle_diameter": "nozzle_diameter",
        "layer_height": "layer_height",
        "wall_loops": "wall_loops",
        "sparse_infill_density": "sparse_infill_density",
        "printer_model": "printer_model",
        "total layer number": "total_layers",
        "max_z_height": "max_z_height",
    }
    for line in blob.splitlines():
        kv = _KV_RE.match(line)
        if not kv:
            continue
        key = kv.group(1).strip().lower()
        if key in wanted_keys and wanted_keys[key] not in result:
            result[wanted_keys[key]] = _coerce(kv.group(2).strip())

    return result


def analyze_layers(
    gcode_path: str | Path,
    z_min: float | None = None,
    z_max: float | None = None,
    max_layers: int = 300,
) -> dict:
    """Per-layer stats (features, fan, speed range), optionally windowed by Z.

    This is the tool you want for inspecting a specific geometric region,
    e.g. an overhang/shoulder transition: pass z_min/z_max around the region
    and compare fan speed, feature mix, and speed ranges layer by layer.
    """
    path = Path(gcode_path)
    layers: list[LayerStats] = []
    current: LayerStats | None = None
    fan_pct: float | None = None

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()

            if any(line.startswith(mk) for mk in _LAYER_MARKERS):
                if current is not None:
                    current.line_end = lineno - 1
                    layers.append(current)
                current = LayerStats(index=len(layers), line_start=lineno,
                                     fan_speed_pct=fan_pct)
                continue

            if current is None:
                m = _FAN_RE.match(line)
                if m:
                    fan_pct = round(int(m.group(1)) / 255 * 100, 1)
                continue

            if current.z is None:
                zm = _Z_RE.match(line)
                if zm:
                    current.z = float(zm.group(1))
                    continue
            if current.layer_height is None:
                hm = _HEIGHT_RE.match(line)
                if hm:
                    current.layer_height = float(hm.group(1))
                    continue

            fm = _FEATURE_RE.match(line)
            if fm:
                feat = fm.group(1)
                if feat not in current.features:
                    current.features.append(feat)
                continue

            m = _FAN_RE.match(line)
            if m:
                fan_pct = round(int(m.group(1)) / 255 * 100, 1)
                current.fan_speed_pct = fan_pct
                continue
            if _FAN_OFF_RE.match(line):
                fan_pct = 0.0
                current.fan_speed_pct = 0.0
                continue

            em = _MOVE_E_RE.match(line)
            if em:
                speed = float(em.group(1)) / 60.0  # F is mm/min
                current.extrusion_line_count += 1
                if current.min_print_speed_mms is None or speed < current.min_print_speed_mms:
                    current.min_print_speed_mms = round(speed, 1)
                if current.max_print_speed_mms is None or speed > current.max_print_speed_mms:
                    current.max_print_speed_mms = round(speed, 1)

    if current is not None:
        current.line_end = lineno
        layers.append(current)

    if z_min is not None or z_max is not None:
        lo = z_min if z_min is not None else float("-inf")
        hi = z_max if z_max is not None else float("inf")
        layers = [layer for layer in layers if layer.z is not None and lo <= layer.z <= hi]

    truncated = len(layers) > max_layers
    layers = layers[:max_layers]

    return {
        "gcode_path": str(path),
        "layer_count_returned": len(layers),
        "truncated": truncated,
        "layers": [layer.to_dict() for layer in layers],
    }


def _pretty_time(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _coerce(value: str):
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value
