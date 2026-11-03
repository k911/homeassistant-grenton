"""Compatibility imports for the shared CLU transport library."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components"))

from homeassistant_grenton.domain.api.clu_files import (
    ManagementProtocol,
    download_file,
    fetch_report,
)

__all__ = ['ManagementProtocol', 'download_file', 'fetch_report']
