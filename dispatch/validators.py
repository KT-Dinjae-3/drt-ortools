"""
dispatch/validators.py -- 입력 검증 전처리 계층
================================================
배차 엔진에 전달하기 전 비즈니스 규칙을 검증합니다.

- 과거 시간 요청 거부 (Seq 10)
- 임박 취소 플래그 세팅 (Seq 15)
- 필수 필드 누락 검증
"""

from __future__ import annotations

from typing import Optional, Tuple

from .config import DispatchConfig, DEFAULT_CONFIG
from .models import (
    DispatchEvent,
    DispatchResult,
    EventType,
    ErrorCode,
    ActionType,
    minutes_to_time_str,
)


def validate_event(
    event: DispatchEvent,
    config: DispatchConfig = DEFAULT_CONFIG,
) -> Tuple[bool, Optional[DispatchResult]]:
    """
    이벤트를 검증한다.

    Parameters
    ----------
    event : DispatchEvent
        파싱된 이벤트.
    config : DispatchConfig
        설정값.

    Returns
    -------
    (is_valid, error_result)
        유효하면 (True, None).
        유효하지 않으면 (False, DispatchResult) — 바로 반환할 에러 응답.
    """
    if event.event_type == EventType.NEW_REQUEST:
        return _validate_new_request(event, config)
    elif event.event_type == EventType.CANCELLATION:
        # 취소 자체는 항상 유효 (임박 여부는 엔진에서 처리)
        return True, None
    elif event.event_type == EventType.CHANGE_REQUEST:
        return _validate_change_request(event, config)

    return True, None


def _validate_new_request(
    event: DispatchEvent,
    config: DispatchConfig,
) -> Tuple[bool, Optional[DispatchResult]]:
    """신규 요청 검증 — 과거 시간 거부."""
    payload = event.payload
    pickup_time_str = payload.get("requested_pickup_time", "")

    if not pickup_time_str:
        return False, DispatchResult(
            seq=event.seq,
            status="failed",
            action=ActionType.NEW_RESERVATION.value,
            request_id=event.request_id or "",
            error_code=ErrorCode.NO_VEHICLE_AVAILABLE.value,
            reason="requested_pickup_time is missing.",
        )

    # 시간 파싱
    from .models import time_str_to_minutes
    requested_minutes = time_str_to_minutes(pickup_time_str)
    event_minutes = event.event_time_minutes

    # 과거 시간 요청 체크: 요청 시각보다 이벤트 접수 시각이 더 늦으면 거부
    if requested_minutes < event_minutes:
        # 대안 시간 2개 제시: 이벤트 시각 기준 +20분, +50분
        alt1 = event_minutes + 20
        alt2 = event_minutes + 50
        alternatives = [
            minutes_to_time_str(alt1),
            minutes_to_time_str(alt2),
        ]

        return False, DispatchResult(
            seq=event.seq,
            status="failed",
            action=ActionType.NEW_RESERVATION.value,
            request_id=event.request_id or "",
            error_code="DISPATCH_UNAVAILABLE",
            reason=(
                f"요청 시간({pickup_time_str})이 호출 시점({minutes_to_time_str(event_minutes)}) "
                f"이전으로 이미 지남"
            ),
            alternatives=alternatives,
            reasoning=["요청된 픽업 시간이 현재 시각보다 과거 → 배차 불가"],
        )

    return True, None


def _validate_change_request(
    event: DispatchEvent,
    config: DispatchConfig,
) -> Tuple[bool, Optional[DispatchResult]]:
    """변경 요청 검증."""
    payload = event.payload
    new_time = payload.get("new_requested_pickup_time", "")

    if new_time:
        from .models import time_str_to_minutes
        new_minutes = time_str_to_minutes(new_time)
        if new_minutes < event.event_time_minutes:
            alt1 = event.event_time_minutes + 20
            alt2 = event.event_time_minutes + 50
            return False, DispatchResult(
                seq=event.seq,
                status="failed",
                action=ActionType.CHANGE.value,
                request_id=event.target_reservation_id or "",
                error_code=ErrorCode.PAST_TIME.value,
                reason=f"New pickup time {new_time} is in the past.",
                alternatives=[
                    minutes_to_time_str(alt1),
                    minutes_to_time_str(alt2),
                ],
            )

    return True, None


def check_imminent_cancellation(
    event: DispatchEvent,
    original_pickup_time_minutes: int,
    config: DispatchConfig = DEFAULT_CONFIG,
) -> bool:
    """
    임박 취소 여부를 판단한다.

    픽업 시간까지 남은 시간이 threshold 이내이면 임박 취소로 판정.
    이미 픽업 시각이 지난 경우(노쇼)도 임박으로 판정.

    Returns
    -------
    bool
        True면 임박 취소.
    """
    time_until_pickup = original_pickup_time_minutes - event.event_time_minutes
    return time_until_pickup <= config.IMMINENT_CANCEL_THRESHOLD_MIN
