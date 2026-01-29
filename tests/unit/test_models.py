"""Tests for data models."""

import pytest
from src.data.models import (
    StorageInfo,
    StorageHealthStatus,
    SystemStatus,
    SystemOperationalStatus,
    AllocationInfo,
    AllocationStatus,
    QueueInfo,
    QueueState,
    UserContext,
    InsightSeverity,
    SystemInsight,
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
        assert storage.status == StorageHealthStatus.HEALTHY
        assert storage.status_str == "healthy"

    def test_status_warning(self):
        storage = StorageInfo(
            mount_point="$HOME",
            filesystem="/dev/sda1",
            total_gb=100.0,
            used_gb=85.0,
            available_gb=15.0,
            percent_used=85.0,
        )
        assert storage.status == StorageHealthStatus.WARNING
        assert storage.status_str == "warning"

    def test_status_critical(self):
        storage = StorageInfo(
            mount_point="$HOME",
            filesystem="/dev/sda1",
            total_gb=100.0,
            used_gb=96.0,
            available_gb=4.0,
            percent_used=96.0,
        )
        assert storage.status == StorageHealthStatus.CRITICAL
        assert storage.status_str == "critical"


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


class TestQueueInfo:
    def test_walltime_parsing_hms(self):
        queue = QueueInfo(
            name="standard",
            queue_type="BATCH",
            max_walltime="24:00:00",
        )
        assert queue.max_walltime_seconds == 86400

    def test_walltime_parsing_dhms(self):
        queue = QueueInfo(
            name="long",
            queue_type="BATCH",
            max_walltime="7:00:00:00",
        )
        assert queue.max_walltime_seconds == 7 * 86400

    def test_default_state(self):
        queue = QueueInfo(
            name="standard",
            queue_type="BATCH",
            max_walltime="24:00:00",
        )
        assert queue.state == QueueState.ACTIVE


class TestAllocationInfo:
    def test_status_healthy(self):
        alloc = AllocationInfo(
            system="Nautilus",
            subproject="PROJ001",
            hours_allocated=100000,
            hours_used=50000,
            hours_remaining=50000,
            percent_remaining=50.0,
        )
        assert alloc.status == AllocationStatus.HEALTHY

    def test_status_low(self):
        alloc = AllocationInfo(
            system="Nautilus",
            subproject="PROJ001",
            hours_allocated=100000,
            hours_used=85000,
            hours_remaining=15000,
            percent_remaining=15.0,
        )
        assert alloc.status == AllocationStatus.LOW

    def test_status_critical(self):
        alloc = AllocationInfo(
            system="Nautilus",
            subproject="PROJ001",
            hours_allocated=100000,
            hours_used=98000,
            hours_remaining=2000,
            percent_remaining=2.0,
        )
        assert alloc.status == AllocationStatus.CRITICAL

    def test_status_exhausted(self):
        alloc = AllocationInfo(
            system="Nautilus",
            subproject="PROJ001",
            hours_allocated=100000,
            hours_used=100000,
            hours_remaining=0,
            percent_remaining=0.0,
        )
        assert alloc.status == AllocationStatus.EXHAUSTED

    def test_percent_used_calculated(self):
        alloc = AllocationInfo(
            system="Nautilus",
            subproject="PROJ001",
            hours_allocated=100000,
            hours_used=72000,
            hours_remaining=28000,
            percent_remaining=28.0,
        )
        assert alloc.percent_used == 72.0


class TestSystemInsight:
    def test_severity_from_priority_critical(self):
        insight = SystemInsight(
            type="ALERT",
            message="System is down",
            priority=5,
        )
        assert insight.severity == InsightSeverity.CRITICAL

    def test_severity_from_priority_warning(self):
        insight = SystemInsight(
            type="WARNING",
            message="High queue depth",
            priority=4,
        )
        assert insight.severity == InsightSeverity.WARNING

    def test_severity_from_priority_info(self):
        insight = SystemInsight(
            type="INFO",
            message="System operational",
            priority=2,
        )
        assert insight.severity == InsightSeverity.INFO

    def test_severity_from_priority_suggestion(self):
        insight = SystemInsight(
            type="RECOMMENDATION",
            message="Consider debug queue",
            priority=1,
        )
        assert insight.severity == InsightSeverity.SUGGESTION


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

    def test_operational_status_enum(self):
        system = SystemStatus(system="Nautilus", status="UP")
        assert system.operational_status == SystemOperationalStatus.UP

    def test_operational_status_degraded(self):
        system = SystemStatus(system="Nautilus", status="DEGRADED")
        assert system.operational_status == SystemOperationalStatus.DEGRADED

    def test_operational_status_unknown_value(self):
        system = SystemStatus(system="Nautilus", status="INVALID")
        assert system.operational_status == SystemOperationalStatus.UNKNOWN
