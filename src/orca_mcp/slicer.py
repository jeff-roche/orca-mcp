"""Cross-platform discovery and invocation of the OrcaSlicer CLI.

Discovery order (first hit wins):
  1. ORCASLICER_PATH environment variable (explicit override)
  2. Executable on PATH (orca-slicer / orca_slicer / OrcaSlicer / orcaslicer)
  3. Well-known install locations per platform
  4. Flatpak (Linux): flatpak run <app-id> — tries com.orcaslicer.OrcaSlicer,
     then the legacy io.github.softfever.OrcaSlicer

On Linux, headless invocation is attempted directly first; if the binary
aborts because it cannot create a GL/display context, we transparently retry
under xvfb-run when available.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Candidate executable names, most common first.
_EXE_NAMES = ["orca-slicer", "orca_slicer", "OrcaSlicer", "orcaslicer"]

# Known Flatpak app IDs, newest first. The default entrypoint of these
# flatpaks passes CLI args through, so no --command override is needed.
_FLATPAK_APP_IDS = [
    "com.orcaslicer.OrcaSlicer",         # current Flathub ID
    "io.github.softfever.OrcaSlicer",    # legacy SoftFever-era ID
]

# Substrings in stderr that indicate a missing display / GL context on Linux.
_DISPLAY_ERROR_MARKERS = (
    "cannot open display",
    "could not connect to display",
    "failed to create",
    "glx",
    "egl",
    "wayland",
    "Gtk-WARNING",
    "gtk_init",
)


class SlicerNotFoundError(RuntimeError):
    """Raised when no OrcaSlicer installation can be located."""


class SlicerError(RuntimeError):
    """Raised when an OrcaSlicer invocation fails."""

    def __init__(self, message: str, *, returncode: int | None = None,
                 stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@dataclass
class SlicerCommand:
    """A resolved way to invoke OrcaSlicer.

    ``argv_prefix`` is everything before the OrcaSlicer arguments, e.g.
    ``["/usr/bin/orca-slicer"]`` or
    ``["flatpak", "run", "--command=orca-slicer", "io.github...."]``.
    """

    argv_prefix: list[str]
    kind: str  # "native" | "flatpak" | "appimage"
    description: str = ""
    flatpak_app_id: str | None = None

    def build(self, args: list[str]) -> list[str]:
        return [*self.argv_prefix, *args]


@dataclass
class SlicerResult:
    returncode: int
    stdout: str
    stderr: str
    argv: list[str] = field(default_factory=list)
    used_xvfb: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _windows_candidates() -> list[Path]:
    roots = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        val = os.environ.get(env)
        if val:
            roots.append(Path(val))
    candidates = []
    for root in roots:
        candidates.append(root / "OrcaSlicer" / "orca-slicer.exe")
        candidates.append(root / "OrcaSlicer" / "OrcaSlicer.exe")
        candidates.append(root / "Programs" / "OrcaSlicer" / "orca-slicer.exe")
    return candidates


def _macos_candidates() -> list[Path]:
    return [
        Path("/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"),
        Path.home() / "Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
    ]


def _linux_candidates() -> list[Path]:
    home = Path.home()
    candidates = [
        Path("/usr/bin/orca-slicer"),
        Path("/usr/local/bin/orca-slicer"),
        Path("/opt/orcaslicer/bin/orca-slicer"),
        home / ".local/bin/orca-slicer",
    ]
    # AppImages people commonly drop in ~/Applications or ~/bin
    for d in (home / "Applications", home / "bin", home / "Downloads"):
        if d.is_dir():
            for f in sorted(d.glob("OrcaSlicer*.AppImage")):
                candidates.append(f)
    return candidates


def _installed_flatpak_id() -> str | None:
    if platform.system() != "Linux" or not shutil.which("flatpak"):
        return None
    for app_id in _FLATPAK_APP_IDS:
        try:
            out = subprocess.run(
                ["flatpak", "info", app_id],
                capture_output=True, text=True, timeout=15,
            )
            if out.returncode == 0:
                return app_id
        except (subprocess.SubprocessError, OSError):
            continue
    return None


def find_slicer() -> SlicerCommand:
    """Locate an OrcaSlicer installation, raising SlicerNotFoundError if absent."""
    # 1. Explicit override
    override = os.environ.get("ORCASLICER_PATH")
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            kind = "appimage" if p.suffix.lower() == ".appimage" else "native"
            return SlicerCommand([str(p)], kind, f"ORCASLICER_PATH={p}")
        raise SlicerNotFoundError(
            f"ORCASLICER_PATH is set to '{override}' but no file exists there."
        )

    # 2. PATH lookup
    for name in _EXE_NAMES:
        found = shutil.which(name)
        if found:
            return SlicerCommand([found], "native", f"found on PATH: {found}")

    # 3. Well-known locations
    system = platform.system()
    if system == "Windows":
        candidates = _windows_candidates()
    elif system == "Darwin":
        candidates = _macos_candidates()
    else:
        candidates = _linux_candidates()

    for c in candidates:
        if c.is_file():
            kind = "appimage" if c.suffix.lower() == ".appimage" else "native"
            return SlicerCommand([str(c)], kind, f"found at {c}")

    # 4. Flatpak
    flatpak_id = _installed_flatpak_id()
    if flatpak_id:
        return SlicerCommand(
            ["flatpak", "run", flatpak_id],
            "flatpak",
            f"flatpak install of OrcaSlicer ({flatpak_id})",
            flatpak_app_id=flatpak_id,
        )

    raise SlicerNotFoundError(
        "Could not find an OrcaSlicer installation. Install OrcaSlicer, add it "
        "to PATH, or set the ORCASLICER_PATH environment variable to the "
        "executable (on macOS: .../OrcaSlicer.app/Contents/MacOS/OrcaSlicer)."
    )


def _looks_like_display_failure(result: subprocess.CompletedProcess) -> bool:
    blob = (result.stderr or "") + (result.stdout or "")
    blob = blob.lower()
    return result.returncode != 0 and any(m in blob for m in _DISPLAY_ERROR_MARKERS)


def run_slicer(
    args: list[str],
    *,
    timeout: int = 600,
    cwd: str | None = None,
) -> SlicerResult:
    """Run OrcaSlicer with ``args``, handling Linux headless fallback."""
    cmd = find_slicer()
    argv = cmd.build(args)

    def _run(argv_: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            argv_, capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )

    try:
        proc = _run(argv)
    except subprocess.TimeoutExpired as e:
        def _decode(s: bytes | str | None) -> str:
            return s.decode(errors="replace") if isinstance(s, bytes) else (s or "")

        stdout, stderr = _decode(e.stdout), _decode(e.stderr)
        raise SlicerError(
            f"OrcaSlicer timed out after {timeout}s", returncode=None,
            stdout=stdout, stderr=stderr,
        ) from e

    used_xvfb = False
    if (
        platform.system() == "Linux"
        and _looks_like_display_failure(proc)
        and not os.environ.get("DISPLAY")
        and shutil.which("xvfb-run")
    ):
        proc = _run(["xvfb-run", "-a", *argv])
        used_xvfb = True

    return SlicerResult(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        argv=argv,
        used_xvfb=used_xvfb,
    )


def slicer_info() -> dict:
    """Diagnostic info about the resolved installation (for check_installation)."""
    try:
        cmd = find_slicer()
    except SlicerNotFoundError as e:
        return {"found": False, "error": str(e), "platform": platform.system()}

    version = None
    try:
        res = run_slicer(["--help"], timeout=60)
        # First line of --help output usually contains the version string.
        for line in (res.stdout + res.stderr).splitlines():
            line = line.strip()
            if line:
                version = line
                break
    except SlicerError:
        pass

    return {
        "found": True,
        "invocation": cmd.argv_prefix,
        "kind": cmd.kind,
        "detail": cmd.description,
        "version_banner": version,
        "platform": platform.system(),
    }
