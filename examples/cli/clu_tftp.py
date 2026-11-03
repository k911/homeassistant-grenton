"""Compatibility imports for the shared CLU transport library."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components"))

from homeassistant_grenton.domain.api.tftp import (
    TftpError,
    read_tftp,
)

__all__ = ['TftpError', 'read_tftp']
