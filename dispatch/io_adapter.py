"""
dispatch/io_adapter.py -- 파이프라인 I/O 변환 계층
===================================================
오케스트레이터 팀과의 인터페이스 경계(Contract).

팀 기준 JSON 포맷:
  - 입력: dispatch_input_timeline.json
    - 위치 참조: 키 문자열 (예: "seosan_bus_terminal")
    - event_time: "HH:MM" 포맷
    - vehicles_initial_state_at_09_00 배열
  - 출력: dispatch_output_answer.json
    - input_ref, session_id, reasoning 등 포함
"""

from __future__ import annotations

from typing import Any, Dict, List

from .models import (
    DispatchEvent,
    DispatchResult,
    EventType,
    Location,
    Vehicle,
    time_str_to_minutes,
)
from .locations import get_location, load_locations_from_json


class DispatchIOAdapter:
    """
    팀 기준 JSON <-> 내부 모델 변환기.

    Usage::

        adapter = DispatchIOAdapter()
        locations, vehicles, events = adapter.parse_input_file(json_data)
        # ... engine processes each event ...
        output = adapter.build_output(result)
    """

    # ------------------------------------------------------------------
    # Input: 팀 기준 JSON -> internal model
    # ------------------------------------------------------------------

    def parse_input_file(self, data: Dict[str, Any]):
        """
        dispatch_input_timeline.json 전체를 파싱.

        Returns
        -------
        (locations_dict, vehicles, events_raw)
        """
        # 1) locations 섹션 로드 -> DB 업데이트
        locations_dict = data.get("locations", {})
        if locations_dict:
            load_locations_from_json(locations_dict)

        # 2) 차량 파싱
        vehicles = self.parse_vehicles(data.get("vehicles_initial_state_at_09_00", []))

        # 3) 이벤트 리스트
        events_raw = data.get("event_timeline", [])

        return locations_dict, vehicles, events_raw

    def parse_event(self, raw: Dict[str, Any]) -> DispatchEvent:
        """
        event_timeline의 단일 이벤트를 DispatchEvent로 변환.

        팀 기준 포맷::

            {
                "seq": 1,
                "event_time": "09:30",
                "event_type": "new_request",
                "request_id": "req_001",
                "payload": {
                    "pickup": "seosan_bus_terminal",
                    "dropoff": "seosan_city_hall",
                    "requested_pickup_time": "10:00",
                    "passenger_count": 1
                }
            }
        """
        event_type = EventType(raw["event_type"])
        event_time = raw.get("event_time", "")
        event_time_minutes = time_str_to_minutes(event_time) if event_time else 0

        return DispatchEvent(
            seq=raw.get("seq", 0),
            event_type=event_type,
            event_time=event_time,
            event_time_minutes=event_time_minutes,
            user_id=raw.get("user_id", ""),
            request_id=raw.get("request_id"),
            target_reservation_id=raw.get("target_reservation_id"),
            payload=raw.get("payload", {}),
        )

    def parse_vehicles(
        self, raw_vehicles: List[Dict[str, Any]]
    ) -> List[Vehicle]:
        """
        vehicles_initial_state_at_09_00 배열을 Vehicle 리스트로 변환.

        팀 기준 포맷::

            {
                "vehicle_id": "DRT-SS-01",
                "current_location": "seosan_city_hall",
                "capacity_total": 4,
                "capacity_used": 0,
                "scheduled_reservations": []
            }
        """
        vehicles = []
        for v in raw_vehicles:
            # 위치 키 -> Location 객체
            loc_key = v.get("current_location", "")
            loc = get_location(loc_key) if loc_key else None
            if loc is None:
                raise ValueError(
                    f"unknown vehicle current_location: {loc_key or '<empty>'}"
                )

            vehicles.append(Vehicle(
                vehicle_id=v["vehicle_id"],
                capacity=v.get("capacity_total", 4),
                current_location=loc,
                current_load=v.get("capacity_used", 0),
            ))
        return vehicles

    def parse_payload_locations(self, payload: Dict[str, Any]):
        """
        payload에서 pickup/dropoff Location을 추출한다.
        팀 기준: 위치가 키 문자열 (예: "seosan_bus_terminal")

        Returns
        -------
        (pickup_loc, dropoff_loc, requested_pickup_time_str, passenger_count)
        """
        pickup_key = payload.get("pickup", "")
        dropoff_key = payload.get("dropoff", "")

        # 키 문자열 -> Location 객체
        if isinstance(pickup_key, str):
            pickup_loc = get_location(pickup_key)
            if pickup_loc is None:
                raise ValueError(f"unknown pickup location: {pickup_key or '<empty>'}")
        elif isinstance(pickup_key, dict):
            # 폴백: 기존 dict 형식도 지원
            pickup_loc = Location.from_dict(pickup_key)
        else:
            pickup_loc = None

        if isinstance(dropoff_key, str):
            dropoff_loc = get_location(dropoff_key)
            if dropoff_loc is None:
                raise ValueError(f"unknown dropoff location: {dropoff_key or '<empty>'}")
        elif isinstance(dropoff_key, dict):
            dropoff_loc = Location.from_dict(dropoff_key)
        else:
            dropoff_loc = None

        pickup_time = payload.get("requested_pickup_time", "")
        passenger_count = payload.get("passenger_count", 1)

        return pickup_loc, dropoff_loc, pickup_time, passenger_count

    # ------------------------------------------------------------------
    # Output: internal model -> 팀 기준 JSON
    # ------------------------------------------------------------------

    def build_output(self, result: DispatchResult) -> Dict[str, Any]:
        """
        DispatchResult를 팀 기준 dispatch_output_answer.json 포맷으로 변환.

        팀 기준 출력 포맷::

            {
                "seq": 1,
                "input_ref": "req_001",
                "session_id": "sess_001",
                "status": "success",
                "action": "new_reservation",
                "vehicle_id": "DRT-SS-01",
                "pickup_time": "10:00",
                "pickup_location": "서산버스터미널",
                "dropoff_location": "서산시청",
                "reasoning": ["..."]
            }
        """
        return result.to_dict()

    def build_outputs(self, results: List[DispatchResult]) -> Dict[str, Any]:
        """다수 결과를 팀 기준 포맷으로 일괄 변환."""
        return {
            "_meta": {
                "description": "배차 모듈 출력 — 배차 알고리즘이 생성한 결과",
                "input_source": "dispatch_input_timeline.json의 event_timeline과 1:1 대응",
            },
            "results": [self.build_output(r) for r in results],
        }
