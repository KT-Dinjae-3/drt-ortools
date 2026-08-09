"""최신 drt-call-backend의 OR-Tools provider 계약 회귀 테스트."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from dispatch import server


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

    def tearDown(self):
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
            "operation_id": "be-contract-call-001:create",
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

    def test_create_is_deterministic_and_keeps_no_server_reservation(self):
        created = self._create_reservation()

        self.assertEqual(created.status_code, 200)
        body = created.json()
        self.assertEqual(body["status"], "success")
        self.assertTrue(body["reservation_id"])
        self.assertEqual(body["input_ref"], "be-contract-call-001:create")
        self.assertTrue(body["plan_id"])
        self.assertTrue(body["vehicle_id"])
        self.assertRegex(body["pickup_time"], r"^\d{2}:\d{2}$")

        repeated = self._create_reservation().json()
        self.assertEqual(repeated["plan_id"], body["plan_id"])
        self.assertEqual(self.client.get("/drt/reservations").json()["count"], 0)

    def test_cancel_declares_backend_ownership(self):
        response = self.client.post(
            "/drt/reservations/cancel",
            json={"target_reservation_id": "call:create", "reason": "이용자 요청"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(response.json()["error_code"], "STATELESS_CANCEL_OWNED_BY_BACKEND")

    def test_failed_create_does_not_contaminate_live_dispatch_state(self):
        failed = self._create_reservation(
            client_ref="be-contract-impossible",
            passenger_count=99,
        )

        self.assertEqual(failed.status_code, 400)
        self.assertIn("error", failed.json())
        listed = self.client.get("/drt/reservations").json()
        self.assertEqual(listed["count"], 0)

    def test_later_request_replays_backend_active_reservations(self):
        first = self._create_reservation().json()
        active = [{
            "request_id": first["input_ref"],
            "passenger_phone": "01012345678",
            "origin_stop_id": "ST-H-130",
            "destination_stop_id": "ST-H-129",
            "requested_pickup_at": "2030-08-04T13:00:00+09:00",
            "passenger_count": 1,
        }]

        second = self._create_reservation(
            client_ref="be-contract-call-002",
            operation_id="be-contract-call-002:create",
            origin="서산버스터미널",
            destination="서산시청",
            requested_pickup_at="2030-08-04T13:30:00+09:00",
            active_reservations=active,
        )

        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(second.json()["plan_id"], first["plan_id"])
        self.assertEqual(self.client.get("/drt/reservations").json()["count"], 0)


if __name__ == "__main__":
    unittest.main()
