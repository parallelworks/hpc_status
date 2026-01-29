"""Tests for scheduler-agnostic data normalization."""

import pytest
from src.data.normalization import (
    SchedulerType,
    detect_scheduler,
    normalize_node_state,
    normalize_queue_state,
    normalize_job_state,
    parse_walltime,
    normalize_resource_name,
    normalize_memory_to_gb,
    normalize_cluster_data,
)


class TestSchedulerDetection:
    def test_detect_pbs_explicit(self):
        assert detect_scheduler({"scheduler": "PBS"}) == SchedulerType.PBS
        assert detect_scheduler({"scheduler": "pbs"}) == SchedulerType.PBS
        assert detect_scheduler({"scheduler": "OpenPBS"}) == SchedulerType.PBS

    def test_detect_slurm_explicit(self):
        assert detect_scheduler({"scheduler": "Slurm"}) == SchedulerType.SLURM
        assert detect_scheduler({"scheduler": "SLURM"}) == SchedulerType.SLURM

    def test_detect_from_indicators(self):
        assert detect_scheduler({"pbs_version": "20.0"}) == SchedulerType.PBS
        assert detect_scheduler({"slurm_version": "23.02"}) == SchedulerType.SLURM

    def test_detect_from_queue_names(self):
        # PBS uses @ in queue names
        assert detect_scheduler({"queues": [{"name": "batch@server"}]}) == SchedulerType.PBS

    def test_detect_unknown(self):
        assert detect_scheduler({}) == SchedulerType.UNKNOWN


class TestNodeStateNormalization:
    def test_pbs_states(self):
        assert normalize_node_state("free", SchedulerType.PBS) == "IDLE"
        assert normalize_node_state("job-exclusive", SchedulerType.PBS) == "ALLOCATED"
        assert normalize_node_state("down", SchedulerType.PBS) == "DOWN"
        assert normalize_node_state("offline", SchedulerType.PBS) == "DOWN"
        assert normalize_node_state("maintenance", SchedulerType.PBS) == "MAINTENANCE"

    def test_slurm_states(self):
        assert normalize_node_state("idle", SchedulerType.SLURM) == "IDLE"
        assert normalize_node_state("alloc", SchedulerType.SLURM) == "ALLOCATED"
        assert normalize_node_state("mix", SchedulerType.SLURM) == "MIXED"
        assert normalize_node_state("drain", SchedulerType.SLURM) == "DRAINING"
        assert normalize_node_state("down", SchedulerType.SLURM) == "DOWN"
        assert normalize_node_state("down*", SchedulerType.SLURM) == "DOWN"
        assert normalize_node_state("maint", SchedulerType.SLURM) == "MAINTENANCE"

    def test_state_modifiers_stripped(self):
        assert normalize_node_state("idle*", SchedulerType.SLURM) == "IDLE"
        assert normalize_node_state("drain~", SchedulerType.SLURM) == "DRAINING"

    def test_generic_fallback(self):
        assert normalize_node_state("OFFLINE", SchedulerType.UNKNOWN) == "DOWN"
        assert normalize_node_state("unknown_state") == "UNKNOWN"


class TestQueueStateNormalization:
    def test_pbs_states(self):
        assert normalize_queue_state("started", SchedulerType.PBS) == "ACTIVE"
        assert normalize_queue_state("enabled", SchedulerType.PBS) == "ACTIVE"
        assert normalize_queue_state("stopped", SchedulerType.PBS) == "INACTIVE"
        assert normalize_queue_state("disabled", SchedulerType.PBS) == "INACTIVE"

    def test_slurm_states(self):
        assert normalize_queue_state("up", SchedulerType.SLURM) == "ACTIVE"
        assert normalize_queue_state("up*", SchedulerType.SLURM) == "ACTIVE"
        assert normalize_queue_state("down", SchedulerType.SLURM) == "OFFLINE"
        assert normalize_queue_state("drain", SchedulerType.SLURM) == "DRAINING"

    def test_generic_fallback(self):
        assert normalize_queue_state("running") == "ACTIVE"
        assert normalize_queue_state("offline") == "OFFLINE"


class TestJobStateNormalization:
    def test_pbs_states(self):
        assert normalize_job_state("Q", SchedulerType.PBS) == "PENDING"
        assert normalize_job_state("R", SchedulerType.PBS) == "RUNNING"
        assert normalize_job_state("H", SchedulerType.PBS) == "HELD"
        assert normalize_job_state("F", SchedulerType.PBS) == "COMPLETED"

    def test_slurm_states(self):
        assert normalize_job_state("PD", SchedulerType.SLURM) == "PENDING"
        assert normalize_job_state("R", SchedulerType.SLURM) == "RUNNING"
        assert normalize_job_state("CD", SchedulerType.SLURM) == "COMPLETED"
        assert normalize_job_state("F", SchedulerType.SLURM) == "FAILED"
        assert normalize_job_state("CA", SchedulerType.SLURM) == "CANCELLED"


class TestWalltimeParsing:
    def test_hms_format(self):
        seconds, display = parse_walltime("24:00:00")
        assert seconds == 86400
        # 24 hours = 1 day, so display could be either
        assert "24 hour" in display or "1 day" in display

    def test_dhms_pbs_format(self):
        seconds, display = parse_walltime("7:00:00:00")
        assert seconds == 7 * 86400
        assert "7 day" in display

    def test_dhms_slurm_format(self):
        seconds, display = parse_walltime("7-00:00:00")
        assert seconds == 7 * 86400
        assert "7 day" in display

    def test_hm_format(self):
        seconds, display = parse_walltime("01:30")
        assert seconds == 5400
        assert "hour" in display or "minute" in display

    def test_minutes_only(self):
        seconds, display = parse_walltime("60")
        assert seconds == 3600
        assert "1 hour" in display

    def test_special_values(self):
        seconds, display = parse_walltime("INFINITE")
        assert seconds is None
        assert display == "unlimited"

        seconds, display = parse_walltime("-")
        assert seconds is None

    def test_empty(self):
        seconds, display = parse_walltime("")
        assert seconds is None


class TestResourceNameNormalization:
    def test_cpu_variations(self):
        assert normalize_resource_name("ncpus") == "cores"
        assert normalize_resource_name("cpus") == "cores"
        assert normalize_resource_name("CORES") == "cores"
        assert normalize_resource_name("procs") == "cores"
        assert normalize_resource_name("ppn") == "cores"

    def test_node_variations(self):
        assert normalize_resource_name("nodes") == "nodes"
        assert normalize_resource_name("nodect") == "nodes"
        assert normalize_resource_name("nnodes") == "nodes"

    def test_gpu_variations(self):
        assert normalize_resource_name("gpus") == "gpus"
        assert normalize_resource_name("ngpus") == "gpus"
        assert normalize_resource_name("gres/gpu") == "gpus"

    def test_memory_variations(self):
        assert normalize_resource_name("mem") == "memory_gb"
        assert normalize_resource_name("vmem") == "memory_gb"


class TestMemoryNormalization:
    def test_gb_format(self):
        assert normalize_memory_to_gb("128gb") == 128.0
        assert normalize_memory_to_gb("128G") == 128.0

    def test_mb_format(self):
        assert normalize_memory_to_gb("1024mb") == 1.0
        assert normalize_memory_to_gb("2048M") == 2.0

    def test_tb_format(self):
        assert normalize_memory_to_gb("1tb") == 1024.0

    def test_no_unit(self):
        # No unit defaults to bytes, not GB (safer assumption)
        result = normalize_memory_to_gb("64")
        # 64 bytes = very small in GB
        assert result is not None and result < 0.001

    def test_invalid(self):
        assert normalize_memory_to_gb("") is None
        assert normalize_memory_to_gb("invalid") is None


class TestClusterDataNormalization:
    def test_basic_normalization(self):
        raw = {
            "scheduler": "PBS",
            "queues": [
                {"name": "batch", "state": "started", "max_walltime": "24:00:00"},
                {"name": "debug", "state": "enabled", "max_walltime": "00:30:00"},
            ],
        }

        normalized = normalize_cluster_data(raw)

        assert normalized["scheduler"] == "PBS"
        assert len(normalized["queues"]) == 2
        assert normalized["queues"][0]["state"] == "ACTIVE"
        assert normalized["queues"][0]["max_walltime_seconds"] == 86400
        assert normalized["queues"][0]["queue_type"] == "BATCH"
        assert normalized["queues"][1]["queue_type"] == "DEBUG"

    def test_auto_detect_scheduler(self):
        raw = {
            "queues": [{"name": "batch@server"}],
        }

        normalized = normalize_cluster_data(raw)
        assert normalized["scheduler"] == "PBS"
        assert normalized["scheduler_detected"] is True

    def test_node_normalization(self):
        raw = {
            "scheduler": "SLURM",
            "nodes": [
                {"state": "idle", "ncpus": 32},
                {"state": "alloc", "ncpus": 64},
            ],
        }

        normalized = normalize_cluster_data(raw)
        assert normalized["nodes"][0]["state"] == "IDLE"
        assert normalized["nodes"][0]["cores"] == 32
        assert normalized["nodes"][1]["state"] == "ALLOCATED"
