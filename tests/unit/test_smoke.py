import importlib

import pytest

import pipe

SUBPACKAGES = ["common", "capture", "link", "inference", "ingest", "gateway", "control", "quality"]


def test_version() -> None:
    assert pipe.__version__


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_importable(name: str) -> None:
    importlib.import_module(f"pipe.{name}")
