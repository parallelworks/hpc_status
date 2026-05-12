"""Tests for the NOAA / raw-Slurm parsers used by pw_cluster.py.

Fixtures under tests/fixtures/ are fully anonymised — no real user or
project identifiers, no real disk-usage figures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.collectors import _slurm_helpers as sh


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


# ---------------------------------------------------------------------------
# Capability probe
# ---------------------------------------------------------------------------


def test_capability_probe_round_trip():
    probe = sh.build_capability_probe()
    # The shell snippet should reference every command we care about
    for name in sh.PROBE_COMMANDS:
        assert name in probe

    fake_stdout = "\n".join(
        [
            "show_usage=1",
            "show_queues=0",
            "show_storage=0",
            "saccount_params=1",
            "sfairshare=1",
            "sreport=1",
            "sshare=1",
            "sinfo=1",
            "squeue=1",
            "sacctmgr=1",
            "scontrol=1",
            "lfs=0",
            "quota=1",
            "nvidia-smi=0",
        ]
    )
    caps = sh.parse_capability_probe(fake_stdout)
    assert caps["show_usage"] is True
    assert caps["show_queues"] is False
    assert caps["saccount_params"] is True
    assert caps["sfairshare"] is True
    assert caps["sreport"] is True
    assert caps["lfs"] is False
    assert caps["nvidia-smi"] is False


def test_capability_probe_handles_missing_lines():
    # Anything we didn't see → False
    caps = sh.parse_capability_probe("show_usage=1\n")
    assert caps["show_usage"] is True
    assert caps["saccount_params"] is False


# ---------------------------------------------------------------------------
# saccount_params parser
# ---------------------------------------------------------------------------


def test_parse_saccount_params_extracts_projects_and_quotas():
    output = _read("noaa_saccount_params.txt")
    systems, storage, home = sh.parse_saccount_params(output, "hera")

    assert len(systems) == 2
    alpha = next(s for s in systems if s["subproject"] == "project-alpha")
    assert alpha["system"] == "hera"
    assert alpha["fairshare_score"] == 0.125
    assert alpha["fairshare_rank"] == "10/40"
    assert alpha["partition_access"] == "ALL"
    assert "batch" in alpha["qoses"]
    assert "windfall" in alpha["qoses"]

    beta = next(s for s in systems if s["subproject"] == "project-beta")
    assert beta["fairshare_score"] is None
    assert beta["qoses"] == []

    # Home quota mapped to df-style row
    assert home["filesystem"] == "/home/first.last"
    assert home["used"].endswith("G")  # 4096 MB → 4.0G
    assert home["percent_used"]  # non-empty

    # Per-directory storage
    assert "scratch3_project-alpha" in storage
    assert "scratch4_project-alpha" in storage
    assert "scratch3_project-beta" in storage

    s3a = storage["scratch3_project-alpha"]
    assert s3a["used"] == "12000G"
    assert s3a["size"] == "400000G"
    assert s3a["files_used"] == 1234567
    assert s3a["files_quota"] == 320000000
    assert s3a["subproject"] == "project-alpha"


def test_parse_saccount_params_handles_empty_output():
    systems, storage, home = sh.parse_saccount_params("", "anywhere")
    assert systems == []
    assert storage == {}
    assert home == {}


# ---------------------------------------------------------------------------
# sshare parser
# ---------------------------------------------------------------------------


def test_parse_sshare_usage_converts_seconds_to_hours():
    output = _read("slurm_sshare.txt")
    rows = sh.parse_sshare_usage(output, "gaea", user="user-a")
    assert len(rows) == 3
    alpha = next(r for r in rows if r["subproject"] == "project-alpha")
    # 36000 seconds = 10 hours
    assert alpha["hours_used"] == 10
    assert alpha["system"] == "gaea"
    assert alpha["user"] == "user-a"


def test_parse_sshare_usage_filters_by_user_when_provided():
    output = "acct1|user-a|7200\nacct1|user-b|10800\n"
    rows = sh.parse_sshare_usage(output, "gaea", user="user-a")
    # Only the user-a row should survive
    assert len(rows) == 1
    assert rows[0]["user"] == "user-a"
    assert rows[0]["hours_used"] == 2


# ---------------------------------------------------------------------------
# scontrol / squeue / sacctmgr parsers + queue assembly
# ---------------------------------------------------------------------------


def test_parse_slurm_nodes_groups_by_partition_and_state():
    output = _read("slurm_scontrol_nodes.txt")
    info = sh.parse_slurm_nodes(output)
    by_part = info["by_partition"]

    # batch: 3 up (ALLOCATED, MIXED, IDLE), 1 down (DRAIN)
    assert by_part["batch"]["nodes_up"] == 3
    assert by_part["batch"]["nodes_down"] == 1
    assert by_part["batch"]["cores_up"] == 3 * 128
    # cores_per_node is now per-partition (cores_up / nodes_up), not modal.
    assert by_part["batch"]["cores_per_node"] == 128

    # gpu: 2 up (IDLE, ALLOCATED), 0 down
    assert by_part["gpu"]["nodes_up"] == 2
    assert by_part["gpu"]["nodes_down"] == 0
    assert by_part["gpu"]["cores_up"] == 2 * 64
    assert by_part["gpu"]["cores_per_node"] == 64

    # Overall CPN = total_cores / total_nodes
    assert info["overall"]["cores_per_node"] == (3 * 128 + 2 * 64) // 5


def test_parse_slurm_nodes_skips_nodes_without_partitions():
    # Login / management / orphaned nodes have no Partitions= field on
    # NOAA Gaea. They shouldn't appear as an "unknown" bucket.
    blob = (
        "NodeName=login01 Sockets=2 CoresPerSocket=64 CPUTot=128 "
        "State=IDLE\n"
        "NodeName=cn01 Sockets=2 CoresPerSocket=64 CPUTot=128 "
        "State=IDLE Partitions=batch\n"
    )
    info = sh.parse_slurm_nodes(blob)
    assert set(info["by_partition"].keys()) == {"batch"}
    assert info["by_partition"]["batch"]["nodes_up"] == 1


def test_parse_slurm_nodes_extracts_heterogeneous_cpn_and_gpus():
    # Mirrors NOAA Ursa: u1-gh nodes have CPUTot=72 + Gres=gpu:gh200:1,
    # u1-mi300x nodes have CPUTot=96 + Gres=gpu:mi300x:8. The previous
    # implementation showed the cluster-wide modal which made the
    # "nodes × cpn vs cores_available" math look broken.
    blob = (
        "NodeName=gh01 CPUTot=72 Gres=gpu:gh200:1 State=IDLE "
        "Partitions=u1-gh\n"
        "NodeName=gh02 CPUTot=72 Gres=gpu:gh200:1 State=MIXED "
        "Partitions=u1-gh\n"
        "NodeName=mi01 CPUTot=96 Gres=gpu:mi300x:8 State=ALLOCATED "
        "Partitions=u1-mi300x\n"
        "NodeName=cn01 CPUTot=192 Gres=(null) State=IDLE "
        "Partitions=u1-compute\n"
    )
    info = sh.parse_slurm_nodes(blob)
    gh = info["by_partition"]["u1-gh"]
    mi = info["by_partition"]["u1-mi300x"]
    cn = info["by_partition"]["u1-compute"]

    # Per-partition CPN matches the actual hardware, not the cluster modal
    assert gh["cores_per_node"] == 72
    assert mi["cores_per_node"] == 96
    assert cn["cores_per_node"] == 192

    # GPU counts come from Gres
    assert gh["gpus_per_node"] == 1
    assert gh["gpus_up"] == 2
    assert gh["gpu_types"] == ["gh200"]
    assert mi["gpus_per_node"] == 8
    assert mi["gpus_up"] == 8
    assert mi["gpu_types"] == ["mi300x"]
    assert cn["gpus_per_node"] == 0
    assert cn["gpus_up"] == 0
    assert cn["gpu_types"] == []


def test_parse_gres_gpus_handles_common_forms():
    assert sh._parse_gres_gpus("gpu:h100:4") == (4, ["h100"])
    assert sh._parse_gres_gpus("gpu:gh200:1") == (1, ["gh200"])
    assert sh._parse_gres_gpus("(null)") == (0, [])
    assert sh._parse_gres_gpus("") == (0, [])
    # Multiple GPU lines collapse: count sums, types dedup.
    assert sh._parse_gres_gpus("gpu:h100:4,gpu:a100:8") == (12, ["h100", "a100"])
    # No explicit count → 1 per entry (uncommon but valid).
    assert sh._parse_gres_gpus("gpu:h100") == (1, ["h100"])


def test_parse_squeue_jobs_extracts_fields():
    output = _read("slurm_squeue.txt")
    rows = sh.parse_squeue_jobs(output)
    assert len(rows) == 5
    job100 = rows[0]
    assert job100["jobid"] == "100"
    assert job100["user"] == "user-a"
    assert job100["state"] == "RUNNING"
    assert job100["cpus"] == "256"


def test_build_slurm_queue_data_aggregates_running_and_pending():
    nodes_blob = _read("slurm_scontrol_nodes.txt")
    squeue_blob = _read("slurm_squeue.txt")

    node_info = sh.parse_slurm_nodes(nodes_blob)
    squeue_rows = sh.parse_squeue_jobs(squeue_blob)
    qd = sh.build_slurm_queue_data({}, node_info, squeue_rows, partition_info=None)

    queues = {q["queue_name"]: q for q in qd["queues"]}
    # batch: 2 running (256+128=384 cores), 1 pending (512 cores)
    assert queues["batch"]["jobs_running"] == "2"
    assert queues["batch"]["jobs_pending"] == "1"
    assert queues["batch"]["cores_running"] == "384"
    assert queues["batch"]["cores_pending"] == "512"

    # gpu: 1 running (64), 1 pending (128)
    assert queues["gpu"]["jobs_running"] == "1"
    assert queues["gpu"]["jobs_pending"] == "1"
    assert queues["gpu"]["cores_running"] == "64"

    # Node rows include cores_free = cores_up - cores_running
    nodes = {n["node_type"]: n for n in qd["nodes"]}
    assert nodes["batch"]["cores_available"] == str(3 * 128)
    assert nodes["batch"]["cores_running"] == "384"
    assert nodes["batch"]["cores_free"] == str(3 * 128 - 384)


def test_parse_sacctmgr_qos_keyed_by_name():
    header_and_rows = (
        "Name|Priority|State|MaxWall|MaxJobs|GrpJobs|MaxTRES\n"
        "batch|100|UP|3-00:00:00|50||node=512\n"
        "debug|200|UP|00:30:00|2||node=4\n"
    )
    info = sh.parse_sacctmgr_qos(header_and_rows)
    assert set(info.keys()) == {"batch", "debug"}
    assert info["batch"]["MaxWall"] == "3-00:00:00"
    assert info["batch"]["MaxTRES"] == "node=512"


# ---------------------------------------------------------------------------
# df blob parser
# ---------------------------------------------------------------------------


def test_parse_sfairshare_csv_extracts_metrics():
    output = _read("noaa_sfairshare.csv")
    rows = sh.parse_sfairshare_csv(output)
    assert set(rows.keys()) == {
        "project-alpha",
        "project-beta",
        "project-gamma (W)",
        "project-delta",
    }
    alpha = rows["project-alpha"]
    assert alpha["fairshare"] == 0.541234
    assert alpha["rank_str"] == "12/40"
    assert alpha["norm_shares"] == 0.043210
    assert alpha["eff_usage"] == 0.038456
    assert alpha["raw_shares"] == 203674.0
    # 4786415009 sec / 3600 ≈ 1329559 hours
    assert alpha["raw_usage_hours"] == 1329559

    # Windfall row shows up but with zeros
    wf = rows["project-gamma (W)"]
    assert wf["fairshare"] == 1.0
    assert wf["raw_usage_hours"] == 0


def test_parse_sfairshare_csv_handles_empty():
    assert sh.parse_sfairshare_csv("") == {}


def test_parse_sreport_account_user_returns_account_totals():
    output = _read("noaa_sreport_account_user.txt")
    totals = sh.parse_sreport_account_user(output)
    # Only rows with empty user field count as account totals
    assert totals == {
        "project-alpha": 10500,
        "project-beta": 7200,
        "project-delta": 120000,
    }


def test_fiscal_year_start_rolls_over_in_october():
    import datetime as dt

    # Before Oct 1: FY starts in the prior year
    assert sh.fiscal_year_start(dt.date(2026, 5, 12)) == "2025-10-01"
    # On Oct 1: new FY begins
    assert sh.fiscal_year_start(dt.date(2026, 10, 1)) == "2026-10-01"
    # December: same FY as October
    assert sh.fiscal_year_start(dt.date(2026, 12, 31)) == "2026-10-01"


def test_parse_df_blob_groups_by_label():
    blob = """HOME:
nfs:/home  100G  60G  40G  60% /home
WORK:
nfs:/work  500G  200G  300G  40% /work
SCRATCH:
/scratch  10T  4T  6T  40% /scratch
"""
    storage = sh.parse_df_blob(blob)
    assert set(storage.keys()) == {"home", "work", "scratch"}
    assert storage["scratch"]["size"] == "10T"
    assert storage["scratch"]["percent_used"] == "40"
