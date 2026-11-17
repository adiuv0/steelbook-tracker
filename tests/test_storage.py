"""Telling a mapped data folder from one that dies with the container."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import storage

VOLUME_ID = "3f2a9c0d" * 8  # 64 hex characters, as Docker names an anonymous volume

ROOT = "2511 2301 0:412 / / rw,relatime master:801 - overlay overlay rw,lowerdir=/l,upperdir=/u,workdir=/w"
PROC = "2512 2511 0:415 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw"
# No mapping: Docker made a volume for the image's VOLUME /data. First as Unraid's
# docker.img shows it, then as a stock Docker install does.
ANONYMOUS_UNRAID = f"2520 2511 0:38 /volumes/{VOLUME_ID}/_data /data rw,noatime master:1 - btrfs /dev/loop2 rw,space_cache=v2"
ANONYMOUS_STOCK = f"2520 2511 8:1 /var/lib/docker/volumes/{VOLUME_ID}/_data /data rw,relatime master:1 - ext4 /dev/sda1 rw"
# What the Unraid template sets up: /mnt/user/appdata/steelbook-tracker -> /data.
BIND = "2520 2511 0:47 /appdata/steelbook-tracker /data rw,noatime master:1 - fuse.shfs shfs rw,user_id=0,group_id=0"
NAMED = "2520 2511 8:1 /var/lib/docker/volumes/steelbook/_data /data rw,relatime master:1 - ext4 /dev/sda1 rw"


def mounts(*lines: str) -> str:
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("line", [ANONYMOUS_UNRAID, ANONYMOUS_STOCK])
def test_unmapped_volume_is_not_persistent(line):
    result = storage.check(Path("/data"), mountinfo=mounts(ROOT, PROC, line), in_container=True)
    assert result.persistent is False
    assert "temporary Docker volume" in result.detail


def test_data_dir_inside_the_container_is_not_persistent():
    # DATA_DIR pointed somewhere with no mount at all: it lives in the container's own layer.
    result = storage.check(Path("/srv/tracker"), mountinfo=mounts(ROOT, PROC), in_container=True)
    assert result.persistent is False


@pytest.mark.parametrize("line", [BIND, NAMED])
def test_mapped_folder_and_named_volume_are_persistent(line):
    result = storage.check(Path("/data"), mountinfo=mounts(ROOT, PROC, line), in_container=True)
    assert result.persistent is True


def test_data_dir_below_a_mapped_folder_is_persistent():
    bind = BIND.replace(" /data ", " /config ")
    result = storage.check(Path("/config/tracker"), mountinfo=mounts(ROOT, bind), in_container=True)
    assert result.persistent is True


def test_a_similarly_named_mount_is_not_mistaken_for_the_data_dir():
    # /database is mapped, /data is not: /data must not ride on /database's mapping.
    other = BIND.replace(" /data ", " /database ")
    result = storage.check(Path("/data"), mountinfo=mounts(ROOT, other), in_container=True)
    assert result.persistent is False


def test_mount_point_with_a_space_is_matched():
    bind = BIND.replace(" /data ", " /my\\040data ")
    result = storage.check(Path("/my data"), mountinfo=mounts(ROOT, bind), in_container=True)
    assert result.persistent is True


def test_outside_a_container_nothing_is_flagged():
    assert storage.check(Path("/data"), mountinfo=mounts(ROOT), in_container=False).persistent is True


def test_unreadable_mount_table_gives_no_verdict(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "MOUNTINFO", tmp_path / "missing")
    assert storage.check(Path("/data"), in_container=True).persistent is None


@pytest.fixture
def client():
    return TestClient(main.app)  # no lifespan: the scheduler doesn't start


def test_page_and_summary_warn_when_data_will_not_survive_an_update(client, monkeypatch):
    monkeypatch.setattr(main, "data_storage", storage.Storage(False, "/data is a temporary Docker volume"))
    page = client.get("/").text
    assert 'role="alert"' in page
    assert "not being saved outside this container" in page
    assert "/mnt/user/appdata/steelbook-tracker" in page
    assert client.get("/api/summary").json()["data_persistent"] is False


@pytest.mark.parametrize("persistent", [True, None])
def test_no_warning_when_data_is_mapped_or_unknown(client, monkeypatch, persistent):
    monkeypatch.setattr(main, "data_storage", storage.Storage(persistent, ""))
    assert 'role="alert"' not in client.get("/").text
    assert client.get("/api/summary").json()["data_persistent"] is persistent
