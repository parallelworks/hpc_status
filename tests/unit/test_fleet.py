"""Merging the three sources that each know part of the fleet.

A user went looking for Coral on the home page and could not find it. The
page rendered the status collector's payload and nothing else, so a
machine that is real, documented and reachable — but not published on the
centre's status page — did not exist as far as the dashboard was
concerned.

These tests pin what each source is allowed to contribute, and what none
of them may do: invent a status for a machine nothing is watching.
"""

import pytest

from src.data.fleet import (
    SOURCE_CATALOG,
    SOURCE_LIVE_SESSION,
    SOURCE_STATUS_PAGE,
    UNMONITORED,
    build_fleet,
)


@pytest.fixture
def status_page():
    return {
        "meta": {"generated_at": "2026-08-10T12:00:00Z"},
        "systems": [
            {
                "system": "Makau",
                "status": "UP",
                "dsrc": "MHPCC DSRC",
                "login": "makau.mhpcc.hpc.mil",
                "scheduler": "slurm",
                "observed_at": "2026-08-10T12:00:00Z",
            },
            {
                "system": "Narwhal",
                "status": "DOWN",
                "dsrc": "navy",
                "login": "narwhal.navydsrc.hpc.mil",
                "scheduler": "pbs",
            },
        ],
    }


@pytest.fixture
def live_sessions():
    return [
        {
            "cluster_metadata": {
                "name": "coral",
                "uri": "pw://user/coral",
                "status": "active",
                "hostname": "coral.mhpcc.hpc.mil",
                "timestamp": "2026-08-10T12:05:00Z",
            },
            "gpu_data": [{"cores_available": "400", "cores_running": "150"}],
        }
    ]


@pytest.fixture
def catalog():
    return [
        {
            "slug": "coral",
            "name": "Coral",
            "description": "Heterogeneous x86_64 and ARM cluster.",
            "tags": ["slurm", "mhpcc", "hpc"],
            "scheduler": "SLURM",
            "subtype": "existing",
        },
        {
            "slug": "builder",
            "name": "Builder",
            "description": "Builder MHPCC System",
            "tags": ["mhpcc", "hpc"],
            "scheduler": None,
            "subtype": "existing",
        },
    ]


def by_slug(fleet):
    return {system["slug"]: system for system in fleet["systems"]}


class TestTheMissingSystem:
    """The complaint that started this."""

    def test_a_connected_system_appears_even_if_unpublished(
        self, status_page, live_sessions, catalog
    ):
        fleet = build_fleet(status_page, live_sessions, catalog, platform="hpcmp")
        assert "coral" in by_slug(fleet), (
            "Coral is reachable and documented; only the status page does not "
            "list it, and that used to be the whole home page"
        )

    def test_it_arrives_with_what_each_source_knows(
        self, status_page, live_sessions, catalog
    ):
        coral = by_slug(build_fleet(status_page, live_sessions, catalog))["coral"]
        assert coral["name"] == "Coral", "the listing's label beats the cluster slug"
        assert coral["description"].startswith("Heterogeneous")
        assert coral["connected"] is True
        assert coral["capacity"]["cores_total"] == 400
        assert coral["sources"] == [SOURCE_LIVE_SESSION, SOURCE_CATALOG]


class TestStatusPrecedence:
    def test_a_live_session_can_say_up(self, status_page, live_sessions, catalog):
        coral = by_slug(build_fleet(status_page, live_sessions, catalog))["coral"]
        assert coral["status"] == "UP"
        assert coral["status_source"] == SOURCE_LIVE_SESSION

    def test_a_lost_session_never_says_down(self, status_page):
        """Losing a session is a fact about the monitor, not the machine."""
        stale = [
            {
                "cluster_metadata": {
                    "name": "Makau",
                    "uri": "pw://user/makau",
                    "status": "inactive",
                }
            }
        ]
        makau = by_slug(build_fleet(status_page, stale, []))["makau"]
        assert makau["status"] == "UP", "the status page still says it is up"
        assert makau["status_source"] == SOURCE_STATUS_PAGE

    def test_the_status_page_keeps_its_own_verdict(self, status_page):
        narwhal = by_slug(build_fleet(status_page, [], []))["narwhal"]
        assert narwhal["status"] == "DOWN"
        assert narwhal["sources"] == [SOURCE_STATUS_PAGE]


class TestUnmonitoredSystems:
    def test_a_catalog_only_system_has_no_status(self, status_page, catalog):
        builder = by_slug(build_fleet(status_page, [], catalog))["builder"]
        assert builder["monitored"] is False
        assert builder["status"] == UNMONITORED, (
            "UNKNOWN would imply somebody looked; nothing looked"
        )
        assert builder["sources"] == [SOURCE_CATALOG]

    def test_it_is_still_findable(self, status_page, catalog):
        """Being invisible is the bug; being unmonitored is just a fact."""
        assert "builder" in by_slug(build_fleet(status_page, [], catalog))

    def test_uptime_ignores_what_nobody_is_watching(
        self, status_page, live_sessions, catalog
    ):
        summary = build_fleet(status_page, live_sessions, catalog)["summary"]
        assert summary["total_systems"] == 4
        assert summary["monitored"] == 3
        assert summary["catalog_only"] == 1
        # Makau up, Narwhal down, Coral up -> 2 of 3.
        assert summary["up"] == 2
        assert summary["uptime_ratio"] == pytest.approx(2 / 3, abs=1e-4)

    def test_uptime_is_null_when_nothing_is_monitored(self, catalog):
        summary = build_fleet(None, [], catalog)["summary"]
        assert summary["monitored"] == 0
        assert summary["uptime_ratio"] is None, "0% would be a lie"


class TestCatalogDiscipline:
    def test_a_provisioning_template_is_not_a_machine(self, status_page):
        """gcphpc and awssmall are recipes for clusters, not clusters."""
        templates = [
            {"slug": "awshpc", "name": "AWS HPC", "subtype": "aws-slurm", "tags": []},
            {"slug": "gcphpc", "name": "GCP HPC", "subtype": "google-slurm", "tags": []},
        ]
        fleet = by_slug(build_fleet(status_page, [], templates))
        assert "awshpc" not in fleet and "gcphpc" not in fleet

    def test_but_it_may_describe_a_cluster_you_are_running(self, status_page):
        """A cloud cluster the monitor is connected to still gets its text."""
        clusters = [
            {
                "cluster_metadata": {
                    "name": "dewdbetacluster",
                    "uri": "pw://user/dewdbetacluster",
                    "status": "active",
                }
            }
        ]
        listings = [
            {
                "slug": "dewdbetacluster",
                "name": "DEWD Beta Cluster",
                "description": "Beta cluster for DEWD.",
                "subtype": "aws-slurm",
                "tags": ["afrl"],
            }
        ]
        entry = by_slug(build_fleet(status_page, clusters, listings))["dewdbetacluster"]
        assert entry["description"] == "Beta cluster for DEWD."
        assert entry["site"] == "afrl", "the listing's tag names its facility"

    def test_a_listing_never_overrides_a_status(self, status_page, catalog):
        """The catalog says what a machine is, never how it is doing."""
        listings = catalog + [
            {"slug": "narwhal", "name": "Narwhal", "subtype": "existing", "tags": []}
        ]
        narwhal = by_slug(build_fleet(status_page, [], listings))["narwhal"]
        assert narwhal["status"] == "DOWN"


class TestPlacement:
    def test_a_tag_places_a_system_nothing_else_can(self, catalog):
        builder = by_slug(build_fleet(None, [], catalog))["builder"]
        assert (builder["site"], builder["site_source"]) == ("mhpcc", "listing-tag")

    def test_a_hostname_still_wins_over_a_tag(self, live_sessions, catalog):
        coral = by_slug(build_fleet(None, live_sessions, catalog))["coral"]
        assert coral["site_source"] == "hostname"

    def test_sites_carry_their_membership(self, status_page, live_sessions, catalog):
        fleet = build_fleet(status_page, live_sessions, catalog, platform="hpcmp")
        sites = {site["id"]: site for site in fleet["sites"]}
        assert sites["mhpcc"]["systems"] == 3  # Makau, Coral, Builder
        assert sites["navy"]["systems"] == 1

    def test_unassigned_sorts_last(self):
        """It is a residue, not a place."""
        payload = {
            "meta": {},
            "systems": [
                {"system": "Zeta", "status": "UP", "dsrc": "erdc"},
                {"system": "Oddbox", "status": "UP", "dsrc": "existing"},
            ],
        }
        sites = [site["id"] for site in build_fleet(payload, [], [])["sites"]]
        assert sites[-1] == "unassigned"


class TestMetadata:
    def test_it_reports_which_sources_answered(
        self, status_page, live_sessions, catalog
    ):
        meta = build_fleet(status_page, live_sessions, catalog)["meta"]
        assert meta["sources"] == [
            SOURCE_STATUS_PAGE,
            SOURCE_LIVE_SESSION,
            SOURCE_CATALOG,
        ]

    def test_a_lone_source_is_not_an_error(self, status_page):
        fleet = build_fleet(status_page, [], [])
        assert fleet["meta"]["sources"] == [SOURCE_STATUS_PAGE]
        assert len(fleet["systems"]) == 2

    def test_no_sources_at_all_still_answers(self):
        fleet = build_fleet(None, [], [])
        assert fleet["systems"] == []
        assert fleet["summary"]["total_systems"] == 0

    def test_facilities_are_named_per_platform(self, status_page):
        assert build_fleet(status_page, [], [], platform="hpcmp")["meta"]["site_label"] == "DSRC"
        assert build_fleet(status_page, [], [], platform="noaa")["meta"]["site_label"] == "Site"

    def test_monitored_systems_sort_before_unmonitored(
        self, status_page, live_sessions, catalog
    ):
        """What nobody is watching goes last, whatever it is called."""
        systems = build_fleet(status_page, live_sessions, catalog)["systems"]
        monitored = [s["monitored"] for s in systems]
        assert monitored == sorted(monitored, reverse=True)
        assert systems[-1]["name"] == "Builder"


class TestSameMachineDifferentNames:
    """The same machine goes by different names in different places.

    The marketplace lists "chessie" and "builder"; the clusters they
    describe are registered as "chessiearl" and "buildermhpcc". Matched on
    the slug alone they became two entries each — one connected, and one
    reported NOT MONITORED, which duplicated the machine and lied about
    the copy.
    """

    @staticmethod
    def clusters(*names):
        return [
            {
                "cluster_metadata": {
                    "name": name,
                    "uri": f"pw://user/{name}",
                    "status": "active",
                }
            }
            for name in names
        ]

    def test_a_facility_suffix_is_the_same_machine(self):
        listings = [
            {
                "slug": "builder",
                "name": "Builder",
                "description": "Builder MHPCC System",
                "subtype": "existing",
                "tags": ["mhpcc"],
            }
        ]
        fleet = by_slug(build_fleet(None, self.clusters("buildermhpcc"), listings))
        assert "builder" not in fleet, "that is the same machine, listed twice"
        assert fleet["buildermhpcc"]["description"] == "Builder MHPCC System"

    def test_the_merged_machine_is_up_not_unmonitored(self):
        """The complaint: a connected system reported as NOT MONITORED."""
        listings = [
            {"slug": "chessie", "name": "Chessie", "subtype": "existing", "tags": []}
        ]
        entry = by_slug(build_fleet(None, self.clusters("chessiearl"), listings))[
            "chessiearl"
        ]
        assert entry["connected"] is True
        assert entry["monitored"] is True
        assert entry["status"] == "UP"
        assert entry["status"] != UNMONITORED

    def test_it_takes_the_curated_name(self):
        listings = [
            {"slug": "chessie", "name": "Chessie", "subtype": "existing", "tags": []}
        ]
        fleet = by_slug(build_fleet(None, self.clusters("chessiearl"), listings))
        assert fleet["chessiearl"]["name"] == "Chessie"

    def test_a_suffix_that_is_not_a_facility_stays_separate(self):
        """Only a site the catalog knows may join two names."""
        listings = [
            {"slug": "coral", "name": "Coral", "subtype": "existing", "tags": []}
        ]
        fleet = by_slug(build_fleet(None, self.clusters("coralreef"), listings))
        assert "coral" in fleet and "coralreef" in fleet, (
            "'reef' is not a site, so these are two machines"
        )

    def test_it_works_in_the_other_direction_too(self):
        """A cluster may be the short name and the listing the long one."""
        listings = [
            {
                "slug": "buildermhpcc",
                "name": "Builder MHPCC",
                "description": "The MHPCC build box.",
                "subtype": "existing",
                "tags": [],
            }
        ]
        fleet = by_slug(build_fleet(None, self.clusters("builder"), listings))
        assert len(fleet) == 1
        assert fleet["builder"]["description"] == "The MHPCC build box."

    def test_a_cluster_joins_the_status_page_entry_it_matches(self):
        payload = {
            "meta": {},
            "systems": [
                {"system": "Chessie", "status": "UP", "dsrc": "arl", "scheduler": "slurm"}
            ],
        }
        fleet = by_slug(build_fleet(payload, self.clusters("chessiearl"), []))
        assert len(fleet) == 1, "one machine, whichever name each source uses"
        assert fleet["chessie"]["connected"] is True

    def test_an_unmatched_listing_is_still_unmonitored(self):
        """Reconciling names must not quietly adopt every listing."""
        listings = [
            {"slug": "rqhap", "name": "RQ HAP", "subtype": "existing", "tags": ["afrl"]}
        ]
        entry = by_slug(build_fleet(None, self.clusters("coral"), listings))["rqhap"]
        assert entry["monitored"] is False
        assert entry["status"] == UNMONITORED


class TestControlPlaneListing:
    """The listing is the freshest word on connectedness.

    A telemetry sweep of a large fleet takes minutes; the listing is one
    cheap call, refreshed between sweeps. A user who just connected a
    machine watched it sit at NOT MONITORED for up to two sweep cycles
    because connectedness could only arrive with telemetry.
    """

    CONNS = {
        "checked_at": "2026-08-18T12:08:00Z",
        "active": [
            {"name": "coral", "uri": "pw://u/coral"},
            {"name": "chessiearl", "uri": "pw://u/chessiearl"},
        ],
    }

    @staticmethod
    def cluster(name, status="active"):
        return {
            "cluster_metadata": {
                "name": name,
                "uri": f"pw://u/{name}",
                "status": status,
            }
        }

    def test_a_listed_machine_appears_before_its_first_sweep(self):
        fleet = by_slug(build_fleet(None, [], [], self.CONNS))
        coral = fleet["coral"]
        assert coral["connected"] is True
        assert coral["status"] == "UP"
        assert coral["status_source"] == SOURCE_LIVE_SESSION
        assert coral["awaiting_telemetry"] is True, (
            "say the sweep is coming rather than showing an empty machine"
        )

    def test_a_machine_with_telemetry_is_not_marked_awaiting(self):
        fleet = by_slug(
            build_fleet(None, [self.cluster("chessiearl")], [], self.CONNS)
        )
        assert fleet["chessiearl"].get("awaiting_telemetry") is None

    def test_a_cluster_missing_from_the_listing_is_disconnected(self):
        """The cache says connected; the fresher listing does not name it."""
        fleet = by_slug(build_fleet(None, [self.cluster("makau")], [], self.CONNS))
        makau = fleet["makau"]
        assert makau["connected"] is False
        assert makau["status"] == "UNKNOWN", (
            "the session was its only witness, and the session is gone"
        )
        assert makau["status_source"] == "control plane"

    def test_the_status_page_verdict_survives_a_lost_session(self):
        payload = {
            "meta": {},
            "systems": [
                {"system": "Makau", "status": "UP", "dsrc": "mhpcc", "scheduler": "slurm"}
            ],
        }
        fleet = by_slug(
            build_fleet(payload, [self.cluster("makau")], [], self.CONNS)
        )
        makau = fleet["makau"]
        assert makau["connected"] is False
        assert makau["status"] == "UP", "losing a session never says DOWN"
        assert makau["status_source"] == SOURCE_STATUS_PAGE

    def test_the_listing_merges_by_facility_suffix_too(self):
        """'chessie' from the marketplace is 'chessiearl' in the listing."""
        listings = [
            {"slug": "chessie", "name": "Chessie", "subtype": "existing", "tags": []}
        ]
        fleet = by_slug(build_fleet(None, [], listings, self.CONNS))
        assert "chessie" not in fleet or not fleet.get("chessie", {}).get("connected") is None
        entry = fleet["chessiearl"]
        assert entry["connected"] is True
        assert entry["name"] == "Chessie"

    def test_no_listing_changes_nothing(self):
        """Deployments whose worker predates the listing keep old behaviour."""
        fleet = by_slug(build_fleet(None, [self.cluster("makau")], [], None))
        assert fleet["makau"]["connected"] is True


class TestSchedulerClusterCapacity:
    """DSRC machines report inventory via queue_data, not gpu_data.

    The fleet reader only looked at gpu_data/node_classes — right for the
    cloud GPU box it was written against, empty ({}) for every
    scheduler-driven cluster — so a fully healthy sweep of the HPCMP
    fleet showed no cores anywhere.
    """

    BARFOOT = {
        "cluster_metadata": {
            "name": "barfoot",
            "uri": "pw://u/barfoot",
            "status": "active",
        },
        "gpu_data": {},
        "queue_data": {
            "queues": [{"queue_name": "urgent"}],
            "nodes": [],
            "cluster_totals": {
                "cores_total": 9408,
                "cores_running": 4200,
                "cores_free": 5208,
                "nodes_total": 49,
                "gpus_total": 0,
            },
            "source": "slurm",
        },
    }

    def test_cores_come_from_queue_data_totals(self):
        fleet = by_slug(build_fleet(None, [self.BARFOOT], []))
        capacity = fleet["barfoot"]["capacity"]
        assert capacity["cores_total"] == 9408
        assert capacity["cores_running"] == 4200
        assert capacity["utilization_percent"] == pytest.approx(44.6, abs=0.1)

    def test_gpu_data_rows_still_work(self):
        """The cloud box shape that the reader was originally right about."""
        cluster = {
            "cluster_metadata": {"name": "a30", "uri": "pw://u/a30", "status": "active"},
            "gpu_data": [{"cores_available": "400", "cores_running": "150"}],
            "queue_data": {},
        }
        capacity = by_slug(build_fleet(None, [cluster], []))["a30"]["capacity"]
        assert capacity["cores_total"] == 400

    def test_no_inventory_at_all_is_none_not_zero(self):
        """A license server with no nodes should not show '0% of 0 cores'."""
        cluster = {
            "cluster_metadata": {"name": "lic", "uri": "pw://u/lic", "status": "active"},
            "gpu_data": {},
            "queue_data": {
                "queues": [],
                "nodes": [],
                "cluster_totals": {"cores_total": 0, "cores_running": 0},
            },
        }
        assert by_slug(build_fleet(None, [cluster], []))["lic"]["capacity"] is None
