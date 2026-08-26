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
        # cluster_uri -> (hostname, timestamp); the active login node the SSH
        # session lands on. Surfaced as the Fleet status "Login node" column.
        self._hostname_cache: Dict[str, Tuple[str, datetime]] = {}
        # Cached saccount_params output so we can derive both usage + storage
        # from a single SSH call. (cluster_uri -> (parsed_tuple, timestamp))
        self._saccount_cache: Dict[
            str,
            Tuple[Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Any]], datetime],
        ] = {}

    def _recover_from_stale_env_key(self) -> bool:
        """Drop an inherited PW_API_KEY that no longer works.

        A dashboard launched from a workflow run inherits that run's
        injected key, frozen into its environment — and the key does not
        outlive the run's grace period. PW_API_KEY also takes precedence
        over the credentials file, so once it dies it *shadows* any valid
        auth the workspace has: a `pw auth` in a terminal fixes the file
        and changes nothing here.

        So when auth fails and the environment holds a key, test whether
        the credentials file works on its own — and if it does, remove the
        key from this process's environment, which every future `pw`
        subprocess inherits. That one pop heals the collector, the
        marketplace catalog, and anything else this server shells out to.

        Returns True when recovery happened and auth should be retried.
        """
        import os

        if "PW_API_KEY" not in os.environ:
            return False
        stripped = {k: v for k, v in os.environ.items() if k != "PW_API_KEY"}
        try:
            result = subprocess.run(
                ["pw", "auth", "whoami"],
                capture_output=True,
                text=True,
                timeout=10,
                env=stripped,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode == 0 and result.stdout.strip():
            os.environ.pop("PW_API_KEY", None)
            _log(
                "[pw_cluster] Inherited PW_API_KEY is no longer valid; "
                "switched to the workspace's saved credentials"
            )
            return True
        return False

    def _pw(self, *args: str) -> List[str]:
        """Build a ``pw`` command, prepending ``--context`` if pinned."""
        cmd: List[str] = ["pw"]
        if self.pw_context:
            cmd.extend(["--context", self.pw_context])
        cmd.extend(args)
        return cmd

    # Ask for the fully-qualified name and fall back to the short one. The
    # FQDN is what says where a cluster physically lives —
    # ``crux.mhpcc.hpc.mil`` identifies the DSRC that ``pw://user/crux``
    # cannot — so it is worth asking for even though many sites only
    # answer with a short name.
    _HOSTNAME_COMMAND = "hostname -f 2>/dev/null || hostname"

    def probe_login(self, cluster_uri: str) -> Tuple[Optional[int], Optional[str]]:
        """Time one round trip and learn the login node's name in the same call.

        Returns ``(latency_ms, hostname)``. Both are None when the probe
        fails; a failed probe is not an outage signal on its own, so callers
        should treat it as "unknown".

        The latency measures the whole control-plane path — ``pw`` CLI
        start-up, auth, and the SSH round trip — not a network ping. It is
        the number that predicts how long a collection sweep will take,
        which is what the topology view wants, but it must never be
        presented as raw network latency.
        """
        try:
            started = time.monotonic()
            result = subprocess.run(
                self._pw("ssh", cluster_uri, self._HOSTNAME_COMMAND),
                capture_output=True,
                text=True,
                timeout=min(30, self.ssh_timeout),
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if result.returncode != 0:
                return None, None
            lines = (result.stdout or "").strip().splitlines()
            host = lines[0].strip() if lines else None
            if host:
                self._hostname_cache[cluster_uri] = (host, datetime.utcnow())
            return elapsed_ms, host or None
        except Exception as e:
            _log(f"[pw_cluster] login probe failed for {cluster_uri}: {e}")
            return None, None

    def get_login_hostname(self, cluster_uri: str) -> Optional[str]:
        """Return the actual hostname the SSH session lands on, cached.

        The PW URI (``pw://user/clustername``) is not a real DNS name — it
        resolves to whichever login / front-end node the PW agent routes
        to. Surfacing that hostname (``hfe02``, ``crux.mhpcc.hpc.mil``)
        makes the Fleet table's "Login node" column actually useful, and
        gives the topology view a way to tell which site a cluster is at.

        Cached for the same 10-minute TTL as capability probes since the
        active login is stable across refreshes.
        """
        cached = self._hostname_cache.get(cluster_uri)
        if cached and datetime.utcnow() - cached[1] < _CAPABILITY_TTL:
            return cached[0]
        return self.probe_login(cluster_uri)[1]

    def measure_latency(self, cluster_uri: str) -> Optional[int]:
        """Time one control-plane round trip, in milliseconds."""
        return self.probe_login(cluster_uri)[0]

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
            elif self._recover_from_stale_env_key():
                return self.check_auth()
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

    def collect(self, progress_cb=None) -> Dict[str, Any]:
        """Collect data from all active PW clusters.

        Args:
            progress_cb: Optional callable invoked twice per cluster — once
                as ``progress_cb("start", idx, total, name, None)`` before
                each cluster's data is gathered, and again as
                ``progress_cb("complete", idx+1, total, name, cluster_data)``
                once the SSH round-trips finish. Lets the worker stream
                progress + partial results to the UI during long first sweeps.

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

            # Newly connected clusters go first. A sweep of a large fleet
            # takes minutes, and someone who just connected a machine is
            # usually watching for it — the machines that were already
            # there can wait their turn.
            fresh = [c for c in clusters if c["uri"] not in self._known_clusters]
            if fresh and len(fresh) < len(clusters):
                names = ", ".join(c["uri"].rsplit("/", 1)[-1] for c in fresh)
                _log(f"[pw_cluster] {len(fresh)} newly connected cluster(s) first: {names}")
                known = [c for c in clusters if c["uri"] in self._known_clusters]
                clusters = fresh + known

            results = []
            total = len(clusters)
            for idx, cluster in enumerate(clusters):
                cluster_name = cluster["uri"].split("/")[-1]
                if progress_cb is not None:
                    try:
                        progress_cb("start", idx, total, cluster_name, None)
                    except Exception as cb_exc:
                        _log(f"[pw_cluster] progress_cb(start) raised: {cb_exc}")
                cluster_data = self._process_cluster(cluster)
                if cluster_data:
                    results.append(cluster_data)
                    self._known_clusters.add(cluster["uri"])
                if progress_cb is not None:
                    try:
                        progress_cb("complete", idx + 1, total, cluster_name, cluster_data)
                    except Exception as cb_exc:
                        _log(f"[pw_cluster] progress_cb(complete) raised: {cb_exc}")

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

    def get_active_clusters(self, quiet: bool = False) -> List[Dict[str, str]]:
        """Get active clusters using pw CLI command.

        Returns list of clusters with type='existing' and an active status.

        Args:
            quiet: skip the table dump — the between-sweep listing watcher
                calls this every half minute, and logging the whole fleet
                each time buries the lines that matter.

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
            if not quiet:
                _log(f"[pw_cluster] pw clusters ls output:\n{result.stdout.strip()}")
            clusters = self._parse_cluster_table(result.stdout, quiet=quiet)
            if not quiet:
                _log(f"[pw_cluster] Parsed {len(clusters)} active clusters from table")
            return clusters
        except subprocess.CalledProcessError as e:
            raise CollectorError(self.name, f"Error getting clusters: {e}", e)
        except subprocess.TimeoutExpired as e:
            raise CollectorError(self.name, "Timeout getting cluster list", e)
        except Exception as e:
            raise CollectorError(self.name, f"Unexpected error: {e}", e)

    def _parse_cluster_table(self, table_output: str, quiet: bool = False) -> List[Dict[str, str]]:
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
                elif not quiet:
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
        # One probe, two answers: round-trip time and the login node's name.
        latency_ms, login_hostname = self.probe_login(cluster["uri"])
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
                "latency_ms": latency_ms,
                "hostname": login_hostname,
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
                # core-hour totals for the current NOAA fiscal year +
                # per-account/QoS operational limits (MaxWall, MaxJobs, etc).
                self._enrich_with_sfairshare(
                    systems, cluster_uri, caps=caps
                )
                self._enrich_with_sreport(
                    systems, cluster_uri, caps=caps
                )
                self._enrich_with_assoc_and_qos(
                    systems, cluster_uri, caps=caps
                )
                source_tags = ["saccount_params"]
                if caps.get("sfairshare"):
                    source_tags.append("sfairshare")
                if caps.get("sreport"):
                    source_tags.append("sreport")
                if caps.get("sacctmgr"):
                    source_tags.append("sacctmgr")
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

    def _enrich_with_assoc_and_qos(
        self,
        systems: List[Dict[str, Any]],
        cluster_uri: str,
        caps: Dict[str, bool],
    ) -> None:
        """Attach per-account allocation limits to each project row.

        For each project the user belongs to, populate:
          - ``account_max_jobs``  concurrent-job cap on the association
          - ``account_qoses``     QoSes the *association* grants
          - ``qos_limits``        dict[qos_name] → {max_wall, max_jobs,
                                                    max_tres, grp_jobs}

        Uses ``sacctmgr show association user=$USER`` for the per-account
        record and ``sacctmgr show qos`` for the QoS ceilings. Both are
        rolled into a single SSH round-trip.
        """
        if not (caps.get("sacctmgr") and systems):
            return
        try:
            sep = "---QOS---"
            cmd = self._pw(
                "ssh",
                cluster_uri,
                "sacctmgr --parsable2 show association user=$(whoami) "
                "format=Account,User,QOS,Fairshare,MaxJobs,GrpJobs 2>/dev/null; "
                f"echo {sep}; "
                "sacctmgr --parsable2 show qos "
                "format=Name,MaxWall,MaxJobs,GrpJobs,MaxTRES,Priority 2>/dev/null",
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.ssh_timeout,
            )
            if result.returncode != 0:
                return
            stdout = result.stdout or ""
            assoc_part, _, qos_part = stdout.partition(sep)
            assoc_rows = sh.parse_sacctmgr_assoc(assoc_part)
            qos_info = sh.parse_sacctmgr_qos(qos_part.strip())

            for row in systems:
                acct = row.get("subproject")
                if not acct:
                    continue
                # Per-account fields
                if acct in assoc_rows:
                    a = assoc_rows[acct]
                    row["account_max_jobs"] = a.get("max_jobs") or a.get("grp_jobs") or None
                    if a.get("qos"):
                        row["account_qoses"] = a["qos"]
                # Per-QoS limits — only for QoSes this project can actually use
                project_qoses = row.get("qoses") or []
                if project_qoses:
                    limits: Dict[str, Dict[str, Any]] = {}
                    for q in project_qoses:
                        rec = qos_info.get(q)
                        if not rec:
                            continue
                        limits[q] = {
                            "max_wall": rec.get("MaxWall") or "",
                            "max_jobs": rec.get("MaxJobs") or "",
                            "grp_jobs": rec.get("GrpJobs") or "",
                            "max_tres": rec.get("MaxTRES") or "",
                            "priority": rec.get("Priority") or "",
                        }
                    if limits:
                        row["qos_limits"] = limits
        except Exception as e:
            _log(f"[pw_cluster] sacctmgr assoc/qos error for {cluster_uri}: {e}")

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
            sep_sinfo = "---SINFO---"
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
                f"echo {sep_sinfo}; "
                # sinfo's cluster-wide CPU summary lands the right number on
                # Cray-style clusters (Gaea) where scontrol show nodes from
                # the login can't see the batch compute nodes. Format is
                # one line per node-state group: "A/I/O/T" (alloc/idle/
                # other/total). We sum across rows.
                'sinfo -h --noheader -o "%C" 2>/dev/null; '
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
            parts_part = self._slice(stdout, sep_parts, sep_sinfo)
            sinfo_part = self._slice(stdout, sep_sinfo, sep_jobs)
            jobs_part = self._slice(stdout, sep_jobs, None)

            # sacctmgr --parsable2 emits no header with --noheader, so synthesize one
            qos_with_header = (
                "Name|Priority|State|MaxWall|MaxJobs|GrpJobs|MaxTRES\n" + qos_part
            )
            qos_info = sh.parse_sacctmgr_qos(qos_with_header)
            node_info = sh.parse_slurm_nodes(nodes_part)
            partition_info = sh.parse_scontrol_partitions(parts_part)
            squeue_rows = sh.parse_squeue_jobs(jobs_part)
            sinfo_totals = sh.parse_sinfo_cpu_state(sinfo_part)

            queue_data = sh.build_slurm_queue_data(
                qos_info,
                node_info,
                squeue_rows,
                partition_info=partition_info,
                sinfo_totals=sinfo_totals,
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

    # show_queues header labels → the field names we emit. Labels are
    # matched after lowercasing; anything unrecognized is kept but ignored.
    _QUEUE_COLUMN_ALIASES: Dict[str, str] = {
        "queue": "queue_name",
        "queuename": "queue_name",
        "name": "queue_name",
        "maxtime": "max_walltime",
        "maxwalltime": "max_walltime",
        "walltime": "max_walltime",
        "maxjobs": "max_jobs",
        "maxcores": "max_cores",
        "maxcoresperjob": "max_cores_per_job",
        "maxcores/job": "max_cores_per_job",
        "cores/job": "max_cores_per_job",
        "running": "jobs_running",
        "jobsrunning": "jobs_running",
        "run": "jobs_running",
        "pending": "jobs_pending",
        "jobspending": "jobs_pending",
        "pend": "jobs_pending",
        "coresrun": "cores_running",
        "coresrunning": "cores_running",
        "corespend": "cores_pending",
        "corespending": "cores_pending",
        "type": "queue_type",
        "queuetype": "queue_type",
    }

    def _map_queue_columns(self, header: str) -> Optional[List[str]]:
        """Map a show_queues header row to output field names, by position.

        Header labels are multi-word ("Queue Name", "Cores Run") while data
        rows are whitespace-split, so tokens are matched longest-run-first:
        "Queue Name" has to beat a bare "Queue", and "Max Cores Per Job" has
        to beat "Max Cores".

        Returns None when any label is unrecognizable, which sends the
        caller back to the fixed-position fallback rather than risk lining
        values up against the wrong fields.
        """
        tokens = header.split()
        fields: List[str] = []
        index = 0
        while index < len(tokens):
            matched = None
            for span in range(min(4, len(tokens) - index), 0, -1):
                key = re.sub(r"[^a-z0-9/]", "", "".join(tokens[index : index + span]).lower())
                field = self._QUEUE_COLUMN_ALIASES.get(key)
                if field:
                    matched = (field, span)
                    break
            if not matched:
                return None
            fields.append(matched[0])
            index += matched[1]
        if "queue_name" not in fields or len(fields) < 6:
            return None
        return fields

    def _parse_queue_output(self, queue_output: str) -> Dict[str, Any]:
        """Parse the queue output from SSH command."""
        queue_data = {
            "queues": [],
            "nodes": [],
        }

        lines = queue_output.strip().split("\n")

        in_queue_section = False
        in_node_section = False
        queue_columns: Optional[List[str]] = None

        for line in lines:
            if "QUEUE INFORMATION:" in line or "Queue Name" in line:
                in_queue_section = True
                in_node_section = False
                # Remember the column layout: sites publish show_queues with
                # and without the "max cores per job" column, and positional
                # indexes alone silently drop every row on the shorter form.
                if "Queue Name" in line:
                    queue_columns = self._map_queue_columns(line)
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
                    queue_info = None
                    if queue_columns and len(parts) == len(queue_columns):
                        queue_info = {
                            field: parts[idx].strip()
                            for idx, field in enumerate(queue_columns)
                            if field
                        }
                    elif len(parts) >= 10:
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
                    if queue_info and queue_info.get("queue_name"):
                        queue_data["queues"].append(queue_info)

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
