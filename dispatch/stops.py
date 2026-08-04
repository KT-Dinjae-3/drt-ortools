"""Canonical Seosan Happy Bus stop catalog.

The optimization engine needs coordinates, while callers also need the source
registry metadata used to review duplicate stop names.  This module keeps both
concerns together without collapsing physical stops that share a display name.
The immutable ``stop_id`` (for example ``ST-H-130``) is always the primary key.
"""

from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Location


DEFAULT_STOP_DATA_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "seosan_stops.json"
)

REGION_NAMES: Dict[str, str] = {
    "SEOSAN_DAESAN": "대산",
    "SEOSAN_HAEMI": "해미",
    "SEOSAN_GOBUK": "고북",
}

_REGION_ALIASES: Dict[str, str] = {
    "대산": "SEOSAN_DAESAN",
    "대산읍": "SEOSAN_DAESAN",
    "해미": "SEOSAN_HAEMI",
    "해미면": "SEOSAN_HAEMI",
    "고북": "SEOSAN_GOBUK",
    "고북면": "SEOSAN_GOBUK",
}


def normalize_stop_name(value: str) -> str:
    """Return a conservative lookup key without guessing similar place names."""

    normalized = unicodedata.normalize("NFKC", value or "").casefold().strip()
    return re.sub(r"[\s_]+", "", normalized)


def normalize_region_code(value: Optional[str]) -> Optional[str]:
    """Normalize a region name/code; ``SEOSAN_CITY`` means all child regions."""

    if value is None or not value.strip():
        return None
    stripped = value.strip()
    upper = stripped.upper()
    if upper == "SEOSAN_CITY" or upper in REGION_NAMES:
        return upper
    return _REGION_ALIASES.get(stripped)


def _lookup_variants(value: str) -> set[str]:
    key = normalize_stop_name(value)
    if not key:
        return set()
    variants = {key}
    # Every record in this catalog is a boarding/alighting stop.  Callers often
    # append or omit "승강장", so support only that safe suffix variation.
    if key.endswith("승강장"):
        variants.add(key[: -len("승강장")])
    else:
        variants.add(f"{key}승강장")
    return variants


@dataclass(frozen=True)
class StopRecord:
    stop_id: str
    region_code: str
    region_name: str
    source_name: str
    display_name: str
    source_item_count: int
    source_type: str
    address: Optional[str]
    has_address: bool
    kakao_group: str
    source_url: str
    duplicate_review_id: Optional[str]
    review_status: str
    aliases: Tuple[str, ...]
    map_marker: Dict[str, Any]
    lat: Optional[float]
    lng: Optional[float]

    @property
    def routable(self) -> bool:
        return self.lat is not None and self.lng is not None

    def to_location(self) -> Location:
        if not self.routable:
            raise ValueError(f"stop {self.stop_id} has no routing coordinates")
        return Location(
            lat=float(self.lat),
            lng=float(self.lng),
            name=self.display_name,
            location_id=self.stop_id,
            region_code=self.region_code,
        )

    def to_dict(self, *, distance_km: Optional[float] = None) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "stop_id": self.stop_id,
            "region_code": self.region_code,
            "region_name": self.region_name,
            "source_name": self.source_name,
            "display_name": self.display_name,
            "source_item_count": self.source_item_count,
            "source_type": self.source_type,
            "address": self.address,
            "has_address": self.has_address,
            "kakao_group": self.kakao_group,
            "source_url": self.source_url,
            "duplicate_review_id": self.duplicate_review_id,
            "review_status": self.review_status,
            "aliases": list(self.aliases),
            "map_marker": dict(self.map_marker),
            "lat": self.lat,
            "lng": self.lng,
            "routable": self.routable,
        }
        if distance_km is not None:
            result["distance_km"] = round(distance_km, 3)
        return result


@dataclass(frozen=True)
class StopResolution:
    query: str
    status: str
    candidates: Tuple[StopRecord, ...] = ()
    region_code: Optional[str] = None

    @property
    def record(self) -> Optional[StopRecord]:
        return self.candidates[0] if self.status == "exact" else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "region_code": self.region_code,
            "match_status": self.status,
            "candidates": [record.to_dict() for record in self.candidates],
        }


class StopCatalog:
    """Read-only indexed view of the generated canonical stop registry."""

    def __init__(self, records: Iterable[StopRecord], metadata: Dict[str, Any]):
        self.metadata = dict(metadata)
        self.by_id: Dict[str, StopRecord] = {}
        self._source_index: Dict[str, List[str]] = {}
        self._name_index: Dict[str, List[str]] = {}

        for record in records:
            if record.stop_id in self.by_id:
                raise ValueError(f"duplicate stop ID: {record.stop_id}")
            if record.region_code not in REGION_NAMES:
                raise ValueError(
                    f"stop {record.stop_id} has unknown region {record.region_code}"
                )
            if (record.lat is None) != (record.lng is None):
                raise ValueError(
                    f"stop {record.stop_id} must have both lat and lng"
                )
            if record.routable and (
                not math.isfinite(float(record.lat))
                or not math.isfinite(float(record.lng))
                or not -90 <= float(record.lat) <= 90
                or not -180 <= float(record.lng) <= 180
            ):
                raise ValueError(
                    f"stop {record.stop_id} has invalid routing coordinates"
                )
            self.by_id[record.stop_id] = record

            source_key = normalize_stop_name(record.source_name)
            self._source_index.setdefault(source_key, []).append(record.stop_id)

            names = {record.source_name, record.display_name, *record.aliases}
            for name in names:
                for key in _lookup_variants(name):
                    ids = self._name_index.setdefault(key, [])
                    if record.stop_id not in ids:
                        ids.append(record.stop_id)

        for ids in self._source_index.values():
            ids.sort()
        for ids in self._name_index.values():
            ids.sort()

    def __len__(self) -> int:
        return len(self.by_id)

    @staticmethod
    def _filter_region(
        records: Sequence[StopRecord], region_code: Optional[str]
    ) -> List[StopRecord]:
        if region_code in (None, "SEOSAN_CITY"):
            return list(records)
        return [record for record in records if record.region_code == region_code]

    def get(self, stop_id: str) -> Optional[StopRecord]:
        return self.by_id.get((stop_id or "").strip().upper())

    def resolve(
        self, query: str, region_code: Optional[str] = None
    ) -> StopResolution:
        normalized_region = normalize_region_code(region_code)
        if region_code and normalized_region is None:
            return StopResolution(
                query=query,
                status="invalid_region",
                region_code=region_code,
            )

        by_id = self.get(query)
        if by_id is not None:
            filtered = self._filter_region([by_id], normalized_region)
            return StopResolution(
                query=query,
                status="exact" if filtered else "region_mismatch",
                candidates=tuple(filtered or [by_id]),
                region_code=normalized_region,
            )

        key = normalize_stop_name(query)
        source_records = [
            self.by_id[stop_id] for stop_id in self._source_index.get(key, [])
        ]
        source_filtered = self._filter_region(source_records, normalized_region)
        if len(source_filtered) == 1:
            return StopResolution(
                query=query,
                status="exact",
                candidates=tuple(source_filtered),
                region_code=normalized_region,
            )

        candidate_ids: List[str] = []
        for variant in _lookup_variants(query):
            for stop_id in self._name_index.get(variant, []):
                if stop_id not in candidate_ids:
                    candidate_ids.append(stop_id)
        all_candidates = [self.by_id[stop_id] for stop_id in candidate_ids]
        candidates = self._filter_region(all_candidates, normalized_region)

        if len(candidates) == 1:
            status = "exact"
        elif len(candidates) > 1:
            status = "ambiguous"
        elif all_candidates:
            status = "region_mismatch"
            candidates = all_candidates
        else:
            status = "not_found"

        return StopResolution(
            query=query,
            status=status,
            candidates=tuple(candidates),
            region_code=normalized_region,
        )

    def search(
        self,
        query: Optional[str] = None,
        region_code: Optional[str] = None,
        limit: int = 50,
    ) -> List[StopRecord]:
        normalized_region = normalize_region_code(region_code)
        if region_code and normalized_region is None:
            return []

        records = self._filter_region(
            list(self.by_id.values()), normalized_region
        )
        if query:
            key = normalize_stop_name(query)
            records = [
                record
                for record in records
                if key in normalize_stop_name(record.stop_id)
                or any(
                    key in normalize_stop_name(name)
                    for name in (
                        record.source_name,
                        record.display_name,
                        *record.aliases,
                    )
                )
            ]
        return sorted(records, key=lambda record: record.stop_id)[:limit]

    def nearest(
        self,
        lat: float,
        lng: float,
        region_code: Optional[str] = None,
        limit: int = 5,
    ) -> List[Tuple[StopRecord, float]]:
        normalized_region = normalize_region_code(region_code)
        if region_code and normalized_region is None:
            return []
        origin = Location(lat=lat, lng=lng, name="query")
        records = self._filter_region(
            list(self.by_id.values()), normalized_region
        )
        distances = [
            (record, origin.distance_to(record.to_location()))
            for record in records
            if record.routable
        ]
        distances.sort(key=lambda item: (item[1], item[0].stop_id))
        return distances[:limit]

    def region_counts(self) -> Dict[str, int]:
        return {
            code: sum(
                1 for record in self.by_id.values() if record.region_code == code
            )
            for code in REGION_NAMES
        }


def load_stop_catalog(path: Optional[Path] = None) -> StopCatalog:
    configured_path = os.environ.get("ORTOOLS_STOP_DATA_PATH")
    data_path = Path(configured_path) if configured_path else (path or DEFAULT_STOP_DATA_PATH)
    with data_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    records = [
        StopRecord(
            stop_id=item["stop_id"],
            region_code=item["region_code"],
            region_name=item["region_name"],
            source_name=item["source_name"],
            display_name=item["display_name"],
            source_item_count=int(item["source_item_count"]),
            source_type=item["source_type"],
            address=item.get("address"),
            has_address=bool(item["has_address"]),
            kakao_group=item["kakao_group"],
            source_url=item["source_url"],
            duplicate_review_id=item.get("duplicate_review_id"),
            review_status=item["review_status"],
            aliases=tuple(item.get("aliases", [])),
            map_marker=dict(item.get("map_marker", {})),
            lat=item.get("lat"),
            lng=item.get("lng"),
        )
        for item in raw["stops"]
    ]
    catalog = StopCatalog(records, raw.get("metadata", {}))

    expected_rows = catalog.metadata.get("source_rows")
    if expected_rows is not None and int(expected_rows) != len(catalog):
        raise ValueError(
            f"stop metadata expected {expected_rows} records, loaded {len(catalog)}"
        )
    expected_counts = catalog.metadata.get("region_counts")
    if expected_counts is not None and expected_counts != catalog.region_counts():
        raise ValueError(
            f"stop metadata region counts do not match loaded records: {expected_counts}"
        )
    return catalog


STOP_CATALOG = load_stop_catalog()
STOP_DB = STOP_CATALOG.by_id


__all__ = [
    "DEFAULT_STOP_DATA_PATH",
    "REGION_NAMES",
    "STOP_CATALOG",
    "STOP_DB",
    "StopCatalog",
    "StopRecord",
    "StopResolution",
    "load_stop_catalog",
    "normalize_region_code",
    "normalize_stop_name",
]
