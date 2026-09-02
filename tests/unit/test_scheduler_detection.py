"""A scheduler exists or it does not; having work is a separate question."""

from unittest.mock import patch

from src.collectors._slurm_helpers import scheduler_from_capabilities
from src.collectors.pw_cluster import PWClusterCollector


class TestSchedulerFromCapabilities:
    def test_slurm_commands_name_slurm(self):
        assert scheduler_from_capabilities({"sinfo": True, "squeue": True}) == "SLURM"

    def test_a_single_slurm_command_is_enough(self):
        assert scheduler_from_capabilities({"scontrol": True}) == "SLURM"

    def test_the_hpcmp_wrappers_prove_one_without_naming_it(self):
        """show_queues fronts whichever scheduler the centre runs."""
        assert scheduler_from_capabilities({"show_queues": True}) == "scheduler"

    def test_storage_tools_are_not_a_scheduler(self):
        assert scheduler_from_capabilities({"lfs": True, "quota": True}) == ""

    def test_nothing_found_is_empty(self):
        assert scheduler_from_capabilities({}) == ""
        assert scheduler_from_capabilities(None) == ""


class TestIdleCloudCluster:
    """The reported bug, end to end through _process_cluster."""

    CAPS = {"sinfo": True, "squeue": True, "scontrol": True, "sacctmgr": True}

    def build(self, caps, queues, systems):
        collector = PWClusterCollector()
        with patch.object(collector, "probe_login", return_value=(12, "host")), \
             patch.object(collector, "_get_capabilities", return_value=caps), \
             patch.object(collector, "_get_cluster_usage", return_value={"systems": systems}), \
             patch.object(collector, "_get_cluster_queues", return_value={"queues": queues, "source": "slurm"}), \
             patch.object(collector, "_get_gpu_info", return_value={}), \
             patch.object(collector, "_get_system_info", return_value={}), \
             patch.object(collector, "_get_storage_info", return_value={}):
            return collector._process_cluster(
                {"uri": "pw://u/dewdbetacluster", "status": "active", "type": "aws-slurm"}
            )

    def test_slurm_with_no_partitions_still_has_a_scheduler(self):
        meta = self.build(self.CAPS, [], [])["cluster_metadata"]
        assert meta["has_scheduler"] is True, (
            "a cloud cluster scaled to zero reports no partitions; that is "
            "not the same as having no scheduler"
        )
        assert meta["scheduler"] == "SLURM"
        assert meta["has_queue_data"] is False, "and the card can say so"

    def test_a_box_with_no_scheduler_at_all(self):
        meta = self.build({"lfs": True}, [], [])["cluster_metadata"]
        assert meta["has_scheduler"] is False
        assert meta["scheduler"] == ""

    def test_a_working_cluster_reports_both(self):
        meta = self.build(self.CAPS, [{"queue_name": "debug"}], [])["cluster_metadata"]
        assert meta["has_scheduler"] is True
        assert meta["has_queue_data"] is True
