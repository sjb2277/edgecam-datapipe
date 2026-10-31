# edgecam-datapipe: Agent Rules

엣지 비전 데이터 파이프라인(카메라 → 시뮬레이션 링크 → 엣지 추론 → 버스 → Hot/Cold 저장 → 배치 정제 → API·웹)을 만드는 개인 프로젝트다. 5주(25 작업일) 일정이고, 일차별 계획은 사용자가 별도로 관리한다.

## 작업 방식 (최우선)
- 작업 단위마다 계획을 먼저 보고한다: 변경·생성할 파일, 실행할 명령, 예상 영향.
- 사용자가 승인한 계획에 포함된 파일 수정과 명령만 실행한다.
- 계획에 없는 명령이나 파일 수정이 필요해지면 중단하고 보고한다.
- docker/docker compose, sudo, ip netns, tc, 패키지 설치, git commit/push, 레포 밖 쓰기, 삭제는 계획에 명시해 승인받는다.
- lint/test 실패는 승인된 계획 범위 안에서 최대 3회까지 수정 후 재실행한다. 그래도 실패하면 로그와 함께 보고한다.
- 사용자 보고는 한국어로 한다.
- devlog/ 는 사용자가 직접 작성한다. 요청이 없으면 수정하지 않는다.

## 계획 문서
- 일차별 계획은 docs/plan/W{n}.md(D01~D25)에 있다. 전체 개요·스택·포트·데이터 계약은 docs/plan/overview.md, 부품은 docs/plan/hardware.md.
- 사용자가 지시한 일차 항목만 읽는다. 계획 문서 전체를 한 번에 읽지 않는다.
- 계획과 레포 현재 상태가 충돌하면 진행하지 말고 보고한다.

## 환경
- Ubuntu, Python 3.11(uv), Docker Compose profiles: core, exp, batch, ops, ran
- GPU 없음(Intel CPU, OpenVINO), RAM 16GB. 프로파일 동시 기동 조합은 보고 후 결정한다.
- Redpanda: --memory 1G --smp 1 --overprovisioned (기동 실패 시 --reserve-memory 0M 검토 후 보고)
- 포트: 컨테이너 내부 Kafka 9092 / Schema Registry 8081, 호스트는 external 19092 / 18081. Postgres 5432, SeaweedFS S3 9000(Master UI 9333, Filer UI 8888)
- 이미지 태그와 패키지 버전은 고정한다(latest 금지).

## 데이터 계약
- pydantic 모델이 원본이고 schemas/*.json은 생성물이다(직접 편집 금지).
- envelope는 상속으로 포함한다(중첩 금지): event_id, run_id, source, schema_version, event_ts, ingest_ts
- JSON Schema에 $ref/$defs가 생기면 레지스트리 등록 전에 보고한다.
- 토픽: frame.sent, frame.recv, link.metrics, net.probe, inference.detections, device.telemetry, control.command, control.ack, 각 <topic>.dlq
- Producer는 auto.register.schemas=False, 스키마는 등록 스크립트로만 등록, 호환성 BACKWARD
- 검증 실패 시 중단하지 않고 <topic>.dlq에 {원문, 오류, 원래 토픽, 시각, 단계(producer|loader)}를 전송한다.

## 구성과 품질
- 포트, 토픽명, 경로는 .env/config/에서 주입한다(.env.example만 커밋). 링크 시나리오는 scenarios/*.yaml.
- 단위 테스트(브로커 불필요)와 통합 테스트(@pytest.mark.integration)를 분리한다. make test는 단위만.
- 커밋은 Conventional Commits. 비밀과 원본 데이터는 커밋하지 않는다.
- 되돌리기 어려운 결정은 docs/adr/에 ADR 초안을 제안한다.

## 레포 구조
docker/compose.yaml, scenarios/, schemas/(생성물), src/pipe/{common,capture,link,inference,ingest,gateway,control,quality}, dbt/, airflow/dags/, api/, web/, firmware/esp32/, tests/, reports/runs/, docs/{adr,figures}, devlog/
