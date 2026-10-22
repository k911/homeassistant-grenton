"""Dynamic thermostat settings and day-grouped weekly schedule selector."""

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.helpers import selector

from ..thermostat_schedule import DAYS, encode_temperature
from .configurable import (
    BaseGrentonEntityConfigurationSchema,
    StepDefinition,
    StepResult,
)


def schedule_sections(
    schedule: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Group days that share identical periods, without losing any changes."""
    sections: list[dict[str, Any]] = []
    for day in (*DAYS[1:], DAYS[0]):
        periods = schedule[day]
        existing = next((item for item in sections if item["periods"] == periods), None)
        if existing is not None:
            existing["days"].append(day)
        else:
            sections.append({"days": [day], "periods": deepcopy(periods)})
    return sections


def sections_schedule(sections: Any) -> dict[str, list[dict[str, Any]]]:
    """Require a complete, unambiguous week before any CLU write."""
    if not isinstance(sections, list) or not 1 <= len(sections) <= 7:
        raise vol.Invalid("Use one to seven schedule sections")
    schedule: dict[str, list[dict[str, Any]]] = {}
    for section in sections:
        if not isinstance(section, dict) or set(section) != {"days", "periods"}:
            raise vol.Invalid("Each section needs days and periods")
        days = section["days"]
        if not isinstance(days, list) or not days:
            raise vol.Invalid("Select at least one day per section")
        for day in days:
            if not isinstance(day, str) or day not in DAYS or day in schedule:
                raise vol.Invalid("Each weekday must belong to exactly one section")
            schedule[day] = deepcopy(section["periods"])
    if set(schedule) != set(DAYS):
        raise vol.Invalid("Include all seven weekdays")
    return schedule


@selector.SELECTORS.register("grenton_thermostat")
class GrentonThermostatSelector(selector.Selector):
    """Settings with repeatable sections and repeatable quarter-hour periods."""

    selector_type = "grenton_thermostat"
    CONFIG_SCHEMA = vol.Schema(
        {
            vol.Required("cooling_available"): bool,
            vol.Required("schedule_available"): bool,
            vol.Optional("minimum"): vol.Coerce(float),
            vol.Optional("maximum"): vol.Coerce(float),
            vol.Optional("direction"): vol.In(["heating", "cooling"]),
        }
    )

    def __call__(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise vol.Invalid("Thermostat settings must be an object")
        cooling = data.get("cooling_enabled")
        if not isinstance(cooling, bool) or (
            cooling and not self.config["cooling_available"]
        ):
            raise vol.Invalid("Cooling needs readable and writable direction bindings")
        edit = data.get("edit_schedule", False)
        if not isinstance(edit, bool):
            raise vol.Invalid("Schedule editing must be enabled or disabled")
        result: dict[str, Any] = {"cooling_enabled": cooling}
        direction = data.get("control_direction", "unchanged")
        if direction not in ("unchanged", "heating", "cooling"):
            raise vol.Invalid("Choose heating, cooling, or keep the current direction")
        if cooling and direction != "unchanged":
            result["control_direction"] = direction
        if edit:
            if not self.config["schedule_available"]:
                raise vol.Invalid(
                    "Wait for the current schedule and limits from the CLU"
                )
            result["schedule"] = sections_schedule(data.get("sections"))
            for periods in result["schedule"].values():
                if not isinstance(periods, list) or not 1 <= len(periods) <= 96:
                    raise vol.Invalid("Use one to 96 periods per section")
                previous = ""
                for index, period in enumerate(periods):
                    if not isinstance(period, dict) or set(period) != {
                        "time",
                        "temperature",
                    }:
                        raise vol.Invalid(
                            "Each period needs a start time and temperature"
                        )
                    time = period["time"]
                    if (
                        not isinstance(time, str)
                        or not re.fullmatch(r"(?:[01]\d|2[0-3]):(?:00|15|30|45)", time)
                        or (index == 0 and time != "00:00")
                        or time <= previous
                    ):
                        raise vol.Invalid(
                            "Start at 00:00 with increasing quarter-hour times"
                        )
                    try:
                        encode_temperature(
                            period["temperature"], self.config["maximum"]
                        )
                        if period["temperature"] < self.config["minimum"]:
                            raise ValueError(
                                "Temperature is below the configured minimum"
                            )
                    except (TypeError, ValueError) as err:
                        raise vol.Invalid(str(err)) from err
                    previous = time
        return result


@dataclass
class GrentonThermostatConfigurationSchema(BaseGrentonEntityConfigurationSchema):
    settings: dict[str, Any]
    sections: list[dict[str, Any]]

    @property
    def steps(self) -> list[StepDefinition]:
        return [StepDefinition("configure_thermostat", self._build)]

    def _build(
        self, current: dict[str, Any], accumulated: dict[str, Any]
    ) -> StepResult:
        draft = deepcopy(
            current.get(
                "thermostat",
                {
                    "cooling_enabled": current.get("cooling_enabled", False),
                    "edit_schedule": False,
                    "control_direction": "unchanged",
                    "sections": self.sections,
                },
            )
        )
        return StepResult(
            schema=vol.Schema(
                {
                    vol.Required(
                        "thermostat", description={"suggested_value": draft}
                    ): GrentonThermostatSelector(self.settings)
                }
            ),
            validator=lambda data: data["thermostat"],
        )
