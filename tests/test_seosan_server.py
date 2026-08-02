"""
tests/test_seosan_server.py -- 서산시 행복택시 OR-Tools REST API 서버 검증 테스트
================================================================================
실행방법:
    cd /home/jovyan/LEG/ORtools
    python -m pytest tests/test_seosan_server.py -v
    또는
    python tests/test_seosan_server.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

# ORtools 패키지 경로 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

from dispatch.server import app


class TestSeosanDRTServer(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_01_health_check(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["service"], "seosan-drt-ortools")
        self.assertGreaterEqual(data["active_vehicles"], 1)

    def test_02_get_regions(self):
        response = self.client.get("/drt/regions")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("regions", data)
        region_codes = [r["region_code"] for r in data["regions"]]
        self.assertIn("SEOSAN_CITY", region_codes)

    def test_03_availability_check_seosan(self):
        payload = {
            "region_code": "SEOSAN_CITY",
            "origin": "음암면 마을회관",
            "destination": "서산의료원",
            "requested_pickup_at": "2026-07-28T14:00:00+09:00",
            "passenger_count": 1,
        }
        response = self.client.post("/drt/availability/check", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertTrue(data["available"])
        self.assertIn("estimated_pickup_time", data)
        self.assertIn("vehicle_id", data)

    def test_04_create_reservation_seosan(self):
        payload = {
            "client_ref": "sess_seosan_001",
            "passenger_phone": "010-1234-5678",
            "origin": "음암면 마을회관",
            "destination": "서산의료원",
            "requested_pickup_at": "2026-07-28T14:00:00+09:00",
            "passenger_count": 1,
            "region_code": "SEOSAN_CITY",
        }
        response = self.client.post("/drt/reservations", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["action"], "new_reservation")
        self.assertIn("reservation_id", data)
        self.assertIn("vehicle_id", data)
        self.assertEqual(data["pickup_location"], "음암면 마을회관")
        self.assertEqual(data["dropoff_location"], "서산의료원")

    def test_05_list_reservations(self):
        response = self.client.get("/drt/reservations")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("reservations", data)
        self.assertGreaterEqual(data["count"], 1)

    def test_06_cancel_reservation(self):
        # 1) 현재 활성화된 예약 ID 조회
        list_res = self.client.get("/drt/reservations").json()
        self.assertGreaterEqual(list_res["count"], 1)
        target_id = list_res["reservations"][0]["request_id"]

        payload = {
            "target_reservation_id": target_id,
            "reason": "승객 일정 변경",
        }
        response = self.client.post("/drt/reservations/cancel", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["action"], "cancellation")



if __name__ == "__main__":
    unittest.main()
