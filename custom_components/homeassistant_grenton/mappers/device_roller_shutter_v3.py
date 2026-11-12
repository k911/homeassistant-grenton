"""Map the roller shutter's imported state and action bindings."""

from ..coordinator import GrentonCoordinator
from ..domain.devices.roller_shutter_v3 import GrentonDeviceRollerShutterV3
from ..domain.state_object import GrentonStateObject
from ..domain.action import GrentonAction
from ..domain.entities.roller_shutter_v3 import GrentonEntityRollerShutterV3
from ..dto.widgets.roller_shutter_v3 import GrentonWidgetRollerShutterV3Dto
from ..dto.value import GrentonValueAttributeDto


class DeviceRollerShutterV3Mapper:
    """Mapper for GrentonWidgetRollerShutterDto to GrentonDeviceRollerShutter."""

    @staticmethod
    def to_domain(dto: GrentonWidgetRollerShutterV3Dto, coordinator: GrentonCoordinator) -> GrentonDeviceRollerShutterV3:
        """Convert DTO to domain object."""
        device = GrentonDeviceRollerShutterV3(
            type=dto.type,
            id=dto.id,
            entities=[],
        )
        
        setting_bindings = {
            'lamel_move_timeout': ('lamelMoveTimeout', 'setLamelMoveTimeoutAction'),
            'reverse_position': ('reversePosition', None),
            'mechanical_offset': ('mechanicalOffset', 'setMechanicalOffsetAction'),
            'blinds_up_max_time': ('blindsUpMaxTime', 'setBlindsUpMaxTimeAction'),
            'blinds_down_max_time': ('blindsDownMaxTime', 'setBlindsDownMaxTimeAction'),
        }
        states, actions = {}, {}
        for key, (read_name, write_name) in setting_bindings.items():
            read = getattr(dto.object, read_name)
            write = getattr(dto.object, write_name) if write_name else None
            if read is not None:
                states[key] = GrentonStateObject.from_dto(read)
            if write is not None:
                actions[key] = GrentonAction.from_dto(write)
            elif key == 'reverse_position' and isinstance(read, GrentonValueAttributeDto):
                # ReversePosition is a writable numeric 0/1 attribute; OM exports no setter.
                from ..domain.action import GrentonActionAttribute
                from ..domain.enums import GrentonActionEventType
                actions[key] = GrentonActionAttribute(
                    read.cluId, read.objectName, GrentonActionEventType.CLICK, '', read.index
                )
        legacy = dto.object.maxTime
        if (legacy is None and isinstance(dto.object.position, GrentonValueAttributeDto)
                and dto.object.position.index == '7'
                and dto.object.position.objectName.startswith('ROL')):
            legacy = dto.object.position.model_copy(update={'index': '9'})
        entity = GrentonEntityRollerShutterV3(
            coordinator=coordinator,
            id=dto.id,
            label=dto.label,
            cover_state=GrentonStateObject.from_dto(dto.object.state),
            cover_position=GrentonStateObject.from_dto(dto.object.position),
            set_position=GrentonAction.from_dto(dto.object.setPositionAction),
            cover_tilt_position=GrentonStateObject.from_dto(dto.object.lamelPosition) if dto.object.lamelMoveTimeout else None,
            set_tilt_position=GrentonAction.from_dto(dto.object.setLamelPositionAction) if dto.object.lamelMoveTimeout else None,
            stop=GrentonAction.from_dto(dto.object.stopAction) if dto.object.stopAction else None,
            lamel_move_timeout=GrentonStateObject.from_dto(dto.object.lamelMoveTimeout) if dto.object.lamelMoveTimeout else None,
            reverse_position=GrentonStateObject.from_dto(dto.object.reversePosition) if dto.object.reversePosition else None,
            setting_states=states,
            setting_actions=actions,
            calibration_action=GrentonAction.from_dto(dto.object.calibrationAction) if dto.object.calibrationAction else None,
            up_state=GrentonStateObject.from_dto(dto.object.up) if dto.object.up else None,
            down_state=GrentonStateObject.from_dto(dto.object.down) if dto.object.down else None,
            legacy_max_time=GrentonStateObject.from_dto(legacy) if legacy else None,
            device_info=device.device_info,
        )

        device.entities = [entity]
        return device
