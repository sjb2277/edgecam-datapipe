# 프로젝트 개요 · 스택 · 아키텍처

이 문서가 정본이다. 일차별 계획은 `W1.md`~`W5.md`, 부품과 장비 구성은 `hardware.md`, 기록 양식은 `../templates/`에 있다. 에이전트 규칙은 루트 `CLAUDE.md`를 따른다.

최종 갱신: 2026-10-30 (기술 검증 결과 반영)

## 1. 목표

5주(2026-11-02 ~ 12-04) 동안 카메라, 시뮬레이션 링크, 센서 장비에서 나오는 6종 데이터를 수집부터 서빙까지 하나의 파이프라인으로 직접 구축한다. 통신은 데이터 소스 중 하나이고, 주인공은 데이터 계층이다.

한 줄 정의: 엣지 비전 서비스의 영상·링크·장비 이벤트를 스키마로 관리하며 스트림과 배치로 처리하고, 품질 검사와 데이터셋 버전까지 갖춘 재현 가능한 데이터 파이프라인.

### 데이터 파이프라인 중심으로 잡은 이유

- 통신 R&D가 아니라 데이터 엔지니어, 플랫폼, 스마트팩토리/IoT, 검증 직무에 공통으로 통하는 역량을 증명한다.
- 이전 5G 프로젝트 경험은 "링크 지표를 이해하는 도메인 지식"으로 재활용한다.
- 기업이 확인하려는 "어디까지 해봤나"에 답하도록, 수집·저장·정제·품질·서빙·제어를 전부 한 사람이 구축한다.

### 이전 프로젝트 대비 새로 증명할 것

| 영역 | 이전 5G 프로젝트 | 이번 프로젝트 |
| --- | --- | --- |
| 수집 | 소켓으로 직접 수신 | 메시지 버스 + 스키마 레지스트리 + DLQ |
| 저장 | InfluxDB 단일 | Hot(TimescaleDB) / Cold(MinIO Parquet) 분리 |
| 처리 | 실시간 시각화만 | 배치 정제(dbt) + 오케스트레이션(Airflow) |
| 신뢰성 | 확인 안 함 | 장애 주입, 멱등 적재, 재처리(replay) |
| 품질 | 없음 | run마다 자동 품질 리포트, 격리(quarantine) |
| 산출물 | 대시보드 | 버전 붙은 데이터셋 + API + 직접 만든 웹 |

### 완료 기준

- [ ] `make demo` 한 명령으로 시나리오 실행, 적재, 리포트 생성까지 끝난다
- [ ] 6종 소스 모두 스키마 등록 후 적재된다
- [ ] run마다 품질 리포트가 자동 생성된다
- [ ] 같은 raw를 재처리하면 gold 결과가 동일하다
- [ ] 웹에서 라이브 뷰와 run 비교가 동작한다
- [ ] 장비 제어 폐루프 1개 이상이 동작한다
- [ ] README, 구조도, 데모 영상, ADR 5개 이상, devlog 25편이 repo에 있다

## 2. 환경·언어·스택

Ubuntu 네이티브에 Docker Compose로 인프라를 띄우고, 파이프라인은 Python, 변환은 SQL(dbt), 대시보드는 TypeScript, 장비는 C++로 작성한다. 사양 제약 때문에 "실험 실행"과 "배치 처리"를 시간대로 나눠 돌린다.

### 사양(i7-1360P 4P+8E 16스레드, DDR5 16GB, Iris Xe 내장)이 바꾼 결정 3가지

1. **NVIDIA GPU가 없다.** 이전 프로젝트의 TensorRT 경로는 못 쓴다. YOLO를 OpenVINO로 변환하면 Intel CPU에서 최대 약 3배 빨라지고, `intel:gpu` 지정으로 내장 GPU 추론도 가능하다. CPU와 iGPU 지연을 직접 측정해 고른다.
2. **RAM이 16GB다.** OAI 공식 튜토리얼의 CN5G+gNB 최소 사양은 8코어 3.5GHz, RAM 32GB다. 그 절반이라 rfsim 상시 구동은 보장할 수 없다. 그래서 링크 계층을 교체형으로 만든다: `link=netem`(기본), `link=rfsim`(검증). 두 모드는 같은 스키마로 지표를 낸다.
3. **Airflow는 Docker 메모리 4GB 이상, 권장 8GB다.** 실험 중에는 띄우지 않고 배치 단계에만 쓴다.

데스크탑을 쓰게 되면 시계가 분리되어 지연 측정에 시간 동기(chrony 등)가 필요하고, Redpanda external 주소가 데스크탑 IP로 바뀐다. 아직 미정이며, 정해지면 ADR로 남긴다.

### 스택 확정표

| 계층 | 선택 | 제친 대안 | 선택 이유 |
| --- | --- | --- | --- |
| OS | Ubuntu 네이티브 | Windows, WSL | V4L2, netns, tc 직접 사용 |
| 런타임 | Python 3.11 + uv, Docker Compose profiles | conda | 빠른 설치, lockfile 재현성 |
| 영상 | GStreamer + OpenCV | FFmpeg 단독 | 이전 경험, tee 분기 |
| 추론 | YOLO11n → OpenVINO | PyTorch CPU, TensorRT | 내장 GPU 활용 |
| 메시지 버스 | Redpanda (Kafka API) | Kafka, Redis Streams, MQTT | Kafka 호환 경험이 범용, 단일 바이너리에 Schema Registry 내장, 메모리 상한 지정 가능 |
| 스키마 | pydantic(원본) → JSON Schema(생성물), confluent-kafka JSONSerializer | Avro | 초반 디버깅 쉬움, W4에 Avro 전환 여부를 ADR로 |
| Hot 저장 | PostgreSQL + TimescaleDB | InfluxDB | SQL 조인, dbt 연동, 이전 경험과 차별 |
| Cold 저장 | MinIO + Parquet | 로컬 폴더 | S3 API 범용성 |
| 분석 엔진 | DuckDB, Polars | Spark | 노트북 규모에 Spark는 과함 (이 판단도 ADR로) |
| 변환 | dbt-core | SQL 스크립트 | 테스트, lineage, 문서 자동화 |
| 오케스트레이션 | Airflow (LocalExecutor) | Dagster, Prefect | 채용공고 노출 빈도, 메모리는 운용으로 해결 |
| 품질 | pandera + dbt tests | Great Expectations | 가벼움 |
| 데이터셋 버전 | DVC (MinIO remote) | 수동 manifest | 복원 가능한 버전 |
| API | FastAPI + WebSocket | Flask | async, OpenAPI 자동 문서 |
| 웹 | React + Vite + TypeScript + ECharts | Streamlit, Grafana | 직접 구현 증명, Grafana는 파이프라인 운영 모니터링 전용 |
| 장비 | ESP32 + PlatformIO (C++) | Arduino Uno | 처리 여유, 확장성 |
| 개발 품질 | ruff, pytest, pre-commit, GitHub Actions | 없음 | 커밋마다 자동 검증 |

이미지 태그와 패키지 버전은 D01에서 확정해 compose와 lockfile에 고정한다(latest 금지).

### 메모리 운용: 실행 프로파일 3개

| 프로파일 | 띄우는 것 | 쓰는 시점 |
| --- | --- | --- |
| core (상시) | Redpanda(`--memory 1G`), Postgres/Timescale, MinIO | 항상 |
| exp | core + 카메라·추론 + 링크(netem 또는 rfsim) + API·웹 | 실험 실행 |
| batch | core + Airflow + dbt | 실험 종료 후 정제·리포트 |

rfsim과 Airflow는 동시에 띄우지 않는다. 각 프로파일의 실제 메모리는 D01과 D04에 `docker stats`, `htop`으로 실측해 devlog에 남긴다. compose에는 `core`, `exp`, `batch`, `ops`, `ran` 프로파일을 두고, core 서비스에도 `profiles: [core]`를 명시한다.

### 포트 배정

Redpanda는 컨테이너 간 통신용 internal 리스너와 호스트 접속용 external 리스너를 분리한다. 호스트(uv로 실행하는 Python)는 external 주소로 접속한다.

| 서비스 | 컨테이너 내부 | 호스트 | 비고 |
| --- | --- | --- | --- |
| Redpanda Kafka API | 9092 | 19092 | internal/external 리스너 분리 |
| Schema Registry | 8081 | 18081 | |
| Redpanda Console | 8080 | 8080 | |
| PostgreSQL(TimescaleDB) | 5432 | 5432 | |
| MinIO API / 콘솔 | 9000 / 9001 | 9000 / 9001 | |
| Airflow Web | 8080 | 8085 | Console과 충돌 방지 (제안, D01에서 확정) |
| Grafana | 3000 | 3000 | 제안, D01에서 확정 |
| FastAPI | 8000 | 8000 | 제안, D01에서 확정 |
| Web (Vite dev) | 5173 | 5173 | 제안, D01에서 확정 |

확정된 값은 `.env.example`에 둔다. 코드에 하드코딩하지 않는다.

## 3. 시스템 아키텍처

모든 이벤트는 공통 envelope을 달고 버스에 들어간 뒤 Hot, Cold 두 경로로 동시에 저장되고, run이 끝나면 배치가 silver·gold로 정제한다. Cold의 raw Parquet이 원본이라 언제든 재처리할 수 있다.

데이터 흐름: 소스 4계열(카메라 송신, 엣지 수신·추론, 링크 어댑터, 장비 게이트웨이) → Redpanda 버스(Schema Registry, 실패는 DLQ) → Hot 경로(stream-loader → TimescaleDB bronze)와 Cold 경로(archiver → MinIO Parquet) → 배치 정제(Airflow DAG: pandera 검증 → reference 추론 → dbt silver·gold → 품질 리포트 → DVC 데이터셋) → FastAPI·WebSocket → React 대시보드. 엣지 컨트롤러는 라이브 이벤트를 구독해 제어 명령을 링크 너머 장비로 보낸다. replay는 Cold raw에서 bronze로 다시 적재하는 경로다.

### 데이터 소스 6종

| # | 소스 | 생성 위치 | 주기 | 토픽 | 주요 필드 |
| --- | --- | --- | --- | --- | --- |
| 1 | 프레임 메타 | 송신 agent, 수신 서버 | 프레임마다 | `frame.sent`, `frame.recv` | seq, rtp_ts, capture_ts, recv_ts, bytes |
| 2 | 추론 이벤트 | 엣지 추론 | 처리 프레임마다 | `inference.detections` | rtp_ts, class, conf, bbox, infer_ms |
| 3 | 링크 지표 | netem 설정값 또는 gNB 통계 로그 | 1초 | `link.metrics` | mode, delay, loss, rate, (rfsim) MCS, BLER |
| 4 | 네트워크 프로브 | 프로브 스크립트 | 1초 | `net.probe` | rtt, jitter, loss |
| 5 | 장비 텔레메트리 | ESP32 → device-gateway | 0.5~1초 | `device.telemetry` | distance_cm, servo_angle, led |
| 6 | 제어 명령·응답 | 컨트롤러, 장비 | 이벤트 | `control.command`, `control.ack` | cmd_id, issued_ts, ack_ts, result |

## 4. 데이터 계약

- 이벤트 모델은 pydantic이 원본이고 `schemas/*.json`은 생성물이다(직접 편집 금지). 생성은 `make schemas`.
- envelope 필드(`event_id`(UUID), `run_id`, `source`, `schema_version`, `event_ts`, `ingest_ts`)는 `EventBase`에 두고 각 이벤트가 **상속**한다. envelope를 필드로 중첩하지 않는다. 중첩하면 JSON Schema에 `$ref`/`$defs`가 생기는데, Redpanda 문서가 버전별로 이 경우의 호환성 검사 동작을 다르게 설명한다.
- 스키마에 `title`이 있어야 한다(Python JSONSerializer가 요구).
- subject는 `<topic>-value`(TopicNameStrategy), 호환성은 BACKWARD.
- Producer는 `auto.register.schemas=False`다. 스키마는 등록 스크립트(`pipe schema register`)로만 등록한다.
- 직렬화 시 페이로드 검증(`validate`)을 켠다. 레지스트리는 스키마를 저장하고 호환성만 검사하며, 메시지 내용 검증은 클라이언트가 한다.
- 검증 실패는 예외로 중단하지 않고 `<topic>.dlq`로 보낸다. DLQ 메시지는 스키마 없이 JSON 문자열이다:

```json
{
  "original_payload": "<원문 문자열>",
  "error": "<오류 메시지>",
  "source_topic": "frame.sent",
  "failed_at": "<ISO8601>",
  "stage": "producer"
}
```

- stream-loader도 역직렬화·검증에 실패한 메시지를 같은 형식으로 DLQ에 보낸다(`stage`: `loader`). DLQ 건수는 품질 리포트에서 집계한다.

## 5. 처리 경로 4가지

- **Hot**: 버스 → stream-loader → TimescaleDB `bronze.*` → API·WebSocket 라이브 뷰. 적재 지연 p95를 측정 대상으로 삼는다.
- **Cold**: 버스 → archiver → MinIO `raw/source=/run_id=/` Parquet. 불변 원본이고, 원본 프레임은 샘플링해 JPEG로 남긴다.
- **Batch**: run 종료 → Airflow DAG → bronze 검증 → reference 추론 → dbt silver·gold → 품질 리포트 → 데이터셋 등록.
- **Replay**: MinIO raw → bronze 재적재 → gold 체크섬 비교. `event_id` 기반 upsert로 멱등성을 보장한다.

## 6. 계층 설계

| 계층 | 위치 | 대표 테이블 | 하는 일 |
| --- | --- | --- | --- |
| bronze | Timescale, MinIO | `bronze.frame_sent`, `bronze.link_metrics` | 원본 그대로, 스키마 통과분만 |
| silver | Postgres (dbt) | `silver.frames`, `silver.link_1s`, `silver.detections` | 중복 제거, 송수신 프레임 매칭, 1초 버킷 정렬 |
| gold | Postgres (dbt) | `gold.run_second_features`, `gold.run_summary` | 윈도우 조인, 자동 라벨(network_state), 시나리오 요약 |
| catalog | Postgres | `meta.runs`, `meta.datasets`, `meta.quality_reports` | run 설정 해시, git SHA, 데이터셋 버전, 품질 결과 |

송수신 프레임은 RTP 타임스탬프를 키로 매칭한다. 영상 스트림 안에 seq를 심지 않아도 되고, 송신·수신이 같은 노트북 시계를 쓰니 프레임 단위 지연이 정확하게 나온다(데스크탑을 쓰면 시간 동기가 필요하다).

## 7. 운영 규칙

평일 D01~D25가 작업일, 토요일은 버퍼와 주간 리포트, 일요일은 휴식이다.

| 항목 | 규칙 |
| --- | --- |
| 브랜치 | `main` + 기능 브랜치 `feat/D07-stream-loader`. 혼자여도 PR로 병합하고, PR 본문에 그날 변경 요약을 쓴다 |
| 커밋 | Conventional Commits: `feat(ingest): ...`, `fix(...)`, `test(...)`, `docs(devlog): D07`. 하루 최소 기능 커밋 1개 + devlog 커밋 1개 |
| 이슈 | 일차 1개 = 이슈 1개, Milestone W1~W5, Projects 보드로 진행 상태 관리 |
| 비밀·데이터 | `.env`는 gitignore, raw 데이터는 커밋 금지(DVC로 관리), 샘플만 소량 커밋 |
| 태그 | 토요일마다 `w1-done`~`w4-done`, 최종 `v1.0.0` |
| 테스트 | 단위 테스트(브로커 불필요)와 통합 테스트(`@pytest.mark.integration`) 분리. `make test`는 단위만 |
| 기록 | devlog는 사용자가 직접 기록한다. 채팅 기반 일일 리포트는 초안 자료로만 쓰고 자기 말로 고쳐 쓴다. AI를 쓴 지점은 devlog의 "AI 활용" 칸에 남긴다 |

예정 ADR: 0001 메시지 버스, 0002 프레임 식별, 0003 링크 모드, 0004 추론 디바이스, 0005 프레임 샘플링, 0006 시간 기준, 0007 스키마 진화, 0008 Spark를 쓰지 않은 이유.

run 리포트(`reports/runs/<run_id>.md`)는 D15부터 DAG가 자동 작성한다: 실행 정보(시나리오, 설정 해시, git SHA, 시작·종료 시각, 링크 모드), 수치(프레임 수, 드롭률, 지연 p50·p95, 검출 일치율, 명령 왕복 지연), 품질(갭, 역전, 중복률, 결측률, DLQ 건수, 판정).

## 8. 리스크와 결정 게이트

가장 큰 리스크는 rfsim 자원 부족과 범위 팽창이다. 둘 다 "데이터 계층을 먼저 지킨다"는 원칙으로 대응한다.

| 리스크 | 감지 신호 | 대응 | 판단 시점 |
| --- | --- | --- | --- |
| rfsim 자원 부족 (공식 최소 RAM 32GB vs 16GB) | CPU 포화, UE 연결 끊김 | netem 기본, rfsim은 시연 run 1회 | D04 (G1) |
| 메모리 경합 | OOM, swap 급증 | 프로파일 분리, Redpanda 메모리 상한 | 상시 |
| 스키마 레지스트리의 `$ref`/`$defs` 동작 차이 | 등록 거부 | envelope 상속 유지, 평탄화, 이미지 태그 고정 | D05 |
| 부품 미조달 | D09 전 미확보 | 같은 스키마의 시뮬 장치로 진행, 도착 후 교체 | D09 |
| 손실·중복 미해결 | 장애 주입 결과 손실 > 0 | W3 진입 보류, 토요일 버퍼 투입 | D10 (G2) |
| Airflow 학습 지연 | D14 미완 | 같은 태스크를 Makefile로 임시 실행, W4에 이관 | D14 |
| 대시보드 일정 초과 | D20 미완 | Run 비교 화면만 완성, Live·Catalog 축소 | D20 (G4) |
| 범위 팽창 | 이틀 연속 계획 미달 | 토요일 버퍼 사용, 그래도 밀리면 W5 폐루프는 시나리오 3만 | 매주 토 |
| 지원 일정 충돌 | 서류 마감, 면접 | 그 주는 각 일차 완료 기준만 지키고 확장 작업 생략 | 수시 |

**절대 줄이지 않는 것:** 스키마·DLQ, Hot/Cold 적재, 멱등성 검증, 품질 리포트, devlog.
**먼저 줄이는 것:** rfsim, Live 화면, 서보 추적, Avro 전환.

## 9. 출처

- OAI 5G NR SA tutorial with OAI nrUE: https://git.yigid.dev/yigid/openairinterface5g/src/commit/d569e7ddc96345e57eaed1b7b01b26a213181a95/doc/NR_SA_Tutorial_OAI_nrUE.md (최소 하드웨어 사양, rfsim 실행)
- OAI Running 5G Softmodems: https://git.yigid.dev/yigid/openairinterface5g/raw/commit/dac55975b545e4da4120b7231f12c293d9498122/doc/RUNMODEM.md (RFsimulator)
- Ultralytics Intel OpenVINO Export: https://docs.ultralytics.com/integrations/openvino (CPU 가속, intel:gpu 지정)
- Deploy Redpanda with Docker Compose: https://computingforgeeks.com/deploy-redpanda-docker-compose/ (단일 노드, 메모리 상한, internal/external 리스너)
- Redpanda Schema Registry: https://docs.redpanda.com/docs/platform/console/features/schema-registry/ (지원 형식, 기본 포트 8081)
- Redpanda Schema Registry 개요(JSON Schema 제한): https://docs.redpanda.com/cloud-data-platform/manage/schema-reg/schema-reg-overview.md
- Redpanda Schema Registry 개요(현재 버전): https://docs.redpanda.com/current/manage/schema-reg/schema-reg-overview
- confluent-kafka-python JSONSerializer: https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/_modules/confluent_kafka/schema_registry/json_schema.html (validate, auto.register.schemas)
- Airflow Docker Compose 요구 사양: https://apache.googlesource.com/airflow/+/bc3b2d16d3563d5b9bccd283db3f9e290d1d823d/docs/apache-airflow/howto/docker-compose/index.rst
