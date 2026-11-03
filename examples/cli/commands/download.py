"""Download CLU files with an atomic local save."""

import argparse
import asyncio
import logging
import os
import tempfile
from pathlib import Path

from config import get_config_path
from grenton_api import GrentonManager

_LOGGER = logging.getLogger(__name__)


def positive_size(value: str) -> int:
    """Validate a maximum size before activating the transfer service."""
    try:
        size = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("maximum size must be a positive integer") from error
    if size <= 0:
        raise argparse.ArgumentTypeError("maximum size must be positive")
    return size


def _save(payload: bytes, output: Path, force: bool) -> None:
    """Publish a complete file; exclusive link prevents overwrite races."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            os.replace(temporary, output)
        else:
            os.link(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def download_command(args: argparse.Namespace) -> None:
    """Read one remote file, preserving its received bytes and any old output."""
    output = Path(args.output).expanduser()
    try:
        if os.path.lexists(output) and not args.force:
            raise FileExistsError(f"Output already exists: {output}; use --force to replace it")
        if not output.parent.is_dir():
            raise FileNotFoundError(f"Output directory does not exist: {output.parent}")
        if output.is_dir():
            raise IsADirectoryError(f"Output is a directory: {output}")
        payload = asyncio.run(
            GrentonManager().download_file(
                get_config_path(args.config), args.clu, args.remote,
                timeout=args.timeout, max_bytes=args.max_bytes,
            )
        )
        _save(payload, output, args.force)
    except (OSError, ValueError) as error:
        _LOGGER.error("Download failed: %s", error)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        _LOGGER.info("Download cancelled")
        raise SystemExit(130) from None
    _LOGGER.info("Downloaded %s: %d bytes saved to %s", args.remote, len(payload), output)
