"""
tests/test_scenarios.py -- 15개 시나리오 통합 테스트
====================================================
팀 기준 dispatch_input_timeline.json의 15개 이벤트를 순차 실행하며
각 시나리오의 핵심 동작을 검증합니다.

실행:
    python tests/test_scenarios.py
"""

from __future__ import annotations

import json
import os
import sys

# 프로젝트 루트를 sys.path에 추가
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dispatch import DynamicDRTDispatcher, DispatchIOAdapter
from dispatch.locations import DEFAULT_DEPOT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check(ok, label):
    tag = "[PASS]" if ok else "[FAIL]"
    print(f"  {tag} - {label}")
    return ok


def dump_result(output, label="Result"):
    print(f"\n  [{label}]")
    for line in json.dumps(output, indent=4, ensure_ascii=False, default=str).split("\n"):
        try:
            print(f"  {line}")
        except UnicodeEncodeError:
            print(f"  {line.encode('ascii', 'replace').decode()}")


# ---------------------------------------------------------------------------
# Test Runner
# ---------------------------------------------------------------------------

def run_all_scenarios():
    print("\n" + "=" * 70)
    print("  Seosan DRT 15-Scenario Integration Test (Team JSON Format)")
    print("=" * 70)

    # Load team input data
    data_path = os.path.join(PROJECT_ROOT, "data", "dispatch_input_timeline.json")
    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Setup
    adapter = DispatchIOAdapter()
    locations_dict, vehicles, events_raw = adapter.parse_input_file(data)

    depot = vehicles[0].current_location if vehicles else DEFAULT_DEPOT
    dispatcher = DynamicDRTDispatcher(vehicles=vehicles, depot=depot)

    results = []
    all_passed = True

    for raw_event in events_raw:
        seq = raw_event["seq"]
        etype = raw_event["event_type"]
        event = adapter.parse_event(raw_event)

        print(f"\n  -- Seq {seq}: {etype} --")

        result = dispatcher.process_event(event)
        results.append(result)
        output = adapter.build_output(result)
        dump_result(output, f"Seq {seq}")

        # ── Per-scenario assertions ──
        ok = True

        if seq == 1:
            ok &= check(result.status == "success", "Seq 1: First reservation success")
            ok &= check(result.vehicle_id is not None, "Seq 1: Vehicle assigned")

        elif seq == 2:
            ok &= check(result.status == "success", "Seq 2: Second reservation success")

        elif seq == 3:
            ok &= check(result.status == "success", "Seq 3: Third reservation success")
            if result.ride_share_with:
                ok &= check(True, f"Seq 3: ride_share_with = {result.ride_share_with}")

        elif seq == 4:
            ok &= check(result.status == "success", "Seq 4: Haemi -> Eumam town hall success")

        elif seq == 5:
            ok &= check(result.status == "success", "Seq 5: Eumam office -> Seosan Medical Center success")

        elif seq == 6:
            ok &= check(result.status == "success", "Seq 6: Seongyeon -> Seosan Medical Center success")

        elif seq == 7:
            ok &= check(result.status == "success", "Seq 7: Additional 10:30 reservation success")

        elif seq == 8:
            ok &= check(result.status == "success", "Seq 8: Cancellation of req_002")
            ok &= check(result.action == "cancellation", f"Seq 8: action = cancellation")
            ok &= check(result.cancelled_reservation_id == "req_002",
                        f"Seq 8: cancelled_reservation_id = {result.cancelled_reservation_id}")
            ok &= check(result.vehicle_freed is not None,
                        f"Seq 8: vehicle_freed = {result.vehicle_freed}")

        elif seq == 9:
            ok &= check(result.status == "success", "Seq 9: Fill vacant seat success")

        elif seq == 10:
            ok &= check(result.status == "failed", "Seq 10: Past-time request REJECTED")
            ok &= check(result.error_code == "DISPATCH_UNAVAILABLE",
                        f"Seq 10: error_code = DISPATCH_UNAVAILABLE (got {result.error_code})")
            ok &= check(result.alternatives is not None and len(result.alternatives) >= 1,
                        f"Seq 10: alternatives = {result.alternatives}")

        elif seq == 11:
            ok &= check(result.status == "success", "Seq 11: Time change to 11:30")
            ok &= check(result.action == "change", f"Seq 11: action = change")

        elif seq == 12:
            ok &= check(result.status == "success", "Seq 12: Long-distance reservation")

        elif seq == 13:
            ok &= check(result.status == "success", "Seq 13: Haemi -> Seosan City Hall")

        elif seq == 14:
            ok &= check(result.status == "success", "Seq 14: Haemi -> Seosan Medical Center")

        elif seq == 15:
            ok &= check(result.status == "success", "Seq 15: Imminent cancellation")
            ok &= check(result.is_imminent == True,
                        f"Seq 15: is_imminent = True (got {result.is_imminent})")
            ok &= check(result.cancelled_reservation_id == "req_004",
                        f"Seq 15: cancelled_reservation_id = {result.cancelled_reservation_id}")

        if not ok:
            all_passed = False

    # ── Summary ──
    print("\n" + "=" * 70)
    print("  Test Result Summary")
    print("=" * 70)
    for r in results:
        action_str = r.action or "?"
        print(f"  Seq {r.seq:>2} | {action_str:<16} | {r.status:<8} | {r.request_id}")

    print()
    if all_passed:
        print("  *** ALL SCENARIO CHECKS PASSED ***")
    else:
        print("  *** SOME CHECKS FAILED ***")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(run_all_scenarios())
