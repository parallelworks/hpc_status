"""NOAA RDHPCS system collector.

Provides status information for NOAA Research and Development HPC Systems.
"""

from typing import Dict, Any, List
from .base import BaseCollector, CollectorError

# NOAA RDHPCS systems and their properties
NOAA_SYSTEMS = {
    "hera": {
        "name": "Hera",
        "location": "NESCC (Fairmont, WV)",
        "scheduler": "Slurm",
        "description": "Dell PowerEdge cluster for weather and climate research",
        "login_node": "hera.rdhpcs.noaa.gov",
    },
    "jet": {
        "name": "Jet",
        "location": "ESRL (Boulder, CO)",
        "scheduler": "Slurm",
        "description": "Research system for NOAA laboratories",
        "login_node": "jet.rdhpcs.noaa.gov",
    },
    "gaea": {
        "name": "Gaea",
        "location": "ORNL (Oak Ridge, TN)",
        "scheduler": "Slurm",
        "description": "Cray XC40 for GFDL climate modeling",
        "login_node": "gaea.rdhpcs.noaa.gov",
    },
    "orion": {
        "name": "Orion",
        "location": "MSU (Starkville, MS)",
        "scheduler": "Slurm",
        "description": "Dell cluster for operational modeling",
        "login_node": "orion.rdhpcs.noaa.gov",
    },
    "hercules": {
        "name": "Hercules",
        "location": "MSU (Starkville, MS)",
        "scheduler": "Slurm",
        "description": "AMD-based cluster for research workloads",
        "login_node": "hercules.rdhpcs.noaa.gov",
    },
    "ppan": {
        "name": "PPAN",
        "location": "GFDL (Princeton, NJ)",
        "scheduler": "Slurm",
        "description": "Post-processing and analysis cluster",
        "login_node": "ppan.rdhpcs.noaa.gov",
    },
}


class NOAADocsCollector(BaseCollector):
    """Collector for NOAA RDHPCS system information.

    Provides static system definitions and can be extended to scrape
    the NOAA RDHPCS documentation site for status updates.
    """

    def __init__(self, url: str = None, timeout: int = 30):
        """Initialize the collector.

        Args:
            url: NOAA docs URL (optional, for future scraping)
            timeout: Request timeout in seconds
        """
        self._url = url or "https://docs.rdhpcs.noaa.gov/systems/index.html"
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "noaa_docs"

    @property
    def display_name(self) -> str:
        return "NOAA RDHPCS Systems"

    def is_available(self) -> bool:
        """Check if collector is available.

        Returns:
            Always True since we have static system definitions.
        """
        return True

    def collect(self) -> Dict[str, Any]:
        """Collect NOAA system information.

        Returns:
            Dictionary with system information.
        """
        systems = []

        for system_id, info in NOAA_SYSTEMS.items():
            systems.append({
                "id": system_id,
                "system": info["name"],
                "status": "UP",  # Default to UP; actual status from PW cluster monitor
                "location": info["location"],
                "scheduler": info["scheduler"],
                "login_node": info["login_node"],
                "description": info["description"],
            })

        return {
            "source": "noaa_docs",
            "platform": "noaa",
            "systems": systems,
            "total_systems": len(systems),
        }

    def get_system_info(self, system_name: str) -> Dict[str, Any]:
        """Get information for a specific system.

        Args:
            system_name: System name (case-insensitive)

        Returns:
            System information dictionary, or empty dict if not found.
        """
        key = system_name.lower()
        if key in NOAA_SYSTEMS:
            info = NOAA_SYSTEMS[key]
            return {
                "id": key,
                "system": info["name"],
                "location": info["location"],
                "scheduler": info["scheduler"],
                "login_node": info["login_node"],
                "description": info["description"],
            }
        return {}

    def list_systems(self) -> List[str]:
        """Get list of known NOAA systems.

        Returns:
            List of system names.
        """
        return [info["name"] for info in NOAA_SYSTEMS.values()]
