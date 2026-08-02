# OR-Tools 배차 서비스 ↔ BE 연동 협의안

## 1. 문서 목적

이 문서는 OR-Tools 기반 DRT 배차 서비스와 원격 BE(Voice Session Server 포함)를
Tailscale 네트워크로 연동하기 전에 양 팀이 합의해야 할 API 계약과 책임 범위를
정리한 자료입니다.

현재 OR-Tools 서비스는 B200 서버에서 FastAPI로 실행하며, 원격 BE가 Tailscale
주소를 통해 호출하는 구성을 전제로 합니다.

---

## 2. 제안 아키텍처

```text
사용자 전화
  → 원격 BE / Voice Session Server
      - 통화 세션 관리
      - 사용자 정보 및 대화 상태 관리
      - 예약 데이터 영속화
      - OR-Tools API 호출 및 재시도
          ↓ Tailscale HTTP
      OR-Tools 배차 서비스
      - 차량 및 운행 경로 상태 관리
      - 시간창·정원·승하차 순서 제약 검증
      - 최적 차량 및 경로 결정
      - 배차 결과 반환
```

### 권장 책임 분리

| 구분 | BE | OR-Tools |
|---|---|---|
| 전화 및 대화 세션 | 소유 | 미관여 |
| 승객·전화번호 등 개인정보 | 소유 | 배차에 필요한 최소 정보만 사용 |
| 예약 업무 상태 | 원본(Source of Truth) | 배차 상태 참조 |
| 예약 ID | 생성·소유 권장 | 요청과 연결해 보관 |
| 차량·경로 최적화 상태 | 조회·표시 | 소유 |
| 배차 가능성 검증 | 요청 | 계산 |
| 차량 선택 및 경로 생성 | 결과 사용 | 수행 |
| 요청 재시도 | 수행 | 멱등 처리 |
| 예약·통화 DB | 소유 | 필요 시 배차 상태 스냅샷 보관 |

---

## 3. OR-Tools 담당 범위

OR-Tools 측에서 독립적으로 구현할 수 있는 항목입니다.

- 차량 정원 제약
- 픽업 후 하차 순서 제약
- 픽업 시간창 및 서비스 운영시간 검증
- 기존 승객의 최대 우회시간·지연 한도 검증
- 신규 요청의 최적 차량 및 경로 삽입 위치 계산
- 동시 배차 요청에 대한 상태 잠금 또는 직렬화
- 알 수 없는 위치 및 잘못된 입력 거절
- 존재하지 않는 예약에 대한 안전한 취소 실패 처리
- 배차 알고리즘 단위·통합·동시성 테스트
- 구조화된 오류 코드와 계산 근거 반환

---

## 4. BE와 반드시 합의할 사항

### 4.1 ID 소유권

결정이 필요한 ID:

- `reservation_id`: 사용자에게 안내하고 BE DB에 저장하는 업무 예약 ID
- `dispatch_id`: OR-Tools 내부 배차·경로 ID
- `client_ref`: BE 요청의 멱등성 키
- `session_id`: 전화 통화 세션 ID

권장안:

- BE가 `reservation_id`를 생성하고 원본으로 관리합니다.
- OR-Tools는 별도 `dispatch_id`를 생성합니다.
- BE는 매 예약 작업마다 고유한 `client_ref`를 보냅니다.
- 전화 재연결이나 후속 요청에 `session_id`를 예약 식별자로 사용하지 않습니다.

합의 질문:

1. `reservation_id`는 BE와 OR-Tools 중 누가 생성합니까?
2. `client_ref`는 예약 단위입니까, API 호출 단위입니까?
3. 예약 변경 후에도 같은 `reservation_id`를 유지합니까?

### 4.2 멱등성과 재시도

Tailscale 연결 지연이나 타임아웃 때문에 BE가 같은 요청을 재전송할 수 있습니다.
따라서 동일한 `client_ref`로 같은 예약 생성 요청이 들어오면 중복 배차하지 않고
처음 생성된 결과를 반환해야 합니다.

합의 질문:

1. BE의 기본 타임아웃은 몇 초입니까?
2. 최대 재시도 횟수와 간격은 얼마입니까?
3. 동일 `client_ref`에 다른 요청 내용이 오면 `409 Conflict`로 처리할까요?
4. 멱등성 기록을 얼마 동안 유지할까요?

### 4.3 Availability와 예약 확정

선택 가능한 방식:

#### A. 조회 후 확정

```text
availability/check → 사용자 확인 → reservations 생성
```

조회와 확정 사이에 다른 요청이 들어오면 차량 상태가 바뀔 수 있습니다.
Availability 결과는 확정을 보장하지 않는다는 점이 필요합니다.

#### B. 한 번에 검증 및 확정 — 권장

```text
reservations 생성 요청 → OR-Tools가 원자적으로 검증·배정
```

경쟁 상태가 적고 구현이 단순합니다. 사용자 최종 확인 전 단순 안내가 필요하다면
availability는 참고 정보만 반환하고 차량을 홀드하지 않는 방식이 적합합니다.

합의 질문:

1. Availability 호출이 차량을 홀드합니까?
2. 홀드한다면 만료 시간과 `hold_id`가 필요합니까?
3. 조회 성공 후 확정 실패가 가능한 것을 BE가 처리할 수 있습니까?

### 4.4 상태의 원본과 재시작 복구

권장안:

- 예약의 업무 상태와 승객 정보는 BE DB가 원본입니다.
- OR-Tools는 현재 차량 위치, 탑승 인원, 활성 스톱과 경로를 관리합니다.
- OR-Tools 재시작 시 BE가 활성 예약과 차량 상태를 다시 전송하거나,
  OR-Tools가 자체 스냅샷·이벤트 로그로 복구합니다.

합의 질문:

1. 차량 위치와 운행 상태는 어느 시스템이 공급합니까?
2. OR-Tools 재시작 시 활성 예약을 어떻게 복원합니까?
3. BE와 OR-Tools 상태가 다르면 어느 쪽을 기준으로 조정합니까?
4. 완료·노쇼·차량 고장 이벤트는 어떤 시스템이 발생시킵니까?

### 4.5 날짜와 시간

현재처럼 `HH:MM`만 사용하면 오늘과 내일 예약을 구분할 수 없습니다.

권장안:

- 모든 외부 API 시간은 ISO 8601 형식을 사용합니다.
- 예: `2026-07-30T10:00:00+09:00`
- 서비스 기준 타임존은 `Asia/Seoul`로 고정합니다.
- OR-Tools 내부 계산은 날짜가 포함된 절대 시각 또는 명시적 service date를 사용합니다.

합의 질문:

1. 과거 요청 판단의 기준 시각은 BE와 OR-Tools 중 어디의 시계입니까?
2. 당일·익일 예약 허용 범위는 어떻게 됩니까?
3. 운영시간 경계와 휴무일 정책은 누가 관리합니까?

### 4.6 취소와 변경

취소 요청은 정확한 `reservation_id` 또는 `dispatch_id`로만 처리해야 합니다.
예약을 찾지 못했을 때 최근 예약을 대신 취소하는 동작은 허용하지 않습니다.

합의 질문:

1. 변경을 기존 예약 수정으로 처리합니까, 취소 후 신규 예약으로 처리합니까?
2. 승차 임박 시 취소·변경 제한이 있습니까?
3. 이미 완료·취소된 예약에 대한 재요청 응답은 어떻게 합니까?
4. 부분 변경 시 어떤 필드를 보낼지, 전체 예약 스냅샷을 보낼지 결정해야 합니다.

### 4.7 오류와 HTTP 상태 코드

권장 예시:

| HTTP | 오류 코드 | 의미 |
|---:|---|---|
| 400 | `INVALID_REQUEST` | 형식 또는 필수값 오류 |
| 404 | `RESERVATION_NOT_FOUND` | 예약 없음 |
| 409 | `IDEMPOTENCY_CONFLICT` | 같은 키에 다른 요청 |
| 409 | `STATE_CONFLICT` | 취소·완료 등 상태 충돌 |
| 422 | `DISPATCH_UNAVAILABLE` | 제약조건상 배차 불가 |
| 422 | `OUT_OF_SERVICE_HOURS` | 운행시간 외 |
| 422 | `UNSUPPORTED_LOCATION` | 지원하지 않는 위치 |
| 503 | `SOLVER_UNAVAILABLE` | 엔진 초기화·내부 상태 장애 |
| 504 | `SOLVER_TIMEOUT` | 제한시간 내 해를 찾지 못함 |

오류 응답 권장 형식:

```json
{
  "error": {
    "code": "DISPATCH_UNAVAILABLE",
    "message": "해당 시간에는 배차 가능한 차량이 없습니다.",
    "retryable": false,
    "request_id": "req-123"
  }
}
```

---

## 5. API 계약 초안

### 5.1 예약 및 배차 확정

`POST /drt/reservations`

```json
{
  "request_id": "req-123",
  "client_ref": "reserve:call-abc:turn-7",
  "reservation_id": "R-20260730-0001",
  "session_id": "call-abc",
  "region_code": "SEOSAN_CITY",
  "origin": {
    "location_id": "emam_town_hall",
    "name": "음암면 마을회관"
  },
  "destination": {
    "location_id": "seosan_medical_center",
    "name": "서산의료원"
  },
  "requested_pickup_at": "2026-07-30T10:00:00+09:00",
  "passenger_count": 2
}
```

성공 응답:

```json
{
  "status": "confirmed",
  "reservation_id": "R-20260730-0001",
  "dispatch_id": "D-20260730-0042",
  "vehicle": {
    "vehicle_id": "DRT-SS-01",
    "label": "1호차"
  },
  "assigned_pickup_at": "2026-07-30T10:05:00+09:00",
  "estimated_dropoff_at": "2026-07-30T10:27:00+09:00",
  "shared_ride": true
}
```

### 5.2 예약 취소

`POST /drt/reservations/{reservation_id}/cancel`

```json
{
  "request_id": "req-124",
  "client_ref": "cancel:R-20260730-0001",
  "reason": "PASSENGER_REQUEST"
}
```

### 5.3 예약 변경

`POST /drt/reservations/{reservation_id}/change`

변경 방식은 BE와 합의가 필요합니다. 전체 요청 스냅샷을 보내고 OR-Tools가 기존
경로에서 제거한 뒤 새 조건을 원자적으로 검증하는 방식을 권장합니다.

### 5.4 상태 동기화

OR-Tools 재시작 복구를 BE 주도로 한다면 다음과 같은 내부 API 또는 시작 시 동기화
절차가 필요합니다.

```text
POST /internal/dispatch/snapshot
POST /internal/dispatch/events
GET  /internal/dispatch/state
```

외부 노출 API와 내부 동기화 API는 인증 및 접근 범위를 분리하는 것을 권장합니다.

---

## 6. 현재 OR-Tools 서버에서 개선이 필요한 부분

현재 `dispatch/server.py` 기준으로 확인된 사항입니다.

1. `client_ref`를 받지만 멱등성 처리에 사용하지 않습니다.
2. 외부 `reservation_id`와 내부 `req_###`의 명시적 매핑이 없습니다.
3. 취소 대상을 못 찾으면 마지막 내부 예약을 취소할 수 있습니다.
4. Availability가 실제 OR-Tools 경로·시간창 검증을 수행하지 않습니다.
5. 예상 픽업 시간이 고정 5분으로 계산됩니다.
6. ISO 8601의 날짜와 타임존을 버리고 `HH:MM`만 사용합니다.
7. 전역 sequence와 dispatcher 상태에 동시성 보호가 없습니다.
8. 프로세스 재시작 시 예약과 차량 경로 상태가 사라집니다.
9. 알 수 없는 위치를 임의 좌표로 생성해 배차를 계속합니다.
10. 조회와 예약 확정에서 서로 다른 검증 로직을 사용합니다.

위 항목 중 알고리즘·내부 안전성은 OR-Tools 담당이고, ID·상태 원본·재시도·시간 및
오류 계약은 BE와 합의 후 구현해야 합니다.

---

## 7. BE 팀 회의 체크리스트

- [ ] 예약 ID 생성 주체
- [ ] `client_ref`, `request_id`, `session_id`의 정의와 수명
- [ ] 멱등성 보장 범위와 보관 기간
- [ ] Availability의 차량 홀드 여부
- [ ] 예약 생성의 원자성
- [ ] 예약·차량·경로 상태의 원본 시스템
- [ ] OR-Tools 재시작 및 상태 복구 방식
- [ ] 취소·변경 API와 상태 전이 규칙
- [ ] ISO 8601 및 `Asia/Seoul` 사용
- [ ] 운영시간·휴무일 정책 소유 주체
- [ ] HTTP 상태 코드와 업무 오류 코드
- [ ] BE 타임아웃, 재시도 횟수 및 간격
- [ ] Solver 시간 제한과 타임아웃 응답
- [ ] Tailscale 서비스 주소와 포트
- [ ] 인증 방식, 접근 제어 및 요청 추적 ID
- [ ] API 버전 관리 방식

---

## 8. 회의에서 전달할 요약

> OR-Tools 팀은 차량 정원, 시간창, 픽업·하차 순서, 최대 우회시간을 적용해 최적
> 차량과 경로를 계산하겠습니다. 연동 구현 전에 예약 ID 생성 주체, 멱등성 키,
> Availability의 홀드 여부, 예약 및 차량 상태의 원본, 취소·변경 규칙, 날짜·시간
> 형식, 타임아웃·재시도·오류 코드를 BE 팀과 확정해야 합니다. 예약의 업무 원본은
> BE가, 차량·경로 최적화 상태는 OR-Tools가 소유하고, 예약 생성 요청은 원자적으로
> 검증·배정하는 구조를 권장합니다.
