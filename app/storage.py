"""Is the data folder somewhere that outlives the container?

Updating a container replaces it. The database only survives that when DATA_DIR
is mapped to a folder on the host (or a named volume). Without a mapping Docker
quietly gives the image's ``VOLUME /data`` a throwaway volume, so everything
looks fine until the first update starts again from an empty database.
"""

import re
from dataclasses import dataclass
from pathlib import Path

MOUNTINFO = Path("/proc/self/mountinfo")
DOCKERENV = Path("/.dockerenv")

# Docker names a volume nobody asked for with 64 hex characters.
_ANONYMOUS_VOLUME = re.compile(r"/volumes/[0-9a-f]{64}/_data(/|$)")
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class Storage:
    persistent: bool | None  # None: could not tell, so no warning is shown
    detail: str


def _unescape(field: str) -> str:
    """mountinfo writes spaces and friends as octal escapes (``\\040``)."""
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def _mount_for(path: Path, mountinfo: str) -> tuple[str, str] | None:
    """(root, mount point) of the mount that holds ``path``."""
    best: tuple[str, str] | None = None
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        root, mount_point = _unescape(fields[3]), _unescape(fields[4])
        if not path.is_relative_to(mount_point):
            continue
        # The deepest mount point wins; of two at the same path, the later one is on top.
        if best is None or len(mount_point) >= len(best[1]):
            best = (root, mount_point)
    return best


def check(data_dir: Path, *, mountinfo: str | None = None, in_container: bool | None = None) -> Storage:
    """Classify where ``data_dir`` lives. The keyword arguments exist for tests."""
    if in_container is None:
        in_container = DOCKERENV.exists()
    if not in_container:
        return Storage(True, "not running in a container")
    if mountinfo is None:
        try:
            mountinfo = MOUNTINFO.read_text()
        except OSError:
            return Storage(None, "could not read the mount table")
    try:
        path = data_dir.resolve()
    except OSError:
        path = data_dir.absolute()
    mount = _mount_for(path, mountinfo)
    if mount is None:
        return Storage(None, "could not find the data folder in the mount table")
    root, mount_point = mount
    if mount_point == "/":
        return Storage(False, f"{data_dir} is inside the container itself")
    if _ANONYMOUS_VOLUME.search(root):
        return Storage(False, f"{mount_point} is a temporary Docker volume, not a folder on the server")
    return Storage(True, f"{mount_point} is mapped outside the container")
