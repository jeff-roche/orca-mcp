"""Discovery and manipulation of OrcaSlicer profiles (machine/process/filament).

OrcaSlicer stores user profiles as JSON under a per-platform config dir:

  Linux:   ~/.config/OrcaSlicer/user/<account>/{machine,process,filament}/
  macOS:   ~/Library/Application Support/OrcaSlicer/user/<account>/...
  Windows: %APPDATA%/OrcaSlicer/user/<account>/...

System (vendor) presets live under a sibling ``system/`` tree inside the
installation's resources, and additionally get cached in the config dir on
some platforms. User profiles frequently use ``"inherits"`` to point at a
system preset; the CLI resolves inheritance itself as long as it can find its
resource tree, so we pass profiles through untouched except when the caller
requests overrides — in which case we shallow-merge overrides into a copy of
the profile JSON written to a temp file.

The flatpak config dir is also checked on Linux:
  ~/.var/app/*OrcaSlicer*/config/OrcaSlicer/... (any installed app ID)
"""

from __future__ import annotations

import json
import os
import platform
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

PROFILE_TYPES = ("machine", "process", "filament")


@dataclass
class Profile:
    name: str
    type: str          # machine | process | filament
    path: str
    source: str        # "user" | "explicit"
    inherits: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _config_roots() -> list[Path]:
    # ORCASLICER_CONFIG_DIR overrides discovery entirely (see README) so tests
    # and non-standard installs aren't polluted by platform default locations.
    override = os.environ.get("ORCASLICER_CONFIG_DIR")
    if override:
        root = Path(override).expanduser()
        return [root] if root.is_dir() else []

    system = platform.system()
    home = Path.home()
    roots: list[Path] = []
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.append(Path(appdata) / "OrcaSlicer")
    elif system == "Darwin":
        roots.append(home / "Library/Application Support/OrcaSlicer")
    else:
        roots.append(home / ".config/OrcaSlicer")
        # Flatpak sandbox config dirs — app ID varies by install era, so glob.
        var_app = home / ".var/app"
        if var_app.is_dir():
            for d in sorted(var_app.glob("*OrcaSlicer*/config/OrcaSlicer")):
                roots.append(d)
    return [r for r in roots if r.is_dir()]


def discover_profiles(profile_type: str | None = None) -> list[Profile]:
    """List user profiles found in OrcaSlicer config directories."""
    types = [profile_type] if profile_type else list(PROFILE_TYPES)
    for t in types:
        if t not in PROFILE_TYPES:
            raise ValueError(f"profile_type must be one of {PROFILE_TYPES}, got {t!r}")

    profiles: list[Profile] = []
    seen: set[str] = set()
    for root in _config_roots():
        user_root = root / "user"
        if not user_root.is_dir():
            continue
        for account_dir in sorted(user_root.iterdir()):
            for t in types:
                type_dir = account_dir / t
                if not type_dir.is_dir():
                    continue
                for f in sorted(type_dir.glob("*.json")):
                    key = f"{t}:{f.stem}"
                    if key in seen:
                        continue
                    seen.add(key)
                    inherits = None
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                        inherits = data.get("inherits")
                    except (json.JSONDecodeError, OSError):
                        pass
                    profiles.append(
                        Profile(name=f.stem, type=t, path=str(f),
                                source="user", inherits=inherits)
                    )
    return profiles


def resolve_profile(name_or_path: str, profile_type: str) -> Profile:
    """Resolve a profile reference: an explicit path, or a user-profile name."""
    p = Path(name_or_path).expanduser()
    if p.is_file():
        inherits = None
        try:
            inherits = json.loads(p.read_text(encoding="utf-8")).get("inherits")
        except (json.JSONDecodeError, OSError):
            pass
        return Profile(name=p.stem, type=profile_type, path=str(p),
                       source="explicit", inherits=inherits)

    matches = [
        prof for prof in discover_profiles(profile_type)
        if prof.name.lower() == name_or_path.lower()
    ]
    if matches:
        return matches[0]

    # Fall back to substring match so "PolyFlex TPU95" finds
    # "Generic TPU @Elegoo Centauri Carbon - PolyFlex TPU95" style names.
    partial = [
        prof for prof in discover_profiles(profile_type)
        if name_or_path.lower() in prof.name.lower()
    ]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        options = ", ".join(prof.name for prof in partial[:8])
        raise FileNotFoundError(
            f"Ambiguous {profile_type} profile '{name_or_path}'. "
            f"Matches: {options}"
        )

    available = ", ".join(
        prof.name for prof in discover_profiles(profile_type)[:12]
    ) or "(none found)"
    raise FileNotFoundError(
        f"No {profile_type} profile named '{name_or_path}' found and it is not "
        f"a file path. Available user profiles: {available}"
    )


def read_profile_json(profile: Profile) -> dict:
    return json.loads(Path(profile.path).read_text(encoding="utf-8"))


def apply_overrides(profile: Profile, overrides: dict, workdir: Path) -> Path:
    """Write a temp copy of ``profile`` with ``overrides`` merged in.

    OrcaSlicer profile JSON is flat key -> value (values are strings or lists
    of strings, one per extruder/filament). We coerce scalars to strings to
    match the native format, and wrap values in a single-element list when the
    existing key is list-typed.
    """
    data = read_profile_json(profile)

    for key, value in overrides.items():
        existing = data.get(key)
        if isinstance(existing, list):
            if isinstance(value, list):
                data[key] = [str(v) for v in value]
            else:
                data[key] = [str(value)] * max(1, len(existing))
        else:
            data[key] = str(value) if not isinstance(value, (dict, list)) else value

    # Give the derived profile a distinct name so G-code metadata reflects it.
    suffix = "+".join(f"{k}={overrides[k]}" for k in list(overrides)[:3])
    data["name"] = f"{data.get('name', profile.name)} [{suffix}]"[:120]

    workdir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f"{profile.type}_override_", suffix=".json", dir=str(workdir)
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return Path(tmp_path)
