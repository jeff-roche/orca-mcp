# orcaslicer-mcp

An [MCP](https://modelcontextprotocol.io) server that wraps the **OrcaSlicer CLI**, letting any MCP client (Claude Desktop, Claude Code, etc.) slice models, run parameter sweeps, and analyze the resulting G-code.

```
MCP client ──stdio──▶ orcaslicer-mcp ──subprocess──▶ orca-slicer CLI ──▶ G-code + estimates
```

Works on **Linux, macOS, and Windows**, including Flatpak and AppImage installs of OrcaSlicer.

## Why

The OrcaSlicer CLI can slice headlessly, but driving it by hand means juggling profile paths and grepping G-code comments. This server turns that into structured tools an AI agent can use: *"slice this disc with fan speeds of 20/40/60% and tell me which layers at the shoulder transition change"* becomes a two-tool-call workflow.

## Requirements

- Python 3.10+
- OrcaSlicer installed (native package, AppImage, Flatpak, or Windows/macOS installer)
- Linux headless machines: `xvfb-run` recommended (`dnf install xorg-x11-server-Xvfb` / `apt install xvfb`) — some OrcaSlicer builds need a display context even in CLI mode. The server detects this and retries under xvfb automatically.

## Install

```bash
pip install orcaslicer-mcp        # once published
# or from source:
git clone https://github.com/CHANGEME/orcaslicer-mcp
cd orcaslicer-mcp && pip install .
```

## Configure your MCP client

**Claude Desktop** (`claude_desktop_config.json`) or **Claude Code** (`claude mcp add`):

```json
{
  "mcpServers": {
    "orcaslicer": {
      "command": "orcaslicer-mcp"
    }
  }
}
```

If OrcaSlicer isn't on your PATH, point the server at it:

```json
{
  "mcpServers": {
    "orcaslicer": {
      "command": "orcaslicer-mcp",
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
> "Slice `disc.stl` with my TPU profiles and tell me the print time and filament weight."

**Parameter sweep:**
> "Sweep `fan_max_speed` over 20, 40, 60, 80 on `disc.stl` and compare print times."

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
pip install -e ".[dev]"    # or just: pip install -e . pytest
pytest
```

The test suite covers G-code parsing, layer analysis, profile discovery, and override merging with synthetic fixtures — no OrcaSlicer install needed to run it.

## License

MIT
