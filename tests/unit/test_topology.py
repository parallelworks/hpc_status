"""Tests for the fleet topology graph builder."""

from datetime import datetime, timedelta

import pytest

from src.data.persistence import DataStore
from src.data.topology import (
    build_topology,
    describe_site,
    index_clusters,
    match_cluster,
    resolve_site_id,
    slugify,
)


@pytest.fixture
def fleet_payload():
    return {
        "meta": {
            "generated_at": "2026-07-29T12:00:00Z",
            "source_url": "https://centers.hpc.mil/systems/unclassified.html",
        },
        "systems": [
            {
                "system": "Narwhal",
                "status": "UP",
                "dsrc": "navy",
                "login": "narwhal.navydsrc.hpc.mil",
                "scheduler": "slurm",
                "observed_at": "2026-07-29T12:00:00Z",
                "raw_alt": "Narwhal is currently Up.",
            },
            {
                "system": "Carpenter",
                "status": "DEGRADED",
                "dsrc": "ERDC DSRC",
                "login": "carpenter.erdc.hpc.mil",
                "scheduler": "pbs",
                "observed_at": "2026-07-29T12:00:00Z",
            },
            {
                "system": "Mystery",
                "status": "DOWN",
                "dsrc": None,
                "login": None,
                "scheduler": None,
            },
        ],
    }


@pytest.fixture
def cluster_payload():
    return [
        {
            "cluster_metadata": {
                "name": "narwhal",
                "uri": "pw://user/narwhal",
                "status": "on",
                "type": "existing",
                "timestamp": "2026-07-29T11:58:00Z",
                "has_scheduler": True,
                "capabilities": {"squeue": True, "sinfo": True, "nvidia_smi": False},
            },
            "usage_data": {
                "systems": [
                    {
                        "system": "narwhal",
                        "hours_allocated": "1,000",
                        "hours_used": "600",
                        "hours_remaining": "400",
                    }
                ]
            },
            "queue_data": {
                "queues": [
                    {
                        "queue_name": "standard",
                        "jobs_running": 10,
                        "jobs_pending": 4,
                        "cores_running": 800,
                        "cores_pending": 320,
                    }
                ],
                "nodes": [{"node_type": "standard", "cores_available": 1000}],
                "cluster_totals": {
                    "cores_total": 1000,
                    "cores_running": 800,
                    "nodes_total": 20,
                    "gpus_total": 8,
                },
            },
            "system_info": {"hostname": "nfe01", "cpu_count": 128},
        },
        {
            "cluster_metadata": {
                "name": "sidecar",
                "uri": "pw://user/sidecar",
                "status": "on",
                "type": "existing",
                "timestamp": "2026-07-29T11:59:00Z",
                "has_scheduler": False,
                "capabilities": {},
            },
            "usage_data": {},
            "queue_data": {},
            "system_info": {},
        },
    ]


class TestHelpers:
    def test_slugify_strips_everything_but_alphanumerics(self):
        assert slugify("ERDC DSRC-1") == "erdcdsrc1"
        assert slugify(None) == ""

    def test_resolve_site_id_canonicalizes_dsrc_labels(self):
        assert resolve_site_id("ERDC DSRC", "Carpenter") == "erdc"
        assert resolve_site_id("erdc", "Carpenter") == "erdc"

    def test_resolve_site_id_falls_back_to_system_name_hint(self):
        # PW reports type="existing", which says nothing about location.
        assert resolve_site_id("existing", "gaea-c5") == "ornl"

    def test_resolve_site_id_unassigned_when_unknown(self):
        assert resolve_site_id(None, "somebox") == "unassigned"

    def test_describe_site_merges_overrides(self):
        site = describe_site("erdc", {"erdc": {"location": "Somewhere else"}})
        assert site["name"] == "ERDC DSRC"
        assert site["location"] == "Somewhere else"

    def test_match_cluster_prefers_exact_then_prefix(self, cluster_payload):
        index = index_clusters(cluster_payload)
        assert match_cluster("narwhal", index) == "narwhal"
        assert match_cluster("narwhalprod", index) == "narwhal"
        assert match_cluster("nope", index) is None

    def test_match_cluster_ignores_short_slugs(self):
        # Fuzzy matching on 3-letter names would pair unrelated systems.
        index = {"jean": {}}
        assert match_cluster("jea", index) is None


class TestBuildTopology:
    def test_builds_three_tier_graph(self, fleet_payload, cluster_payload):
        graph = build_topology(fleet_payload, cluster_payload, platform="hpcmp")

        kinds = [node["kind"] for node in graph["nodes"]]
        assert kinds.count("monitor") == 1
        # 3 fleet systems + 1 unmatched PW cluster
        assert kinds.count("system") == 4
        assert kinds.count("site") == len(graph["sites"])

        # Every site and system is reachable from the monitor.
        edge_targets = {edge["target"] for edge in graph["edges"]}
        for node in graph["nodes"]:
            if node["kind"] != "monitor":
                assert node["id"] in edge_targets

    def test_merges_cluster_telemetry_into_matching_system(
        self, fleet_payload, cluster_payload
    ):
        graph = build_topology(fleet_payload, cluster_payload)
        narwhal = next(n for n in graph["nodes"] if n.get("slug") == "narwhal")

        assert narwhal["origin"] == "both"
        assert narwhal["connected"] is True
        assert narwhal["capacity"]["cores_total"] == 1000
        assert narwhal["capacity"]["utilization_percent"] == 80.0
        assert narwhal["queues"]["pending_jobs"] == 4
        assert narwhal["allocation"]["percent_remaining"] == 40.0
        assert narwhal["connection"]["capabilities"] == ["sinfo", "squeue"]

    def test_unmatched_systems_have_no_telemetry(self, fleet_payload, cluster_payload):
        graph = build_topology(fleet_payload, cluster_payload)
        carpenter = next(n for n in graph["nodes"] if n.get("slug") == "carpenter")

        assert carpenter["origin"] == "fleet"
        assert carpenter["connected"] is False
        assert carpenter["capacity"] is None
        assert carpenter["status"] == "DEGRADED"

    def test_pw_only_cluster_becomes_its_own_node(self, fleet_payload, cluster_payload):
        graph = build_topology(fleet_payload, cluster_payload)
        sidecar = next(n for n in graph["nodes"] if n.get("slug") == "sidecar")

        assert sidecar["origin"] == "pw"
        assert sidecar["site"] == "unassigned"
        assert sidecar["connected"] is True

    def test_site_rollup_and_summary(self, fleet_payload, cluster_payload):
        graph = build_topology(fleet_payload, cluster_payload, platform="hpcmp")

        navy = next(site for site in graph["sites"] if site["id"] == "navy")
        assert navy["systems"] == 1
        assert navy["connected"] == 1
        assert navy["location"] == "Stennis Space Center, MS"

        summary = graph["summary"]
        assert summary["systems"] == 4
        assert summary["connected"] == 2
        assert summary["status_counts"]["UP"] == 2  # Narwhal + sidecar
        assert summary["capacity"]["cores_total"] == 1000
        assert graph["meta"]["site_label"] == "DSRC"

    def test_generic_platform_labels_sites_generically(self, fleet_payload):
        graph = build_topology(fleet_payload, [], platform="generic")
        assert graph["meta"]["site_label"] == "Site"

    def test_address_lookup_is_used_for_dns_names_only(self, fleet_payload):
        seen = []

        def lookup(host):
            seen.append(host)
            return "10.0.0.1"

        graph = build_topology(fleet_payload, [], address_lookup=lookup)
        narwhal = next(n for n in graph["nodes"] if n.get("slug") == "narwhal")
        mystery = next(n for n in graph["nodes"] if n.get("slug") == "mystery")

        assert narwhal["address"] == "10.0.0.1"
        assert mystery["address"] is None  # no login node to resolve
        assert "narwhal.navydsrc.hpc.mil" in seen

    def test_connection_history_is_attached(self, fleet_payload, cluster_payload):
        now = datetime(2026, 7, 29, 12, 0, 0)
        stats = {
            "cluster:narwhal": {
                "first_seen": "2026-07-01T00:00:00Z",
                "connected_since": "2026-07-29T09:00:00Z",
                "last_change": "2026-07-29T09:00:00Z",
                "uptime_ratio": 0.95,
                "uptime_window_hours": 24,
                "transitions": 2,
            }
        }
        graph = build_topology(
            fleet_payload, cluster_payload, connection_stats=stats, now=now
        )
        narwhal = next(n for n in graph["nodes"] if n.get("slug") == "narwhal")

        assert narwhal["connection"]["connected_for_seconds"] == 3 * 3600
        assert narwhal["connection"]["uptime_ratio"] == 0.95

    def test_insights_attach_to_their_node(self, fleet_payload, cluster_payload):
        insights = [
            {
                "type": "warning",
                "message": "narwhal: Allocation critically low (4% remaining)",
                "priority": 5,
                "cluster": "narwhal",
            },
            {
                "type": "info",
                "message": "narwhal/standard: pending demand is 2.1x the running load",
                "priority": 2,
                "cluster": "narwhal",
                "queue": "standard",
            },
            {
                "type": "info",
                "message": "somewhere else entirely",
                "priority": 1,
                "cluster": "unrelated",
            },
        ]
        graph = build_topology(fleet_payload, cluster_payload, insights=insights)

        narwhal = next(n for n in graph["nodes"] if n.get("slug") == "narwhal")
        carpenter = next(n for n in graph["nodes"] if n.get("slug") == "carpenter")

        assert len(narwhal["insights"]) == 2
        # Highest priority first, and the severity reflects the worst one.
        assert narwhal["insights"][0]["priority"] == 5
        assert narwhal["alert"] == "critical"
        assert carpenter["insights"] == []
        assert carpenter["alert"] is None

        navy = next(site for site in graph["sites"] if site["id"] == "navy")
        assert navy["alerts"] == 1
        assert graph["summary"]["alerts"] == 1

    def test_latency_reaches_node_and_edge(self, fleet_payload, cluster_payload):
        cluster_payload[0]["cluster_metadata"]["latency_ms"] = 412
        graph = build_topology(fleet_payload, cluster_payload)

        narwhal = next(n for n in graph["nodes"] if n.get("slug") == "narwhal")
        assert narwhal["connection"]["latency_ms"] == 412

        edge = next(e for e in graph["edges"] if e["target"] == "sys:narwhal")
        assert edge["latency_ms"] == 412

    def test_status_spans_are_passed_through(self, fleet_payload, cluster_payload):
        stats = {
            "cluster:narwhal": {
                "uptime_ratio": 0.5,
                "uptime_window_hours": 24,
                "window_start": "2026-07-28T12:00:00Z",
                "window_end": "2026-07-29T12:00:00Z",
                "spans": [
                    {"status": "UP", "from": "2026-07-28T12:00:00Z", "to": "2026-07-29T00:00:00Z", "seconds": 43200},
                    {"status": "DOWN", "from": "2026-07-29T00:00:00Z", "to": "2026-07-29T12:00:00Z", "seconds": 43200},
                ],
            }
        }
        graph = build_topology(fleet_payload, cluster_payload, connection_stats=stats)
        narwhal = next(n for n in graph["nodes"] if n.get("slug") == "narwhal")
        assert [s["status"] for s in narwhal["connection"]["spans"]] == ["UP", "DOWN"]

    def test_handles_empty_inputs(self):
        graph = build_topology(None, None)
        assert graph["summary"]["systems"] == 0
        assert graph["sites"] == []
        assert [n["kind"] for n in graph["nodes"]] == ["monitor"]

    def test_links_point_at_the_cluster_pages(self, fleet_payload):
        graph = build_topology(fleet_payload, [])
        narwhal = next(n for n in graph["nodes"] if n.get("slug") == "narwhal")
        assert narwhal["links"]["queues"] == "queues.html?cluster=narwhal"


class TestConnectionHistory:
    def test_records_only_transitions(self, temp_data_dir):
        store = DataStore(temp_data_dir)

        assert store.record_system_statuses([("system:a", "UP", None)]) == 1
        assert store.record_system_statuses([("system:a", "UP", None)]) == 0
        assert store.record_system_statuses([("system:a", "DOWN", None)]) == 1

        history = store.get_system_history("system:a")
        assert [row["status"] for row in history] == ["DOWN", "UP"]

    def test_connection_stats_track_current_run(self, temp_data_dir):
        store = DataStore(temp_data_dir)
        now = datetime.utcnow()

        # Hand-write history: UP two days ago, DOWN yesterday, UP an hour ago.
        with store._get_connection() as conn:
            for offset, status in [
                (timedelta(days=2), "UP"),
                (timedelta(days=1), "DOWN"),
                (timedelta(hours=1), "UP"),
            ]:
                conn.execute(
                    "INSERT INTO system_history (system_name, status, timestamp, details) "
                    "VALUES (?, ?, ?, ?)",
                    ("system:a", status, (now - offset).isoformat(), None),
                )

        stats = store.get_connection_stats(window_hours=24)["system:a"]
        assert stats["status"] == "UP"
        # Connected since the most recent transition into a reachable state.
        assert stats["connected_since"].startswith((now - timedelta(hours=1)).strftime("%Y-%m-%dT%H"))
        # Down for 23 of the last 24 hours, so uptime is roughly 1/24.
        assert 0.02 < stats["uptime_ratio"] < 0.06
        assert stats["transitions"] == 1

    def test_cleanup_keeps_the_latest_row(self, temp_data_dir):
        store = DataStore(temp_data_dir)
        with store._get_connection() as conn:
            for days, status in ((90, "UP"), (89, "DOWN")):
                conn.execute(
                    "INSERT INTO system_history (system_name, status, timestamp, details) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        "system:a",
                        status,
                        (datetime.utcnow() - timedelta(days=days)).isoformat(),
                        None,
                    ),
                )

        store.cleanup_old_data(days=30)
        remaining = store.get_system_history("system:a")
        assert [row["status"] for row in remaining] == ["DOWN"]
