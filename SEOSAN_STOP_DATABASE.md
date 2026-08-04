# 서산 행복버스 정류장 DB 및 API

## 적재 데이터

- 원본: `data/seosan_stops_source.tsv`
- 배차용 DB: `data/seosan_stops.json`
- 총 589개: 대산 238, 해미 153, 고북 198
- 기본키: 원본의 `통합ID`를 그대로 사용한 `stop_id`
- 좌표: 원본의 권역별 카카오맵 공개 폴더 마커를 이름으로 1:1 대조한 뒤
  WGS84 위·경도로 변환

대표명은 물리 정류장의 기본키가 아니다. 예를 들어 `해미우체국`은
`ST-H-130`, `ST-H-131` 두 정류장이므로 대표명만 들어오면 임의 선택하지
않고 후보를 반환한다.

DB 재생성:

```bash
node scripts/import_seosan_stops.mjs
```

스크립트는 원본 행 수, 통합ID 중복, 권역, 공개 마커의 1:1 대응, 좌표,
좌표 변환기 SHA-256을 검증한 뒤 JSON을 만든다.

## 정류장 API

```text
GET  /drt/stops
GET  /drt/stops/nearest
POST /drt/stops/resolve
GET  /drt/stops/{stop_id}
```

검색:

```http
GET /drt/stops?region_code=SEOSAN_HAEMI&query=해미우체국
```

이름 해석:

```json
POST /drt/stops/resolve
{
  "query": "해미우체국",
  "region_code": "SEOSAN_HAEMI"
}
```

`match_status`는 `exact`, `ambiguous`, `not_found`,
`region_mismatch` 중 하나다. `ambiguous`이면 BE 또는 AI가
`candidates`를 이용해 사용자에게 어느 정류장인지 다시 확인해야 한다.

현재 좌표에서 가까운 정류장:

```http
GET /drt/stops/nearest?lat=36.7140&lng=126.5440&region_code=SEOSAN_HAEMI&limit=3
```

## 배차 API 연결

기존 `origin`, `destination` 문자열도 정확히 하나로 해석되는 동안
지원한다. 운영 연동에서는 중복을 피하기 위해 통합ID 필드를 권장한다.

```json
POST /drt/availability/check
{
  "region_code": "SEOSAN_HAEMI",
  "origin_stop_id": "ST-H-130",
  "destination_stop_id": "ST-H-129",
  "requested_pickup_at": "2026-08-04T13:56:00+09:00",
  "passenger_count": 2
}
```

예약 생성도 같은 정류장 필드를 사용한다.

```json
POST /drt/reservations
{
  "client_ref": "call-session-001",
  "passenger_phone": "010-0000-0000",
  "region_code": "SEOSAN_HAEMI",
  "origin_stop_id": "ST-H-130",
  "destination_stop_id": "ST-H-129",
  "requested_pickup_at": "2026-08-04T13:56:00+09:00",
  "passenger_count": 2
}
```

위치 입력 오류는 임의 좌표나 `(0, 0)`으로 바꾸지 않고 다음 코드로
거절한다.

- `UNKNOWN_STOP`
- `AMBIGUOUS_STOP`
- `REGION_MISMATCH`
- `INVALID_REGION`
- `MISSING_STOP`
- `CONFLICTING_STOP_REFERENCE`
- `STOP_COORDINATES_MISSING`

## 발표 시나리오 관련 주의

- `해미시내버스승강장`: `ST-H-129`
- `해미우체국`: `ST-H-130`, `ST-H-131` 두 후보
