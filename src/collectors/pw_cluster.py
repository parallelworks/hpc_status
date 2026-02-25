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
        ssh_timeout: int = 60,
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

        Raises:
            CollectorError: If the pw CLI command fails.
        """
        try:
            cmd = [
                "pw",
                "clusters",
                "ls",
                "--status=active",
                "-o",
                "table",
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
            raise CollectorError(self.name, f"Error getting clusters: {e}", e)
        except subprocess.TimeoutExpired as e:
            raise CollectorError(self.name, "Timeout getting cluster list", e)
        except Exception as e:
            raise CollectorError(self.name, f"Unexpected error: {e}", e)

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

                if cluster_type == "existing" and status == "active":
                    clusters.append(
                        {
                            "uri": uri,
                            "status": status,
                            "type": cluster_type,
                        }
                    )

        return clusters

    def _process_cluster(self, cluster: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """Process a single cluster and return its data."""
        cluster_name = cluster["uri"].split("/")[-1]

        usage_data = self._get_cluster_usage(cluster["uri"])
        queue_data = self._get_cluster_queues(cluster["uri"])

        # For clusters without schedulers, try to get GPU and system info
        gpu_data = None
        system_info = None
        has_scheduler = bool(usage_data and usage_data.get("systems")) or bool(
            queue_data and queue_data.get("queues")
        )

        if not has_scheduler:
            # This is likely a standalone compute server - get GPU/system info
            gpu_data = self._get_gpu_info(cluster["uri"])
            system_info = self._get_system_info(cluster["uri"])

        # Always collect storage info for all clusters
        storage_data = self._get_storage_info(cluster["uri"])

        return {
            "cluster_metadata": {
                "name": cluster_name,
                "uri": cluster["uri"],
                "status": cluster["status"],
                "type": cluster["type"],
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "has_scheduler": has_scheduler,
            },
            "usage_data": usage_data or {},
            "queue_data": queue_data or {},
            "gpu_data": gpu_data or {},
            "system_info": system_info or {},
            "storage_data": storage_data or {},
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
            cmd = [
                "pw",
                "ssh",
                cluster_uri,
                "nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu --format=csv,noheader,nounits 2>/dev/null",
            ]
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
            print(f"[pw_cluster] Error getting GPU info for {cluster_uri}: {e}")
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
            cmd = [
                "pw",
                "ssh",
                cluster_uri,
                'echo "CPU:$(nproc 2>/dev/null || echo 0)"; '
                "echo \"MEM:$(free -m 2>/dev/null | awk '/^Mem:/ {print $2,$3,$4}' || echo '0 0 0')\"; "
                "echo \"LOAD:$(cat /proc/loadavg 2>/dev/null | awk '{print $1,$2,$3}' || echo '0 0 0')\"; "
                'echo "HOST:$(hostname 2>/dev/null || echo unknown)"',
            ]
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
            print(f"[pw_cluster] Error getting system info for {cluster_uri}: {e}")
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

    def _get_storage_info(self, cluster_uri: str) -> Optional[Dict[str, Any]]:
        """Get storage information for a cluster.

        Runs `df -h` on common filesystem paths to get storage usage.
        """
        try:
            # Single command to get all filesystem info at once
            storage_cmd = [
                "pw",
                "ssh",
                cluster_uri,
                "echo 'HOME:'; df -h $HOME 2>/dev/null | tail -1; "
                "echo 'WORK:'; df -h ${WORKDIR:-$HOME} 2>/dev/null | tail -1; "
                "echo 'SCRATCH:'; df -h /scratch 2>/dev/null | tail -1 || df -h /tmp 2>/dev/null | tail -1",
            ]
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
            print(f"[pw_cluster] Error getting storage for {cluster_uri}: {e}")
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
