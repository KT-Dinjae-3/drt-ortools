"""최신 drt-call-backend의 OR-Tools provider 계약 회귀 테스트."""

from __future__ import annotations

import copy
import unittest

from fastapi.testclient import TestClient

from dispatch import server
from dispatch.engine import DynamicDRTDispatcher
from dispatch.models import RequestStatus


BACKEND_ALLOWED_LOCATIONS = (
    "서산시청",
    "서산버스터미널",
    "서산의료원",
    "음암면 마을회관",
    "음암면 행정복지센터",
    "대산읍 행정복지센터",
    "운산면 행정복지센터",
    "해미읍성",
    "부석면 행정복지센터",
    "성연면 행정복지센터",
    "지곡면 행정복지센터",
    "집",
)


class TestBackendProviderContract(unittest.TestCase):
    """BE internal/ortoolsclient가 실제로 소비하는 wire contract를 검증합니다."""

    def setUp(self):
        self.client = TestClient(server.app)
        with server.state_lock:
            self._original_dispatcher = server.dispatcher
            self._original_seq_counter = server.seq_counter
            server.dispatcher = DynamicDRTDispatcher(
                vehicles=copy.deepcopy(server.DEFAULT_VEHICLES),
                depot=copy.deepcopy(server.seosan_depot),
            )
            server.seq_counter = 0

    def tearDown(self):
        with server.state_lock:
            server.dispatcher = self._original_dispatcher
            server.seq_counter = self._original_seq_counter
        self.client.close()

    @staticmethod
    def _reservation_payload(**overrides):
        payload = {
            "client_ref": "be-contract-call-001",
            "passenger_phone": "01012345678",
            "origin": "서산시청",
            "destination": "서산의료원",
            "requested_pickup_at": "2030-08-04T13:00:00+09:00",
            "passenger_count": 1,
            "region_code": "SEOSAN_CITY",
        }
        payload.update(overrides)
        return payload

    def _create_reservation(self, **overrides):
        return self.client.post(
            "/drt/reservations",
            json=self._reservation_payload(**overrides),
        )

    def test_availability_matches_backend_wire_contract(self):
        response = self.client.post(
            "/drt/availability/check",
            json={
                "region_code": "SEOSAN_CITY",
                "origin": "서산시청",
                "destination": "서산의료원",
                "requested_pickup_at": "2030-08-04T13:00:00+09:00",
                "passenger_count": 1,
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertIs(body["available"], True)
        self.assertRegex(body["estimated_pickup_time"], r"^\d{2}:\d{2}$")
        self.assertTrue(body["vehicle_id"])

    def test_backend_allowlist_locations_remain_backward_compatible(self):
        for location in BACKEND_ALLOWED_LOCATIONS:
            with self.subTest(location=location):
                destination = (
                    "서산의료원" if location == "서산시청" else "서산시청"
                )
                response = self.client.post(
                    "/drt/availability/check",
                    json={
                        "region_code": "SEOSAN_CITY",
                        "origin": location,
                        "destination": destination,
                        "requested_pickup_at": (
                            "2030-08-04T13:00:00+09:00"
                        ),
                        "passenger_count": 1,
                    },
                )
                self.assertEqual(
                    response.status_code,
                    200,
                    msg=f"{location}: {response.text}",
                )

    def test_create_and_cancel_match_backend_wire_contract(self):
        created = self._create_reservation()

        self.assertEqual(created.status_code, 200)
        body = created.json()
        self.assertEqual(body["status"], "success")
        self.assertTrue(body["reservation_id"])
        self.assertRegex(body["input_ref"], r"^req_[0-9]{3,}$")
        self.assertTrue(body["vehicle_id"])
        self.assertRegex(body["pickup_time"], r"^\d{2}:\d{2}$")

        request_id = body["input_ref"]
        cancelled = self.client.post(
            "/drt/reservations/cancel",
            json={
                "target_reservation_id": request_id,
                "reason": "이용자 전화 취소",
            },
        )

        self.assertEqual(cancelled.status_code, 200)
        cancel_body = cancelled.json()
        self.assertEqual(cancel_body["status"], "success")
        self.assertEqual(cancel_body["cancelled_reservation_id"], request_id)

    def test_unknown_cancel_never_falls_back_to_another_reservation(self):
        created = self._create_reservation().json()
        real_request_id = created["input_ref"]

        unknown = self.client.post(
            "/drt/reservations/cancel",
            json={"target_reservation_id": "req_999999", "reason": "잘못된 ID"},
        )

        self.assertEqual(unknown.status_code, 200)
        unknown_body = unknown.json()
        self.assertEqual(unknown_body["status"], "failed")
        self.assertEqual(unknown_body["error_code"], "REQUEST_NOT_FOUND")
        self.assertNotIn("cancelled_reservation_id", unknown_body)

        with server.state_lock:
            self.assertEqual(
                server.dispatcher.requests[real_request_id].status,
                RequestStatus.ASSIGNED,
            )

        exact = self.client.post(
            "/drt/reservations/cancel",
            json={"target_reservation_id": real_request_id},
        )
        self.assertEqual(exact.json()["cancelled_reservation_id"], real_request_id)

        repeated = self.client.post(
            "/drt/reservations/cancel",
            json={"target_reservation_id": real_request_id},
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json()["status"], "failed")
        self.assertEqual(repeated.json()["error_code"], "ALREADY_CANCELLED")

    def test_failed_create_does_not_contaminate_live_dispatch_state(self):
        failed = self._create_reservation(
            client_ref="be-contract-impossible",
            passenger_count=99,
        )

        self.assertEqual(failed.status_code, 400)
        self.assertIn("error", failed.json())
        listed = self.client.get("/drt/reservations").json()
        self.assertEqual(listed["count"], 0)

    def test_later_request_preserves_existing_vehicle_and_pickup_commitment(self):
        first = self._create_reservation().json()
        first_id = first["input_ref"]
        with server.state_lock:
            before = copy.deepcopy(server.dispatcher.requests[first_id])

        second = self._create_reservation(
            client_ref="be-contract-call-002",
            origin="서산버스터미널",
            destination="서산시청",
            requested_pickup_at="2030-08-04T13:30:00+09:00",
        )

        self.assertEqual(second.status_code, 200)
        with server.state_lock:
            after = server.dispatcher.requests[first_id]
            self.assertEqual(after.assigned_vehicle_id, before.assigned_vehicle_id)
            self.assertEqual(after.promised_pickup_time, before.promised_pickup_time)
            self.assertEqual(after.status, RequestStatus.ASSIGNED)


if __name__ == "__main__":
    unittest.main()
