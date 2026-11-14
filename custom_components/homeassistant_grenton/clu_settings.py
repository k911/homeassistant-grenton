"""Per-CLU subscription identity and renewal cadence alongside polling settings."""

import math
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from .read_policy import CONF_CLU_READ_SETTINGS

CONF_SUBSCRIPTION_ID = "subscription_id"
DEFAULT_SUBSCRIPTION_ID = 1
MIN_SUBSCRIPTION_ID = 1
MAX_SUBSCRIPTION_ID = 65535
CONF_REGISTRATION_INTERVAL = "registration_interval"
DEFAULT_REGISTRATION_INTERVAL = 45
MIN_REGISTRATION_INTERVAL = 1
MAX_REGISTRATION_INTERVAL = 60


def validate_registration_interval(value: Any) -> int:
    """Validate the renewal cadence, independently of the response timeout."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not MIN_REGISTRATION_INTERVAL <= value <= MAX_REGISTRATION_INTERVAL
        or not math.isfinite(value)
        or value != int(value)
    ):
        raise vol.Invalid("Use a whole registration interval between 1 and 60 seconds")
    return int(value)


def registration_interval(options: Mapping[str, Any], clu_id: str) -> int:
    """Keep the existing 45-second cadence for missing or malformed settings."""
    settings = options.get(CONF_CLU_READ_SETTINGS, {})
    current = settings.get(clu_id, {}) if isinstance(settings, Mapping) else {}
    try:
        return validate_registration_interval(
            current.get(CONF_REGISTRATION_INTERVAL, DEFAULT_REGISTRATION_INTERVAL)
            if isinstance(current, Mapping) else DEFAULT_REGISTRATION_INTERVAL
        )
    except vol.Invalid:
        return DEFAULT_REGISTRATION_INTERVAL


def validate_subscription_id(value: Any) -> int:
    """Accept whole numbers from HA's number selector within a 16-bit range."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not MIN_SUBSCRIPTION_ID <= value <= MAX_SUBSCRIPTION_ID
        or not math.isfinite(value)
        or value != int(value)
    ):
        raise vol.Invalid("Use a whole subscription ID between 1 and 65535")
    return int(value)


def subscription_id(options: Mapping[str, Any], clu_id: str) -> int:
    """Use the CLU's configured ID, falling back safely for older entries."""
    settings = options.get(CONF_CLU_READ_SETTINGS, {})
    current = settings.get(clu_id, {}) if isinstance(settings, Mapping) else {}
    value = (
        current.get(CONF_SUBSCRIPTION_ID, DEFAULT_SUBSCRIPTION_ID)
        if isinstance(current, Mapping)
        else DEFAULT_SUBSCRIPTION_ID
    )
    try:
        return validate_subscription_id(value)
    except vol.Invalid:
        return DEFAULT_SUBSCRIPTION_ID
