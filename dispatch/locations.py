"""
dispatch/locations.py -- 정류장 좌표 <-> 한글 명칭 매핑 DB
=========================================================
팀 공유 dispatch_input_timeline.json의 locations 섹션과 동기화됩니다.
키 문자열(예: jongno3ga_station) <-> Location 객체 변환을 담당합니다.
"""

from __future__ import annotations

from typing import Dict, Optional

from .models import Location


# ---------------------------------------------------------------------------
# 서산시 정류장/거점 DB (행복택시/DRT 전용) & 종로구 호환 DB
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
    "home_default": Location(
        lat=36.7900, lng=126.4900, name="집"
    ),

    # ── 레거시 종로구 정류장 DB (호환용) ──
    "jongno_gu_office": Location(
        lat=37.5730, lng=126.9794, name="종로구청"
    ),
    "jongno_senior_center_main": Location(
        lat=37.5824, lng=127.0024, name="종로노인종합복지관 본관"
    ),
    "seoul_senior_welfare_center": Location(
        lat=37.5705, lng=126.9849, name="서울노인복지센터"
    ),
    "hyehwa_station": Location(
        lat=37.5824, lng=127.0019, name="혜화역"
    ),
    "jongno3ga_station": Location(
        lat=37.5703, lng=126.9919, name="종로3가역"
    ),
    "seoul_univ_hospital": Location(
        lat=37.5797, lng=126.9966, name="서울대학교병원"
    ),
    "kyunghee_oriental_hospital": Location(
        lat=37.5926, lng=127.0517, name="경희대학교 한방병원"
    ),
    "tapgol_park": Location(
        lat=37.5712, lng=126.9883, name="탑골공원"
    ),
    "dongmyo_station": Location(
        lat=37.5726, lng=127.0166, name="동묘앞역"
    ),
    "gyeongbokgung_station": Location(
        lat=37.5759, lng=126.9733, name="경복궁역"
    ),
}

# 한글 명칭 -> 키 역매핑
_NAME_TO_KEY: Dict[str, str] = {loc.name: key for key, loc in LOCATION_DB.items()}

# 기본 Depot 위치 (서산시청)
DEFAULT_DEPOT = LOCATION_DB["seosan_city_hall"]



# ---------------------------------------------------------------------------
# 유틸리티
# ---------------------------------------------------------------------------

def get_location(key: str) -> Optional[Location]:
    """키 문자열(예: 'emam_town_hall') 또는 한글 명칭(예: '음암면 마을회관')으로 Location 조회."""
    if not key:
        return None
    if key in LOCATION_DB:
        return LOCATION_DB[key]
    if key in _NAME_TO_KEY:
        return LOCATION_DB[_NAME_TO_KEY[key]]
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
    """Location 객체에서 키 문자열(예: 'jongno3ga_station')을 반환."""
    if loc.name and loc.name in _NAME_TO_KEY:
        return _NAME_TO_KEY[loc.name]

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
        LOCATION_DB[key] = Location(
            lat=loc_data.get("lat", 0.0),
            lng=loc_data.get("lng", 0.0),
            name=loc_data.get("name", key),
        )
    # 역매핑 갱신
    _NAME_TO_KEY.clear()
    _NAME_TO_KEY.update({loc.name: key for key, loc in LOCATION_DB.items()})
