"""Storage monitoring collector.

Collects $HOME and $WORKDIR storage information from clusters via PW SSH.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base import BaseCollector, CollectorError
from ..data.models import StorageInfo


class StorageCollector(BaseCollector):
    """Collector for storage capacity information.

    Uses `pw ssh df -h` to get storage usage for $HOME and $WORKDIR.
    """

    def __init__(self, ssh_timeout: int = 30):
        self.ssh_timeout = ssh_timeout
        self._cluster_uris: List[str] = []

    @property
    def name(self) -> str:
        return "storage"

    @property
    def display_name(self) -> str:
        return "Storage Capacity"

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

    def set_clusters(self, cluster_uris: List[str]) -> None:
        """Set the list of cluster URIs to monitor."""
        self._cluster_uris = cluster_uris

    def collect(self) -> Dict[str, Any]:
        """Collect storage information from all configured clusters.

        Returns:
            Dictionary with storage data per cluster.
        """
        if not self._cluster_uris:
            return {
                "meta": {
                    "generated_at": datetime.utcnow().isoformat() + "Z",
                    "collector": self.name,
                },
                "clusters": {},
            }

        results = {}
        for uri in self._cluster_uris:
            cluster_name = uri.split("/")[-1]
            storage_data = self._get_cluster_storage(uri)
            if storage_data:
                results[cluster_name] = storage_data

        return {
            "meta": {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "collector": self.name,
                "clusters_polled": len(self._cluster_uris),
                "clusters_succeeded": len(results),
            },
            "clusters": results,
        }

    def _get_cluster_storage(self, cluster_uri: str) -> Optional[Dict[str, Any]]:
        """Get storage information for a single cluster."""
        try:
            home_info = self._get_df_info(cluster_uri, "$HOME")
            workdir_info = self._get_df_info(cluster_uri, "$WORKDIR")
            scratch_info = self._get_df_info(cluster_uri, "/scratch")

            return {
                "uri": cluster_uri,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "home": self._to_storage_dict(home_info, "$HOME") if home_info else None,
                "workdir": self._to_storage_dict(workdir_info, "$WORKDIR") if workdir_info else None,
                "scratch": self._to_storage_dict(scratch_info, "/scratch") if scratch_info else None,
            }
        except Exception as e:
            print(f"[storage] Error collecting from {cluster_uri}: {e}")
            return None

    def _get_df_info(self, cluster_uri: str, path: str) -> Optional[Dict[str, str]]:
        """Run df -h on a path and parse the output."""
        try:
            cmd = ["pw", "ssh", cluster_uri, f"df -h {path}"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.ssh_timeout,
            )
            if result.returncode != 0:
                return None
            return self._parse_df_output(result.stdout)
        except subprocess.TimeoutExpired:
            print(f"[storage] Timeout getting df for {path} on {cluster_uri}")
            return None
        except Exception as e:
            print(f"[storage] Error running df on {cluster_uri}: {e}")
            return None

    def _parse_df_output(self, df_output: str) -> Optional[Dict[str, str]]:
        """Parse df -h output into structured data."""
        lines = df_output.strip().split("\n")
        if len(lines) < 2:
            return None

        # Handle wrapped lines (when filesystem name is long)
        data_line = lines[-1]
        if len(lines) > 2 and not data_line[0].isspace():
            # Check if previous line is continuation
            prev = lines[-2].strip()
            if prev and not prev.startswith("Filesystem"):
                data_line = prev + " " + data_line

        parts = data_line.split()
        if len(parts) >= 5:
            try:
                return {
                    "filesystem": parts[0],
                    "size": parts[1] if len(parts) > 1 else "0",
                    "used": parts[2] if len(parts) > 2 else "0",
                    "available": parts[3] if len(parts) > 3 else "0",
                    "percent_used": parts[4].rstrip("%") if len(parts) > 4 else "0",
                }
            except (IndexError, ValueError):
                return None
        return None

    def _to_storage_dict(self, df_info: Dict[str, str], mount_point: str) -> Dict[str, Any]:
        """Convert df info to storage dictionary."""
        return {
            "mount_point": mount_point,
            "filesystem": df_info.get("filesystem", ""),
            "size": df_info.get("size", "0"),
            "used": df_info.get("used", "0"),
            "available": df_info.get("available", "0"),
            "percent_used": float(df_info.get("percent_used", 0)),
            "status": self._calculate_status(float(df_info.get("percent_used", 0))),
        }

    def _calculate_status(self, percent_used: float) -> str:
        """Calculate status based on usage percentage."""
        if percent_used >= 95:
            return "critical"
        elif percent_used >= 80:
            return "warning"
        return "healthy"

    def to_storage_info(self, data: Dict[str, Any], mount_point: str) -> Optional[StorageInfo]:
        """Convert raw storage data to StorageInfo model."""
        if not data:
            return None
        try:
            return StorageInfo(
                mount_point=mount_point,
                filesystem=data.get("filesystem", ""),
                total_gb=self._parse_size(data.get("size", "0")),
                used_gb=self._parse_size(data.get("used", "0")),
                available_gb=self._parse_size(data.get("available", "0")),
                percent_used=float(data.get("percent_used", 0)),
            )
        except (ValueError, TypeError):
            return None

    def _parse_size(self, size_str: str) -> float:
        """Parse size string (e.g., '100G', '1.5T') to GB."""
        size_str = size_str.strip().upper()
        if not size_str or size_str == "0":
            return 0.0

        try:
            if size_str.endswith("T"):
                return float(size_str[:-1]) * 1024
            elif size_str.endswith("G"):
                return float(size_str[:-1])
            elif size_str.endswith("M"):
                return float(size_str[:-1]) / 1024
            elif size_str.endswith("K"):
                return float(size_str[:-1]) / (1024 * 1024)
            else:
                # Assume bytes
                return float(size_str) / (1024 * 1024 * 1024)
        except ValueError:
            return 0.0


def get_storage_warnings(storage_data: Dict[str, Any]) -> List[Dict[str, str]]:
    """Generate warnings for storage that is running low.

    Args:
        storage_data: Storage data from StorageCollector.collect()

    Returns:
        List of warning dictionaries with cluster, path, and message.
    """
    warnings = []
    for cluster_name, cluster_data in storage_data.get("clusters", {}).items():
        for path_key in ["home", "workdir", "scratch"]:
            path_data = cluster_data.get(path_key)
            if not path_data:
                continue
            percent = path_data.get("percent_used", 0)
            mount = path_data.get("mount_point", path_key)

            if percent >= 95:
                warnings.append({
                    "cluster": cluster_name,
                    "path": mount,
                    "message": f"Storage critically full ({percent:.0f}% used). Clean up immediately.",
                    "severity": "critical",
                    "percent_used": percent,
                })
            elif percent >= 80:
                warnings.append({
                    "cluster": cluster_name,
                    "path": mount,
                    "message": f"Storage running low ({percent:.0f}% used). Consider cleanup.",
                    "severity": "warning",
                    "percent_used": percent,
                })

    return sorted(warnings, key=lambda w: w["percent_used"], reverse=True)
