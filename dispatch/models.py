"""
dispatch/models.py -- 배차 최적화 데이터 모델
=============================================
Location, PassengerRequest, Vehicle, DispatchResult 등
모든 데이터 구조체를 정의합니다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EventType(Enum):
    NEW_REQUEST = "new_request"
    CANCELLATION = "cancellation"
    CHANGE_REQUEST = "change_request"


class RequestStatus(Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_TRANSIT = "in_transit"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ActionType(Enum):
    """dispatch_output_answer.json의 action 필드."""
    NEW_RESERVATION = "new_reservation"
    CANCELLATION = "cancellation"
    CHANGE = "change"


class ErrorCode(Enum):
    """배차 실패 에러 코드."""
    PAST_TIME = "PAST_TIME"                    # 과거 시간 요청
    NO_VEHICLE_AVAILABLE = "NO_VEHICLE_AVAILABLE"  # 차량 없음
    CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"    # 용량 초과
    DELAY_VIOLATION = "DELAY_VIOLATION"         # 기존 승객 지연 위반
    REQUEST_NOT_FOUND = "REQUEST_NOT_FOUND"    # 요청 미발견
    ALREADY_CANCELLED = "ALREADY_CANCELLED"    # 이미 취소됨


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

@dataclass
class Location:
    """위치 정보 (위경도 + 한글 명칭)."""
    lat: float
    lng: float
    name: str = ""

    def distance_to(self, other: "Location") -> float:
        """Haversine 거리 (km)."""
        R = 6371.0  # 지구 반지름 (km)
        dlat = math.radians(other.lat - self.lat)
        dlng = math.radians(other.lng - self.lng)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(math.radians(self.lat))
            * math.cos(math.radians(other.lat))
            * math.sin(dlng / 2) ** 2
        )
        c = 2 * math.asin(math.sqrt(a))
        return R * c

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "lat": self.lat, "lng": self.lng}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Location":
        return cls(
            lat=d.get("lat", 0.0),
            lng=d.get("lng", 0.0),
            name=d.get("name", ""),
        )


# ---------------------------------------------------------------------------
# Passenger Request
# ---------------------------------------------------------------------------

@dataclass
class PassengerRequest:
    """승객 1건의 승/하차 요청."""
    request_id: str
    user_id: str
    passenger_count: int
    pickup_location: Location
    dropoff_location: Location
    requested_pickup_time: int                   # 분(minutes) -- 자정 기준
    status: RequestStatus = RequestStatus.PENDING
    assigned_vehicle_id: Optional[str] = None
    promised_pickup_time: Optional[int] = None   # 최초 안내 픽업 시간
    promised_dropoff_time: Optional[int] = None  # 최초 안내 하차 시간
    ride_share_with: List[str] = field(default_factory=list)
    is_insertion: bool = False                   # 기존 경로에 동적 삽입 여부


# ---------------------------------------------------------------------------
# Vehicle
# ---------------------------------------------------------------------------

@dataclass
class Vehicle:
    """차량 상태."""
    vehicle_id: str
    capacity: int = 4
    current_location: Location = field(
        default_factory=lambda: Location(0.0, 0.0, "depot")
    )
    current_load: int = 0
    is_active: bool = False


# ---------------------------------------------------------------------------
# Dispatch Event (파이프라인 입력 이벤트)
# ---------------------------------------------------------------------------

@dataclass
class DispatchEvent:
    """dispatch_input_timeline.json 에서 파싱된 단일 이벤트."""
    seq: int
    event_type: EventType
    event_time: str                              # ISO 8601
    event_time_minutes: int                      # 자정 기준 분
    user_id: str
    request_id: Optional[str] = None             # 신규 요청 시
    target_reservation_id: Optional[str] = None  # 취소/변경 시
    payload: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Dispatch Result (배차 결과)
# ---------------------------------------------------------------------------

@dataclass
class DispatchResult:
    """배차 엔진의 단일 이벤트 처리 결과."""
    seq: int
    status: str                                  # "success", "failed"
    action: str                                  # ActionType value
    request_id: str
    vehicle_id: Optional[str] = None
    pickup_time: Optional[str] = None            # "HH:MM"
    pickup_time_minutes: Optional[int] = None
    dropoff_time: Optional[str] = None
    dropoff_time_minutes: Optional[int] = None
    pickup_location: Optional[str] = None        # 한글 명칭
    dropoff_location: Optional[str] = None       # 한글 명칭
    ride_share_with: Optional[List[str]] = None
    insertion: bool = False
    is_imminent: bool = False                    # 임박 취소 여부
    error_code: Optional[str] = None
    reason: Optional[str] = None
    alternatives: Optional[List[str]] = None
    # ── 팀 기준 추가 필드 ──
    input_ref: Optional[str] = None              # "req_001" 또는 "req_002 취소"
    session_id: Optional[str] = None             # "sess_001"
    cancelled_reservation_id: Optional[str] = None  # 취소된 예약 ID
    vehicle_freed: Optional[str] = None          # 취소로 빈 차량 ID
    new_pickup_time: Optional[str] = None        # 변경 시 새 픽업 시각
    target_reservation_id: Optional[str] = None  # 변경/취소 대상 ID
    reasoning: Optional[List[str]] = None        # CoT 배열
    # 내부용 — 전체 경로 정보
    routes: Optional[List[Dict]] = None
    objective_value: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """팀 기준 dispatch_output_answer.json 포맷 딕셔너리로 변환."""
        d: Dict[str, Any] = {
            "seq": self.seq,
            "input_ref": self.input_ref or self.request_id,
            "session_id": self.session_id or f"sess_{self.seq:03d}",
            "status": self.status,
            "action": self.action,
        }

        # 성공 시 차량/시간/위치 정보
        if self.vehicle_id:
            d["vehicle_id"] = self.vehicle_id
        if self.pickup_time and self.action != "change":
            d["pickup_time"] = self.pickup_time
        if self.pickup_location:
            d["pickup_location"] = self.pickup_location
        if self.dropoff_location:
            d["dropoff_location"] = self.dropoff_location

        # 합승 정보
        if self.ride_share_with:
            # 팀 기준: 단일 값 문자열 (첫 번째 합승 대상)
            if isinstance(self.ride_share_with, list) and len(self.ride_share_with) == 1:
                d["ride_share_with"] = self.ride_share_with[0]
            else:
                d["ride_share_with"] = self.ride_share_with

        # 동적 삽입 플래그
        if self.insertion:
            d["insertion"] = True

        # 변경 전용 필드
        if self.action == "change":
            if self.target_reservation_id:
                d["target_reservation_id"] = self.target_reservation_id
            if self.new_pickup_time or self.pickup_time:
                d["new_pickup_time"] = self.new_pickup_time or self.pickup_time

        # 취소 전용 필드
        if self.action == "cancellation":
            if self.cancelled_reservation_id:
                d["cancelled_reservation_id"] = self.cancelled_reservation_id
            if self.vehicle_freed:
                d["vehicle_freed"] = self.vehicle_freed
            d["is_imminent"] = self.is_imminent

        # 실패 시 에러 정보
        if self.error_code:
            d["error_code"] = self.error_code
        if self.reason:
            d["reason"] = self.reason
        if self.alternatives:
            d["alternatives"] = self.alternatives

        # 판단 근거 (CoT)
        if self.reasoning:
            d["reasoning"] = self.reasoning

        return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def time_str_to_minutes(t: str) -> int:
    """'HH:MM' -> 자정 기준 분."""
    h, m = map(int, t.split(":"))
    return h * 60 + m


def minutes_to_time_str(m: int) -> str:
    """분 -> 'HH:MM'."""
    m = max(0, m)
    return f"{m // 60:02d}:{m % 60:02d}"
