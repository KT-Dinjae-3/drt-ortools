"""
engine.py -- OR-Tools 솔버 코어
================================================
DynamicDRTDispatcher 클래스 — 배차 최적화의 핵심 엔진.

담당 기능:
  - OR-Tools RoutingModel 구성 (Pickup & Delivery)
  - 시간창(Time Window) 제약
  - 용량(Capacity) 제약
  - 3중 목적함수 (총 운행시간 + 지연 페널티 + 차량 대수)
  - 합승 탐지 / 동적 삽입 검증 / 대안 시간 역산
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .config import DispatchConfig, DEFAULT_CONFIG
from .locations import resolve_location_name
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
from .validators import check_imminent_cancellation, validate_event
from .io_adapter import DispatchIOAdapter


class DynamicDRTDispatcher:
    """
    실시간 수요응답형 버스(DRT) 동적 배차 최적화 엔진.

    Usage::

        from dispatch import DynamicDRTDispatcher, DispatchIOAdapter
        from dispatch.models import Vehicle, Location

        dispatcher = DynamicDRTDispatcher(
            vehicles=[Vehicle("DRT-SS-01", capacity=11)],
            depot=Location(36.7845, 126.4501, "서산시청 차고지"),
        )
        adapter = DispatchIOAdapter()

        event = adapter.parse_event(raw_json)
        result = dispatcher.process_event(event)
        output = adapter.build_output(result)
    """

    def __init__(
        self,
        vehicles: List[Vehicle],
        depot: Location,
        config: DispatchConfig = DEFAULT_CONFIG,
    ):
        self.depot = depot
        self.config = config
        self.vehicles: Dict[str, Vehicle] = {v.vehicle_id: v for v in vehicles}
        self.requests: Dict[str, PassengerRequest] = {}
        self.previous_solution: Optional[Dict] = None
        self._adapter = DispatchIOAdapter()

    # ==================================================================
    # Public API
    # ==================================================================

    def process_event(self, event: DispatchEvent) -> DispatchResult:
        """
        단일 DispatchEvent를 처리하고 DispatchResult를 반환한다.

        1) 입력 검증 (과거 시간 등)
        2) 이벤트 유형별 처리
        3) OR-Tools 최적화
        4) 결과 반환
        """
        # ── Step 1: 검증 ──
        is_valid, error_result = validate_event(event, self.config)
        if not is_valid:
            return error_result

        # ── Step 2: 이벤트 유형별 분기 ──
        if event.event_type == EventType.NEW_REQUEST:
            return self._handle_new_request(event)
        elif event.event_type == EventType.CANCELLATION:
            return self._handle_cancellation(event)
        elif event.event_type == EventType.CHANGE_REQUEST:
            return self._handle_change_request(event)
        else:
            return DispatchResult(
                seq=event.seq,
                status="failed",
                action="unknown",
                request_id=event.request_id or "",
                error_code="UNKNOWN_EVENT",
                reason=f"Unknown event type: {event.event_type}",
            )

    # ==================================================================
    # Event Handlers
    # ==================================================================

    def _handle_new_request(self, event: DispatchEvent) -> DispatchResult:
        """신규 승차 요청 처리."""
        payload = event.payload
        pickup_loc, dropoff_loc, pickup_time_str, pax_count = (
            self._adapter.parse_payload_locations(payload)
        )

        if not pickup_loc or not dropoff_loc or not pickup_time_str:
            return DispatchResult(
                seq=event.seq,
                status="failed",
                action=ActionType.NEW_RESERVATION.value,
                request_id=event.request_id or "",
                error_code=ErrorCode.NO_VEHICLE_AVAILABLE.value,
                reason="Missing pickup/dropoff location or time.",
            )

        req_id = event.request_id or f"RES-{uuid.uuid4().hex[:8]}"
        req = PassengerRequest(
            request_id=req_id,
            user_id=event.user_id,
            passenger_count=pax_count,
            pickup_location=pickup_loc,
            dropoff_location=dropoff_loc,
            requested_pickup_time=time_str_to_minutes(pickup_time_str),
        )
        self.requests[req_id] = req

        return self._solve_and_build_result(
            seq=event.seq,
            target_request_id=req_id,
            action=ActionType.NEW_RESERVATION,
        )

    def _handle_cancellation(self, event: DispatchEvent) -> DispatchResult:
        """취소 처리 -- Capacity 반환 + 임박 취소 플래그."""
        target_id = event.target_reservation_id or event.request_id or ""

        if target_id not in self.requests:
            return DispatchResult(
                seq=event.seq,
                status="failed",
                action=ActionType.CANCELLATION.value,
                request_id=target_id,
                input_ref=f"{target_id} 취소",
                error_code=ErrorCode.REQUEST_NOT_FOUND.value,
                reason=f"Reservation {target_id} not found.",
            )

        req = self.requests[target_id]
        if req.status == RequestStatus.CANCELLED:
            return DispatchResult(
                seq=event.seq,
                status="failed",
                action=ActionType.CANCELLATION.value,
                request_id=target_id,
                input_ref=f"{target_id} 취소",
                error_code=ErrorCode.ALREADY_CANCELLED.value,
                reason=f"Reservation {target_id} is already cancelled.",
            )

        freed_vehicle = req.assigned_vehicle_id

        # 임박 취소 체크
        is_imminent = check_imminent_cancellation(
            event, req.requested_pickup_time, self.config
        )

        # Capacity 반환
        if req.assigned_vehicle_id and req.assigned_vehicle_id in self.vehicles:
            v = self.vehicles[req.assigned_vehicle_id]
            v.current_load = max(0, v.current_load - req.passenger_count)

        req.status = RequestStatus.CANCELLED

        # 판단 근거
        reasoning = [f"{target_id} 취소 처리"]
        if freed_vehicle:
            reasoning.append(f"{freed_vehicle} 잔여 용량 {req.passenger_count}석 회복")
        if is_imminent:
            reasoning.append(f"픽업 예정시각({minutes_to_time_str(req.requested_pickup_time)}) 이후 시점({event.event_time})의 취소 — 위약 처리 필요 여부 플래그")

        # 기본 취소 결과
        cancel_result_fields = dict(
            seq=event.seq,
            status="success",
            action=ActionType.CANCELLATION.value,
            request_id=target_id,
            input_ref=f"{target_id} 취소",
            pickup_location=resolve_location_name(req.pickup_location),
            dropoff_location=resolve_location_name(req.dropoff_location),
            is_imminent=is_imminent,
            cancelled_reservation_id=target_id,
            vehicle_freed=freed_vehicle,
            reasoning=reasoning,
        )

        # 활성 요청 확인
        active = self._active_requests()
        if not active:
            return DispatchResult(**cancel_result_fields)

        result = self._solve_and_build_result(
            seq=event.seq,
            target_request_id=target_id,
            action=ActionType.CANCELLATION,
        )
        # 취소 전용 필드 덮어쓰기
        result.is_imminent = is_imminent
        result.pickup_location = resolve_location_name(req.pickup_location)
        result.dropoff_location = resolve_location_name(req.dropoff_location)
        result.cancelled_reservation_id = target_id
        result.vehicle_freed = freed_vehicle
        result.input_ref = f"{target_id} 취소"
        result.reasoning = reasoning
        return result

    def _handle_change_request(self, event: DispatchEvent) -> DispatchResult:
        """기존 요청 변경 처리."""
        target_id = event.target_reservation_id or ""
        payload = event.payload

        if target_id not in self.requests:
            return DispatchResult(
                seq=event.seq,
                status="failed",
                action=ActionType.CHANGE.value,
                request_id=target_id,
                input_ref=f"{target_id} 변경",
                error_code=ErrorCode.REQUEST_NOT_FOUND.value,
                reason=f"Reservation {target_id} not found.",
            )

        req = self.requests[target_id]
        if req.status == RequestStatus.CANCELLED:
            return DispatchResult(
                seq=event.seq,
                status="failed",
                action=ActionType.CHANGE.value,
                request_id=target_id,
                input_ref=f"{target_id} 변경",
                error_code=ErrorCode.ALREADY_CANCELLED.value,
                reason="Cannot change a cancelled reservation.",
            )

        # 변경 적용
        new_time = payload.get("new_requested_pickup_time")
        if new_time:
            req.requested_pickup_time = time_str_to_minutes(new_time)

        if "pickup" in payload:
            pickup_val = payload["pickup"]
            if isinstance(pickup_val, str):
                from .locations import get_location
                loc = get_location(pickup_val)
                if loc:
                    req.pickup_location = loc
            elif isinstance(pickup_val, dict):
                req.pickup_location = Location.from_dict(pickup_val)
        if "dropoff" in payload:
            dropoff_val = payload["dropoff"]
            if isinstance(dropoff_val, str):
                from .locations import get_location
                loc = get_location(dropoff_val)
                if loc:
                    req.dropoff_location = loc
            elif isinstance(dropoff_val, dict):
                req.dropoff_location = Location.from_dict(dropoff_val)
        if "passenger_count" in payload:
            req.passenger_count = payload["passenger_count"]

        # promised 시간 초기화
        req.promised_pickup_time = None
        req.promised_dropoff_time = None

        # 판단 근거
        reasoning = []
        if new_time:
            reasoning.append(f"{new_time} 시점 차량 일정 재확인")

        result = self._solve_and_build_result(
            seq=event.seq,
            target_request_id=target_id,
            action=ActionType.CHANGE,
        )
        result.input_ref = f"{target_id} 변경"
        result.target_reservation_id = target_id
        result.new_pickup_time = new_time
        if reasoning:
            result.reasoning = reasoning
        return result

    # ==================================================================
    # Core: OR-Tools Solve
    # ==================================================================

    def _active_requests(self) -> List[PassengerRequest]:
        """취소/완료가 아닌 활성 요청 목록."""
        return [
            r for r in self.requests.values()
            if r.status
            not in (
                RequestStatus.CANCELLED,
                RequestStatus.COMPLETED,
                RequestStatus.FAILED,
            )
        ]

    def _build_nodes(self, active: List[PassengerRequest]):
        """노드 리스트 구성 (depot=0, pickup=2i+1, dropoff=2i+2)."""
        nodes: List[Location] = [self.depot]
        pickup_indices: List[int] = []
        dropoff_indices: List[int] = []
        req_for_node: Dict[int, PassengerRequest] = {}

        for i, req in enumerate(active):
            pi = 2 * i + 1
            di = 2 * i + 2
            nodes.append(req.pickup_location)
            nodes.append(req.dropoff_location)
            pickup_indices.append(pi)
            dropoff_indices.append(di)
            req_for_node[pi] = req
            req_for_node[di] = req

        return nodes, pickup_indices, dropoff_indices, req_for_node

    def _build_distance_matrix(self, nodes: List[Location]) -> List[List[int]]:
        """Haversine 기반 Distance Matrix (정수, 미터 단위)."""
        n = len(nodes)
        matrix = [[0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                if i != j:
                    matrix[i][j] = int(nodes[i].distance_to(nodes[j]) * 1000)
        return matrix

    def _build_time_matrix(self, dist_matrix: List[List[int]]) -> List[List[int]]:
        """Distance -> Time (분) 변환."""
        speed_m_per_min = self.config.SPEED_KMH * 1000 / 60
        n = len(dist_matrix)
        time_matrix = [[0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                if i != j:
                    time_matrix[i][j] = max(1, int(dist_matrix[i][j] / speed_m_per_min))
        return time_matrix

    def _solve_and_build_result(
        self,
        seq: int,
        target_request_id: str,
        action: ActionType,
    ) -> DispatchResult:
        """활성 요청 기반 OR-Tools 최적화 -> DispatchResult."""
        active = self._active_requests()
        cfg = self.config

        if not active:
            return DispatchResult(
                seq=seq,
                status="success",
                action=action.value,
                request_id=target_request_id,
                reason="No active requests.",
            )

        nodes, pickups, dropoffs, req_map = self._build_nodes(active)
        dist_matrix = self._build_distance_matrix(nodes)
        time_matrix = self._build_time_matrix(dist_matrix)

        num_nodes = len(nodes)
        num_vehicles = len(self.vehicles)
        vehicle_list = list(self.vehicles.values())
        vehicle_index_by_id = {
            vehicle.vehicle_id: index
            for index, vehicle in enumerate(vehicle_list)
        }
        depot_index = 0

        # ── OR-Tools 모델 구성 ──
        manager = pywrapcp.RoutingIndexManager(num_nodes, num_vehicles, depot_index)
        routing = pywrapcp.RoutingModel(manager)

        # Time callback
        def time_callback(from_idx, to_idx):
            return time_matrix[manager.IndexToNode(from_idx)][manager.IndexToNode(to_idx)]

        time_cb = routing.RegisterTransitCallback(time_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(time_cb)

        # Time Dimension
        routing.AddDimension(
            time_cb, cfg.MAX_ROUTE_HORIZON, cfg.MAX_ROUTE_HORIZON, False, "Time"
        )
        time_dim = routing.GetDimensionOrDie("Time")

        # Capacity callback
        demands = [0] * num_nodes
        for pi, di in zip(pickups, dropoffs):
            demands[pi] = req_map[pi].passenger_count
            demands[di] = -req_map[di].passenger_count

        def demand_callback(idx):
            return demands[manager.IndexToNode(idx)]

        demand_cb = routing.RegisterUnaryTransitCallback(demand_callback)
        routing.AddDimensionWithVehicleCapacity(
            demand_cb, 0, [v.capacity for v in vehicle_list], True, "Capacity"
        )

        # Pickup & Delivery 쌍
        solver = routing.solver()
        for pi, di in zip(pickups, dropoffs):
            pi_idx = manager.NodeToIndex(pi)
            di_idx = manager.NodeToIndex(di)
            routing.AddPickupAndDelivery(pi_idx, di_idx)
            solver.Add(routing.VehicleVar(pi_idx) == routing.VehicleVar(di_idx))
            solver.Add(time_dim.CumulVar(pi_idx) <= time_dim.CumulVar(di_idx))
            request = req_map[pi]
            if request.assigned_vehicle_id in vehicle_index_by_id:
                solver.Add(
                    routing.VehicleVar(pi_idx)
                    == vehicle_index_by_id[request.assigned_vehicle_id]
                )

        # Time Window 제약
        for pi in pickups:
            req = req_map[pi]
            idx = manager.NodeToIndex(pi)
            if req.promised_pickup_time is not None:
                time_dim.CumulVar(idx).SetRange(
                    req.promised_pickup_time,
                    req.promised_pickup_time,
                )
            else:
                tw_start = max(
                    0, req.requested_pickup_time - cfg.PICKUP_TW_EARLY_SLACK
                )
                tw_end = req.requested_pickup_time + cfg.PICKUP_TW_LATE_SLACK
                time_dim.CumulVar(idx).SetRange(tw_start, tw_end)
                time_dim.SetCumulVarSoftUpperBound(
                    idx, req.requested_pickup_time, cfg.DELAY_PENALTY_COEFF
                )

        for di in dropoffs:
            req = req_map[di]
            pi = di - 1
            travel = time_matrix[pi][di]
            tw_s = max(0, req.requested_pickup_time - cfg.PICKUP_TW_EARLY_SLACK + travel)
            tw_e = req.requested_pickup_time + cfg.PICKUP_TW_LATE_SLACK + travel + cfg.DROPOFF_BUFFER_MIN
            idx = manager.NodeToIndex(di)
            time_dim.CumulVar(idx).SetRange(tw_s, tw_e)

        # 기존 승객 보호 (Soft Upper Bound)
        for pi, di in zip(pickups, dropoffs):
            req = req_map[pi]
            if req.promised_dropoff_time is not None:
                idx = manager.NodeToIndex(di)
                max_allowed = req.promised_dropoff_time + cfg.MAX_EXISTING_DELAY
                time_dim.SetCumulVarSoftUpperBound(
                    idx, max_allowed, cfg.DELAY_PENALTY_COEFF * 100
                )

        # 차량 고정 비용
        routing.SetFixedCostOfAllVehicles(cfg.VEHICLE_FIXED_COST)

        # Depot TW
        for v_idx in range(num_vehicles):
            time_dim.CumulVar(routing.Start(v_idx)).SetRange(0, cfg.MAX_ROUTE_HORIZON)
            time_dim.CumulVar(routing.End(v_idx)).SetRange(0, cfg.MAX_ROUTE_HORIZON)

        # 신규 요청만 선택적으로 거절할 수 있습니다. 이미 확정된 요청까지
        # drop 가능하게 두면 후속 최적화가 BE에 확정 응답을 준 예약을 조용히
        # 제거할 수 있으므로 기존 요청은 필수 노드로 유지합니다.
        for pi, di in zip(pickups, dropoffs):
            request = req_map[pi]
            if (
                action == ActionType.NEW_RESERVATION
                and request.request_id == target_request_id
            ):
                routing.AddDisjunction(
                    [manager.NodeToIndex(pi)], cfg.PENALTY_FOR_DROPPING
                )
                routing.AddDisjunction(
                    [manager.NodeToIndex(di)], cfg.PENALTY_FOR_DROPPING
                )

        # ── 솔버 파라미터 ──
        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.AUTOMATIC
        )
        search_parameters.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        search_parameters.time_limit.seconds = cfg.SOLVER_TIME_LIMIT_SEC
        search_parameters.log_search = False  # OR-Tools 내부 로그 끄기

        # ── 풀이 ──
        solution = routing.SolveWithParameters(search_parameters)

        if not solution:
            return self._build_failed_result(
                seq, target_request_id, action, active
            )

        return self._extract_result(
            manager, routing, solution, time_dim,
            vehicle_list, active, pickups, dropoffs, req_map,
            seq, target_request_id, action, time_matrix,
        )

    # ==================================================================
    # Solution Extraction
    # ==================================================================

    def _extract_result(
        self, manager, routing, solution, time_dim,
        vehicle_list, active, pickups, dropoffs, req_map,
        seq, target_request_id, action, time_matrix,
    ) -> DispatchResult:
        """OR-Tools Solution -> DispatchResult."""
        assigned: Dict[str, Dict] = {}
        vehicle_req_map: Dict[str, List[str]] = {}
        dropped = set()

        # 드롭 노드 식별
        for pi, di in zip(pickups, dropoffs):
            pi_idx = manager.NodeToIndex(pi)
            if solution.Value(routing.NextVar(pi_idx)) == pi_idx:
                dropped.add(pi)
                dropped.add(di)

        # 경로 순회
        routes = []
        for v_idx, vehicle in enumerate(vehicle_list):
            stops = []
            v_req_ids = []
            index = routing.Start(v_idx)

            while not routing.IsEnd(index):
                node = manager.IndexToNode(index)
                arrival = solution.Value(time_dim.CumulVar(index))

                if node != 0 and node not in dropped:
                    req = req_map.get(node)
                    if req:
                        is_pickup = node in pickups
                        loc = req.pickup_location if is_pickup else req.dropoff_location
                        stops.append({
                            "request_id": req.request_id,
                            "type": "pickup" if is_pickup else "dropoff",
                            "location": resolve_location_name(loc),
                            "scheduled_time": minutes_to_time_str(arrival),
                            "scheduled_time_minutes": arrival,
                        })

                        if req.request_id not in assigned:
                            assigned[req.request_id] = {
                                "request_id": req.request_id,
                                "vehicle_id": vehicle.vehicle_id,
                                "status": "assigned",
                                "pickup_location": resolve_location_name(req.pickup_location),
                                "dropoff_location": resolve_location_name(req.dropoff_location),
                            }

                        if is_pickup:
                            assigned[req.request_id]["pickup_time"] = minutes_to_time_str(arrival)
                            assigned[req.request_id]["pickup_time_minutes"] = arrival
                            if req.promised_pickup_time is None:
                                req.promised_pickup_time = arrival
                        else:
                            assigned[req.request_id]["dropoff_time"] = minutes_to_time_str(arrival)
                            assigned[req.request_id]["dropoff_time_minutes"] = arrival
                            if req.promised_dropoff_time is None:
                                req.promised_dropoff_time = arrival

                        req.assigned_vehicle_id = vehicle.vehicle_id
                        req.status = RequestStatus.ASSIGNED
                        if req.request_id not in v_req_ids:
                            v_req_ids.append(req.request_id)

                index = solution.Value(routing.NextVar(index))

            vehicle_req_map[vehicle.vehicle_id] = v_req_ids
            if stops:
                routes.append({
                    "vehicle_id": vehicle.vehicle_id,
                    "stops": stops,
                })

        # ── 합승 탐지 ──
        for vid, req_ids in vehicle_req_map.items():
            if len(req_ids) > 1:
                for rid in req_ids:
                    others = [r for r in req_ids if r != rid]
                    if rid in assigned:
                        assigned[rid]["ride_share_with"] = others
                    if rid in self.requests:
                        self.requests[rid].ride_share_with = others

        # ── 동적 삽입 검증 ──
        is_insertion = False
        insertion_violations = []
        if self.previous_solution and action == ActionType.NEW_RESERVATION:
            is_insertion = target_request_id in assigned  # 기존 경로에 삽입됨
            for rid, info in assigned.items():
                prev = self.previous_solution.get(rid)
                if prev and "dropoff_time_minutes" in prev and "dropoff_time_minutes" in info:
                    delay = info["dropoff_time_minutes"] - prev["dropoff_time_minutes"]
                    if delay > self.config.MAX_EXISTING_DELAY:
                        insertion_violations.append(rid)

        if insertion_violations:
            target_req = self.requests.get(target_request_id)
            if target_req:
                target_req.status = RequestStatus.PENDING
                target_req.assigned_vehicle_id = None
            return DispatchResult(
                seq=seq,
                status="failed",
                action=action.value,
                request_id=target_request_id,
                error_code=ErrorCode.DELAY_VIOLATION.value,
                reason=f"Insertion would delay passengers {insertion_violations} by >{self.config.MAX_EXISTING_DELAY}min.",
                alternatives=self._compute_alternatives(target_request_id),
            )

        # ── 실패한 요청 처리 ──
        failed = [r for r in active if r.request_id not in assigned]
        for r in failed:
            r.status = RequestStatus.FAILED

        # ── 이전 솔루션 저장 ──
        self.previous_solution = copy.deepcopy(assigned)

        # ── 타겟 요청의 결과 추출 ──
        target_info = assigned.get(target_request_id, {})
        target_req = self.requests.get(target_request_id)

        # 타겟이 실패한 경우
        if target_request_id not in assigned and action != ActionType.CANCELLATION:
            return DispatchResult(
                seq=seq,
                status="failed",
                action=action.value,
                request_id=target_request_id,
                error_code=ErrorCode.NO_VEHICLE_AVAILABLE.value,
                reason="No feasible vehicle assignment found.",
                alternatives=self._compute_alternatives(target_request_id),
            )

        # ── 성공 결과 구성 ──
        result = DispatchResult(
            seq=seq,
            status="success",
            action=action.value,
            request_id=target_request_id,
            vehicle_id=target_info.get("vehicle_id"),
            pickup_time=target_info.get("pickup_time"),
            pickup_time_minutes=target_info.get("pickup_time_minutes"),
            dropoff_time=target_info.get("dropoff_time"),
            dropoff_time_minutes=target_info.get("dropoff_time_minutes"),
            pickup_location=target_info.get("pickup_location",
                resolve_location_name(target_req.pickup_location) if target_req else ""),
            dropoff_location=target_info.get("dropoff_location",
                resolve_location_name(target_req.dropoff_location) if target_req else ""),
            ride_share_with=target_info.get("ride_share_with"),
            insertion=is_insertion and bool(self.previous_solution),
            routes=routes,
            objective_value=solution.ObjectiveValue(),
        )

        # 변경 시 new_pickup_time으로 표기
        if action == ActionType.CHANGE and result.pickup_time:
            pass  # pickup_time 필드에 이미 새 시간이 들어감

        return result

    # ==================================================================
    # Alternatives & Failure
    # ==================================================================

    def _build_failed_result(
        self, seq, target_id, action, active
    ) -> DispatchResult:
        """솔루션 전혀 못 찾았을 때."""
        return DispatchResult(
            seq=seq,
            status="failed",
            action=action.value,
            request_id=target_id,
            error_code=ErrorCode.NO_VEHICLE_AVAILABLE.value,
            reason="No feasible solution found for any vehicle.",
            alternatives=self._compute_alternatives(target_id),
        )

    def _compute_alternatives(self, request_id: str) -> List[str]:
        """배차 실패 요청에 대해 가장 가까운 유휴 시간대 2개를 역산."""
        req = self.requests.get(request_id)
        if not req:
            return []

        candidates = []
        for vid, vehicle in self.vehicles.items():
            latest_end = self._estimate_vehicle_free_time(vid)
            travel = self._travel_time_minutes(vehicle.current_location, req.pickup_location)
            earliest = latest_end + travel
            slot = max(earliest, req.requested_pickup_time - self.config.PICKUP_TW_EARLY_SLACK)
            candidates.append(slot)
            candidates.append(slot + 30)

        candidates = sorted(set(candidates))
        future = [c for c in candidates if c >= req.requested_pickup_time]
        if len(future) < 2:
            future = candidates[:2]

        return [minutes_to_time_str(t) for t in future[:2]]

    def _estimate_vehicle_free_time(self, vehicle_id: str) -> int:
        if not self.previous_solution:
            return 0
        latest = 0
        for info in self.previous_solution.values():
            if info.get("vehicle_id") == vehicle_id:
                latest = max(latest, info.get("dropoff_time_minutes", 0))
        return latest

    def _travel_time_minutes(self, a: Location, b: Location) -> int:
        dist_km = a.distance_to(b)
        speed_km_min = self.config.SPEED_KMH / 60
        return max(1, int(dist_km / speed_km_min))
