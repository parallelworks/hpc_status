"""PW CLI cluster collector.

Collects cluster usage, queue, and status information using the PW CLI.
This is the core data collector that works across all deployments.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from . import _slurm_helpers as sh
from .base import BaseCollector, CollectorError

# How long to trust a cached capability probe before re-probing.
_CAPABILITY_TTL = timedelta(minutes=10)


def _log(msg: str) -> None:
    """Print with flush for reliable output in daemon threads."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


class PWClusterCollector(BaseCollector):
    """Collector for PW-connected clusters.

    Uses `pw clusters ls` and `pw ssh` commands to gather usage and queue data.
    """

    def __init__(
        self,
        max_retries: int = 3,
        retry_delay: int = 5,
        ssh_timeout: int = 60,
        pw_context: Optional[str] = None,
    ):
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.ssh_timeout = ssh_timeout
        # Pin a specific PW context (e.g. "user:foo@noaa.parallel.works") for
        # every ``pw`` call. Useful when the operator wants the dashboard to
        # always talk to a specific platform regardless of the user's active
        # context. ``None`` = use whatever context pw resolves on its own.
        self.pw_context = pw_context
        self._known_clusters: set = set()
        # cluster_uri -> (capabilities, timestamp)
        self._capability_cache: Dict[str, Tuple[Dict[str, bool], datetime]] = {}
        # Cached saccount_params output so we can derive both usage + storage
        # from a single SSH call. (cluster_uri -> (parsed_tuple, timestamp))
        self._saccount_cache: Dict[
            str,
            Tuple[Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Any]], datetime],
        ] = {}

    def _pw(self, *args: str) -> List[str]:
        """Build a ``pw`` command, prepending ``--context`` if pinned."""
        cmd: List[str] = ["pw"]
        if self.pw_context:
            cmd.extend(["--context", self.pw_context])
        cmd.extend(args)
        return cmd

    @property
    def name(self) -> str:
        return "pw_cluster"

    @property
    def display_name(self) -> str:
        return "PW Clusters"

    def is_available(self) -> bool:
        """Check if pw CLI is available."""
        try:
            result = subprocess.run(
                ["pw", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def check_auth(self) -> tuple:
        """Check if the user is authenticated with PW.

        Returns:
            Tuple of (is_authenticated: bool, detail: str)
        """
        try:
            result = subprocess.run(
                ["pw", "auth", "whoami"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = result.stdout.strip()
            stderr = result.stderr.strip()
            if result.returncode == 0 and output:
                _log(f"[pw_cluster] Authenticated as: {output}")
                return True, output
            else:
                detail = stderr or output or "Unknown auth error"
                _log(f"[pw_cluster] Authentication check failed (rc={result.returncode}): {detail}")
                return False, detail
        except subprocess.TimeoutExpired:
            _log("[pw_cluster] Authentication check timed out")
            return False, "Auth check timed out"
        except Exception as e:
            _log(f"[pw_cluster] Authentication check error: {e}")
            return False, str(e)

    def collect(self) -> Dict[str, Any]:
        """Collect data from all active PW clusters.

        Returns:
            Dictionary with 'clusters' list and 'meta' information.

        Raises:
            CollectorError: If collection fails.
        """
        try:
            clusters = self.get_active_clusters()
            if not clusters:
                return {
                    "meta": {
                        "generated_at": datetime.utcnow().isoformat() + "Z",
                        "collector": self.name,
                        "cluster_count": 0,
                    },
                    "clusters": [],
                }

            results = []
            for cluster in clusters:
                cluster_data = self._process_cluster(cluster)
                if cluster_data:
                    results.append(cluster_data)
                    self._known_clusters.add(cluster["uri"])

            return {
                "meta": {
                    "generated_at": datetime.utcnow().isoformat() + "Z",
                    "collector": self.name,
                    "cluster_count": len(results),
                },
                "clusters": results,
            }
        except Exception as e:
            raise CollectorError(self.name, str(e), e)

    def get_active_clusters(self) -> List[Dict[str, str]]:
        """Get active clusters using pw CLI command.

        Returns list of clusters with type='existing' and an active status.

        Raises:
            CollectorError: If the pw CLI command fails.
        """
        try:
            cmd = self._pw(
                "clusters",
                "ls",
                "--status=active",
                "-o",
                "table",
                "--owned",
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            _log(f"[pw_cluster] pw clusters ls output:\n{result.stdout.strip()}")
            clusters = self._parse_cluster_table(result.stdout)
            _log(f"[pw_cluster] Parsed {len(clusters)} active clusters from table")
            return clusters
        except subprocess.CalledProcessError as e:
            raise CollectorError(self.name, f"Error getting clusters: {e}", e)
        except subprocess.TimeoutExpired as e:
            raise CollectorError(self.name, "Timeout getting cluster list", e)
        except Exception as e:
            raise CollectorError(self.name, f"Unexpected error: {e}", e)

    def _parse_cluster_table(self, table_output: str) -> List[Dict[str, str]]:
        """Parse the cluster table output from pw CLI.

        Handles both pipe-delimited and space-separated table formats.
        """
        clusters = []
        lines = table_output.strip().split("\n")

        # Detect format: pipe-delimited vs space-separated
        has_pipes = any("|" in line and not line.startswith("+") for line in lines)

        for line in lines:
            # Skip separator and empty lines
            if line.startswith("+") or not line.strip():
                continue
            # Skip header lines
            if "URI" in line and "STATUS" in line:
                continue

            clean_line = line.strip()
            if not clean_line:
                continue

            # Parse columns based on format
            if has_pipes:
                parts = [p.strip() for p in clean_line.strip("|").split("|") if p.strip()]
            else:
                parts = clean_line.split()

            if len(parts) >= 3:
                uri = parts[0].strip()
                status = parts[1].strip().lower()
                cluster_type = parts[2].strip().lower()

                # Accept on-prem ("existing") and cloud-Slurm clusters (
                # google-slurm / aws-slurm / azure-slurm / oci-slurm). NOAA
                # RDHPCS makes both visible in the same `pw clusters ls`.
                cloud_slurm_types = (
                    "google-slurm",
                    "aws-slurm",
                    "azure-slurm",
                    "oci-slurm",
                )
                is_existing = cluster_type == "existing"
                is_cloud_slurm = cluster_type in cloud_slurm_types
                if (is_existing or is_cloud_slurm) and status in ("on", "active"):
                    clusters.append(
                        {
                            "uri": uri,
                            "status": status,
                            "type": cluster_type,
                        }
                    )
                else:
                    _log(f"[pw_cluster] Skipping cluster: uri={uri} status={status} type={cluster_type}")

        return clusters

    def _process_cluster(self, cluster: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """Process a single cluster and return its data.

        Dispatches collection to the right command flavour based on the
        cluster's detected capabilities:
          - HPCMP-style ``show_*`` scripts when present
          - NOAA ``saccount_params`` for usage + storage when present
          - Raw Slurm (``sshare`` / ``sacctmgr`` / ``scontrol`` / ``squeue``)
            as a fallback for Slurm-only clusters
          - ``df``-only path for analysis/data-mover nodes
        """
        cluster_name = cluster["uri"].split("/")[-1]
        caps = self._get_capabilities(cluster["uri"])

        usage_data = self._get_cluster_usage(cluster["uri"], caps, cluster_name)
        queue_data = self._get_cluster_queues(cluster["uri"], caps)

        gpu_data = None
        system_info = None
        has_scheduler = bool(usage_data and usage_data.get("systems")) or bool(
            queue_data and queue_data.get("queues")
        )

        if not has_scheduler:
            # Only run nvidia-smi if the cluster reports it. Probing it on a
            # data-mover or analysis node can hang for the full ssh timeout.
            if caps.get("nvidia-smi"):
                gpu_data = self._get_gpu_info(cluster["uri"])
            system_info = self._get_system_info(cluster["uri"])

        storage_data = self._get_storage_info(cluster["uri"], caps)

        return {
            "cluster_metadata": {
                "name": cluster_name,
                "uri": cluster["uri"],
                "status": cluster["status"],
                "type": cluster["type"],
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "has_scheduler": has_scheduler,
                "capabilities": caps,
            },
            "usage_data": usage_data or {},
            "queue_data": queue_data or {},
            "gpu_data": gpu_data or {},
            "system_info": system_info or {},
            "storage_data": storage_data or {},
        }

    def _get_capabilities(self, cluster_uri: str) -> Dict[str, bool]:
        """Detect available collection commands on a cluster (cached)."""
        cached = self._capability_cache.get(cluster_uri)
        if cached and datetime.utcnow() - cached[1] < _CAPABILITY_TTL:
            return cached[0]
        try:
            cmd = self._pw("ssh", cluster_uri, sh.build_capability_probe())
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.ssh_timeout,
            )
            if result.returncode != 0:
                _log(
                    f"[pw_cluster] Capability probe failed for {cluster_uri} "
                    f"(rc={result.returncode}): {result.stderr.strip()[:120]}"
                )
                caps = {c: False for c in sh.PROBE_COMMANDS}
            else:
                caps = sh.parse_capability_probe(result.stdout)
            _log(
                f"[pw_cluster] {cluster_uri.split('/')[-1]} caps: "
                + ", ".join(f"{k}={'Y' if v else 'N'}" for k, v in caps.items() if v)
            )
        except subprocess.TimeoutExpired:
            _log(f"[pw_cluster] Capability probe timed out for {cluster_uri}")
            caps = {c: False for c in sh.PROBE_COMMANDS}
        except Exception as e:
            _log(f"[pw_cluster] Capability probe error for {cluster_uri}: {e}")
            caps = {c: False for c in sh.PROBE_COMMANDS}
        self._capability_cache[cluster_uri] = (caps, datetime.utcnow())
        return caps

    def _get_saccount_params(
        self, cluster_uri: str, cluster_name: str
    ) -> Optional[Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Any]]]:
        """Fetch and parse ``saccount_params`` once per refresh cycle.

        Cached briefly so the usage path and storage path share one SSH call.
        """
        cached = self._saccount_cache.get(cluster_uri)
        if cached and datetime.utcnow() - cached[1] < timedelta(seconds=30):
            return cached[0]
        try:
            cmd = self._pw("ssh", cluster_uri, "saccount_params")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.ssh_timeout,
            )
            if result.returncode != 0:
                _log(
                    f"[pw_cluster] saccount_params failed for {cluster_uri} "
                    f"(rc={result.returncode})"
                )
                return None
            parsed = sh.parse_saccount_params(result.stdout, cluster_name)
            self._saccount_cache[cluster_uri] = (parsed, datetime.utcnow())
            return parsed
        except Exception as e:
            _log(f"[pw_cluster] saccount_params error for {cluster_uri}: {e}")
            return None

    def _get_cluster_usage(
        self,
        cluster_uri: str,
        caps: Optional[Dict[str, bool]] = None,
        cluster_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get usage information for a cluster, dispatching by capability.

        Order of preference:
          1. ``show_usage`` (HPCMP-shaped)
          2. ``saccount_params`` (NOAA Hera/Ursa)
          3. ``sshare`` (raw Slurm, anywhere with the binary)
        """
        if caps is None:
            caps = self._get_capabilities(cluster_uri)
        if cluster_name is None:
            cluster_name = cluster_uri.split("/")[-1]

        if caps.get("show_usage"):
            return self._get_cluster_usage_via_show(cluster_uri)
        if caps.get("saccount_params"):
            parsed = self._get_saccount_params(cluster_uri, cluster_name)
            if parsed is not None:
                systems, _storage, _home = parsed
                # Enrich rows with sfairshare allocation/usage + sreport
                # core-hour totals for the current NOAA fiscal year.
                self._enrich_with_sfairshare(
                    systems, cluster_uri, caps=caps
                )
                self._enrich_with_sreport(
                    systems, cluster_uri, caps=caps
                )
                source_tags = ["saccount_params"]
                if caps.get("sfairshare"):
                    source_tags.append("sfairshare")
                if caps.get("sreport"):
                    source_tags.append("sreport")
                return {
                    "header": f"NOAA RDHPCS usage for {cluster_name}",
                    "fiscal_year_info": f"FY since {sh.fiscal_year_start()}",
                    "systems": systems,
                    "source": "+".join(source_tags),
                }
        if caps.get("sshare"):
            return self._get_cluster_usage_via_sshare(cluster_uri, cluster_name)
        return None

    def _enrich_with_sfairshare(
        self,
        systems: List[Dict[str, Any]],
        cluster_uri: str,
        caps: Dict[str, bool],
    ) -> None:
        """Run ``sfairshare -C`` and fold the data into each project row.

        Adds fairshare-flavoured fields (norm_shares, eff_usage, raw_shares,
        raw_usage_hours) and updates fairshare_score/fairshare_rank with the
        authoritative sfairshare value when present.
        """
        if not caps.get("sfairshare"):
            return
        try:
            cmd = self._pw("ssh", cluster_uri, "sfairshare -C 2>/dev/null")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.ssh_timeout,
            )
            if result.returncode != 0:
                return
            fs_rows = sh.parse_sfairshare_csv(result.stdout)
            for row in systems:
                rec = fs_rows.get(row.get("subproject", ""))
                if not rec:
                    continue
                row["fairshare_score"] = rec["fairshare"]
                row["fairshare_rank"] = rec["rank_str"] or row.get("fairshare_rank")
                row["norm_shares"] = rec["norm_shares"]
                row["effective_usage"] = rec["eff_usage"]
                row["raw_shares"] = rec["raw_shares"]
                row["raw_usage_hours"] = rec["raw_usage_hours"]
        except Exception as e:
            _log(f"[pw_cluster] sfairshare error for {cluster_uri}: {e}")

    def _enrich_with_sreport(
        self,
        systems: List[Dict[str, Any]],
        cluster_uri: str,
        caps: Dict[str, bool],
    ) -> None:
        """Run ``sreport cluster AccountUtilizationByUser`` for the user's projects.

        Fills ``hours_used`` with the project-level core-hour total over the
        current NOAA fiscal year window (FY starts Oct 1).
        """
        if not caps.get("sreport") or not systems:
            return
        accounts = ",".join(
            sorted({r.get("subproject", "") for r in systems if r.get("subproject")})
        )
        if not accounts:
            return
        fy_start = sh.fiscal_year_start()
        try:
            cmd = self._pw(
                "ssh",
                cluster_uri,
                f"sreport cluster AccountUtilizationByUser "
                f"start={fy_start} account={accounts} "
                "-t Hours -P --noheader 2>/dev/null",
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.ssh_timeout,
            )
            if result.returncode != 0:
                return
            account_hours = sh.parse_sreport_account_user(result.stdout)
            for row in systems:
                acct = row.get("subproject", "")
                if acct in account_hours:
                    row["hours_used"] = account_hours[acct]
                    row["fiscal_year_start"] = fy_start
        except Exception as e:
            _log(f"[pw_cluster] sreport error for {cluster_uri}: {e}")

    def _get_cluster_usage_via_show(
        self, cluster_uri: str
    ) -> Optional[Dict[str, Any]]:
        """Original HPCMP ``show_usage`` path."""
        try:
            cmd = self._pw("ssh", cluster_uri, "show_usage")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=self.ssh_timeout,
            )
            data = self._parse_usage_output(result.stdout)
            data["source"] = "show_usage"
            return data
        except subprocess.CalledProcessError as e:
            _log(f"[pw_cluster] Error getting usage for {cluster_uri}: {e}")
            return None
        except subprocess.TimeoutExpired:
            _log(f"[pw_cluster] Timeout getting usage for {cluster_uri}")
            return None
        except Exception as e:
            _log(f"[pw_cluster] Unexpected error for {cluster_uri}: {e}")
            return None

    def _get_cluster_usage_via_sshare(
        self, cluster_uri: str, cluster_name: str
    ) -> Optional[Dict[str, Any]]:
        """Fallback Slurm path: aggregate raw usage for the current user.

        If the cluster also has ``sreport`` (most modern Slurm builds do),
        enrich each row with FY-to-date core-hours.
        """
        try:
            cmd = self._pw(
                "ssh",
                cluster_uri,
                # parsable2 emits unpadded '|'-separated rows we can parse cheaply
                "sshare --parsable2 --noheader --format=Account,User,RawUsage -U "
                "2>/dev/null; echo ---WHOAMI---; whoami",
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.ssh_timeout,
            )
            if result.returncode != 0:
                return None
            stdout = result.stdout or ""
            sshare_part, _, who_part = stdout.partition("---WHOAMI---")
            user = who_part.strip() or None
            systems = sh.parse_sshare_usage(sshare_part, cluster_name, user)

            caps = self._capability_cache.get(cluster_uri, (None, None))[0] or {}
            source_tags = ["sshare"]
            fy_info = ""
            if caps.get("sreport") and systems:
                self._enrich_with_sreport(systems, cluster_uri, caps=caps)
                source_tags.append("sreport")
                fy_info = f"FY since {sh.fiscal_year_start()}"

            return {
                "header": f"Slurm usage for {cluster_name}",
                "fiscal_year_info": fy_info,
                "systems": systems,
                "source": "+".join(source_tags),
            }
        except Exception as e:
            _log(f"[pw_cluster] sshare usage error for {cluster_uri}: {e}")
            return None

    def _get_cluster_queues(
        self,
        cluster_uri: str,
        caps: Optional[Dict[str, bool]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get queue information, dispatching by capability."""
        if caps is None:
            caps = self._get_capabilities(cluster_uri)

        if caps.get("show_queues"):
            return self._get_cluster_queues_via_show(cluster_uri)
        if caps.get("scontrol") and caps.get("squeue") and caps.get("sacctmgr"):
            return self._get_cluster_queues_via_slurm(cluster_uri)
        return None

    def _get_cluster_queues_via_show(
        self, cluster_uri: str
    ) -> Optional[Dict[str, Any]]:
        """Original HPCMP ``show_queues`` path."""
        try:
            cmd = self._pw("ssh", cluster_uri, "show_queues")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=self.ssh_timeout,
            )
            return self._parse_queue_output(result.stdout)
        except subprocess.CalledProcessError as e:
            _log(f"[pw_cluster] Error getting queues for {cluster_uri}: {e}")
            return None
        except subprocess.TimeoutExpired:
            _log(f"[pw_cluster] Timeout getting queues for {cluster_uri}")
            return None
        except Exception as e:
            _log(f"[pw_cluster] Unexpected error for {cluster_uri}: {e}")
            return None

    def _get_cluster_queues_via_slurm(
        self, cluster_uri: str
    ) -> Optional[Dict[str, Any]]:
        """Build queue_data from raw Slurm commands (replacement for show_queues).

        Combines ``sacctmgr show qos`` (limits), ``scontrol -o show nodes``
        (node inventory and state), and ``squeue`` (running/pending jobs)
        into one call so we make a single SSH round-trip.
        """
        try:
            sep_qos = "---QOS---"
            sep_nodes = "---NODES---"
            sep_jobs = "---JOBS---"
            sep_parts = "---PARTS---"
            cmd = self._pw(
                "ssh",
                cluster_uri,
                f"echo {sep_qos}; "
                "sacctmgr --parsable2 --noheader show qos "
                "format=Name,Priority,State,MaxWall,MaxJobs,GrpJobs,MaxTRES "
                "2>/dev/null; "
                f"echo {sep_nodes}; "
                "scontrol -o show nodes 2>/dev/null; "
                f"echo {sep_parts}; "
                "scontrol -o show partition 2>/dev/null; "
                f"echo {sep_jobs}; "
                "squeue --all --array --noheader "
                '--format="%i|%u|%a|%P|%q|%T|%D|%C|%j" 2>/dev/null',
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.ssh_timeout,
            )
            if result.returncode != 0:
                _log(
                    f"[pw_cluster] Slurm queues failed for {cluster_uri} "
                    f"(rc={result.returncode}): {result.stderr.strip()[:120]}"
                )
                return None

            stdout = result.stdout or ""
            qos_part = self._slice(stdout, sep_qos, sep_nodes)
            nodes_part = self._slice(stdout, sep_nodes, sep_parts)
            parts_part = self._slice(stdout, sep_parts, sep_jobs)
            jobs_part = self._slice(stdout, sep_jobs, None)

            # sacctmgr --parsable2 emits no header with --noheader, so synthesize one
            qos_with_header = (
                "Name|Priority|State|MaxWall|MaxJobs|GrpJobs|MaxTRES\n" + qos_part
            )
            qos_info = sh.parse_sacctmgr_qos(qos_with_header)
            node_info = sh.parse_slurm_nodes(nodes_part)
            partition_info = sh.parse_scontrol_partitions(parts_part)
            squeue_rows = sh.parse_squeue_jobs(jobs_part)

            queue_data = sh.build_slurm_queue_data(
                qos_info, node_info, squeue_rows, partition_info=partition_info
            )
            queue_data["source"] = "slurm"
            return queue_data
        except Exception as e:
            _log(f"[pw_cluster] Slurm queues error for {cluster_uri}: {e}")
            return None

    @staticmethod
    def _slice(blob: str, start_marker: str, end_marker: Optional[str]) -> str:
        """Extract content between two echo markers in a combined SSH blob."""
        start = blob.find(start_marker)
        if start == -1:
            return ""
        start += len(start_marker)
        if end_marker is None:
            return blob[start:].strip()
        end = blob.find(end_marker, start)
        if end == -1:
            return blob[start:].strip()
        return blob[start:end].strip()

    def _parse_usage_output(self, usage_output: str) -> Dict[str, Any]:
        """Parse the usage output from SSH command."""
        usage_data = {
            "header": "",
            "fiscal_year_info": "",
            "systems": [],
        }

        lines = usage_output.strip().split("\n")

        # Extract header information
        header_lines = []
        for line in lines:
            if (
                line.strip()
                and not line.startswith("System")
                and not line.startswith("=")
            ):
                header_lines.append(line.strip())
            else:
                break

        usage_data["header"] = " ".join(header_lines)

        # Extract fiscal year info
        fiscal_lines = []
        for line in lines:
            if "Fiscal Year" in line or "Hours Remaining" in line:
                fiscal_lines.append(line.strip())

        usage_data["fiscal_year_info"] = " ".join(fiscal_lines)

        # Parse system usage table
        in_table = False
        table_started = False
        separator_found = False

        for line in lines:
            if "System" in line and "Subproject" in line and "Allocated" in line:
                in_table = True
                table_started = True
                separator_found = False
                continue

            if in_table and table_started:
                if line.startswith("=") or line.startswith("--------"):
                    separator_found = True
                    continue

                if not line.strip():
                    continue

                if separator_found:
                    clean_line = line.strip()
                    if clean_line:
                        parts = clean_line.split()
                        if len(parts) >= 7:
                            try:
                                system_info = {
                                    "system": parts[0].strip(),
                                    "subproject": parts[1].strip(),
                                    "hours_allocated": int(parts[2].strip()),
                                    "hours_used": int(parts[3].strip()),
                                    "hours_remaining": int(parts[4].strip()),
                                    "percent_remaining": float(
                                        parts[5].strip().rstrip("%")
                                    ),
                                    "background_hours_used": int(parts[6].strip()),
                                }
                                usage_data["systems"].append(system_info)
                            except (ValueError, IndexError):
                                continue

        return usage_data

    def _parse_queue_output(self, queue_output: str) -> Dict[str, Any]:
        """Parse the queue output from SSH command."""
        queue_data = {
            "queues": [],
            "nodes": [],
        }

        lines = queue_output.strip().split("\n")

        in_queue_section = False
        in_node_section = False

        for line in lines:
            if "QUEUE INFORMATION:" in line or "Queue Name" in line:
                in_queue_section = True
                in_node_section = False
                continue

            if "NODE INFORMATION:" in line or "Node Type" in line:
                in_node_section = True
                in_queue_section = False
                continue

            if in_queue_section:
                if (
                    line.startswith("=")
                    or line.startswith("-")
                    or line.startswith("|")
                    or not line.strip()
                ):
                    continue

                if "Queue Name" not in line and line.strip():
                    parts = line.split()
                    if len(parts) >= 10:
                        try:
                            queue_info = {
                                "queue_name": parts[0].strip(),
                                "max_walltime": parts[1].strip(),
                                "max_jobs": parts[2].strip(),
                                "max_cores": parts[3].strip(),
                                "max_cores_per_job": parts[4].strip(),
                                "jobs_running": parts[5].strip(),
                                "jobs_pending": parts[6].strip(),
                                "cores_running": parts[7].strip(),
                                "cores_pending": parts[8].strip(),
                                "queue_type": parts[9].strip(),
                            }
                            queue_data["queues"].append(queue_info)
                        except (ValueError, IndexError):
                            continue

            if in_node_section:
                if (
                    line.startswith("=")
                    or line.startswith("-")
                    or line.startswith("|")
                    or not line.strip()
                ):
                    continue

                if "Node Type" not in line and line.strip():
                    parts = line.split()
                    if len(parts) >= 5:
                        try:
                            node_info = {
                                "node_type": parts[0].strip(),
                                "nodes_available": parts[1].strip(),
                                "cores_per_node": parts[2].strip(),
                                "cores_available": parts[3].strip(),
                                "cores_running": parts[4].strip(),
                                "cores_free": parts[5].strip()
                                if len(parts) > 5
                                else "0",
                            }
                            queue_data["nodes"].append(node_info)
                        except (ValueError, IndexError):
                            continue

        return queue_data

    def _get_gpu_info(self, cluster_uri: str) -> Optional[Dict[str, Any]]:
        """Get GPU information using nvidia-smi."""
        try:
            cmd = self._pw(
                "ssh",
                cluster_uri,
                "nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu --format=csv,noheader,nounits 2>/dev/null",
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.ssh_timeout,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None
            return self._parse_gpu_output(result.stdout)
        except Exception as e:
            _log(f"[pw_cluster] Error getting GPU info for {cluster_uri}: {e}")
            return None

    def _parse_gpu_output(self, gpu_output: str) -> Dict[str, Any]:
        """Parse nvidia-smi CSV output into structured data."""
        gpus = []
        lines = gpu_output.strip().split("\n")
        for line in lines:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7:
                try:
                    gpus.append(
                        {
                            "index": int(parts[0]),
                            "name": parts[1],
                            "memory_total_mib": int(parts[2]),
                            "memory_used_mib": int(parts[3]),
                            "memory_free_mib": int(parts[4]),
                            "utilization_percent": int(parts[5])
                            if parts[5] != "[N/A]"
                            else 0,
                            "temperature_c": int(parts[6])
                            if parts[6] != "[N/A]"
                            else None,
                        }
                    )
                except (ValueError, IndexError):
                    continue

        total_memory = sum(g["memory_total_mib"] for g in gpus)
        used_memory = sum(g["memory_used_mib"] for g in gpus)
        avg_utilization = (
            sum(g["utilization_percent"] for g in gpus) / len(gpus) if gpus else 0
        )

        return {
            "gpus": gpus,
            "summary": {
                "gpu_count": len(gpus),
                "total_memory_mib": total_memory,
                "used_memory_mib": used_memory,
                "free_memory_mib": total_memory - used_memory,
                "avg_utilization_percent": round(avg_utilization, 1),
            },
        }

    def _get_system_info(self, cluster_uri: str) -> Optional[Dict[str, Any]]:
        """Get basic system information."""
        try:
            # Get CPU, memory, and load info in one command
            cmd = self._pw(
                "ssh",
                cluster_uri,
                'echo "CPU:$(nproc 2>/dev/null || echo 0)"; '
                "echo \"MEM:$(free -m 2>/dev/null | awk '/^Mem:/ {print $2,$3,$4}' || echo '0 0 0')\"; "
                "echo \"LOAD:$(cat /proc/loadavg 2>/dev/null | awk '{print $1,$2,$3}' || echo '0 0 0')\"; "
                'echo "HOST:$(hostname 2>/dev/null || echo unknown)"',
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.ssh_timeout,
            )
            if result.returncode != 0:
                return None
            return self._parse_system_output(result.stdout)
        except Exception as e:
            _log(f"[pw_cluster] Error getting system info for {cluster_uri}: {e}")
            return None

    def _parse_system_output(self, output: str) -> Dict[str, Any]:
        """Parse system info output."""
        info = {
            "cpu_count": 0,
            "memory_total_mb": 0,
            "memory_used_mb": 0,
            "memory_free_mb": 0,
            "load_1m": 0.0,
            "load_5m": 0.0,
            "load_15m": 0.0,
            "hostname": "unknown",
        }
        for line in output.strip().split("\n"):
            if line.startswith("CPU:"):
                try:
                    info["cpu_count"] = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("MEM:"):
                try:
                    parts = line.split(":", 1)[1].strip().split()
                    if len(parts) >= 3:
                        info["memory_total_mb"] = int(parts[0])
                        info["memory_used_mb"] = int(parts[1])
                        info["memory_free_mb"] = int(parts[2])
                except (ValueError, IndexError):
                    pass
            elif line.startswith("LOAD:"):
                try:
                    parts = line.split(":", 1)[1].strip().split()
                    if len(parts) >= 3:
                        info["load_1m"] = float(parts[0])
                        info["load_5m"] = float(parts[1])
                        info["load_15m"] = float(parts[2])
                except (ValueError, IndexError):
                    pass
            elif line.startswith("HOST:"):
                info["hostname"] = line.split(":", 1)[1].strip()
        return info

    def _get_storage_info(
        self,
        cluster_uri: str,
        caps: Optional[Dict[str, bool]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get storage information, dispatching by capability.

        Prefers ``saccount_params`` output (richer: per-project disk and inode
        quotas) on NOAA Hera/Ursa, then falls back to ``df -h`` everywhere else.
        """
        if caps is None:
            caps = self._get_capabilities(cluster_uri)

        if caps.get("saccount_params"):
            cluster_name = cluster_uri.split("/")[-1]
            parsed = self._get_saccount_params(cluster_uri, cluster_name)
            if parsed is not None:
                _systems, storage_dirs, _home = parsed
                if storage_dirs:
                    return storage_dirs

        try:
            storage_cmd = self._pw(
                "ssh",
                cluster_uri,
                "echo 'HOME:'; df -h $HOME 2>/dev/null | tail -1; "
                "echo 'WORK:'; df -h ${WORKDIR:-$HOME} 2>/dev/null | tail -1; "
                "echo 'SCRATCH:'; df -h /scratch 2>/dev/null | tail -1 || df -h /tmp 2>/dev/null | tail -1",
            )
            result = subprocess.run(
                storage_cmd,
                capture_output=True,
                text=True,
                timeout=self.ssh_timeout,
            )

            if result.returncode != 0:
                return None

            return self._parse_storage_output(result.stdout)
        except Exception as e:
            _log(f"[pw_cluster] Error getting storage for {cluster_uri}: {e}")
            return None

    def _parse_storage_output(self, output: str) -> Dict[str, Any]:
        """Parse combined storage output."""
        storage = {}
        current_type = None

        for line in output.strip().split("\n"):
            line = line.strip()
            if line.endswith(":"):
                current_type = line[:-1].lower()
            elif current_type and line:
                parsed = self._parse_df_line(line)
                if parsed:
                    storage[current_type] = parsed

        return storage

    def _parse_df_line(self, line: str) -> Optional[Dict[str, Any]]:
        """Parse a single df output line."""
        parts = line.split()
        if len(parts) >= 5:
            try:
                return {
                    "filesystem": parts[0],
                    "size": parts[1],
                    "used": parts[2],
                    "available": parts[3],
                    "percent_used": parts[4].rstrip("%"),
                }
            except IndexError:
                return None
        return None

    def get_storage_info(self, cluster_uri: str) -> Optional[Dict[str, Any]]:
        """Public method for getting storage info (for backwards compatibility)."""
        return self._get_storage_info(cluster_uri)
