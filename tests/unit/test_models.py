"""Tests for data models."""

import pytest
from src.data.models import (
    StorageInfo,
    SystemStatus,
    AllocationInfo,
    QueueInfo,
    UserContext,
)


class TestStorageInfo:
    def test_status_healthy(self):
        storage = StorageInfo(
            mount_point="$HOME",
            filesystem="/dev/sda1",
            total_gb=100.0,
            used_gb=50.0,
            available_gb=50.0,
            percent_used=50.0,
        )
        assert storage.status == "healthy"

    def test_status_warning(self):
        storage = StorageInfo(
            mount_point="$HOME",
            filesystem="/dev/sda1",
            total_gb=100.0,
            used_gb=85.0,
            available_gb=15.0,
            percent_used=85.0,
        )
        assert storage.status == "warning"

    def test_status_critical(self):
        storage = StorageInfo(
            mount_point="$HOME",
            filesystem="/dev/sda1",
            total_gb=100.0,
            used_gb=96.0,
            available_gb=4.0,
            percent_used=96.0,
        )
        assert storage.status == "critical"


class TestSystemStatus:
    def test_slug_generation(self):
        system = SystemStatus(system="Nautilus", status="UP")
        assert system.slug == "nautilus"

    def test_slug_with_special_chars(self):
        system = SystemStatus(system="Jean-ZAC", status="UP")
        assert system.slug == "jeanzac"

    def test_slug_empty_system(self):
        system = SystemStatus(system="", status="UP")
        assert system.slug == ""


class TestUserContext:
    def test_home_storage(self):
        home = StorageInfo(
            mount_point="$HOME",
            filesystem="/dev/sda1",
            total_gb=100.0,
            used_gb=50.0,
            available_gb=50.0,
            percent_used=50.0,
        )
        work = StorageInfo(
            mount_point="$WORKDIR",
            filesystem="/dev/sdb1",
            total_gb=1000.0,
            used_gb=200.0,
            available_gb=800.0,
            percent_used=20.0,
        )
        user = UserContext(username="testuser", storage=[home, work])

        assert user.home_storage() == home
        assert user.workdir_storage() == work

    def test_no_storage(self):
        user = UserContext(username="testuser")
        assert user.home_storage() is None
        assert user.workdir_storage() is None
