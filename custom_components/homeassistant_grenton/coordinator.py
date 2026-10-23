from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .domain.action import GrentonAction
from .domain.api.clu import GrentonCluApi
from .domain.api.clu_messages import GrentonCluApiActionRequest
from .domain.clu import GrentonClu
from .domain.encryption import GrentonEncryption
from .domain.state_object import GrentonStateObject, GrentonVariableValueObject
from .state import (
    MISSING_STATE,
    GrentonCluState,
    GrentonCluStateAttributeKey,
    GrentonCluStateVariableKey,
    GrentonRegistrationValue,
    GrentonState,
    GrentonValue,
    MissingState,
)

StateKey = GrentonCluStateVariableKey | GrentonCluStateAttributeKey

_LOGGER = logging.getLogger(__name__)

class GrentonCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, clus: list[GrentonClu], encryption: GrentonEncryption):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
        )
        
        self.config_entry = config_entry
        self.clus = clus
        self.encryption = encryption
        self.state = GrentonState(clus={clu.id: GrentonCluState() for clu in clus})
        
        # Map CLU IDs to their API instances
        self._apis: dict[str, GrentonCluApi] = {}
        self._read_locks = {clu.id: asyncio.Lock() for clu in clus}
        self._report_versions: dict[str, dict[StateKey, int]] = {clu.id: {} for clu in clus}
        self._shutdown_lock = asyncio.Lock()
        self._stopping = False
        self._stop_listener: Callable[[], None] | None = None
        
        # Initialize API instances for each CLU
        for clu in clus:
            self._apis[clu.id] = GrentonCluApi(clu, encryption)

    async def _async_update_data(self): # type: ignore
        return self.state
    
    async def _send_ping(self, clu_id: str) -> None:
        if self._stopping:
            return
        api = self._apis.get(clu_id)
        if not api:
            _LOGGER.warning("[%s] No API found for CLU during ping", clu_id)
            return
        
        try:
            success = await api.ping()
            if not success:
                _LOGGER.warning("[%s] Ping failed", clu_id)
        except Exception as e:
            _LOGGER.error("[%s] Error during ping: %s", clu_id, e)
    
    async def _ping_loop(self) -> None:
        while True:
            try:
                # Ping every 5 seconds
                ping_interval = 5
                await asyncio.sleep(ping_interval)
                
                tasks = [self._send_ping(clu_id) for clu_id in self._apis.keys()]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                _LOGGER.debug("Ping loop cancelled")
                break
            except Exception as e:
                _LOGGER.error("Unexpected error in ping loop: %s", e)
    
    @staticmethod
    def _state_key(state: GrentonStateObject) -> GrentonCluStateVariableKey | GrentonCluStateAttributeKey:
        if isinstance(state, GrentonVariableValueObject):
            return GrentonCluStateVariableKey(state.index)
        return GrentonCluStateAttributeKey(state.object_name, state.index)

    async def _send_register(
        self, clu_id: str, required_keys: set[StateKey] | None = None
    ) -> set[StateKey]:
        async with self._read_locks[clu_id]:
            if self._stopping:
                return set()
            return await self._read_registered_states(clu_id, required_keys)

    async def _read_registered_states(
        self, clu_id: str, required_keys: set[StateKey] | None = None
    ) -> set[StateKey]:
        api = self._apis.get(clu_id)
        if not api:
            _LOGGER.warning(
                "[%s] No API found for CLU during state registration", clu_id
            )
            return set()

        clu_state = self.state.clus[clu_id]

        # Check if there are any states to register
        if not clu_state.has_states_to_register():
            _LOGGER.debug("[%s] No component states to register", clu_id)
            return set()

        try:
            keys = clu_state.get_subscription_order()
            versions = self._report_versions[clu_id].copy()
            values = (
                await api.register_component_states(keys)
                if required_keys is None
                else await api.register_component_states(
                    keys, required_keys=required_keys
                )
            )
            if values is None or len(values) != len(keys):
                _LOGGER.warning("[%s] Incomplete state registration response", clu_id)
                return set()
            # A pushed change received during the read is newer than its snapshot.
            current = self._report_versions[clu_id]
            merged: list[GrentonRegistrationValue] = [
                value if current.get(key, 0) == versions.get(key, 0) else MISSING_STATE
                for key, value in zip(keys, values)
            ]
            clu_state.update_keys(keys, merged)
            self.async_set_updated_data(self.state.__dict__)
            return {
                key
                for key, value in zip(keys, values)
                if not isinstance(value, MissingState)
                or current.get(key, 0) != versions.get(key, 0)
            }
        except Exception as e:
            _LOGGER.error("[%s] Error during registration: %s", clu_id, e)
            return set()

    async def _register_loop(self) -> None:
        while True:
            try:
                # Only register if there are actual states to register
                clu_ids_to_register = [
                    clu_id for clu_id, clu_state in self.state.clus.items()
                    if clu_state.has_states_to_register()
                ]
                
                if not clu_ids_to_register:
                    # No states to register, wait longer before checking again
                    await asyncio.sleep(45)
                    continue
                
                # Registration interval
                registration_interval = 45
                await asyncio.sleep(registration_interval)
                
                tasks = [self._send_register(clu_id) for clu_id in clu_ids_to_register]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                _LOGGER.debug("Register loop cancelled")
                break
            except Exception as e:
                _LOGGER.error("Unexpected error in register loop: %s", e)
    
    async def execute_action(
        self, action: GrentonAction, *, raise_on_error: bool = False
    ) -> None:
        if self._stopping:
            if raise_on_error:
                raise HomeAssistantError("Grenton is shutting down")
            return
        api = self._apis.get(action.clu_id)
        if not api:
            if raise_on_error:
                raise HomeAssistantError(f"CLU {action.clu_id} is not loaded")
            _LOGGER.warning("[%s] No API found for CLU during action execution", action.clu_id)
            return
        
        try:
            success = await api.execute_action(action)
            if not success:
                if raise_on_error:
                    raise HomeAssistantError(f"CLU {action.clu_id} rejected the action")
                _LOGGER.warning("[%s] Action execution failed for payload: %s", action.clu_id, GrentonCluApiActionRequest.from_action(action).payload)
        except Exception as e:
            if raise_on_error:
                raise HomeAssistantError(
                    f"Could not execute action on CLU {action.clu_id}: {e}"
                ) from e
            _LOGGER.error("[%s] Error executing action: %s", action.clu_id, e)
    
    async def _process_report(
        self,
        clu_id: str,
        keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey],
        values: list[GrentonValue],
    ) -> None:
        """Process a report from a CLU and update state for the given keys."""
        if self._stopping:
            return
        if len(keys) != len(values):
            _LOGGER.warning("[%s] Ignoring incomplete state report", clu_id)
            return
        clu_state = self.state.clus[clu_id]
        for key in keys:
            versions = self._report_versions[clu_id]
            versions[key] = versions.get(key, 0) + 1
        clu_state.update_keys(keys, values)

        self.async_set_updated_data(self.state.__dict__)
        _LOGGER.debug("[%s] Processed report with %d values", clu_id, len(values))
    
    def register_component_state(self, state: GrentonStateObject, *, large_value: bool = False) -> None:
        self.state.register_state(state)
        if large_value and (api := self._apis.get(state.clu_id)) is not None:
            api.large_value_keys.add(self._state_key(state))

    async def async_refresh_clu_state(
        self, clu_id: str, *, required_states: list[GrentonStateObject] | None = None
    ) -> bool:
        """Read back tracked values; report whether the required reads succeeded."""
        required = (
            {self._state_key(state) for state in required_states}
            if required_states is not None
            else set(self.state.clus[clu_id].get_subscription_order())
        )
        received = await self._send_register(
            clu_id, required if required_states is not None else None
        )
        return required <= received

    async def async_setup(self) -> None:
        self._stop_listener = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_handle_stop
        )
        # Connect all APIs
        connection_tasks: list[Any] = []
        for clu in self.clus:
            api = self._apis[clu.id]
            connection_tasks.append(self._connect_api(api))

        # Wait for all connections to complete
        await asyncio.gather(*connection_tasks, return_exceptions=True)

        # Send initial pings to establish sessions
        ping_tasks = [self._send_ping(clu_id) for clu_id in self._apis.keys()]
        if ping_tasks:
            await asyncio.gather(*ping_tasks, return_exceptions=True)

        # Send initial registrations
        register_tasks = [
            self._send_register(clu_id) for clu_id in self.state.clus.keys()
        ]
        if register_tasks:
            results = await asyncio.gather(*register_tasks, return_exceptions=True)
            # Retry incomplete initial pulls once before publishing entities.
            retries = [
                self._send_register(
                    clu_id,
                    set(self.state.clus[clu_id].get_subscription_order())
                    - (result if isinstance(result, set) else set()),
                )
                for clu_id, result in zip(self.state.clus, results)
                if not isinstance(result, set)
                or not set(self.state.clus[clu_id].get_subscription_order()) <= result
            ]
            if retries:
                await asyncio.gather(*retries, return_exceptions=True)

        # Start background tasks
        if self._stopping:
            raise HomeAssistantError("Grenton setup was interrupted by shutdown")
        self._ping_task = asyncio.create_task(self._ping_loop())
        self._register_task = asyncio.create_task(self._register_loop())

    async def _connect_api(self, api: GrentonCluApi) -> None:
        """Connect a single API instance."""
        try:
            success = await api.connect()
            if not success:
                _LOGGER.error("Failed to connect API for CLU %s", api.clu.id)
                return
            
            # Each chunk's subscription socket reports through this single hook;
            # the API layer fans out per-chunk callbacks internally.
            async def handle_subscription(
                keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey],
                values: list[GrentonValue],
            ) -> None:
                await self._process_report(api.clu.id, keys, values)
            api.on_subscription_report = handle_subscription
        except Exception as e:
            _LOGGER.error("Error connecting API for CLU %s: %s", api.clu.id, e)
    
    async def _async_handle_stop(self, event: Event) -> None:
        # async_listen_once already removed this listener before dispatch.
        self._stop_listener = None
        await self.async_shutdown()

    async def async_shutdown(self) -> None:
        """Stop traffic and close local sockets on unload, HA stop or failed setup."""
        async with self._shutdown_lock:
            self._stopping = True
            if self._stop_listener is not None:
                self._stop_listener()
                self._stop_listener = None
            tasks = [
                task
                for name in ("_ping_task", "_register_task")
                if (task := getattr(self, name, None)) is not None
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # UDP close releases local ports; it is not a remote unsubscribe.
            await asyncio.gather(
                *(self._disconnect_api(api) for api in self._apis.values()),
                return_exceptions=True,
            )
            self._apis.clear()

    async def _disconnect_api(self, api: GrentonCluApi) -> None:
        """Disconnect a single API instance."""
        try:
            await api.disconnect()
        except Exception as e:
            _LOGGER.error("Error disconnecting API for CLU %s: %s", api.clu.id, e)
    
    def get_value_for_component(self, state: GrentonStateObject) -> GrentonValue | None:
        """Get the value for a component from the state."""
        return self.state.get_value_for_component(state)
