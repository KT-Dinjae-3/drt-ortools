# drt-call-backend ↔ OR-Tools 연동 협의서 (BE 우선)

> 검토 기준: `KT-Dinjae-3/drt-call-backend`의 최신 `origin/main@2169e7b`
>
> 검토일: 2026-08-04
>
> 수정 범위: `LEG/ORtools`만. BE, AI, `deploy/k3s/ortools.yaml`은 수정하지 않는다.

## 1. 결론

OR-Tools가 맞춰야 할 계약은 BE가 외부로 노출하는 Mock DRT API가 아니라
`internal/ortoolsclient/client.go`의 provider 계약이다.

최신 BE에는 OR-Tools 전용 HTTP client와 adapter가 이미 구현되어 있다. 따라서
현재 OR-Tools의 평면형 JSON 계약을 유지해야 한다. BE의 다른 API에서 사용하는
중첩 `vehicle` 객체나 BE 전체 예약 객체를 OR 응답에 강제로 적용하면 9002 연동이
깨진다.

현재 역할은 다음과 같다.

- AI는 사용자와 대화하며 출발지, 목적지, 희망 시각, 인원 등을 수집한다.
- BE는 통화 상태, 사용자용 예약번호, PostgreSQL 예약 원본, 멱등성 및 OR 호출을
  소유한다.
- OR-Tools는 차량·경로 상태와 배차 계산을 소유하고 provider용 ID를 반환한다.
- AI가 OR-Tools를 직접 호출하지 않는다.
- BE의 예약 조회는 OR 목록 API가 아니라 BE 데이터베이스에서 수행한다.

## 2. 실제 호출 구조

```text
AI module
  │ 대화 분석/음성 응답
  ▼
drt-call-backend voice-session-server
  │ ortoolsAdapter
  │ internal/ortoolsclient
  │ ORTOOLS_BASE_URL / ORTOOLS_TIMEOUT
  ▼
Tailscale 내부망
  ▼
OR-Tools HTTP service :8092
  ├─ GET  /health
  ├─ POST /drt/availability/check
  ├─ POST /drt/reservations
  └─ POST /drt/reservations/cancel
```

최신 BE에서 OR 경로는 현재 K3s 내선 `9002`의 `dispatch=ortools` canary에만
연결되어 있다. 내선 `9000`은 동일한 경로가 아니다.

## 3. 시스템별 소유권

| 항목 | AI | BE | OR-Tools |
|---|---|---|---|
| 사용자 발화·슬롯 수집 | 소유 | 수신·검증 | 미관여 |
| 통화 세션·도구 실행 상태 | 보조 | 소유 | 미관여 |
| 사용자용 예약번호 | 읽어서 안내 | 소유 | 외부 provider ID만 생성 |
| 예약 원본·조회·취소 상태 | 미관여 | PostgreSQL 원본 | 배차 요청 상태 참조 |
| 외부 호출 멱등성 | 미관여 | 소유 | `client_ref` 멱등성은 아직 없음 |
| 차량·경로·시간창·정원 | 미관여 | 결과 사용 | 소유 |
| 정류장 원장·좌표·통합ID | 이름 수집 | 조회·보존 필요 | 현재 원장 소유 |
| 자유문장 POI 지오코딩 | 표현 수집 | 지도/정책 계층 소유 권장 | 미관여 |
| Tailscale URL·timeout·배포 | 미관여 | 설정 소유 | 서비스 제공 |

## 4. 최신 BE가 실제로 호출하는 API

### 4.1 Health

`GET /health`

BE는 응답 본문의 상세 필드를 해석하지 않고 HTTP `2xx` 여부만 확인한다.

현재 OR 응답 예:

```json
{
  "status": "ok",
  "service": "seosan-drt-ortools",
  "active_vehicles": 3,
  "total_stops": 589
}
```

### 4.2 Availability

`POST /drt/availability/check`

최신 BE 요청:

```json
{
  "region_code": "SEOSAN_CITY",
  "origin": "서산시청",
  "destination": "서산의료원",
  "requested_pickup_at": "2026-08-04T13:56:00+09:00",
  "passenger_count": 1
}
```

가용 시 필수 응답:

```json
{
  "status": "success",
  "available": true,
  "estimated_pickup_time": "14:01",
  "vehicle_id": "DRT-SS-01"
}
```

`estimated_pickup_time`은 `HH:MM` 또는 RFC 3339가 가능하다. `available=true`이면
`estimated_pickup_time`과 `vehicle_id`가 모두 있어야 한다.

배차 불가 표현은 두 가지를 BE가 처리할 수 있다.

```json
{
  "status": "success",
  "available": false,
  "reason_code": "NO_VEHICLE_AVAILABLE",
  "reason": "배차 가능한 차량이 없습니다."
}
```

또는 오류 코드가 포함된 HTTP `4xx` 응답:

```json
{
  "error": {
    "code": "CAPACITY_EXCEEDED",
    "message": "이용 가능한 차량이 없습니다."
  }
}
```

Availability는 차량을 홀드하지 않는다. 현재 OR 구현도 최근접 활성 차량과 좌석을
기준으로 예상 시각을 계산하는 사전 확인이므로, 예약 생성 결과와 항상 동일하다고
보장하지 않는다.

### 4.3 예약 생성

`POST /drt/reservations`

최신 BE 요청:

```json
{
  "client_ref": "call-session-id",
  "passenger_phone": "01012345678",
  "origin": "서산시청",
  "destination": "서산의료원",
  "requested_pickup_at": "2026-08-04T13:56:00+09:00",
  "passenger_count": 1,
  "region_code": "SEOSAN_CITY"
}
```

BE가 검사하는 성공 응답:

```json
{
  "status": "success",
  "reservation_id": "R-20260804-0001",
  "input_ref": "req_001",
  "vehicle_id": "DRT-SS-01",
  "pickup_time": "14:01",
  "pickup_location": "서산시청",
  "dropoff_location": "서산의료원"
}
```

필수 조건:

- HTTP 상태는 `2xx`여야 한다.
- `status`는 정확히 `success`여야 한다.
- `reservation_id`, `vehicle_id`, `pickup_time`은 비어 있으면 안 된다.
- `input_ref`는 정규식 `^req_[0-9]{3,}$`를 만족해야 한다.
- `pickup_location`과 `dropoff_location`은 선택 필드이며, 없으면 BE가 요청값을
  사용한다.
- 정류장 ID, 권역 등 추가 필드는 BE가 무시하므로 하위 호환 방식으로 제공할 수 있다.

`client_ref`는 현재 OR에서 멱등 키로 보장되지 않는다. 최신 BE도 이 때문에 예약 생성
HTTP 요청을 자동 재시도하지 않는다.

### 4.4 예약 취소

`POST /drt/reservations/cancel`

BE는 사용자용 예약번호가 아니라 OR 예약 생성 응답의 `input_ref`를 보낸다.

```json
{
  "target_reservation_id": "req_001",
  "reason": "이용자 전화 취소"
}
```

성공 응답:

```json
{
  "status": "success",
  "action": "cancellation",
  "cancelled_reservation_id": "req_001"
}
```

`cancelled_reservation_id` 또는 `input_ref` 중 하나가 요청한 `req_001`과 정확히
같아야 한다. 존재하지 않는 ID를 부분 일치시키거나 마지막 예약으로 대체하면 안 된다.
이미 취소된 요청도 다시 성공으로 처리하지 않고 `ALREADY_CANCELLED`로 실패시킨다.

### 4.5 BE가 OR에 호출하지 않는 API

현재 최신 BE adapter는 다음 OR API를 호출하지 않는다.

- `GET /drt/reservations`
- `PATCH /drt/reservations/{reservation_id}/cancel`
- `GET /drt/regions`
- 정류장 검색·상세·가까운 정류장 API

이 API들은 운영·디버깅 및 향후 정류장 연동용 추가 기능이다. 현재 9002 계약을
바꾸는 근거로 사용하지 않는다.

## 5. 예약 ID 매핑

이름이 비슷해도 세 ID의 소유권은 다르다.

| BE 저장 필드 | 값 예 | 의미 |
|---|---|---|
| `reservation_id` | `R-20260804-0007` | BE가 만든 사용자용 canonical 예약번호 |
| `dispatch_request_id` | `req_001` | OR `input_ref`. 취소 호출 대상 |
| `dispatch_reservation_id` | `R-20260804-0001` | OR `reservation_id`. 외부 provider 결과 ID |

현재 OR의 외부 ID도 `R-...`처럼 보이지만 BE canonical 예약번호와 같은 ID가 아니다.
BE는 둘을 별도 컬럼으로 저장한다. OR 응답의 `reservation_id`는 opaque provider ID로
취급해야 한다.

## 6. 서산 정류장 DB 연동

OR-Tools 내부 canonical 정류장 원장은 다음 파일로 관리한다.

- 원본 정리 데이터: `data/seosan_stops_source.tsv`
- 런타임 DB: `data/seosan_stops.json`
- 상세 설명: `SEOSAN_STOP_DATABASE.md`

현재 등록 수:

| 권역 | 코드 | 수 |
|---|---|---:|
| 대산 | `SEOSAN_DAESAN` | 238 |
| 해미 | `SEOSAN_HAEMI` | 153 |
| 고북 | `SEOSAN_GOBUK` | 198 |
| 합계 | 전체 조회 `SEOSAN_CITY` | 589 |

물리 정류장 식별자는 `stop_id`다. 표시명은 중복될 수 있으므로 식별자로 사용하지
않는다. 예를 들어 `해미우체국`은 복수 물리 정류장 후보가 있을 수 있고,
`해미시내버스승강장`의 통합ID는 `ST-H-129`다.

현재 OR는 기존 BE 요청을 깨지 않기 위해 `origin`, `destination` 문자열을 계속
받는다. 동시에 다음 additive 필드를 지원한다.

- `origin_stop_id`
- `destination_stop_id`
- `region_code`
- 응답의 정류장 통합ID

정류장용 추가 API:

| 메서드 | 경로 | 용도 |
|---|---|---|
| `GET` | `/drt/stops` | 권역·이름 검색 |
| `POST` | `/drt/stops/resolve` | 이름/통합ID 해석과 중복 후보 반환 |
| `GET` | `/drt/stops/nearest` | 이미 확보한 좌표에서 가까운 정류장 검색 |
| `GET` | `/drt/stops/{stop_id}` | 정류장 상세 |

`/drt/stops/resolve`의 `ambiguous`는 임의로 하나를 고르지 않고 사용자에게 재확인해야
한다. `/drt/stops/nearest`는 지오코딩 API가 아니다. “해미읍성 남문” 같은 POI를
위·경도로 바꾸는 책임은 BE 또는 지도 서비스에 있다.

## 7. 이번 OR-Tools 쪽 반영 사항

최신 BE 계약을 유지하면서 다음 안전성만 보완한다.

- 신규 예약·취소를 lock으로 직렬화한다.
- solver는 복제 상태에서 실행하고 성공한 경우에만 전역 상태에 반영한다.
- 실패한 신규 요청이 이후 예약 목록과 solver 입력을 오염시키지 않는다.
- 취소는 정확한 `req_NNN`만 허용한다.
- 미존재 ID를 마지막 요청으로 바꾸던 fallback을 제거한다.
- 반복 취소는 `ALREADY_CANCELLED`로 실패한다.
- 후속 최적화에서 기존 예약의 차량과 약속한 픽업 시각을 고정한다.
- 후속 요청은 거절할 수 있지만 이미 확정한 예약은 solver가 조용히 drop하지 못한다.
- 정류장 ID 필드는 기존 평면형 문자열 계약에 추가하는 방식으로 유지한다.

## 8. 현재 BE에서 남은 변경

OR만 수정해서는 발표 시나리오 전체가 동작하지 않는다. 다음 항목은 최신 BE가 OR을
호출하기 전에 막는 부분이다.

### 8.1 12개 장소 allowlist

`cmd/voice-session-server/ortools_adapter.go`의 `ortoolsLocations`가 현재 12개
문자열만 허용한다. 다음 발표 장소는 그대로는 OR까지 도달하지 않는다.

- 해미읍성 남문
- 해미우체국 승강장
- 신장2리 마을회관
- 해미 시내버스 승강장

589개 정류장을 사용하려면 BE가 고정 allowlist를 확장하는 대신 OR의 stop resolve
API 또는 공유 정류장 원장을 사용하도록 바꾸는 것이 안전하다. 전환 중에는 기존
12개 문자열 요청도 계속 지원해야 한다.

### 8.2 탑승 인원 하드코딩

최신 BE의 OR adapter는 Availability와 Create 모두 `passenger_count: 1`을 보낸다.
AI가 2명을 인식해도 현재 BE 예약 요청 모델과 adapter를 거치는 동안 값이 전달되지
않는다. 발표의 “2명”을 반영하려면 AI 슬롯 → BE dialog 요청 → OR client까지 인원
필드를 연결해야 한다.

### 8.3 POI·권역 밖·환승 정책

다음 판단은 단순 VRP 문제가 아니다.

- “해미읍성 남문”에서 가까운 지정 승강장 찾기
- 목적지가 운행 권역 밖인지 판단하기
- 환승 지점을 선택하고 시내버스 정보를 안내하기

BE의 지도/운영 정책 계층과 OR 정류장 검색을 조합해야 한다. OR는 좌표가 확정된 뒤
가까운 정류장을 계산하고, 확정된 승·하차 정류장 사이의 차량 경로를 최적화한다.

### 8.4 실행 경로와 저장소

- OR canary는 현재 내선 `9002`다.
- PostgreSQL과 외부 dispatch ID 저장이 정상이어야 한다.
- OR 장애 시 9002는 Mock 예약으로 조용히 성공시키지 않고 fail-closed한다.
- BE가 OR 예약 생성 응답을 받지 못한 경우 안전한 자동 재시도 수단은 아직 없다.

## 9. OR-Tools에 남은 운영 과제

이번 계약 정합화와 별개로 실제 운영 전 결정할 항목이다.

- 프로세스 재시작 후에도 예약·차량 상태를 복구할 영속 저장소
- `client_ref` 기반 멱등 예약 또는 생성 결과 조회 API
- Availability와 실제 solver 예약 결과의 일치 수준
- 실시간 차량 위치·운행 중 승객·차량 비활성 상태 입력
- 운영시간, 권역 간 이동, 휠체어 차량, 최대 대기시간 정책
- timeout, solver 실패, 비정상 입력에 대한 관측 지표와 알림
- Tailnet ACL 또는 서비스 인증 방식

## 10. 배포 설정 경계

최신 BE가 읽는 설정:

- `ORTOOLS_BASE_URL`
- `ORTOOLS_TIMEOUT`

`DRT_SERVICE_URL`과 `MOCK_DRT`는 최신 OR adapter 설정이 아니다.

OR 저장소의 `deploy/k3s/ortools.yaml`은 BE 팀이 관리하는 배포 설정이므로 이번
작업에서 수정하지 않는다. Tailscale 주소, 포트 노출, canary 내선, timeout, Secret과
PostgreSQL 설정도 BE 배포 기준으로 확정한다.

예시:

```text
ORTOOLS_BASE_URL=http://<KT_GROUP3_TAILSCALE_IP>:8092
ORTOOLS_TIMEOUT=5s
```

실제 URL은 BE pod에서 `GET /health`가 되는 주소를 사용해야 한다.

## 11. BE 팀 전달 체크리스트

- [ ] 9002에서 `dispatch=ortools` 경로를 사용한다.
- [ ] `ORTOOLS_BASE_URL`과 `ORTOOLS_TIMEOUT`을 설정한다.
- [ ] BE pod에서 OR `/health`를 호출해 `2xx`를 확인한다.
- [ ] 기존 평면형 `origin`/`destination` 계약을 유지한다.
- [ ] 589개 정류장 사용 시 고정 allowlist를 stop resolve 연동으로 교체한다.
- [ ] `origin_stop_id`와 `destination_stop_id`를 예약 데이터에 보존한다.
- [ ] AI가 수집한 `passenger_count`를 OR까지 전달한다.
- [ ] 사용자용 BE 예약 ID와 OR의 두 provider ID를 별도로 저장한다.
- [ ] 취소 시 `dispatch_request_id=req_NNN`을 보낸다.
- [ ] 발표 장소와 2명 요청을 실제 9002 E2E로 검증한다.

## 12. 최소 연동 확인 예시

```bash
curl -sS "http://<OR_TAILSCALE_HOST>:8092/health"

curl -sS -X POST "http://<OR_TAILSCALE_HOST>:8092/drt/availability/check" \
  -H "Content-Type: application/json" \
  -d '{
    "region_code": "SEOSAN_CITY",
    "origin": "서산시청",
    "destination": "서산의료원",
    "requested_pickup_at": "2026-08-04T13:56:00+09:00",
    "passenger_count": 1
  }'

curl -sS -X POST "http://<OR_TAILSCALE_HOST>:8092/drt/reservations" \
  -H "Content-Type: application/json" \
  -d '{
    "client_ref": "manual-contract-check",
    "passenger_phone": "01012345678",
    "origin": "서산시청",
    "destination": "서산의료원",
    "requested_pickup_at": "2026-08-04T13:56:00+09:00",
    "passenger_count": 1,
    "region_code": "SEOSAN_CITY"
  }'
```

예약 생성 응답의 `input_ref`를 취소 요청의 `target_reservation_id`로 그대로 사용한다.

## 13. 발표 시나리오 판단

현재 코드만으로는 발표 시나리오 전체가 그대로 이어지지 않는다.

1. AI가 장소와 인원을 수집하는 대화는 가능하다.
2. “해미읍성 남문 → 해미우체국 승강장”은 POI 좌표와 nearest-stop 연동이 필요하다.
3. “신장2리 권역 밖 → 환승 지점”은 BE 운영 정책과 환승 데이터가 필요하다.
4. 현재 BE allowlist는 발표 장소를 OR 호출 전에 거절한다.
5. 현재 BE는 2명을 1명으로 바꿔 OR에 보낸다.
6. 출발·도착 정류장과 인원이 확정되어 OR까지 도달하면 Availability, 예약 생성,
   차량 선택, 픽업 시각 반환은 현재 계약으로 수행할 수 있다.

따라서 OR 계약을 BE 전체 예약 형식으로 바꾸는 것이 해결책은 아니다. OR의 현재
provider 계약을 유지하고, BE의 장소 해석·인원 전달·운영 정책 연결을 보완하는 것이
필요하다.
