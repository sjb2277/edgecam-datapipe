# ADR-0001: 메시지 버스로 Redpanda 사용

- 상태: 제안
- 날짜: 2026-10-31

## 배경
카메라 송신, 엣지 수신·추론, 링크 어댑터, 장비 게이트웨이 이벤트가 버스 하나에 모인다. 이 이벤트를 Hot(stream-loader → TimescaleDB)과 Cold(archiver → SeaweedFS S3)로 나눠 보낸다.

요구사항은 네 가지다.
- 스키마 레지스트리: pydantic → JSON Schema, BACKWARD 호환성, `auto.register.schemas=False`
- 토픽별 DLQ
- replay를 위한 보존과 오프셋 재처리
- Kafka 클라이언트 생태계(confluent-kafka)

제약은 단일 노트북(Intel CPU, RAM 16GB, GPU 없음)이다. core 프로파일을 늘 띄워 두고, 그 위에 추론과 배치를 함께 돌려야 한다.

## 선택지

| 선택지 | 장점 | 단점 | 측정값/근거 |
| --- | --- | --- | --- |
| Redpanda | Kafka API 호환, Schema Registry 내장(컨테이너 하나), JVM·ZooKeeper 없음, `--smp/--memory`로 자원 상한 지정 | 단일 벤더, 일부 Kafka 기능 차이 | `v26.2.4`, `--smp 1 --memory 1G --overprovisioned`. 5분 유휴 RSS **193 MiB**, CPU 0.49% |
| Apache Kafka (KRaft) | 사실상 표준, 기능이 가장 완전함 | JVM 힙을 조정해야 함, Schema Registry 컨테이너가 따로 필요(Confluent 또는 Apicurio) | 미측정 |
| NATS JetStream | 아주 가볍고 운영이 단순함 | Kafka API가 아님, 스키마 레지스트리·Kafka 커넥터 생태계를 쓸 수 없음 | 미측정 |

### core 프로파일 유휴 메모리 실측 (2026-10-31T05:17:46Z, 기동 후 약 5분, `docker stats --no-stream`)

| 서비스 | 이미지 | MEM USAGE | CPU % |
| --- | --- | --- | --- |
| redpanda | `redpandadata/redpanda:v26.2.4` | 193.0 MiB | 0.49 |
| seaweedfs | `chrislusf/seaweedfs:4.48` | 160.1 MiB | 0.57 |
| postgres | `timescale/timescaledb:2.30.2-pg16` | 124.0 MiB | 5.06 |
| console | `redpandadata/console:v3.12.0` | 26.4 MiB | 0.00 |
| 합계 | | 약 503.5 MiB | |

## 결정
Redpanda를 쓴다. Schema Registry도 Redpanda 내장 기능을 쓴다.

- 리스너 internal: `redpanda:9092` / SR 8081 (컨테이너 사이)
- 리스너 external: `localhost:19092` / SR 18081 (호스트)
- 자원 옵션: `--smp 1 --memory 1G --overprovisioned`

Kafka API이므로 나중에 Apache Kafka로 옮기더라도 클라이언트 코드는 바뀌지 않는다.

## 감수하는 점
- Apache Kafka와 동작이 미묘하게 다를 수 있다(트랜잭션, 일부 관리 API).
- `--memory 1G`는 상한이다. 유휴 RSS 193 MiB는 부하가 걸리지 않은 상태의 값이고, 부하가 걸리면 1G 가까이 늘 수 있다.
- 대안들의 메모리는 직접 재지 않았다. 비교 근거는 구조적 차이(JVM, 별도 SR 컨테이너)다.

## 다시 검토할 조건
- 부하가 걸렸을 때 메모리가 1G 상한에 닿아 추론과 배치 프로파일을 함께 띄울 수 없을 때
- Redpanda와 Kafka의 비호환 때문에 클라이언트 우회 코드가 생길 때
- 데스크탑이나 클러스터로 옮겨 다중 브로커가 필요해질 때
