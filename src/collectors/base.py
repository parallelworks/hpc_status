"""Base collector interface for data sources."""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional


class BaseCollector(ABC):
    """Abstract base class for data collectors.

    All collectors must implement this interface to provide a consistent
    way to collect data from various sources (HPCMP scraping, PW CLI, etc.).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this collector.

        Returns:
            A short, lowercase identifier (e.g., 'hpcmp', 'pw_cluster')
        """
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name for UI display.

        Returns:
            A user-friendly name (e.g., 'HPCMP Fleet Status', 'PW Clusters')
        """
        pass

    @abstractmethod
    def collect(self) -> Dict[str, Any]:
        """Fetch current data from this source.

        Returns:
            Dictionary containing the collected data. Structure varies by collector.

        Raises:
            CollectorError: If data collection fails.
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this collector can run (dependencies met).

        Returns:
            True if the collector can operate, False otherwise.
        """
        pass

    def get_status(self) -> Dict[str, Any]:
        """Get collector status information.

        Returns:
            Dictionary with status details including availability and last error.
        """
        return {
            "name": self.name,
            "display_name": self.display_name,
            "available": self.is_available(),
        }


class CollectorError(Exception):
    """Exception raised when a collector fails to collect data."""

    def __init__(self, collector_name: str, message: str, cause: Optional[Exception] = None):
        self.collector_name = collector_name
        self.cause = cause
        super().__init__(f"[{collector_name}] {message}")
