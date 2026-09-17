"""Sentinella — Cross-platform system monitor."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sentinella-monitor")
except PackageNotFoundError:
    __version__ = "1.0.0"  # fallback for editable installs without metadata

__app_name__ = "sentinella"

__all__ = ["__version__", "__app_name__"]
