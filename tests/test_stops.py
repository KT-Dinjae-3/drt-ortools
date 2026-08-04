"""서산 행복버스 정류장 원장과 위치 해석 회귀 테스트."""

import unittest

from dispatch.io_adapter import DispatchIOAdapter
from dispatch.locations import LOCATION_DB, get_location
from dispatch.models import Location
from dispatch.stops import STOP_CATALOG


class TestStopCatalog(unittest.TestCase):
    def test_catalog_contains_all_source_stops(self):
        self.assertEqual(len(STOP_CATALOG), 589)
        self.assertEqual(
            STOP_CATALOG.region_counts(),
            {
                "SEOSAN_DAESAN": 238,
                "SEOSAN_HAEMI": 153,
                "SEOSAN_GOBUK": 198,
            },
        )
        self.assertEqual(len(set(STOP_CATALOG.by_id)), 589)
        self.assertTrue(
            all(record.routable for record in STOP_CATALOG.by_id.values())
        )

    def test_canonical_stop_id_preserves_coordinates_and_region(self):
        record = STOP_CATALOG.get("ST-H-129")
        self.assertIsNotNone(record)
        self.assertEqual(record.display_name, "해미시내버스승강장")
        self.assertEqual(record.region_code, "SEOSAN_HAEMI")
        self.assertAlmostEqual(record.lat, 36.71400689)
        self.assertAlmostEqual(record.lng, 126.54399541)

        location = get_location("ST-H-129")
        self.assertIsNotNone(location)
        self.assertEqual(location.location_id, "ST-H-129")
        self.assertEqual(location.region_code, "SEOSAN_HAEMI")
        self.assertEqual(LOCATION_DB["ST-H-129"], location)

    def test_duplicate_display_name_never_selects_arbitrary_stop(self):
        resolution = STOP_CATALOG.resolve("해미우체국", "SEOSAN_HAEMI")
        self.assertEqual(resolution.status, "ambiguous")
        self.assertEqual(
            {record.stop_id for record in resolution.candidates},
            {"ST-H-130", "ST-H-131"},
        )
        self.assertIsNone(get_location("해미우체국"))

    def test_original_name_resolves_one_physical_stop(self):
        resolution = STOP_CATALOG.resolve("해미우체국_3", "SEOSAN_HAEMI")
        self.assertEqual(resolution.status, "exact")
        self.assertIsNotNone(resolution.record)
        self.assertEqual(resolution.record.stop_id, "ST-H-130")

    def test_region_mismatch_is_explicit(self):
        resolution = STOP_CATALOG.resolve("ST-H-130", "SEOSAN_DAESAN")
        self.assertEqual(resolution.status, "region_mismatch")
        self.assertEqual(resolution.candidates[0].region_code, "SEOSAN_HAEMI")

    def test_similar_village_name_is_not_silently_corrected(self):
        # 원장에는 '신장2리'가 없고 '신상2리'만 있어 확인 없이 바꾸지 않습니다.
        resolution = STOP_CATALOG.resolve("신장2리 마을회관")
        self.assertEqual(resolution.status, "not_found")
        self.assertEqual(resolution.candidates, ())

    def test_search_and_nearest_keep_canonical_ids(self):
        matches = STOP_CATALOG.search("해미우체국", "SEOSAN_HAEMI")
        self.assertEqual(
            {record.stop_id for record in matches},
            {"ST-H-130", "ST-H-131"},
        )
        nearest = STOP_CATALOG.nearest(
            36.71400689,
            126.54399541,
            "SEOSAN_HAEMI",
            limit=1,
        )
        self.assertEqual(nearest[0][0].stop_id, "ST-H-129")
        self.assertAlmostEqual(nearest[0][1], 0.0)

    def test_location_dict_requires_real_coordinates(self):
        with self.assertRaisesRegex(ValueError, "requires both lat and lng"):
            Location.from_dict({"name": "좌표 없는 위치"})

    def test_io_adapter_rejects_unknown_location_instead_of_zero_coordinate(self):
        adapter = DispatchIOAdapter()
        with self.assertRaisesRegex(ValueError, "unknown pickup location"):
            adapter.parse_payload_locations(
                {
                    "pickup": "등록되지 않은 장소",
                    "dropoff": "ST-H-129",
                    "requested_pickup_time": "13:56",
                    "passenger_count": 2,
                }
            )


if __name__ == "__main__":
    unittest.main()
