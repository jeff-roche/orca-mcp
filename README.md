# orca-mcp

[![CI](https://github.com/jeff-roche/orca-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/jeff-roche/orca-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/orca-mcp)](https://pypi.org/project/orca-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/orca-mcp)](https://pypi.org/project/orca-mcp/)

An [MCP](https://modelcontextprotocol.io) server that wraps the **OrcaSlicer CLI**, letting any MCP compatible agent (Claude Code, Ollama, Codex, etc.) slice models, run parameter sweeps, and analyze the resulting G-code.

```
MCP client ──stdio──▶ orca-mcp ──subprocess──▶ orca-slicer CLI ──▶ G-code + estimates
```

Works on **Linux, macOS, and Windows**, including Flatpak and AppImage installs of OrcaSlicer.

## Why

The OrcaSlicer CLI can slice headlessly, but driving it by hand means juggling profile paths and grepping G-code comments. This server turns that into structured tools an AI agent can use: *"slice this model with fan speeds of 20/40/60% and tell me which layers at the shoulder transition change"* becomes a two-tool-call workflow.

## Requirements

- Python 3.10+
- OrcaSlicer installed (native package, AppImage, Flatpak, or Windows/macOS installer)
- Linux headless machines: `xvfb-run` recommended (`dnf install xorg-x11-server-Xvfb` / `apt install xvfb`) — some OrcaSlicer builds need a display context even in CLI mode. The server detects this and retries under xvfb automatically.

## Install

```bash
pip install orca-mcp

# or from source:
git clone https://github.com/jeff-roche/orca-mcp
cd orca-mcp && pip install .
```

## Configure your MCP client

**Claude Desktop** (`claude_desktop_config.json`) or **Claude Code** (`claude mcp add`):

```json
{
  "mcpServers": {
    "orca-mcp": {
      "command": "orca-mcp"
    }
  }
}
```

If OrcaSlicer isn't on your PATH, point the server at it:

```json
{
  "mcpServers": {
    "orca-mcp": {
      "command": "orca-mcp",
      "env": {
        "ORCASLICER_PATH": "/home/you/Applications/OrcaSlicer_Linux_V2.3.0.AppImage"
      }
    }
  }
}
```

### How the slicer is located

First match wins:

1. `ORCASLICER_PATH` env var (any executable, including an AppImage)
2. `orca-slicer` / `OrcaSlicer` on PATH
3. Well-known locations:
   - Linux: `/usr/bin`, `/usr/local/bin`, `~/.local/bin`, `~/Applications/*.AppImage`
   - macOS: `/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer`
   - Windows: `%ProgramFiles%\OrcaSlicer\`, `%LocalAppData%\Programs\OrcaSlicer\`
4. Flatpak (`com.orcaslicer.OrcaSlicer`, with legacy `io.github.softfever.OrcaSlicer` as fallback)

### How profiles are located

User profiles are discovered from the standard OrcaSlicer config directory (`~/.config/OrcaSlicer` on Linux, `~/Library/Application Support/OrcaSlicer` on macOS, `%APPDATA%\OrcaSlicer` on Windows, plus the Flatpak sandbox path). Override with `ORCASLICER_CONFIG_DIR` if yours lives elsewhere.

Only *user* profiles are enumerated by `list_profiles`. Vendor/system presets still work — pass a path to an exported profile JSON instead of a name.

## Tools

| Tool | What it does |
|---|---|
| `check_installation` | Verify OrcaSlicer is reachable; report invocation, platform, version |
| `list_profiles` | Enumerate user machine/process/filament profiles |
| `get_profile` | Dump a profile's full JSON (useful to find valid override keys) |
| `get_model_info` | `--info` on an STL/3MF/OBJ/STEP: bounding box, volume, facets |
| `slice_model` | Slice with chosen profiles, optional per-call setting overrides |
| `parameter_sweep` | Slice N variants varying one setting, return a comparison table |
| `get_slice_estimates` | Parse time/filament/settings out of an existing G-code file |
| `analyze_gcode_layers` | Per-layer fan %, speed range, feature types — Z-windowable |

### Tool reference

#### `check_installation()`

No arguments. Returns the resolved executable/invocation, platform, and version banner. Call this first if any other tool fails unexpectedly.

#### `list_profiles(profile_type=None)`

| Arg | Type | Default | Description |
|---|---|---|---|
| `profile_type` | `str \| None` | `None` | Filter: `"machine"`, `"process"`, or `"filament"`. Omit to list all three. |

Returns `{"count", "profiles", "note"}`. Only user profiles are listed — see [How profiles are located](#how-profiles-are-located).

#### `get_profile(name_or_path, profile_type)`

| Arg | Type | Description |
|---|---|---|
| `name_or_path` | `str` | Profile name (user profile) or path to a profile JSON. |
| `profile_type` | `str` | `"machine"`, `"process"`, or `"filament"`. |

Returns `{"profile", "settings"}` — `settings` is the full profile JSON, useful for finding valid `process_overrides`/`filament_overrides` keys.

#### `get_model_info(model_path)`

| Arg | Type | Description |
|---|---|---|
| `model_path` | `str` | Path to an STL/3MF/OBJ/STEP model. |

Runs OrcaSlicer's `--info` and returns bounding box, volume, and facet count.

#### `slice_model(...)`

| Arg | Type | Default | Description |
|---|---|---|---|
| `model_path` | `str` | — | Path to STL/3MF/OBJ/STEP file. |
| `machine_profile` | `str` | — | Machine profile name or JSON path. |
| `process_profile` | `str` | — | Process (print settings) profile name or JSON path. |
| `filament_profile` | `str` | — | Filament profile name or JSON path. |
| `process_overrides` | `dict \| None` | `None` | Process settings to override, e.g. `{"wall_loops": 4}`. See [Overrides](#overrides). |
| `filament_overrides` | `dict \| None` | `None` | Filament settings to override, e.g. `{"fan_cooling_layer_time": 12}`. |
| `output_dir` | `str \| None` | system temp / `orca-mcp` | Where to put the sliced G-code. |
| `plate` | `int` | `1` | Plate index to slice; `0` slices all plates. |
| `timeout_seconds` | `int` | `600` | Kill the slice if it exceeds this. |

Returns G-code path(s) plus parsed estimates (`{"output_dir", "gcode_files", "profiles_used", "estimates", ...}`).

#### `parameter_sweep(...)`

| Arg | Type | Default | Description |
|---|---|---|---|
| `model_path` | `str` | — | Path to the model file. |
| `machine_profile` / `process_profile` / `filament_profile` | `str` | — | As in `slice_model`. |
| `sweep_parameter` | `str` | — | Setting key to vary, e.g. `"fan_max_speed"`. |
| `sweep_values` | `list` | — | Values to try, e.g. `[20, 40, 60, 80]`. Max 12. |
| `parameter_target` | `str` | `"process"` | `"process"` or `"filament"` — which profile the parameter belongs to. |
| `output_dir` | `str \| None` | system temp / `orca-mcp` | Base output directory. |
| `plate` | `int` | `1` | Plate index. |
| `timeout_seconds` | `int` | `600` | Per-slice timeout. |

Slices once per value and returns one entry per value (`"runs"`) plus a flattened `"comparison"` table of time/filament, so results can be diffed — e.g. by feeding each G-code path to `analyze_gcode_layers`.

#### `get_slice_estimates(gcode_path)`

| Arg | Type | Description |
|---|---|---|
| `gcode_path` | `str` | Path to a `.gcode` file produced by OrcaSlicer. |

Parses print time, filament usage, and key settings out of the G-code header comments.

#### `analyze_gcode_layers(gcode_path, z_min=None, z_max=None, max_layers=300)`

| Arg | Type | Default | Description |
|---|---|---|---|
| `gcode_path` | `str` | — | Path to a `.gcode` file. |
| `z_min` | `float \| None` | `None` | Only include layers at or above this Z (mm). |
| `z_max` | `float \| None` | `None` | Only include layers at or below this Z (mm). |
| `max_layers` | `int` | `300` | Cap on returned layers. |

Returns per-layer Z, layer height, feature types, fan speed, and print speed range — useful for inspecting a specific region (e.g. an overhang) and comparing across sweep runs.

### Overrides

`slice_model` accepts `process_overrides` and `filament_overrides` dicts. Keys are OrcaSlicer's internal setting names — run `get_profile` on your process profile to see them. The server merges overrides into a temp copy of the profile (handling Orca's string/list-of-strings value convention) and passes that to the CLI, so your saved profiles are never modified.

```jsonc
// example slice_model arguments
{
  "model_path": "/home/you/discs/fairway-driver-v7.stl",
  "machine_profile": "Elegoo Centauri Carbon 0.6 nozzle",
  "process_profile": "0.2mm TPU disc",
  "filament_profile": "Generic TPU",
  "process_overrides": { "wall_loops": 4, "slow_down_layer_time": 12 },
  "filament_overrides": { "fan_max_speed": [30] }
}
```

### Example agent workflows

**Basic slice + estimate:**
> "Slice `mdoel.stl` with my TPU profiles and tell me the print time and filament weight."

**Parameter sweep:**
> "Sweep `fan_max_speed` over 20, 40, 60, 80 on `model.stl` and compare print times."

**Regional G-code inspection:**
> "For each sweep result, analyze layers between Z=8mm and Z=12mm and tell me where fan speed and print speed diverge."

## Platform notes

- **Linux (native/AppImage):** if running with no `$DISPLAY` and the slicer aborts with a GL/display error, the server retries under `xvfb-run -a` automatically when available.
- **Linux (Flatpak):** invoked via `flatpak run <app-id>`. Note the Flatpak sandbox can only read paths it has permission for — models under `$HOME` are fine by default; use `flatpak override --filesystem=...` for other locations.
- **Windows:** `orca-slicer.exe` (the console binary) is preferred over `OrcaSlicer.exe` when both exist.
- **macOS:** point `ORCASLICER_PATH` inside the app bundle if the auto-detected location doesn't match your install.

## Caveats

- The CLI is a subset of the GUI: no live preview, and some calibration flows are GUI-only. Slice/estimate/export covers the automation use cases.
- CLI flags have shifted slightly across OrcaSlicer versions (e.g. `--load-filaments` vs `--load-filament`, `--export-3mf` availability). This server targets OrcaSlicer 2.x; open an issue with your `check_installation` output if a flag mismatch bites you.
- Estimates come from the G-code header, i.e. the slicer's own time model — treat them as relative comparisons between sweep runs, not stopwatch truth.

## Development

```bash
pip install -e ".[dev]"    # installs pytest + ruff
pytest
ruff check .
```

The test suite covers G-code parsing, layer analysis, profile discovery, and override merging with synthetic fixtures — no OrcaSlicer install needed to run it. Both `pytest` and `ruff check` run in CI on every push/PR (`.github/workflows/ci.yml`).

## License

MIT
