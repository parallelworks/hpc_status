"""Tests for configuration management."""

import pytest
import yaml
from pathlib import Path
from tempfile import NamedTemporaryFile

from src.server.config import (
    Config,
    CollectorConfig,
    UIConfig,
    ServerConfig,
    create_default_config,
)


class TestConfig:
    def test_default_config(self):
        config = Config()
        assert config.deployment_name == "HPC Status Monitor"
        assert config.platform == "generic"
        assert config.server.host == "0.0.0.0"
        assert config.server.port == 8080

    def test_from_dict(self):
        data = {
            "deployment": {
                "name": "Test Monitor",
                "platform": "hpcmp",
            },
            "server": {
                "host": "localhost",
                "port": 9000,
            },
            "ui": {
                "home_page": "fleet",
                "default_theme": "light",
            },
            "collectors": {
                "hpcmp": {
                    "enabled": True,
                    "refresh_interval": 180,
                },
            },
        }
        config = Config.from_dict(data)

        assert config.deployment_name == "Test Monitor"
        assert config.platform == "hpcmp"
        assert config.server.host == "localhost"
        assert config.server.port == 9000
        assert config.ui.home_page == "fleet"
        assert config.ui.default_theme == "light"
        assert config.is_collector_enabled("hpcmp") is True

    def test_from_yaml(self, tmp_path):
        yaml_content = """
deployment:
  name: "YAML Test"
  platform: noaa
server:
  port: 8888
ui:
  home_page: overview
collectors:
  pw_cluster:
    enabled: true
    refresh_interval: 60
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml_content)

        config = Config.from_yaml(config_file)

        assert config.deployment_name == "YAML Test"
        assert config.platform == "noaa"
        assert config.server.port == 8888
        assert config.ui.home_page == "overview"

    def test_get_collector_config(self):
        config = Config(
            collectors={
                "hpcmp": CollectorConfig(enabled=True, refresh_interval=180),
            }
        )

        hpcmp_config = config.get_collector_config("hpcmp")
        assert hpcmp_config.enabled is True
        assert hpcmp_config.refresh_interval == 180

        # Non-existent collector returns default
        other_config = config.get_collector_config("other")
        assert other_config.enabled is True  # Default

    def test_is_collector_enabled(self):
        config = Config(
            collectors={
                "hpcmp": CollectorConfig(enabled=True),
                "disabled": CollectorConfig(enabled=False),
            }
        )

        assert config.is_collector_enabled("hpcmp") is True
        assert config.is_collector_enabled("disabled") is False
        assert config.is_collector_enabled("unknown") is True  # Default

    def test_to_dict(self):
        config = Config(
            deployment_name="Test",
            platform="test",
        )
        data = config.to_dict()

        assert data["deployment"]["name"] == "Test"
        assert data["deployment"]["platform"] == "test"
        assert "server" in data
        assert "ui" in data


class TestCreateDefaultConfig:
    def test_generic_config(self):
        config = create_default_config("generic")
        assert config.platform == "generic"
        assert config.ui.home_page == "overview"
        assert config.is_collector_enabled("pw_cluster") is True

    def test_hpcmp_config(self):
        config = create_default_config("hpcmp")
        assert config.platform == "hpcmp"
        assert config.ui.home_page == "fleet"
        assert config.is_collector_enabled("hpcmp") is True
        assert config.is_collector_enabled("pw_cluster") is True

    def test_noaa_config(self):
        config = create_default_config("noaa")
        assert config.platform == "noaa"
        assert config.ui.home_page == "overview"
        assert config.ui.tabs.get("docs") is True
        assert config.ui.tabs.get("fleet") is False
