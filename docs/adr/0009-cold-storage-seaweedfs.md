# ADR-0009: Cold 저장소로 SeaweedFS(S3 API) 사용

- 상태: 제안
- 날짜: 2026-10-31

## 배경
Cold 경로(archiver → `raw/source=/run_id=/` Parquet), replay, DuckDB 직접 조회, DVC remote가 모두 S3 API 하나에 기대고 있다. 원래 계획은 MinIO였다.
D01에서 이미지 태그를 고정하려고 레지스트리를 조회했는데, MinIO 커뮤니티 이미지를 받을 수 없었다.

- Docker Hub `minio/minio`: 저장소 API가 404를 돌려줌
- quay.io `minio/minio`: 인증 없이는 조회가 안 됨
- 마지막 릴리스로 알려진 `RELEASE.2025-09-07T16-13-09Z` 태그: 두 레지스트리 모두 `docker manifest inspect`에서 매니페스트가 없음

제약은 네 가지다.
- 태그를 고정할 수 있는 공식 이미지가 있어야 한다(latest 금지).
- 단일 노트북(RAM 16GB)에서 core 프로파일로 늘 띄워 둔다.
- path-style S3 API로 boto3, DuckDB httpfs, DVC가 동작해야 한다.

## 선택지

| 선택지 | 장점 | 단점 | 측정값/근거 |
| --- | --- | --- | --- |
| SeaweedFS `server -s3` | 공식 이미지가 계속 배포됨, 컨테이너 하나로 master·volume·filer·S3가 다 뜸, 환경변수로 관리자 키 주입 가능 | MinIO 같은 관리 콘솔이 없음(Filer/Master UI만 있음), 기본값이 익명 허용이라 키 설정이 필수 | `chrislusf/seaweedfs:4.48` (index digest `sha256:4e61d15f…7872d`). 통합 테스트(버킷, put/get, DuckDB Parquet, 잘못된 키 403) 통과. 유휴 메모리는 ADR-0001 실측표 참고 |
| Garage | 매우 가벼움, S3 호환에 집중 | 처음 띄울 때 레이아웃과 키를 만드는 단계를 스크립트로 짜야 함 | 미측정 |
| MinIO 소스 빌드 | 계획 문서와 가장 가깝고, 콘솔이 있음 | 빌드 시간이 들고, 보안 패치를 직접 추적해야 함 | 미측정 |
| 로컬 폴더 | 가장 단순 | S3 API가 아니어서 replay, DVC, 원격 전환 경로가 달라짐 | - |

## 결정
SeaweedFS 4.48 단일 컨테이너(`weed server -filer -s3`)를 쓴다. 포트는 S3 9000, Master UI 9333, Filer UI 8888이다.

- 관리자 키는 `.env`의 `S3_ACCESS_KEY`/`S3_SECRET_KEY`를 `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` 환경변수로 넘긴다. 기동 로그 `Added admin identity from AWS environment variables`로 적용을 확인했다.
- 텔레메트리는 `-master.telemetry=false`로 끈다.
- S3 API 포트를 MinIO와 같은 9000으로 두어, 이후 문서와 설정을 거의 바꾸지 않고 쓸 수 있게 한다.

## 감수하는 점
- 사용자·정책 관리 GUI가 없다. 확인은 다음 수단으로 대신한다.
  - Filer UI(`/buckets/`)로 객체 탐색
  - Master UI로 볼륨 상태 확인
  - `weed shell`(`s3.bucket.list`, `fs.ls`)
  - `make test-integration`
- 단일 컨테이너라 내구성은 볼륨 1개 수준이다. 이 프로젝트는 노트북 1대 범위라 받아들인다.
- MinIO 전용 기능(`mc` 미러, 이벤트 알림)은 쓰지 않는다.

## 다시 검토할 조건
- DuckDB httpfs, pyarrow S3, DVC에서 SeaweedFS 호환성 문제가 반복될 때
- 유휴 메모리가 core 예산을 위협할 때
- MinIO 공식 이미지 배포가 재개되거나, 원격·클라우드 S3로 옮길 때
