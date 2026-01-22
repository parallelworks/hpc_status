"""Configuration management for HPC status monitor.

Supports YAML-based configuration with platform-specific settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class CollectorConfig:
    """Configuration for a data collector."""

    enabled: bool = True
    refresh_interval: int = 120  # seconds
    timeout: int = 30
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RateLimitConfig:
    """Rate limiting configuration."""

    max_concurrent_ssh: int = 3
    ssh_timeout: int = 30
    retry_backoff: List[int] = field(default_factory=lambda: [5, 15, 60])
    min_interval: int = 60
    max_commands_per_poll: int = 5
    failure_threshold: int = 3
    pause_duration: int = 300


@dataclass
class UIConfig:
    """UI configuration."""

    title: str = "HPC Status Monitor"
    eyebrow: str = "HPC STATUS"  # Header eyebrow text
    home_page: str = "overview"  # 'overview', 'fleet', 'queues'
    tabs: Dict[str, bool] = field(
        default_factory=lambda: {
            "overview": True,
            "fleet": True,
            "queues": True,
            "quota": True,
            "storage": True,
        }
    )
    default_theme: str = "dark"


@dataclass
class ServerConfig:
    """Server configuration."""

    host: str = "0.0.0.0"
    port: int = 8080
    url_prefix: str = ""


@dataclass
class Config:
    """Main configuration container."""

    deployment_name: str = "HPC Status Monitor"
    platform: str = "generic"  # 'generic', 'hpcmp', 'noaa'

    server: ServerConfig = field(default_factory=ServerConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    rate_limiting: RateLimitConfig = field(default_factory=RateLimitConfig)

    collectors: Dict[str, CollectorConfig] = field(default_factory=dict)

    # Data directory override
    data_dir: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        """Create config from dictionary."""
        deployment = data.get("deployment", {})

        # Parse server config
        server_data = data.get("server", {})
        server = ServerConfig(
            host=server_data.get("host", "0.0.0.0"),
            port=server_data.get("port", 8080),
            url_prefix=server_data.get("url_prefix", ""),
        )

        # Parse UI config
        ui_data = data.get("ui", {})
        ui = UIConfig(
            title=ui_data.get("title", "HPC Status Monitor"),
            eyebrow=ui_data.get("eyebrow", "HPC STATUS"),
            home_page=ui_data.get("home_page", "overview"),
            tabs=ui_data.get("tabs", UIConfig().tabs),
            default_theme=ui_data.get("default_theme", "dark"),
        )

        # Parse rate limiting config
        rl_data = data.get("rate_limiting", {})
        per_cluster = rl_data.get("per_cluster", {})
        rate_limiting = RateLimitConfig(
            max_concurrent_ssh=rl_data.get("max_concurrent_ssh", 3),
            ssh_timeout=rl_data.get("ssh_timeout", 30),
            retry_backoff=rl_data.get("retry_backoff", [5, 15, 60]),
            min_interval=per_cluster.get("min_interval", 60),
            max_commands_per_poll=per_cluster.get("max_commands_per_poll", 5),
            failure_threshold=rl_data.get("circuit_breaker", {}).get("failure_threshold", 3),
            pause_duration=rl_data.get("circuit_breaker", {}).get("pause_duration", 300),
        )

        # Parse collector configs
        collectors_data = data.get("collectors", {})
        collectors = {}
        for name, coll_data in collectors_data.items():
            if isinstance(coll_data, dict):
                collectors[name] = CollectorConfig(
                    enabled=coll_data.get("enabled", True),
                    refresh_interval=coll_data.get("refresh_interval", 120),
                    timeout=coll_data.get("timeout", 30),
                    extra={k: v for k, v in coll_data.items() if k not in ("enabled", "refresh_interval", "timeout")},
                )

        return cls(
            deployment_name=deployment.get("name", "HPC Status Monitor"),
            platform=deployment.get("platform", "generic"),
            server=server,
            ui=ui,
            rate_limiting=rate_limiting,
            collectors=collectors,
            data_dir=data.get("data_dir"),
        )

    @classmethod
    def from_yaml(cls, path: Path) -> "Config":
        """Load config from YAML file."""
        if not path.exists():
            return cls()
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.from_dict(data)

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "Config":
        """Load config from path or defaults.

        Checks in order:
        1. Provided path
        2. HPC_STATUS_CONFIG env var
        3. ./configs/config.yaml
        4. ./config.yaml
        5. ~/.hpc_status/config.yaml
        5. Default config
        """
        paths_to_try = []

        if config_path:
            paths_to_try.append(Path(config_path))

        if env_path := os.environ.get("HPC_STATUS_CONFIG"):
            paths_to_try.append(Path(env_path))

        paths_to_try.extend([
            Path("./configs/config.yaml"),
            Path("./config.yaml"),
            Path.home() / ".hpc_status" / "config.yaml",
        ])

        for path in paths_to_try:
            if path.exists():
                return cls.from_yaml(path)

        return cls()

    def get_collector_config(self, name: str) -> CollectorConfig:
        """Get config for a specific collector."""
        return self.collectors.get(name, CollectorConfig())

    def is_collector_enabled(self, name: str) -> bool:
        """Check if a collector is enabled."""
        return self.get_collector_config(name).enabled

    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary."""
        return {
            "deployment": {
                "name": self.deployment_name,
                "platform": self.platform,
            },
            "server": {
                "host": self.server.host,
                "port": self.server.port,
                "url_prefix": self.server.url_prefix,
            },
            "ui": {
                "title": self.ui.title,
                "eyebrow": self.ui.eyebrow,
                "home_page": self.ui.home_page,
                "tabs": self.ui.tabs,
                "default_theme": self.ui.default_theme,
            },
            "rate_limiting": {
                "max_concurrent_ssh": self.rate_limiting.max_concurrent_ssh,
                "ssh_timeout": self.rate_limiting.ssh_timeout,
            },
            "collectors": {
                name: {"enabled": coll.enabled, "refresh_interval": coll.refresh_interval}
                for name, coll in self.collectors.items()
            },
        }


def create_default_config(platform: str = "generic") -> Config:
    """Create a default configuration for a platform.

    Args:
        platform: 'generic', 'hpcmp', or 'noaa'
    """
    if platform == "hpcmp":
        return Config(
            deployment_name="HPCMP Status Monitor",
            platform="hpcmp",
            ui=UIConfig(
                home_page="fleet",
                tabs={
                    "fleet": True,
                    "queues": True,
                    "quota": True,
                    "storage": True,
                    "overview": False,
                },
            ),
            collectors={
                "pw_cluster": CollectorConfig(enabled=True, refresh_interval=120),
                "hpcmp": CollectorConfig(
                    enabled=True,
                    refresh_interval=180,
                    extra={"url": "https://centers.hpc.mil/systems/unclassified.html"},
                ),
            },
        )
    elif platform == "noaa":
        return Config(
            deployment_name="NOAA RDHPCS Status Monitor",
            platform="noaa",
            ui=UIConfig(
                home_page="overview",
                tabs={
                    "overview": True,
                    "queues": True,
                    "quota": True,
                    "storage": True,
                    "docs": True,
                    "fleet": False,
                },
            ),
            collectors={
                "pw_cluster": CollectorConfig(enabled=True, refresh_interval=120),
                "noaa_docs": CollectorConfig(
                    enabled=True,
                    refresh_interval=3600,
                    extra={"url": "https://docs.rdhpcs.noaa.gov/systems/index.html"},
                ),
            },
        )
    else:
        return Config(
            deployment_name="HPC Status Monitor",
            platform="generic",
            ui=UIConfig(
                home_page="overview",
                tabs={
                    "overview": True,
                    "queues": True,
                    "quota": True,
                    "storage": True,
                    "fleet": False,
                },
            ),
            collectors={
                "pw_cluster": CollectorConfig(enabled=True, refresh_interval=120),
            },
        )
