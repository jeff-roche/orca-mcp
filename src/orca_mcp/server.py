"""MCP server exposing the OrcaSlicer CLI.

Tools:
  check_installation   - verify OrcaSlicer is reachable, report how
  list_profiles        - enumerate user machine/process/filament profiles
  get_profile          - dump a profile's JSON
  get_model_info       - run `orca-slicer --info` on a model file
  slice_model          - slice with profiles + optional parameter overrides
  parameter_sweep      - slice N variants varying one parameter, compare
  get_slice_estimates  - parse estimates out of an existing G-code file
  analyze_gcode_layers - per-layer fan/speed/feature stats, Z-windowed

Run with:  orca-mcp   (stdio transport, the MCP default)
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import gcode as gcode_mod
from . import profiles as prof_mod
from .slicer import (
    SlicerError,
    SlicerNotFoundError,
    find_slicer,
    run_slicer,
    slicer_info,
)

mcp = FastMCP(
    "orca-mcp",
    instructions=(
        "Wraps the OrcaSlicer CLI. Typical flow: check_installation once, "
        "list_profiles to find machine/process/filament presets, then "
        "slice_model (optionally with process_overrides) and inspect the "
        "result with get_slice_estimates / analyze_gcode_layers. Use "
        "parameter_sweep to compare several values of one setting."
    ),
)

_MODEL_EXTS = {".stl", ".3mf", ".obj", ".step", ".stp", ".amf"}


def _default_output_dir() -> Path:
    """Pick a default output directory the slicer can actually write to.

    A sandboxed slicer (Flatpak) gets a *private* /tmp, so anything the CLI
    writes there is invisible to us and vice versa. Flatpak apps do share
    $HOME (per their filesystem permissions), so when the resolved slicer is
    a Flatpak we default to ~/.cache/orca-mcp instead of the system
    temp dir. Users can always pass output_dir explicitly.
    """
    sandboxed = False
    try:
        sandboxed = find_slicer().kind == "flatpak"
    except SlicerNotFoundError:
        pass

    if sandboxed:
        d = Path.home() / ".cache" / "orca-mcp"
    else:
        d = Path(tempfile.gettempdir()) / "orca-mcp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _validate_model_path(model_path: str) -> Path:
    p = Path(model_path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"Model file not found: {p}")
    if p.suffix.lower() not in _MODEL_EXTS:
        raise ValueError(
            f"Unsupported model extension '{p.suffix}'. "
            f"Supported: {sorted(_MODEL_EXTS)}"
        )
    return p


def _err(e: Exception) -> dict:
    payload = {"error": type(e).__name__, "message": str(e)}
    if isinstance(e, SlicerError):
        payload["returncode"] = e.returncode
        payload["stderr_tail"] = (e.stderr or "")[-2000:]
    return payload


@mcp.tool()
def check_installation() -> dict:
    """Check whether OrcaSlicer is installed and how it will be invoked.

    Returns the resolved executable/invocation, platform, and version banner.
    Call this first if any other tool fails unexpectedly.
    """
    return slicer_info()


@mcp.tool()
def list_profiles(profile_type: str | None = None) -> dict:
    """List user profiles from the OrcaSlicer config directory.

    Args:
        profile_type: Optional filter: 'machine', 'process', or 'filament'.
                      Omit to list all three types.
    """
    try:
        profs = prof_mod.discover_profiles(profile_type)
    except ValueError as e:
        return _err(e)
    return {
        "count": len(profs),
        "profiles": [p.to_dict() for p in profs],
        "note": (
            "Only user profiles are listed. System/vendor presets can still be "
            "referenced by passing an exported JSON file path directly."
        ),
    }


@mcp.tool()
def get_profile(name_or_path: str, profile_type: str) -> dict:
    """Return the full JSON contents of a profile.

    Args:
        name_or_path: Profile name (user profile) or path to a profile JSON.
        profile_type: 'machine', 'process', or 'filament'.
    """
    try:
        prof = prof_mod.resolve_profile(name_or_path, profile_type)
        data = prof_mod.read_profile_json(prof)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        return _err(e)
    return {"profile": prof.to_dict(), "settings": data}


@mcp.tool()
def get_model_info(model_path: str) -> dict:
    """Run OrcaSlicer's --info on a model file (bounding box, volume, facets).

    Args:
        model_path: Path to an STL/3MF/OBJ/STEP model.
    """
    try:
        p = _validate_model_path(model_path)
        res = run_slicer(["--info", str(p)], timeout=120)
    except (FileNotFoundError, ValueError, SlicerNotFoundError, SlicerError) as e:
        return _err(e)
    return {
        "model": str(p),
        "returncode": res.returncode,
        "info": res.stdout.strip(),
        "stderr_tail": res.stderr[-1000:] if res.returncode != 0 else "",
    }


def _do_slice(
    model: Path,
    machine: str,
    process: str,
    filament: str,
    output_dir: Path,
    process_overrides: dict | None,
    filament_overrides: dict | None,
    plate: int,
    timeout: int,
) -> dict:
    machine_p = prof_mod.resolve_profile(machine, "machine")
    process_p = prof_mod.resolve_profile(process, "process")
    filament_p = prof_mod.resolve_profile(filament, "filament")

    workdir = output_dir / "derived-profiles"
    process_path = Path(process_p.path)
    filament_path = Path(filament_p.path)
    if process_overrides:
        process_path = prof_mod.apply_overrides(process_p, process_overrides, workdir)
    if filament_overrides:
        filament_path = prof_mod.apply_overrides(filament_p, filament_overrides, workdir)

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slice_dir = output_dir / f"{model.stem}-{stamp}"
    slice_dir.mkdir(parents=True, exist_ok=True)

    args = [
        "--load-settings", f"{machine_p.path};{process_path}",
        "--load-filaments", str(filament_path),
        "--slice", str(plate),
        "--export-slicedata", str(slice_dir / "slicedata"),
        "--outputdir", str(slice_dir),
        str(model),
    ]
    res = run_slicer(args, timeout=timeout)

    gcode_files = sorted(slice_dir.rglob("*.gcode"))
    if res.returncode != 0 and not gcode_files:
        raise SlicerError(
            "Slicing failed", returncode=res.returncode,
            stdout=res.stdout, stderr=res.stderr,
        )

    result: dict = {
        "returncode": res.returncode,
        "used_xvfb": res.used_xvfb,
        "output_dir": str(slice_dir),
        "gcode_files": [str(g) for g in gcode_files],
        "profiles_used": {
            "machine": machine_p.name,
            "process": str(process_path),
            "filament": str(filament_path),
        },
    }
    if gcode_files:
        result["estimates"] = gcode_mod.parse_estimates(gcode_files[0])
    else:
        result["stdout_tail"] = res.stdout[-1500:]
        result["stderr_tail"] = res.stderr[-1500:]
    return result


@mcp.tool()
def slice_model(
    model_path: str,
    machine_profile: str,
    process_profile: str,
    filament_profile: str,
    process_overrides: dict | None = None,
    filament_overrides: dict | None = None,
    output_dir: str | None = None,
    plate: int = 1,
    timeout_seconds: int = 600,
) -> dict:
    """Slice a model and return G-code path(s) plus parsed estimates.

    Args:
        model_path: Path to STL/3MF/OBJ/STEP file.
        machine_profile: Machine profile name or JSON path.
        process_profile: Process (print settings) profile name or JSON path.
        filament_profile: Filament profile name or JSON path.
        process_overrides: Optional dict of process settings to override,
            e.g. {"wall_loops": 4, "fan_max_speed": 30}. Keys use OrcaSlicer's
            internal names (see get_profile output for valid keys).
        filament_overrides: Same idea for filament settings,
            e.g. {"fan_cooling_layer_time": 12, "nozzle_temperature": [235]}.
        output_dir: Where to put G-code (default: system temp / orca-mcp).
        plate: Plate index to slice (default 1; 0 slices all plates).
        timeout_seconds: Kill the slice if it exceeds this.
    """
    try:
        model = _validate_model_path(model_path)
        out = Path(output_dir).expanduser() if output_dir else _default_output_dir()
        result = _do_slice(
            model, machine_profile, process_profile, filament_profile,
            out, process_overrides, filament_overrides, plate, timeout_seconds,
        )
        warning = _sandbox_path_warning(out)
        if warning:
            result["warning"] = warning
        return result
    except (FileNotFoundError, ValueError, SlicerNotFoundError, SlicerError) as e:
        return _err(e)


def _sandbox_path_warning(path: Path) -> str | None:
    """Flag paths a Flatpak-sandboxed slicer likely cannot see."""
    try:
        cmd = find_slicer()
        if cmd.kind != "flatpak":
            return None
    except SlicerNotFoundError:
        return None
    app_id = cmd.flatpak_app_id or "<orcaslicer-app-id>"
    p = str(path.resolve())
    tmp = str(Path(tempfile.gettempdir()).resolve())
    if p.startswith(tmp) or p.startswith("/var/tmp") or p.startswith("/run/"):
        return (
            f"Output dir '{p}' is under a directory the Flatpak sandbox does "
            "not share with the host — the slicer's output may not appear "
            "there. Use a path under your home directory instead, or grant "
            f"access with: flatpak override --user --filesystem=/tmp {app_id}"
        )
    return None


@mcp.tool()
def parameter_sweep(
    model_path: str,
    machine_profile: str,
    process_profile: str,
    filament_profile: str,
    sweep_parameter: str,
    sweep_values: list,
    parameter_target: str = "process",
    output_dir: str | None = None,
    plate: int = 1,
    timeout_seconds: int = 600,
) -> dict:
    """Slice the same model once per value of a single parameter and compare.

    Args:
        model_path: Path to the model file.
        machine_profile / process_profile / filament_profile: As in slice_model.
        sweep_parameter: Setting key to vary (e.g. 'fan_max_speed',
            'slow_down_layer_time', 'wall_loops').
        sweep_values: List of values to try, e.g. [20, 40, 60, 80].
        parameter_target: 'process' or 'filament' — which profile the
            parameter belongs to.
        output_dir: Base output directory.
        plate: Plate index (default 1).
        timeout_seconds: Per-slice timeout.

    Returns one entry per value with G-code path and estimates, so results
    can be diffed (e.g. with analyze_gcode_layers over the same Z window).
    """
    if parameter_target not in ("process", "filament"):
        return _err(ValueError("parameter_target must be 'process' or 'filament'"))
    if not sweep_values:
        return _err(ValueError("sweep_values must be a non-empty list"))
    if len(sweep_values) > 12:
        return _err(ValueError("Refusing to sweep more than 12 values in one call"))

    try:
        model = _validate_model_path(model_path)
    except (FileNotFoundError, ValueError) as e:
        return _err(e)
    out = Path(output_dir).expanduser() if output_dir else _default_output_dir()

    runs = []
    for value in sweep_values:
        overrides = {sweep_parameter: value}
        try:
            r = _do_slice(
                model, machine_profile, process_profile, filament_profile, out,
                overrides if parameter_target == "process" else None,
                overrides if parameter_target == "filament" else None,
                plate, timeout_seconds,
            )
            runs.append({"value": value, "ok": True, **{
                "gcode": r["gcode_files"][0] if r["gcode_files"] else None,
                "estimates": r.get("estimates"),
                "output_dir": r["output_dir"],
            }})
        except (FileNotFoundError, ValueError, SlicerNotFoundError, SlicerError) as e:
            runs.append({"value": value, "ok": False, **_err(e)})

    comparison = [
        {
            "value": r["value"],
            "time_s": (r.get("estimates") or {}).get("estimated_time_seconds"),
            "filament_g": (r.get("estimates") or {}).get("filament_used_g"),
        }
        for r in runs if r.get("ok")
    ]
    return {
        "sweep_parameter": sweep_parameter,
        "parameter_target": parameter_target,
        "runs": runs,
        "comparison": comparison,
    }


@mcp.tool()
def get_slice_estimates(gcode_path: str) -> dict:
    """Parse print time, filament usage, and key settings from a G-code file.

    Args:
        gcode_path: Path to a .gcode file produced by OrcaSlicer.
    """
    p = Path(gcode_path).expanduser()
    if not p.is_file():
        return _err(FileNotFoundError(f"G-code file not found: {p}"))
    try:
        return gcode_mod.parse_estimates(p)
    except OSError as e:
        return _err(e)


@mcp.tool()
def analyze_gcode_layers(
    gcode_path: str,
    z_min: float | None = None,
    z_max: float | None = None,
    max_layers: int = 300,
) -> dict:
    """Per-layer stats: Z, layer height, features, fan speed, print speed range.

    Useful for inspecting a specific region of a print, e.g. an overhang or
    shoulder transition — pass z_min/z_max bracketing the region and compare
    across sweep runs.

    Args:
        gcode_path: Path to a .gcode file.
        z_min: Only include layers at or above this Z (mm).
        z_max: Only include layers at or below this Z (mm).
        max_layers: Cap on returned layers (default 300).
    """
    p = Path(gcode_path).expanduser()
    if not p.is_file():
        return _err(FileNotFoundError(f"G-code file not found: {p}"))
    try:
        return gcode_mod.analyze_layers(p, z_min=z_min, z_max=z_max,
                                        max_layers=max_layers)
    except OSError as e:
        return _err(e)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
