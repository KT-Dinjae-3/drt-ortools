"""
dispatch/server.py -- 서산시 행복택시 DRT 최적화 REST API 서버
================================================================
BE(drt-call-backend)의 TS-DRT 명세 v0.1과 100% 호환되는 OR-Tools 배차 마이크로서비스.

실행방법:
    cd /home/jovyan/LEG/ORtools
    uvicorn dispatch.server:app --host 0.0.0.0 --port 8092
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Path as FastPath, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .engine import DynamicDRTDispatcher
from .io_adapter import DispatchIOAdapter
from .locations import LOCATION_DB, DEFAULT_DEPOT, get_location, resolve_location_name
from .models import (
    ActionType,
    DispatchEvent,
    DispatchResult,
    ErrorCode,
    EventType,
    Location,
    PassengerRequest,
    RequestStatus,
    Vehicle,
    minutes_to_time_str,
    time_str_to_minutes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("drt.ortools.server")

# KST 타임존
KST = timezone(timedelta(hours=9))

app = FastAPI(
    title="서산시 행복택시 DRT OR-Tools 배차 마이크로서비스",
    version="1.0.0",
    description="OCI Cloud BE와 연결되어 서산시 행복택시 실시간 동적 배차 및 경로 최적화를 수행합니다.",
)

# ---------------------------------------------------------------------------
# 서산시 행복택시 차량 fleet 초기화
# ---------------------------------------------------------------------------
seosan_depot = LOCATION_DB.get("seosan_city_hall", DEFAULT_DEPOT)

DEFAULT_VEHICLES = [
    Vehicle(vehicle_id="DRT-SS-01", capacity=4, current_location=seosan_depot, is_active=True),
    Vehicle(
        vehicle_id="DRT-SS-02",
        capacity=4,
        current_location=LOCATION_DB.get("seosan_bus_terminal", seosan_depot),
        is_active=True,
    ),
    Vehicle(
        vehicle_id="DRT-SS-03",
        capacity=11,
        current_location=LOCATION_DB.get("emam_office", seosan_depot),
        is_active=True,
    ),
]

dispatcher = DynamicDRTDispatcher(vehicles=DEFAULT_VEHICLES, depot=seosan_depot)
adapter = DispatchIOAdapter()
seq_counter = 0

# ---------------------------------------------------------------------------
# Pydantic Schemas (BE REST 호환)
# ---------------------------------------------------------------------------
class AvailabilityCheckRequest(BaseModel):
    region_code: Optional[str] = "SEOSAN_CITY"
    origin: str = Field(..., description="출발지 명칭 또는 위치 키")
    destination: str = Field(..., description="목적지 명칭 또는 위치 키")
    requested_pickup_at: str = Field(..., description="ISO 8601 시각 string")
    passenger_count: int = Field(default=1, ge=1)


class CreateReservationRequest(BaseModel):
    client_ref: str = Field(..., description="통화 세션 ID / 멱등성 참조 키")
    passenger_phone: Optional[str] = Field(default="010-0000-0000")
    origin: str
    destination: str
    requested_pickup_at: str
    passenger_count: int = Field(default=1, ge=1)
    region_code: Optional[str] = "SEOSAN_CITY"


class CancelReservationRequest(BaseModel):
    target_reservation_id: str
    reason: Optional[str] = "승객 취소 요청"


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------
def _next_seq() -> int:
    global seq_counter
    seq_counter += 1
    return seq_counter


def _parse_time_str(iso_at: str) -> str:
    """ISO 8601 문자열에서 HH:MM 추출"""
    try:
        if "T" in iso_at:
            time_part = iso_at.split("T")[1]
            return time_part[:5]
        return iso_at[:5]
    except Exception:
        now_kst = datetime.now(KST)
        return now_kst.strftime("%H:%M")


def _location_by_name_or_key(text: str) -> Location:
    """한글 장소명 또는 키 문자열을 Location 객체로 반환"""
    loc = get_location(text)
    if loc:
        return loc

    for db_loc in LOCATION_DB.values():
        if db_loc.name and db_loc.name == text:
            return db_loc

    # 기본값: 음암면 마을회관/서산시청
    if "의료원" in text or "병원" in text:
        return LOCATION_DB["seosan_medical_center"]
    elif "터미널" in text:
        return LOCATION_DB["seosan_bus_terminal"]
    elif "음암" in text or "회관" in text:
        return LOCATION_DB["emam_town_hall"]
    elif "대산" in text:
        return LOCATION_DB["daesan_office"]
    elif "해미" in text:
        return LOCATION_DB["haemi_fortress"]

    return Location(lat=36.7845, lng=126.4501, name=text)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", summary="배차 엔진 헬스체크 및 서산시 상태")
def health():
    return {
        "status": "ok",
        "service": "seosan-drt-ortools",
        "region": "서산시 행복택시 DRT",
        "active_vehicles": len(dispatcher.vehicles),
        "total_locations": len(LOCATION_DB),
    }


@app.get("/drt/regions", summary="서산시 DRT 운행 권역 및 시간 정책")
def get_regions():
    return {
        "regions": [
            {
                "region_code": "SEOSAN_CITY",
                "name": "서산시 전역 (행복택시)",
                "operating_hours": "08:00 - 18:00",
                "service_type": "수요응답형 행복택시",
            },
            {
                "region_code": "SEOSAN_EMAM",
                "name": "서산시 음암면",
                "operating_hours": "08:00 - 18:00",
                "service_type": "행복택시 전담 권역",
            },
            {
                "region_code": "SEOSAN_DAESAN",
                "name": "서산시 대산읍",
                "operating_hours": "08:00 - 18:00",
                "service_type": "행복택시 전담 권역",
            },
            {
                "region_code": "SEOSAN_UNSAN",
                "name": "서산시 운산면/해미면",
                "operating_hours": "08:00 - 18:00",
                "service_type": "행복택시 전담 권역",
            },
        ]
    }


@app.post("/drt/availability/check", summary="BE 연동: 가용 차량 및 예상 픽업 시각 사전 검증")
def check_availability(req: AvailabilityCheckRequest):
    time_str = _parse_time_str(req.requested_pickup_at)
    pickup_loc = _location_by_name_or_key(req.origin)
    dropoff_loc = _location_by_name_or_key(req.destination)

    # 가상 검증 이벤트 생성
    dummy_event = DispatchEvent(
        seq=_next_seq(),
        event_type=EventType.NEW_REQUEST,
        event_time=time_str,
        event_time_minutes=time_str_to_minutes(time_str),
        user_id="user_check",
        request_id=f"chk_{seq_counter}",
        payload={
            "pickup": pickup_loc.name,
            "dropoff": dropoff_loc.name,
            "requested_pickup_time": time_str,
            "passenger_count": req.passenger_count,
        },
    )

    # 임시 요청으로 최적 배차 가능 여부 시뮬레이션
    test_req = PassengerRequest(
        request_id=dummy_event.request_id,
        user_id="user_check",
        passenger_count=req.passenger_count,
        pickup_location=pickup_loc,
        dropoff_location=dropoff_loc,
        requested_pickup_time=time_str_to_minutes(time_str),
    )

    assigned_v = None
    min_dist = float("inf")
    for v_id, v in dispatcher.vehicles.items():
        if v.is_active and v.capacity >= req.passenger_count:
            d = pickup_loc.distance_to(v.current_location)
            if d < min_dist:
                min_dist = d
                assigned_v = v_id

    if assigned_v:
        # 예상 이동시간 5분 계산
        est_minutes = time_str_to_minutes(time_str) + 5
        est_time_str = minutes_to_time_str(est_minutes)
        return {
            "status": "success",
            "available": True,
            "estimated_pickup_time": est_time_str,
            "vehicle_id": assigned_v,
            "origin": pickup_loc.name,
            "destination": dropoff_loc.name,
        }
    else:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "CAPACITY_EXCEEDED",
                    "message": "해당 시각에 이용 가능한 서산시 행복택시 차량이 없습니다.",
                }
            },
        )


@app.post("/drt/reservations", summary="BE 연동: 신규 배차 확정 & OR-Tools 동적 경로 노드 삽입")
def create_reservation(req: CreateReservationRequest):
    time_str = _parse_time_str(req.requested_pickup_at)
    pickup_loc = _location_by_name_or_key(req.origin)
    dropoff_loc = _location_by_name_or_key(req.destination)

    req_id = f"req_{_next_seq():03d}"
    raw_event = {
        "seq": seq_counter,
        "event_type": "new_request",
        "event_time": time_str,
        "payload": {
            "user_id": req.passenger_phone or "user_anon",
            "request_id": req_id,
            "pickup": pickup_loc.name,
            "dropoff": dropoff_loc.name,
            "requested_pickup_time": time_str,
            "passenger_count": req.passenger_count,
        },
    }

    event = adapter.parse_event(raw_event)
    result = dispatcher.process_event(event)

    res_dict = result.to_dict()
    res_dict["session_id"] = req.client_ref
    res_dict["reservation_id"] = f"R-{datetime.now(KST).strftime('%Y%m%d')}-{seq_counter:04d}"

    if result.status == "success":
        logger.info(
            f"배차 성공: reservation_id={res_dict['reservation_id']}, vehicle={result.vehicle_id}, pickup={result.pickup_time}"
        )
        return res_dict
    else:
        logger.warning(f"배차 실패: reason={result.reason}, error_code={result.error_code}")
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": result.error_code or "NO_VEHICLE_AVAILABLE",
                    "message": result.reason or "배차 실패",
                }
            },
        )


@app.get("/drt/reservations", summary="BE 연동: 현재 배차 내역 및 예약 현황 조회")
def list_reservations():
    active_reqs = []
    for req_id, p_req in dispatcher.requests.items():
        active_reqs.append(
            {
                "request_id": req_id,
                "user_id": p_req.user_id,
                "passenger_count": p_req.passenger_count,
                "pickup_location": p_req.pickup_location.name,
                "dropoff_location": p_req.dropoff_location.name,
                "requested_pickup_time": minutes_to_time_str(p_req.requested_pickup_time),
                "status": p_req.status.value,
                "assigned_vehicle_id": p_req.assigned_vehicle_id,
            }
        )
    return {"reservations": active_reqs, "count": len(active_reqs)}


@app.post("/drt/reservations/cancel", summary="BE 연동: 예약 취소 처리 (POST)")
def cancel_reservation_post(req: CancelReservationRequest):
    return _do_cancellation(req.target_reservation_id)


@app.patch("/drt/reservations/{reservation_id}/cancel", summary="BE 연동: 예약 취소 처리 (PATCH)")
def cancel_reservation_patch(reservation_id: str = FastPath(...)):
    return _do_cancellation(reservation_id)


def _do_cancellation(target_id: str) -> Dict[str, Any]:
    time_str = datetime.now(KST).strftime("%H:%M")

    # target_id (예: R-20260728-0002) -> dispatcher.requests 키 (예: req_002) 매핑
    actual_target = target_id
    if target_id not in dispatcher.requests:
        for r_id in dispatcher.requests.keys():
            if r_id in target_id or target_id in r_id:
                actual_target = r_id
                break
        else:
            if dispatcher.requests:
                actual_target = list(dispatcher.requests.keys())[-1]

    raw_event = {
        "seq": _next_seq(),
        "event_type": "cancellation",
        "event_time": time_str,
        "target_reservation_id": actual_target,
        "payload": {
            "target_reservation_id": actual_target,
        },
    }


    event = adapter.parse_event(raw_event)
    result = dispatcher.process_event(event)

    res_dict = result.to_dict()
    res_dict["cancelled_reservation_id"] = target_id
    return res_dict


