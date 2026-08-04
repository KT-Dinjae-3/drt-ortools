"""
dispatch/server.py -- 서산시 행복버스 DRT 최적화 REST API 서버
================================================================
drt-call-backend의 internal/ortoolsclient provider 계약과 호환되는
OR-Tools 배차 마이크로서비스.

실행방법:
    cd /home/jovyan/LEG/ORtools
    uvicorn dispatch.server:app --host 0.0.0.0 --port 8092
"""

from __future__ import annotations

import copy
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Path as FastPath, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .engine import DynamicDRTDispatcher
from .io_adapter import DispatchIOAdapter
from .locations import DEFAULT_DEPOT, LOCATION_DB, get_location
from .stops import (
    REGION_NAMES,
    STOP_CATALOG,
    StopRecord,
    normalize_region_code,
)
from .models import (
    Location,
    Vehicle,
    minutes_to_time_str,
    time_str_to_minutes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("drt.ortools.server")

# KST 타임존
KST = timezone(timedelta(hours=9))

app = FastAPI(
    title="서산시 행복버스 DRT OR-Tools 배차 마이크로서비스",
    version="1.1.0",
    description="OCI Cloud BE와 연결되어 서산시 행복버스 실시간 동적 배차 및 경로 최적화를 수행합니다.",
)

# ---------------------------------------------------------------------------
# 서산시 행복버스 차량 fleet 초기화
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
state_lock = threading.RLock()

# ---------------------------------------------------------------------------
# Pydantic Schemas (BE REST 호환)
# ---------------------------------------------------------------------------
class AvailabilityCheckRequest(BaseModel):
    region_code: Optional[str] = "SEOSAN_CITY"
    origin: Optional[str] = Field(default=None, description="출발지 명칭 또는 기존 위치 키")
    destination: Optional[str] = Field(default=None, description="목적지 명칭 또는 기존 위치 키")
    origin_stop_id: Optional[str] = Field(default=None, description="출발 정류장 통합ID")
    destination_stop_id: Optional[str] = Field(default=None, description="도착 정류장 통합ID")
    requested_pickup_at: str = Field(..., description="ISO 8601 시각 string")
    passenger_count: int = Field(default=1, ge=1)


class CreateReservationRequest(BaseModel):
    client_ref: str = Field(..., description="통화 세션 ID / 멱등성 참조 키")
    passenger_phone: Optional[str] = Field(default="010-0000-0000")
    origin: Optional[str] = None
    destination: Optional[str] = None
    origin_stop_id: Optional[str] = None
    destination_stop_id: Optional[str] = None
    requested_pickup_at: str
    passenger_count: int = Field(default=1, ge=1)
    region_code: Optional[str] = "SEOSAN_CITY"


class CancelReservationRequest(BaseModel):
    target_reservation_id: str
    reason: Optional[str] = "승객 취소 요청"


class StopResolveRequest(BaseModel):
    query: str = Field(..., min_length=1, description="통합ID, 원본명, 대표명 또는 별칭")
    region_code: Optional[str] = Field(default=None, description="권역 필터")


class APIInputError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        candidates: Optional[List[Dict[str, Any]]] = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.candidates = candidates or []


@app.exception_handler(APIInputError)
async def api_input_error_handler(
    _request: Request, exc: APIInputError
) -> JSONResponse:
    error: Dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.candidates:
        error["candidates"] = exc.candidates
    return JSONResponse(status_code=exc.status_code, content={"error": error})


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------
def _next_seq() -> int:
    global seq_counter
    seq_counter += 1
    return seq_counter


def _process_new_dispatch(
    time_str: str,
    passenger_phone: str,
    pickup_loc: Location,
    dropoff_loc: Location,
    passenger_count: int,
):
    """새 요청을 복제 상태에서 풀고 성공한 경우에만 전역 상태에 반영합니다."""
    global dispatcher

    with state_lock:
        sequence = _next_seq()
        req_id = f"req_{sequence:03d}"
        raw_event = {
            "seq": sequence,
            "event_type": "new_request",
            "event_time": time_str,
            "user_id": passenger_phone or "user_anon",
            "request_id": req_id,
            "payload": {
                "pickup": pickup_loc.to_dict(),
                "dropoff": dropoff_loc.to_dict(),
                "requested_pickup_time": time_str,
                "passenger_count": passenger_count,
            },
        }
        candidate_dispatcher = copy.deepcopy(dispatcher)
        event = adapter.parse_event(raw_event)
        result = candidate_dispatcher.process_event(event)
        if result.status == "success":
            dispatcher = candidate_dispatcher
        return sequence, req_id, result


def _process_cancellation(target_id: str, time_str: str):
    """정확한 OR input_ref만 취소하고 성공한 변경만 커밋합니다."""
    global dispatcher

    with state_lock:
        sequence = _next_seq()
        raw_event = {
            "seq": sequence,
            "event_type": "cancellation",
            "event_time": time_str,
            "target_reservation_id": target_id,
            "payload": {
                "target_reservation_id": target_id,
            },
        }
        candidate_dispatcher = copy.deepcopy(dispatcher)
        event = adapter.parse_event(raw_event)
        result = candidate_dispatcher.process_event(event)
        if result.status == "success":
            dispatcher = candidate_dispatcher
        return result


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


def _candidate_summary(record: StopRecord) -> Dict[str, Any]:
    return {
        "stop_id": record.stop_id,
        "region_code": record.region_code,
        "region_name": record.region_name,
        "source_name": record.source_name,
        "display_name": record.display_name,
        "lat": record.lat,
        "lng": record.lng,
    }


def _validated_region(region_code: Optional[str]) -> Optional[str]:
    normalized = normalize_region_code(region_code)
    if region_code and normalized is None:
        raise APIInputError(
            400,
            "INVALID_REGION",
            f"지원하지 않는 권역입니다: {region_code}",
        )
    return normalized


def _location_by_name_or_key(
    text: str, region_code: Optional[str] = None
) -> Location:
    """통합ID/정확한 장소명을 Location으로 변환하며 추측 매핑은 하지 않습니다."""
    query = (text or "").strip()
    if not query:
        raise APIInputError(422, "MISSING_STOP", "정류장 정보가 필요합니다.")

    normalized_region = _validated_region(region_code)
    resolution = STOP_CATALOG.resolve(query, normalized_region)
    candidates = [_candidate_summary(record) for record in resolution.candidates]

    if resolution.status == "exact" and resolution.record:
        if not resolution.record.routable:
            raise APIInputError(
                422,
                "STOP_COORDINATES_MISSING",
                f"{resolution.record.stop_id} 정류장의 좌표가 없습니다.",
                candidates,
            )
        return resolution.record.to_location()
    if resolution.status == "ambiguous":
        raise APIInputError(
            409,
            "AMBIGUOUS_STOP",
            f"'{query}'에 해당하는 정류장이 여러 개입니다. 통합ID를 선택해주세요.",
            candidates,
        )
    if resolution.status == "region_mismatch":
        raise APIInputError(
            400,
            "REGION_MISMATCH",
            f"'{query}' 정류장은 요청한 권역에 속하지 않습니다.",
            candidates,
        )

    # 기존 데모/BE 위치 키는 정확히 일치하는 경우에만 하위 호환합니다.
    legacy_location = get_location(query)
    if legacy_location:
        return legacy_location
    raise APIInputError(
        404,
        "UNKNOWN_STOP",
        f"등록되지 않은 정류장 또는 위치입니다: {query}",
    )


def _resolve_request_location(
    name_or_key: Optional[str],
    stop_id: Optional[str],
    region_code: Optional[str],
    field_name: str,
) -> Location:
    reference = (stop_id or name_or_key or "").strip()
    if not reference:
        raise APIInputError(
            422,
            "MISSING_STOP",
            f"{field_name} 정류장 정보가 필요합니다.",
        )
    location = _location_by_name_or_key(reference, region_code)

    # 전환 기간에는 이름과 ID를 함께 보낼 수 있습니다. 둘이 다른 물리
    # 정류장을 가리킬 때만 명시적으로 거절하고 ID를 authoritative하게 씁니다.
    if stop_id and name_or_key and location.location_id:
        name_resolution = STOP_CATALOG.resolve(name_or_key, region_code)
        candidate_ids = {record.stop_id for record in name_resolution.candidates}
        if candidate_ids and location.location_id not in candidate_ids:
            raise APIInputError(
                422,
                "CONFLICTING_STOP_REFERENCE",
                f"{field_name} 이름과 통합ID가 서로 다른 정류장을 가리킵니다.",
                [_candidate_summary(record) for record in name_resolution.candidates],
            )
    return location


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", summary="배차 엔진 헬스체크 및 서산시 상태")
def health():
    routable_stops = sum(1 for stop in STOP_CATALOG.by_id.values() if stop.routable)
    return {
        "status": "ok",
        "service": "seosan-drt-ortools",
        "region": "서산시 행복버스 DRT",
        "active_vehicles": sum(1 for vehicle in dispatcher.vehicles.values() if vehicle.is_active),
        "total_locations": len(LOCATION_DB),
        "total_stops": len(STOP_CATALOG),
        "routable_stops": routable_stops,
        "stop_counts_by_region": STOP_CATALOG.region_counts(),
    }


@app.get("/drt/regions", summary="서산시 DRT 운행 권역 및 시간 정책")
def get_regions():
    counts = STOP_CATALOG.region_counts()
    child_regions = [
        {
            "region_code": code,
            "name": f"서산시 {name} 권역",
            "operating_hours": "08:00 - 18:00",
            "service_type": "수요응답형 행복버스",
            "stop_count": counts[code],
        }
        for code, name in REGION_NAMES.items()
    ]
    return {
        "regions": [
            {
                "region_code": "SEOSAN_CITY",
                "name": "서산시 전체 등록 권역",
                "operating_hours": "08:00 - 18:00",
                "service_type": "수요응답형 행복버스",
                "stop_count": len(STOP_CATALOG),
            },
            *child_regions,
        ]
    }


@app.get("/drt/stops", summary="서산시 행복버스 정류장 검색")
def list_stops(
    region_code: Optional[str] = Query(default=None),
    query: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    normalized_region = _validated_region(region_code)
    records = STOP_CATALOG.search(query, normalized_region, limit)
    return {
        "stops": [record.to_dict() for record in records],
        "count": len(records),
        "total_stops": len(STOP_CATALOG),
        "region_code": normalized_region,
        "query": query,
    }


@app.get("/drt/stops/nearest", summary="좌표에서 가까운 행복버스 정류장 검색")
def nearest_stops(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    region_code: Optional[str] = Query(default=None),
    limit: int = Query(default=5, ge=1, le=20),
):
    normalized_region = _validated_region(region_code)
    records = STOP_CATALOG.nearest(lat, lng, normalized_region, limit)
    return {
        "stops": [
            record.to_dict(distance_km=distance_km)
            for record, distance_km in records
        ],
        "count": len(records),
        "region_code": normalized_region,
        "origin": {"lat": lat, "lng": lng},
    }


@app.post("/drt/stops/resolve", summary="정류장명 또는 통합ID 해석")
def resolve_stop(req: StopResolveRequest):
    normalized_region = _validated_region(req.region_code)
    return STOP_CATALOG.resolve(req.query, normalized_region).to_dict()


@app.get("/drt/stops/{stop_id}", summary="통합ID로 정류장 상세 조회")
def get_stop(stop_id: str = FastPath(...)):
    record = STOP_CATALOG.get(stop_id)
    if record is None:
        raise APIInputError(
            404,
            "UNKNOWN_STOP",
            f"등록되지 않은 정류장입니다: {stop_id}",
        )
    return record.to_dict()


@app.post("/drt/availability/check", summary="BE 연동: 가용 차량 및 예상 픽업 시각 사전 검증")
def check_availability(req: AvailabilityCheckRequest):
    time_str = _parse_time_str(req.requested_pickup_at)
    pickup_loc = _resolve_request_location(
        req.origin, req.origin_stop_id, req.region_code, "출발지"
    )
    dropoff_loc = _resolve_request_location(
        req.destination, req.destination_stop_id, req.region_code, "목적지"
    )

    assigned_v = None
    min_dist = float("inf")
    with state_lock:
        vehicles = copy.deepcopy(dispatcher.vehicles)
    for vehicle_id, vehicle in vehicles.items():
        available_seats = vehicle.capacity - vehicle.current_load
        if vehicle.is_active and available_seats >= req.passenger_count:
            distance = pickup_loc.distance_to(vehicle.current_location)
            if distance < min_dist:
                min_dist = distance
                assigned_v = vehicle_id

    if assigned_v:
        # 현재 API 계약의 기본 승차 준비시간을 유지합니다.
        estimated_minutes = time_str_to_minutes(time_str) + 5
        return {
            "status": "success",
            "available": True,
            "estimated_pickup_time": minutes_to_time_str(estimated_minutes),
            "vehicle_id": assigned_v,
            "origin": pickup_loc.name,
            "destination": dropoff_loc.name,
            "origin_stop_id": pickup_loc.location_id,
            "destination_stop_id": dropoff_loc.location_id,
            "region_code": _validated_region(req.region_code),
        }

    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "CAPACITY_EXCEEDED",
                "message": "해당 시각에 이용 가능한 서산시 행복버스 차량이 없습니다.",
            }
        },
    )


@app.post("/drt/reservations", summary="BE 연동: 신규 배차 확정 & OR-Tools 동적 경로 노드 삽입")
def create_reservation(req: CreateReservationRequest):
    time_str = _parse_time_str(req.requested_pickup_at)
    pickup_loc = _resolve_request_location(
        req.origin, req.origin_stop_id, req.region_code, "출발지"
    )
    dropoff_loc = _resolve_request_location(
        req.destination, req.destination_stop_id, req.region_code, "목적지"
    )

    sequence, req_id, result = _process_new_dispatch(
        time_str,
        req.passenger_phone or "user_anon",
        pickup_loc,
        dropoff_loc,
        req.passenger_count,
    )

    res_dict = result.to_dict()
    res_dict["session_id"] = req.client_ref
    res_dict["reservation_id"] = (
        f"R-{datetime.now(KST).strftime('%Y%m%d')}-{sequence:04d}"
    )
    res_dict["origin_stop_id"] = pickup_loc.location_id
    res_dict["destination_stop_id"] = dropoff_loc.location_id
    res_dict["region_code"] = _validated_region(req.region_code)

    if result.status == "success":
        logger.info(
            "배차 성공: reservation_id=%s, vehicle=%s, pickup=%s",
            res_dict["reservation_id"],
            result.vehicle_id,
            result.pickup_time,
        )
        return res_dict

    logger.warning(
        "배차 실패: reason=%s, error_code=%s",
        result.reason,
        result.error_code,
    )
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
    with state_lock:
        requests = copy.deepcopy(dispatcher.requests)
    active_reqs = []
    for req_id, p_req in requests.items():
        active_reqs.append(
            {
                "request_id": req_id,
                "user_id": p_req.user_id,
                "passenger_count": p_req.passenger_count,
                "pickup_location": p_req.pickup_location.name,
                "dropoff_location": p_req.dropoff_location.name,
                "origin_stop_id": p_req.pickup_location.location_id,
                "destination_stop_id": p_req.dropoff_location.location_id,
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

    result = _process_cancellation(target_id, time_str)
    res_dict = result.to_dict()
    if result.status == "success":
        res_dict["cancelled_reservation_id"] = target_id
    return res_dict


