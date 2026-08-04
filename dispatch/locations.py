"""
dispatch/locations.py -- 정류장 좌표 <-> 한글 명칭 매핑 DB
=========================================================
팀 공유 dispatch_input_timeline.json의 locations 섹션과 동기화됩니다.
키 문자열(예: seosan_bus_terminal) <-> Location 객체 변환을 담당합니다.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .models import Location
from .stops import STOP_CATALOG, STOP_DB


# ---------------------------------------------------------------------------
# 서산시 정류장/거점 DB (행복버스/DRT 전용)
# ---------------------------------------------------------------------------

LOCATION_DB: Dict[str, Location] = {
    # ── 서산시 주요 정류장 및 거점 ──
    "seosan_city_hall": Location(
        lat=36.7845, lng=126.4501, name="서산시청"
    ),
    "seosan_bus_terminal": Location(
        lat=36.7801, lng=126.4589, name="서산버스터미널"
    ),
    "seosan_medical_center": Location(
        lat=36.7782, lng=126.4521, name="서산의료원"
    ),
    "emam_town_hall": Location(
        lat=36.7901, lng=126.4912, name="음암면 마을회관"
    ),
    "emam_office": Location(
        lat=36.7915, lng=126.4950, name="음암면 행정복지센터"
    ),
    "daesan_office": Location(
        lat=36.9387, lng=126.4392, name="대산읍 행정복지센터"
    ),
    "unsan_office": Location(
        lat=36.8123, lng=126.5410, name="운산면 행정복지센터"
    ),
    "haemi_fortress": Location(
        lat=36.7135, lng=126.5492, name="해미읍성"
    ),
    "buseok_office": Location(
        lat=36.6890, lng=126.4105, name="부석면 행정복지센터"
    ),
    "seongyeon_office": Location(
        lat=36.8320, lng=126.4420, name="성연면 행정복지센터"
    ),
    "jigok_office": Location(
        lat=36.8720, lng=126.4250, name="지곡면 행정복지센터"
    ),
    # 데모 호환용 기본 자택. 운영 환경에서는 BE의 승객별 좌표로 대체합니다.
    "home_default": Location(
        lat=36.7900, lng=126.4900, name="집"
    ),
}

# 제공된 서산 정류장 원장의 통합ID를 실제 경로 노드로 등록합니다. 동일한
# 대표명을 가진 물리 정류장은 합치지 않고 각각의 통합ID로 유지합니다.
for _stop_id, _stop in STOP_DB.items():
    if _stop.routable:
        LOCATION_DB[_stop_id] = _stop.to_location()


# 한글 명칭 -> 키 목록 역매핑. 이름 중복 시 임의의 한 정류장을 고르지 않습니다.
_NAME_TO_KEYS: Dict[str, List[str]] = {}


def _rebuild_name_index() -> None:
    _NAME_TO_KEYS.clear()
    for location_key, location in LOCATION_DB.items():
        if location.name:
            _NAME_TO_KEYS.setdefault(location.name, []).append(location_key)


_rebuild_name_index()

# 기본 Depot 위치 (서산시청)
DEFAULT_DEPOT = LOCATION_DB["seosan_city_hall"]



# ---------------------------------------------------------------------------
# 유틸리티
# ---------------------------------------------------------------------------

def get_location(key: str) -> Optional[Location]:
    """키 문자열(예: 'emam_town_hall') 또는 한글 명칭(예: '음암면 마을회관')으로 Location 조회."""
    if not key:
        return None
    normalized_key = key.strip()
    if normalized_key in LOCATION_DB:
        return LOCATION_DB[normalized_key]

    stop = STOP_CATALOG.get(normalized_key)
    if stop and stop.routable:
        return stop.to_location()

    name_keys = _NAME_TO_KEYS.get(normalized_key, [])
    if len(name_keys) == 1:
        return LOCATION_DB[name_keys[0]]

    resolution = STOP_CATALOG.resolve(normalized_key)
    if resolution.record and resolution.record.routable:
        return resolution.record.to_location()
    return None



def resolve_location_name(loc: Location) -> str:
    """
    Location 객체에서 한글 명칭을 반환한다.
    이름이 있으면 그대로 반환, 없으면 좌표 기반 최근접 매칭.
    """
    if loc.name:
        return loc.name

    best_name = ""
    best_dist = float("inf")
    for key, db_loc in LOCATION_DB.items():
        d = loc.distance_to(db_loc)
        if d < best_dist:
            best_dist = d
            best_name = db_loc.name

    if best_dist < 0.1:  # 100m 이내
        return best_name

    return f"({loc.lat:.4f}, {loc.lng:.4f})"


def resolve_location_key(loc: Location) -> str:
    """Location 객체에서 키 문자열(예: 'seosan_bus_terminal')을 반환."""
    if loc.location_id and loc.location_id in LOCATION_DB:
        return loc.location_id

    if loc.name:
        name_keys = _NAME_TO_KEYS.get(loc.name, [])
        if len(name_keys) == 1:
            return name_keys[0]

    best_key = ""
    best_dist = float("inf")
    for key, db_loc in LOCATION_DB.items():
        d = loc.distance_to(db_loc)
        if d < best_dist:
            best_dist = d
            best_key = key

    if best_dist < 0.1:
        return best_key
    return ""


def load_locations_from_json(locations_dict: Dict[str, Dict]) -> None:
    """
    dispatch_input_timeline.json의 locations 섹션을 로드하여 DB를 업데이트.
    팀원이 JSON에 정류장을 추가하면 자동 반영됩니다.
    """
    for key, loc_data in locations_dict.items():
        if key in STOP_DB:
            raise ValueError(
                f"canonical stop {key} cannot be overwritten by timeline locations"
            )
        if "lat" not in loc_data or "lng" not in loc_data:
            raise ValueError(f"location {key} requires both lat and lng")
        LOCATION_DB[key] = Location(
            lat=float(loc_data["lat"]),
            lng=float(loc_data["lng"]),
            name=loc_data.get("name", key),
            location_id=loc_data.get("location_id"),
            region_code=loc_data.get("region_code"),
        )
    # 역매핑 갱신
    _rebuild_name_index()
