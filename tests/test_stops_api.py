"""정류장 REST API와 배차 입력 통합 테스트."""

import unittest

from fastapi.testclient import TestClient

from dispatch.server import app


class TestStopAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_health_reports_loaded_stop_database(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_stops"], 589)
        self.assertEqual(data["routable_stops"], 589)
        self.assertEqual(data["stop_counts_by_region"]["SEOSAN_HAEMI"], 153)

    def test_stop_detail_and_search(self):
        detail = self.client.get("/drt/stops/ST-H-129")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["display_name"], "해미시내버스승강장")

        search = self.client.get(
            "/drt/stops",
            params={"region_code": "SEOSAN_HAEMI", "query": "해미우체국"},
        )
        self.assertEqual(search.status_code, 200)
        self.assertEqual(
            {item["stop_id"] for item in search.json()["stops"]},
            {"ST-H-130", "ST-H-131"},
        )

    def test_resolve_returns_ambiguous_candidates_for_duplicate_name(self):
        response = self.client.post(
            "/drt/stops/resolve",
            json={"query": "해미우체국", "region_code": "SEOSAN_HAEMI"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["match_status"], "ambiguous")
        self.assertEqual(
            {item["stop_id"] for item in data["candidates"]},
            {"ST-H-130", "ST-H-131"},
        )

    def test_nearest_stop_endpoint(self):
        response = self.client.get(
            "/drt/stops/nearest",
            params={
                "lat": 36.71400689,
                "lng": 126.54399541,
                "region_code": "SEOSAN_HAEMI",
                "limit": 1,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["stops"][0]["stop_id"], "ST-H-129")

    def test_availability_accepts_canonical_stop_ids(self):
        response = self.client.post(
            "/drt/availability/check",
            json={
                "region_code": "SEOSAN_HAEMI",
                "origin_stop_id": "ST-H-130",
                "destination_stop_id": "ST-H-129",
                "requested_pickup_at": "2026-08-04T13:56:00+09:00",
                "passenger_count": 2,
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["origin_stop_id"], "ST-H-130")
        self.assertEqual(data["destination_stop_id"], "ST-H-129")

    def test_create_reservation_keeps_canonical_stop_ids(self):
        response = self.client.post(
            "/drt/reservations",
            json={
                "client_ref": "stop-api-test-session",
                "passenger_phone": "010-0000-0000",
                "region_code": "SEOSAN_HAEMI",
                "origin_stop_id": "ST-H-130",
                "destination_stop_id": "ST-H-129",
                "requested_pickup_at": "2026-08-04T15:00:00+09:00",
                "passenger_count": 1,
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["origin_stop_id"], "ST-H-130")
        self.assertEqual(data["destination_stop_id"], "ST-H-129")
        self.assertEqual(data["pickup_location"], "해미우체국")
        self.assertEqual(data["dropoff_location"], "해미시내버스승강장")

    def test_ambiguous_and_unknown_stops_are_rejected_without_fallback(self):
        ambiguous = self.client.post(
            "/drt/availability/check",
            json={
                "region_code": "SEOSAN_HAEMI",
                "origin": "해미우체국",
                "destination_stop_id": "ST-H-129",
                "requested_pickup_at": "2026-08-04T13:56:00+09:00",
            },
        )
        self.assertEqual(ambiguous.status_code, 409)
        self.assertEqual(ambiguous.json()["error"]["code"], "AMBIGUOUS_STOP")

        unknown = self.client.get("/drt/stops/NOT-A-STOP")
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["error"]["code"], "UNKNOWN_STOP")


if __name__ == "__main__":
    unittest.main()
