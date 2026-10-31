"""Cold 저장소(SeaweedFS S3) 호환성 확인. `make up PROFILE=core` 후 `make test-integration`."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import boto3
import duckdb
import pytest
from botocore.client import Config
from botocore.exceptions import ClientError

pytestmark = pytest.mark.integration

S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://localhost:9000")
S3_REGION = os.getenv("S3_REGION", "us-east-1")


def _client(access_key: str, secret_key: str):
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        region_name=S3_REGION,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
    )


@pytest.fixture(scope="module")
def creds() -> tuple[str, str]:
    access_key, secret_key = os.getenv("S3_ACCESS_KEY"), os.getenv("S3_SECRET_KEY")
    if not access_key or not secret_key:
        pytest.skip("S3_ACCESS_KEY/S3_SECRET_KEY 미설정 (.env 로드 필요)")
    return access_key, secret_key


@pytest.fixture(scope="module")
def s3(creds: tuple[str, str]):
    return _client(*creds)


@pytest.fixture(scope="module")
def bucket(s3) -> Iterator[str]:
    """테스트 전용 버킷. 이 테스트가 만든 객체와 버킷만 지운다."""
    name = f"it-{uuid.uuid4().hex[:12]}"
    s3.create_bucket(Bucket=name)
    yield name
    for obj in s3.list_objects_v2(Bucket=name).get("Contents", []):
        s3.delete_object(Bucket=name, Key=obj["Key"])
    s3.delete_bucket(Bucket=name)


def test_bucket_listed(s3, bucket: str) -> None:
    names = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    assert bucket in names


def test_object_roundtrip(s3, bucket: str) -> None:
    body = b"edgecam cold storage roundtrip"
    s3.put_object(Bucket=bucket, Key="smoke/hello.txt", Body=body)
    got = s3.get_object(Bucket=bucket, Key="smoke/hello.txt")["Body"].read()
    assert got == body


def test_duckdb_reads_parquet_from_s3(
    s3, bucket: str, creds: tuple[str, str], tmp_path: Path
) -> None:
    local = tmp_path / "sample.parquet"
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT range AS seq, 'cam0' AS source FROM range(3)) TO '{local}' (FORMAT parquet)"
    )
    s3.upload_file(str(local), bucket, "raw/source=cam0/run_id=it/sample.parquet")

    access_key, secret_key = creds
    endpoint = S3_ENDPOINT.removeprefix("http://").removeprefix("https://")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        "CREATE SECRET it_s3 (TYPE s3, KEY_ID ?, SECRET ?, REGION ?, ENDPOINT ?, "
        "URL_STYLE 'path', USE_SSL ?)",
        [access_key, secret_key, S3_REGION, endpoint, S3_ENDPOINT.startswith("https://")],
    )
    rows = con.execute(
        f"SELECT count(*), max(seq) FROM read_parquet('s3://{bucket}/raw/source=cam0/*/*.parquet')"
    ).fetchone()
    assert rows == (3, 2)


def test_wrong_credentials_rejected(bucket: str) -> None:
    bad = _client("wrong-access-key", "wrong-secret-key")
    with pytest.raises(ClientError) as exc:
        bad.list_objects_v2(Bucket=bucket)
    assert exc.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403
