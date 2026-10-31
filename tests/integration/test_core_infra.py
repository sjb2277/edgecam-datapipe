"""core 프로파일 기동 후 호스트에서 접속을 확인한다. `make up PROFILE=core` 선행 필요."""

import json
import os
import socket
import urllib.request

import pytest

pytestmark = pytest.mark.integration

KAFKA_HOST = os.getenv("KAFKA_EXTERNAL_HOST", "localhost")
KAFKA_PORT = int(os.getenv("KAFKA_EXTERNAL_PORT", "19092"))
SR_URL = os.getenv("SCHEMA_REGISTRY_URL", "http://localhost:18081")


def test_kafka_external_listener_reachable() -> None:
    with socket.create_connection((KAFKA_HOST, KAFKA_PORT), timeout=5):
        pass


def test_schema_registry_subjects() -> None:
    with urllib.request.urlopen(f"{SR_URL}/subjects", timeout=5) as resp:
        assert resp.status == 200
        assert isinstance(json.load(resp), list)
