from __future__ import annotations

from ...action import (
    GrentonAction,
    GrentonActionAttribute,
    GrentonActionMethod,
    GrentonActionScript,
    GrentonActionVariable,
)
from ...scene_arguments import quote_lua_string
from .base import GrentonCluApiRequest, GrentonCluApiResponse


class GrentonCluApiActionRequest(GrentonCluApiRequest):
    """Request to execute an action on the CLU.
    
    Supports variable, attribute, method, and script actions.
    """
    
    @staticmethod
    def from_action(
        action: GrentonAction,
        msg_id: str | None = None
    ) -> GrentonCluApiActionRequest:
        """Create request from a GrentonAction.
        
        Args:
            action: GrentonAction instance (Variable, Attribute, Method, or Script)
            msg_id: Optional message ID
            
        Returns:
            GrentonCluApiActionRequest with appropriate payload
        """
        if isinstance(action, GrentonActionVariable):
            payload = f'setVar({quote_lua_string(action.index)},{quote_lua_string(action.value)})'
        elif isinstance(action, GrentonActionAttribute):
            payload = f'{action.object_name}:set({action.index},{quote_lua_string(action.value)})'
        elif isinstance(action, GrentonActionMethod):
            if action.arguments is None:
                payload = f'{action.object_name}:execute({action.index},{quote_lua_string(action.value)})'
            else:
                parameters = ", ".join((action.index, *action.arguments))
                payload = f'{action.object_name}:execute({parameters})'
        elif isinstance(action, GrentonActionScript):
            payload = f'{action.object_name}({action.value})'
        else:
            raise ValueError(f"Unsupported action type: {type(action).__name__}")
        
        return GrentonCluApiActionRequest(payload, msg_id)


class GrentonCluApiActionResponse(GrentonCluApiResponse):
    """Response from action execution."""
    
    def __init__(self, wire_message: str):
        """Initialize from wire format message."""
        super().__init__(wire_message)
