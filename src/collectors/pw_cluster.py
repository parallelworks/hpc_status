"""PW CLI cluster collector.

Collects cluster usage, queue, and status information using the PW CLI.
This is the core data collector that works across all deployments.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base import BaseCollector, CollectorError


class PWClusterCollector(BaseCollector):
    """Collector for PW-connected clusters.

    Uses `pw clusters ls` and `pw ssh` commands to gather usage and queue data.
    """

    def __init__(
        self,
        max_retries: int = 3,
        retry_delay: int = 5,
        ssh_timeout: int = 30,
    ):
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.ssh_timeout = ssh_timeout
        self._known_clusters: set = set()

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

        Returns list of clusters with type='existing' and status='on'.
        """
        try:
            cmd = [
                "pw", "clusters", "ls",
                "--status=on",
                "-o", "table",
                "--owned",
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            return self._parse_cluster_table(result.stdout)
        except subprocess.CalledProcessError as e:
            print(f"[pw_cluster] Error getting clusters: {e}")
            return []
        except subprocess.TimeoutExpired:
            print("[pw_cluster] Timeout getting cluster list")
            return []
        except Exception as e:
            print(f"[pw_cluster] Unexpected error: {e}")
            return []

    def _parse_cluster_table(self, table_output: str) -> List[Dict[str, str]]:
        """Parse the cluster table output from pw CLI."""
        clusters = []
        lines = table_output.strip().split("\n")

        for line in lines:
            # Skip separator lines
            if line.startswith("+") or not line.strip():
                continue
            # Skip header lines
            if "URI" in line or "STATUS" in line or "TYPE" in line:
                continue

            # Clean up the line
            clean_line = line.strip().strip("|").strip()
            if not clean_line:
                continue

            # Split by pipe character
            parts = [part.strip() for part in clean_line.split("|") if part.strip()]

            if len(parts) >= 3:
                uri = parts[0].strip()
                status = parts[1].strip()
                cluster_type = parts[2].strip()

                if cluster_type == "existing" and status == "on":
                    clusters.append({
                        "uri": uri,
                        "status": status,
                        "type": cluster_type,
                    })

        return clusters

    def _process_cluster(self, cluster: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """Process a single cluster and return its data."""
        cluster_name = cluster["uri"].split("/")[-1]

        usage_data = self._get_cluster_usage(cluster["uri"])
        queue_data = self._get_cluster_queues(cluster["uri"])

        return {
            "cluster_metadata": {
                "name": cluster_name,
                "uri": cluster["uri"],
                "status": cluster["status"],
                "type": cluster["type"],
                "timestamp": datetime.utcnow().isoformat() + "Z",
            },
            "usage_data": usage_data or {},
            "queue_data": queue_data or {},
        }

    def _get_cluster_usage(self, cluster_uri: str) -> Optional[Dict[str, Any]]:
        """Get usage information for a specific cluster using SSH."""
        try:
            cmd = ["pw", "ssh", cluster_uri, "show_usage"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=self.ssh_timeout,
            )
            return self._parse_usage_output(result.stdout)
        except subprocess.CalledProcessError as e:
            print(f"[pw_cluster] Error getting usage for {cluster_uri}: {e}")
            return None
        except subprocess.TimeoutExpired:
            print(f"[pw_cluster] Timeout getting usage for {cluster_uri}")
            return None
        except Exception as e:
            print(f"[pw_cluster] Unexpected error for {cluster_uri}: {e}")
            return None

    def _get_cluster_queues(self, cluster_uri: str) -> Optional[Dict[str, Any]]:
        """Get queue information for a specific cluster using SSH."""
        try:
            cmd = ["pw", "ssh", cluster_uri, "show_queues"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=self.ssh_timeout,
            )
            return self._parse_queue_output(result.stdout)
        except subprocess.CalledProcessError as e:
            print(f"[pw_cluster] Error getting queues for {cluster_uri}: {e}")
            return None
        except subprocess.TimeoutExpired:
            print(f"[pw_cluster] Timeout getting queues for {cluster_uri}")
            return None
        except Exception as e:
            print(f"[pw_cluster] Unexpected error for {cluster_uri}: {e}")
            return None

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
            if line.strip() and not line.startswith("System") and not line.startswith("="):
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
                                    "percent_remaining": float(parts[5].strip().rstrip("%")),
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
                if line.startswith("=") or line.startswith("-") or line.startswith("|") or not line.strip():
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
                if line.startswith("=") or line.startswith("-") or line.startswith("|") or not line.strip():
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
                                "cores_free": parts[5].strip() if len(parts) > 5 else "0",
                            }
                            queue_data["nodes"].append(node_info)
                        except (ValueError, IndexError):
                            continue

        return queue_data

    def get_storage_info(self, cluster_uri: str) -> Optional[Dict[str, Any]]:
        """Get storage information for a cluster.

        Runs `df -h` on $HOME and $WORKDIR to get storage usage.
        """
        try:
            # Get $HOME storage
            home_cmd = ["pw", "ssh", cluster_uri, "df -h $HOME"]
            home_result = subprocess.run(
                home_cmd,
                capture_output=True,
                text=True,
                timeout=self.ssh_timeout,
            )

            # Get $WORKDIR storage
            work_cmd = ["pw", "ssh", cluster_uri, "df -h $WORKDIR"]
            work_result = subprocess.run(
                work_cmd,
                capture_output=True,
                text=True,
                timeout=self.ssh_timeout,
            )

            return {
                "home": self._parse_df_output(home_result.stdout) if home_result.returncode == 0 else None,
                "workdir": self._parse_df_output(work_result.stdout) if work_result.returncode == 0 else None,
            }
        except Exception as e:
            print(f"[pw_cluster] Error getting storage for {cluster_uri}: {e}")
            return None

    def _parse_df_output(self, df_output: str) -> Optional[Dict[str, Any]]:
        """Parse df -h output into structured data."""
        lines = df_output.strip().split("\n")
        if len(lines) < 2:
            return None

        # Skip header line
        data_line = lines[-1].split()
        if len(data_line) >= 5:
            try:
                return {
                    "filesystem": data_line[0],
                    "size": data_line[1],
                    "used": data_line[2],
                    "available": data_line[3],
                    "percent_used": data_line[4].rstrip("%"),
                }
            except IndexError:
                return None
        return None
