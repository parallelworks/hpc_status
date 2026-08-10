"""Tests for the fleet topology graph builder."""

from datetime import datetime, timedelta

import pytest

from src.data.persistence import DataStore
from src.data.topology import (
    build_topology,
    site_from_hostname,
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

    @pytest.mark.parametrize(
        "hostname,expected",
        [
            ("crux.mhpcc.hpc.mil", "mhpcc"),
            ("narwhal.navydsrc.hpc.mil", "navy"),
            ("chessie.arl.hpc.mil", "arl"),
            ("gaea54.ncrc.gov", "ornl"),
            ("somebox.example.com", None),
            ("pw://user/cluster", None),
            ("", None),
        ],
    )
    def test_site_from_hostname(self, hostname, expected):
        assert site_from_hostname(hostname) == expected

    @pytest.mark.parametrize(
        "hostname,expected",
        [
            # EC2 puts the region in the instance's own name...
            ("ip-10-1-2-3.us-gov-west-1.compute.internal", "usgovwest1"),
            ("ec2-52-61-1-2.us-gov-west-1.compute.amazonaws.com", "usgovwest1"),
            ("ip-10-0-0-5.us-west-2.compute.internal", "uswest2"),
            ("ip-10-0-0-5.us-gov-east-1.compute.internal", "usgoveast1"),
            # ...except us-east-1, which uses a bare legacy suffix.
            ("ip-10-0-0-5.ec2.internal", "useast1"),
            ("ec2-1-2-3-4.compute-1.amazonaws.com", "useast1"),
        ],
    )
    def test_aws_regions_are_read_from_ec2_hostnames(self, hostname, expected):
        assert site_from_hostname(hostname) == expected

    def test_provider_label_yields_to_a_region_hostname(self):
        """"aws" names a company, not a place — the hostname names the region."""
        assert (
            resolve_site_id("aws", "c1", "ip-10-1-2-3.us-gov-west-1.compute.internal")
            == "usgovwest1"
        )

    def test_provider_label_is_used_when_nothing_better_exists(self):
        assert resolve_site_id("aws", "c1", "") == "aws"
        assert resolve_site_id("Amazon", "c1", "") == "aws"

    def test_cloud_region_default_places_regionless_cloud_clusters(self):
        """A fleet that runs all its cloud in one region can just say so."""
        assert resolve_site_id("aws", "c1", "", None, "usgovwest1") == "usgovwest1"
        # A hostname that names a region is still more specific than a default.
        assert (
            resolve_site_id("aws", "c1", "ip-1.us-east-2.compute.internal", None, "usgovwest1")
            == "useast2"
        )
        # An unknown default is ignored rather than inventing a site.
        assert resolve_site_id("aws", "c1", "", None, "not-a-region") == "aws"
        # And it never overrides a physical site.
        assert resolve_site_id("erdc", "c1", "", None, "usgovwest1") == "erdc"

    def test_a_real_site_still_beats_a_region_hostname(self):
        assert (
            resolve_site_id("erdc", "c1", "ip-10-1-2-3.us-gov-west-1.compute.internal")
            == "erdc"
        )

    def test_cloud_regions_are_mapped_and_flagged(self):
        site = describe_site("usgovwest1")
        assert site["name"] == "AWS GovCloud (US-West)"
        assert site["cloud"] is True
        assert site["lat"] and site["lon"]
        assert describe_site("erdc")["cloud"] is False

    def test_hostname_beats_a_generic_collector_label(self):
        # PW reports type="existing", which says nothing; the login node does.
        assert resolve_site_id("existing", "crux", "crux.mhpcc.hpc.mil") == "mhpcc"

    def test_system_sites_override_wins_outright(self):
        assert (
            resolve_site_id("erdc", "janus", "janus.arl.hpc.mil", {"janus": "navy"})
            == "navy"
        )

    def test_known_systems_fall_back_to_a_name_hint(self):
        # Not on any status page, and a login hostname that gives nothing away.
        assert resolve_site_id(None, "chessie", "chessie01") == "arl"
        assert resolve_site_id(None, "janus", "") == "arl"
        assert resolve_site_id(None, "crux", "") == "mhpcc"

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

    def test_a_live_session_is_evidence_a_system_is_up(self, cluster_payload):
        """Nothing on a status page covers these, but we are logged in."""
        graph = build_topology(None, cluster_payload)
        narwhal = next(n for n in graph["nodes"] if n.get("slug") == "narwhal")
        assert narwhal["status"] == "UP"
        assert narwhal["status_source"] == "live session"

    def test_a_live_session_fills_in_an_unknown_status(self, cluster_payload):
        fleet = {
            "systems": [
                {"system": "Narwhal", "status": "", "dsrc": "navy", "login": "n.navydsrc.hpc.mil"}
            ]
        }
        graph = build_topology(fleet, cluster_payload)
        narwhal = next(n for n in graph["nodes"] if n.get("slug") == "narwhal")
        assert narwhal["status"] == "UP"
        assert narwhal["status_source"] == "live session"

    def test_a_live_session_does_not_overrule_the_site(self, cluster_payload):
        """The site knows about maintenance that a login node cannot show."""
        fleet = {"systems": [{"system": "Narwhal", "status": "MAINTENANCE", "dsrc": "navy"}]}
        graph = build_topology(fleet, cluster_payload)
        narwhal = next(n for n in graph["nodes"] if n.get("slug") == "narwhal")
        assert narwhal["status"] == "MAINTENANCE"
        assert narwhal["status_source"] == "status page"
        assert narwhal["reported_status"] == "MAINTENANCE"

    def test_an_unreachable_pw_cluster_is_not_called_up(self):
        graph = build_topology(
            None,
            [{"cluster_metadata": {"name": "gone", "uri": "pw://u/gone", "status": "off"}}],
        )
        node = next(n for n in graph["nodes"] if n.get("slug") == "gone")
        assert node["status"] == "DOWN"
        assert node["connected"] is False

    def test_unmatched_systems_have_no_telemetry(self, fleet_payload, cluster_payload):
        graph = build_topology(fleet_payload, cluster_payload)
        carpenter = next(n for n in graph["nodes"] if n.get("slug") == "carpenter")

        assert carpenter["origin"] == "fleet"
        assert carpenter["connected"] is False
        assert carpenter["capacity"] is None
        assert carpenter["status"] == "DEGRADED"

    def test_pw_cluster_is_placed_by_its_login_hostname(self, fleet_payload):
        """PW URIs carry no domain; the login node's own name does."""
        clusters = [
            {
                "cluster_metadata": {
                    "name": "crux",
                    "uri": "pw://user/crux",
                    "status": "on",
                    "hostname": "crux.mhpcc.hpc.mil",
                },
            }
        ]
        graph = build_topology(fleet_payload, clusters)
        crux = next(n for n in graph["nodes"] if n.get("slug") == "crux")
        assert crux["hostname"] == "crux.mhpcc.hpc.mil"
        assert crux["site"] == "mhpcc"
        assert crux["site_label"] == "MHPCC DSRC"

    def test_system_sites_config_places_stragglers(self, fleet_payload):
        clusters = [
            {"cluster_metadata": {"name": "oddbox", "uri": "pw://user/oddbox"}}
        ]
        graph = build_topology(fleet_payload, clusters, system_sites={"oddbox": "erdc"})
        node = next(n for n in graph["nodes"] if n.get("slug") == "oddbox")
        assert node["site"] == "erdc"

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

        # Returns the transitions themselves: (name, previous, current, details)
        first = store.record_system_statuses([("system:a", "UP", None)])
        assert first == [("system:a", None, "UP", None)]
        assert store.record_system_statuses([("system:a", "UP", None)]) == []
        assert store.record_system_statuses([("system:a", "DOWN", None)]) == [
            ("system:a", "UP", "DOWN", None)
        ]

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


class TestPlatformScoping:
    """Name hints belong to the deployment that named the machines.

    The catalog knows chessie, janus and crux are HPCMP boxes, and hera,
    gaea and ppan are NOAA ones. Applied everywhere, that put a generic
    deployment's cluster called "janus" on a pin at Aberdeen Proving
    Ground, captioned Army Research Laboratory — confidently wrong, which
    is worse than Unassigned.
    """

    @staticmethod
    def payload(*names):
        return {
            "meta": {},
            "systems": [
                {
                    "system": name,
                    "status": "UP",
                    "dsrc": "existing",
                    "login": f"{name}.example.com",
                    "scheduler": "slurm",
                }
                for name in names
            ],
        }

    def sites_for(self, platform, *names):
        graph = build_topology(self.payload(*names), [], platform=platform)
        return {
            node["slug"]: (node["site"], node["site_source"])
            for node in graph["nodes"]
            if node["kind"] == "system"
        }

    def test_generic_guesses_nothing_from_a_name(self):
        placed = self.sites_for("generic", "janus", "hera")
        assert placed["janus"] == ("unassigned", "none")
        assert placed["hera"] == ("unassigned", "none")

    def test_hpcmp_places_its_own_and_not_noaas(self):
        placed = self.sites_for("hpcmp", "janus", "crux", "hera")
        assert placed["janus"] == ("arl", "name-hint")
        assert placed["crux"] == ("mhpcc", "name-hint")
        assert placed["hera"] == ("unassigned", "none")

    def test_noaa_places_its_own_and_not_hpcmps(self):
        placed = self.sites_for("noaa", "hera", "gaea-c5", "janus")
        assert placed["hera"] == ("nessc", "name-hint")
        # Slugs drop punctuation, so a renamed gaea-c5 still matches.
        assert placed["gaeac5"] == ("ornl", "name-hint")
        assert placed["janus"] == ("unassigned", "none")

    @pytest.mark.parametrize("platform", ["generic", "hpcmp", "noaa"])
    def test_a_hostname_is_evidence_on_any_platform(self, platform):
        """The host names itself; that is not a guess that can collide."""
        graph = build_topology(
            {
                "meta": {},
                "systems": [
                    {
                        "system": "crux",
                        "status": "UP",
                        "dsrc": "existing",
                        "login": "crux.mhpcc.hpc.mil",
                        "scheduler": "pbs",
                    }
                ],
            },
            [],
            platform=platform,
        )
        node = next(n for n in graph["nodes"] if n["kind"] == "system")
        assert (node["site"], node["site_source"]) == ("mhpcc", "hostname")

    @pytest.mark.parametrize("platform", ["generic", "hpcmp", "noaa"])
    def test_what_the_collector_reports_always_wins(self, platform):
        """A reported site is data, not inference — never scoped away."""
        graph = build_topology(
            {
                "meta": {},
                "systems": [
                    {
                        "system": "carpenter",
                        "status": "UP",
                        "dsrc": "ERDC DSRC",
                        "login": "carpenter.example.com",
                        "scheduler": "pbs",
                    }
                ],
            },
            [],
            platform=platform,
        )
        node = next(n for n in graph["nodes"] if n["kind"] == "system")
        assert (node["site"], node["site_source"]) == ("erdc", "collector")

    @pytest.mark.parametrize(
        "platform,label", [("hpcmp", "DSRC"), ("noaa", "Site"), ("generic", "Site")]
    )
    def test_facilities_are_called_what_the_deployment_calls_them(
        self, platform, label
    ):
        graph = build_topology(self.payload("box"), [], platform=platform)
        assert graph["meta"]["site_label"] == label

    @pytest.mark.parametrize(
        "platform,hints",
        [("hpcmp", "hpcmp"), ("noaa", "noaa"), ("generic", "none")],
    )
    def test_the_payload_says_which_hints_were_applied(self, platform, hints):
        """"Why is this machine there?" must be answerable from the API."""
        graph = build_topology(self.payload("box"), [], platform=platform)
        assert graph["meta"]["site_hints"] == hints

    def test_config_overrides_beat_the_absence_of_hints(self):
        """A generic deployment can still say where its machines live."""
        graph = build_topology(
            self.payload("janus"),
            [],
            platform="generic",
            system_sites={"janus": "erdc"},
        )
        node = next(n for n in graph["nodes"] if n["kind"] == "system")
        assert (node["site"], node["site_source"]) == ("erdc", "config")

    def test_cloud_regions_apply_everywhere(self):
        """Any deployment can run in the cloud."""
        for platform in ("generic", "hpcmp", "noaa"):
            assert (
                resolve_site_id(
                    "aws", "c1", "ip-10-0-0-1.us-gov-west-1.compute.internal",
                    None, None, platform,
                )
                == "usgovwest1"
            )

    def test_helpers_with_no_platform_still_see_everything(self):
        """Callers outside a deployment get the permissive behaviour."""
        assert resolve_site_id("existing", "gaea-c5") == "ornl"
        assert resolve_site_id("existing", "janus") == "arl"
