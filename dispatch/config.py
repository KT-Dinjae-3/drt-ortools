"""
dispatch/config.py — 배차 최적화 설정값
========================================
모든 상수/하이퍼파라미터를 한 곳에서 관리합니다.
팀원이 실험 시 이 파일만 수정하면 됩니다.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DispatchConfig:
    """배차 엔진 설정값 (불변 객체)."""

    # ── 시간창(Time Window) ──────────────────────────────
    PICKUP_TW_EARLY_SLACK: int = 5        # 픽업 시간창 앞쪽 여유 (분)
    PICKUP_TW_LATE_SLACK: int = 15        # 픽업 시간창 뒤쪽 여유 (분)

    # ── 기존 승객 보호 ───────────────────────────────────
    MAX_EXISTING_DELAY: int = 10          # 기존 승객 지연 허용 상한 (분)

    # ── 목적함수 가중치 ──────────────────────────────────
    VEHICLE_FIXED_COST: int = 10_000      # 차량 가동 고정 비용
    DELAY_PENALTY_COEFF: int = 100        # 지연 1분당 페널티 계수
    PENALTY_FOR_DROPPING: int = 100_000   # 노드 드롭 페널티

    # ── 솔버 파라미터 ────────────────────────────────────
    SOLVER_TIME_LIMIT_SEC: int = 1        # OR-Tools 시간 제한 (초)
    SPEED_KMH: float = 40.0              # 평균 속도 (km/h)

    # ── 운행 범위 ────────────────────────────────────────
    DROPOFF_BUFFER_MIN: int = 5           # 하차 시간 버퍼 (분)
    MAX_ROUTE_HORIZON: int = 24 * 60      # 24시간 (분)

    # ── 임박 취소 정책 ───────────────────────────────────
    IMMINENT_CANCEL_THRESHOLD_MIN: int = 30  # 픽업까지 30분 이내 취소 → 임박

    # ── 차량 기본값 (팀 기준) ─────────────────────────────
    DEFAULT_VEHICLE_CAPACITY: int = 4     # 팀 시나리오 기준 4인승


# 기본 설정 싱글턴
DEFAULT_CONFIG = DispatchConfig()
