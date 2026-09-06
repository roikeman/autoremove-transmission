"""Single source of truth for the running build's identity.

The version lives in the repo-root VERSION file. At image build time it is
baked in as APP_VERSION so a running container can report exactly what it is
without shipping the file lookup as the only path.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))

# In the image the file sits beside this module (/app/VERSION); in a source
# checkout it sits one level up (<repo>/VERSION).
_CANDIDATES = (
    os.path.join(_HERE, "VERSION"),
    os.path.join(os.path.dirname(_HERE), "VERSION"),
)

UNKNOWN = "0.0.0+unknown"


def _read_version():
    env = os.environ.get("APP_VERSION", "").strip()
    if env:
        return env
    for path in _CANDIDATES:
        try:
            with open(path) as f:
                value = f.read().strip()
            if value:
                return value
        except OSError:
            continue
    return UNKNOWN


__version__ = _read_version()
BUILD_SHA = os.environ.get("BUILD_SHA", "unknown")
BUILD_REF = os.environ.get("BUILD_REF", "unknown")


def info():
    """Build identity, safe to expose — contains no secrets."""
    return {
        "version": __version__,
        "sha": BUILD_SHA[:12] if BUILD_SHA != "unknown" else BUILD_SHA,
        "ref": BUILD_REF,
    }
