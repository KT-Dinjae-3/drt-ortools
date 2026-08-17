# 🚌 서산시 행복버스 DRT 배차 최적화 서비스

서산시 행복버스 예약 요청을 대상으로 차량 가용성과 운행 경로를 계산하는 **Python/FastAPI 기반 OR-tools 마이크로서비스**입니다.

Go 백엔드가 전달한 출발지, 목적지, 희망 승차 시각, 탑승 인원과 활성 예약 snapshot을 바탕으로 요청마다 배차 계획을 계산합니다. FastAPI 서버는 기본적으로 `8092` 포트에서 실행됩니다.

> OR-tools 서비스는 예약의 영구 저장소가 아닙니다. 예약번호, 고객 정보, 예약 상태 및 취소 상태의 source of truth는 백엔드/PostgreSQL이며, OR-tools는 요청 시점의 배차 계획만 계산하는 stateless provider입니다.

---

## 서산시 정류장 데이터베이스

서산시 행복버스 운영 데이터를 기반으로 전체 위치 601개와 실제 배차에 사용하는 정류장 589개를 관리합니다.

| 권역 | 정류장 수 |
|---|---:|
| 대산 (`SEOSAN_DAESAN`) | 238 |
| 해미 (`SEOSAN_HAEMI`) | 153 |
| 고북 (`SEOSAN_GOBUK`) | 198 |
| 합계 | 589 |

각 정류장에는 `ST-H-130`과 같은 canonical stop ID, 정류장명, 위도·경도와 행정 권역이 연결됩니다. `SEOSAN_CITY`는 전체 권역을 의미하며, 세부 권역 코드를 이용한 검색과 검증도 지원합니다.

정류장 카탈로그는 다음 원칙으로 장소를 해석합니다.

- 자연어 정류장명, 원본명, 별칭 또는 canonical stop ID를 조회할 수 있습니다.
- 요청 권역과 실제 정류장 권역이 다르면 `region_mismatch`를 반환합니다.
- 동일한 이름이 여러 정류장과 일치하면 임의로 선택하지 않고 `ambiguous`와 후보 목록을 반환합니다.
- 등록된 이름이나 별칭과 일치하지 않는 유사 지명은 자동 보정하지 않고 `not_found`로 처리합니다.
- 정류장명 검색, 좌표 기반 인근 정류장 검색과 ID 기반 상세 조회를 제공합니다.

원천 데이터와 생성 규칙은 [`SEOSAN_STOP_DATABASE.md`](SEOSAN_STOP_DATABASE.md)에서 확인할 수 있습니다.

---

## OR-tools 배차 엔진

`dispatch.DynamicDRTDispatcher`는 Google OR-Tools의 `RoutingModel`로 동적 배차 경로를 계산합니다.

- 차량 차고지를 시작점으로 승차·하차 지점을 Pickup & Delivery 노드로 구성
- Haversine 거리와 설정된 평균 운행 속도로 거리·시간 행렬 생성
- 차량 정원, 탑승 인원, 승차 후 하차 순서와 승차 시간 범위 적용
- 기존 예약의 배정 차량과 약속된 승차 시각 보존
- 기존 승객의 과도한 하차 지연을 막기 위한 허용 범위와 지연 페널티 적용
- `GUIDED_LOCAL_SEARCH`를 이용해 제한 시간 내 실행 가능한 경로 탐색
- 서산시 행복버스 3대의 정원, 현재 위치와 활성 상태를 기준으로 계획 계산

엔진 라이브러리는 신규 예약, 취소와 변경 이벤트를 처리할 수 있습니다. REST 서버에서는 백엔드가 전달한 활성 예약 snapshot으로 매 요청의 임시 dispatcher를 재구성하고 신규 배차 계획을 계산합니다.

---

## Stateless 예약 계획

### 배차 가능 여부 확인

`POST /drt/availability/check`는 백엔드가 전달한 `active_reservations`를 먼저 재현한 후, 잔여 좌석을 충족하면서 출발지에 가장 가까운 가용 차량과 예상 승차 시각을 반환합니다. 계산 결과를 서버에 저장하지 않습니다.

### 신규 예약 계획

`POST /drt/reservations`는 활성 예약 snapshot과 신규 요청을 함께 최적화합니다. 성공하면 차량 ID, 예상 승차 시각, `input_ref`, canonical 정류장 ID와 결정적 `plan_id`를 반환합니다.

동일한 입력에는 동일한 `PLAN-...` 식별자가 생성됩니다. 호환성을 위해 현재 응답의 `reservation_id`에도 같은 plan ID를 넣지만, 이는 OR-tools가 예약 상태를 소유한다는 의미가 아닙니다.

### 예약 조회와 취소

OR-tools 서버는 예약을 영구 저장하지 않으므로 `GET /drt/reservations`는 빈 목록과 `stateless: true`를 반환합니다.

취소 상태는 백엔드가 소유합니다. 취소 API가 호출되면 `STATELESS_CANCEL_OWNED_BY_BACKEND`를 반환하며 OR-tools 내부 상태를 변경하지 않습니다. 다음 배차 요청에서 백엔드가 취소된 예약을 `active_reservations`에서 제외하면 새로운 계획에 반영됩니다.

### 오류 처리

잘못된 정류장·권역·시간, 중복 정류장명, 정원 초과, 차량 부족, 잘못된 활성 예약 snapshot과 배차 실패를 구분된 오류 코드와 JSON 형식으로 반환합니다. 각 요청은 독립된 dispatcher에서 계산되므로 실패한 요청이 다음 요청의 상태를 오염시키지 않습니다.

---

## REST API

| Method | Path | 설명 |
|---|---|---|
| `GET` | `/health` | 서비스, 차량 및 정류장 데이터 상태 |
| `GET` | `/drt/regions` | 서산시 전체·세부 운행 권역 조회 |
| `GET` | `/drt/stops` | 정류장명과 권역 기반 검색 |
| `GET` | `/drt/stops/nearest` | 좌표 기반 인근 정류장 검색 |
| `POST` | `/drt/stops/resolve` | 자연어 장소명 또는 ID 해석 |
| `GET` | `/drt/stops/{stop_id}` | canonical stop ID 상세 조회 |
| `POST` | `/drt/availability/check` | 비저장 방식의 차량 가용성 확인 |
| `POST` | `/drt/reservations` | stateless 신규 배차 계획 계산 |
| `GET` | `/drt/reservations` | stateless 상태 확인용 빈 목록 |
| `POST` | `/drt/reservations/cancel` | 백엔드 취소 소유권 안내 |
| `PATCH` | `/drt/reservations/{id}/cancel` | 백엔드 취소 소유권 안내 |

FastAPI가 생성하는 상세 명세는 서버 실행 후 `/docs`에서 확인할 수 있습니다.

---

## 백엔드 연동 예시

```json
{
  "client_ref": "call-9002-001",
  "operation_id": "call-9002-001:create",
  "region_code": "SEOSAN_CITY",
  "origin_stop_id": "ST-H-130",
  "destination_stop_id": "ST-H-125",
  "requested_pickup_at": "2030-08-04T13:00:00+09:00",
  "passenger_count": 1,
  "active_reservations": []
}
```

성공 응답에는 다음과 같은 배차 계획 정보가 포함됩니다.

```json
{
  "seq": 1,
  "status": "success",
  "action": "new_reservation",
  "input_ref": "call-9002-001:create",
  "session_id": "call-9002-001",
  "plan_id": "PLAN-15F7D19A85DD0C9D",
  "reservation_id": "PLAN-15F7D19A85DD0C9D",
  "vehicle_id": "DRT-SS-01",
  "pickup_time": "12:55",
  "pickup_location": "해미우체국",
  "dropoff_location": "한서대학교",
  "origin_stop_id": "ST-H-130",
  "destination_stop_id": "ST-H-125",
  "region_code": "SEOSAN_CITY"
}
```

---

## 프로젝트 구조

```text
LEG/ORtools/
├── dispatch/
│   ├── config.py       # 배차 제약과 탐색 설정
│   ├── engine.py       # OR-tools 동적 배차 엔진
│   ├── io_adapter.py   # JSON ↔ 내부 이벤트 변환
│   ├── locations.py    # 위치 모델과 거리 계산
│   ├── models.py       # 차량·예약·배차 결과 모델
│   ├── server.py       # FastAPI REST 서버
│   ├── stops.py        # canonical 정류장 카탈로그
│   └── validators.py   # 요청 유효성 검증
├── data/
│   ├── seosan_stops.json
│   └── seosan_stops_source.tsv
├── deploy/k3s/ortools.yaml
├── tests/
├── Dockerfile
└── README.md
```

---

## 실행 및 테스트

### 서버 실행

```bash
python -m uvicorn dispatch.server:app --host 0.0.0.0 --port 8092
```

### 전체 테스트

```bash
python -m unittest discover -s tests -v
```

현재 정류장 카탈로그, API, stateless provider 계약과 배차 시나리오를 검증하는 unittest 28개가 통과합니다.

### 상태 확인

```bash
curl http://127.0.0.1:8092/health
```

Docker 이미지와 K3s 매니페스트는 각각 `Dockerfile`, `deploy/k3s/ortools.yaml`에서 관리합니다.
