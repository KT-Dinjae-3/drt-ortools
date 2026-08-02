"""
dispatch -- DRT 배차 최적화 패키지
===================================
Public API만 export합니다.

Usage::

    from dispatch import DynamicDRTDispatcher, DispatchIOAdapter
    from dispatch.models import Vehicle, Location
    from dispatch.config import DispatchConfig
"""

from .engine import DynamicDRTDispatcher
from .io_adapter import DispatchIOAdapter
from .models import (
    Vehicle,
    Location,
    DispatchEvent,
    DispatchResult,
    PassengerRequest,
    EventType,
    ActionType,
    ErrorCode,
    RequestStatus,
    time_str_to_minutes,
    minutes_to_time_str,
)
from .config import DispatchConfig, DEFAULT_CONFIG
from .locations import (
    LOCATION_DB, DEFAULT_DEPOT, resolve_location_name, get_location,
    resolve_location_key, load_locations_from_json,
)
from .validators import validate_event, check_imminent_cancellation

__all__ = [
    # Core
    "DynamicDRTDispatcher",
    "DispatchIOAdapter",
    # Models
    "Vehicle",
    "Location",
    "DispatchEvent",
    "DispatchResult",
    "PassengerRequest",
    "EventType",
    "ActionType",
    "ErrorCode",
    "RequestStatus",
    # Config
    "DispatchConfig",
    "DEFAULT_CONFIG",
    # Locations
    "LOCATION_DB",
    "DEFAULT_DEPOT",
    "resolve_location_name",
    "get_location",
    "resolve_location_key",
    "load_locations_from_json",
    # Validators
    "validate_event",
    "check_imminent_cancellation",
    # Helpers
    "time_str_to_minutes",
    "minutes_to_time_str",
]
