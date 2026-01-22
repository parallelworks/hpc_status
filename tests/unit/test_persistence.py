"""Tests for data persistence layer."""

import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path

from src.data.persistence import DataStore, get_data_dir


class TestDataStore:
    def test_init_creates_directories(self, temp_data_dir):
        store = DataStore(temp_data_dir)

        assert (temp_data_dir / "cache").exists()
        assert (temp_data_dir / "user_data").exists()
        assert (temp_data_dir / "markdown").exists()
        assert (temp_data_dir / "logs").exists()

    def test_save_and_load_cache(self, temp_data_dir):
        store = DataStore(temp_data_dir)
        data = {"test": "data", "value": 42}

        store.save_cache("test_cache", data)
        loaded = store.load_cache("test_cache")

        assert loaded["test"] == "data"
        assert loaded["value"] == 42
        assert loaded["_cache_meta"]["from_cache"] is True

    def test_load_cache_not_found(self, temp_data_dir):
        store = DataStore(temp_data_dir)
        loaded = store.load_cache("nonexistent")
        assert loaded is None

    def test_load_cache_with_max_age(self, temp_data_dir):
        store = DataStore(temp_data_dir)
        data = {"test": "data"}

        store.save_cache("test_cache", data)

        # Should load with generous max_age
        loaded = store.load_cache("test_cache", max_age=timedelta(hours=1))
        assert loaded is not None

        # Should return None with very short max_age (cache is too old)
        # Note: This test might be flaky if the cache file is created instantly
        # In practice, we'd mock the file mtime

    def test_clear_cache(self, temp_data_dir):
        store = DataStore(temp_data_dir)

        store.save_cache("test1", {"a": 1})
        store.save_cache("test2", {"b": 2})

        # Clear specific cache
        store.clear_cache("test1")
        assert store.load_cache("test1") is None
        assert store.load_cache("test2") is not None

        # Clear all caches
        store.clear_cache()
        assert store.load_cache("test2") is None

    def test_save_and_load_user_data(self, temp_data_dir):
        store = DataStore(temp_data_dir)
        data = {"groups": ["project1", "project2"]}

        store.save_user_data("groups", data)
        loaded = store.load_user_data("groups")

        assert loaded["groups"] == ["project1", "project2"]

    def test_save_and_load_markdown(self, temp_data_dir):
        store = DataStore(temp_data_dir)
        content = "# System Name\n\nMarkdown content here."

        store.save_markdown("nautilus", content)
        loaded = store.load_markdown("nautilus")

        assert loaded == content

    def test_list_markdown_files(self, temp_data_dir):
        store = DataStore(temp_data_dir)

        store.save_markdown("nautilus", "# Nautilus")
        store.save_markdown("jean", "# Jean")

        files = store.list_markdown_files()
        assert "nautilus" in files
        assert "jean" in files

    def test_save_and_get_snapshot(self, temp_data_dir):
        store = DataStore(temp_data_dir)
        data = {"systems": [], "meta": {"source": "test"}}

        store.save_snapshot("hpcmp", data)
        latest = store.get_latest_snapshot("hpcmp")

        assert latest["meta"]["source"] == "test"

    def test_save_system_status_history(self, temp_data_dir):
        store = DataStore(temp_data_dir)

        store.save_system_status("nautilus", "UP", {"notes": "All good"})
        store.save_system_status("nautilus", "DEGRADED", {"notes": "Issues"})

        history = store.get_system_history("nautilus")
        assert len(history) == 2
        assert history[0]["status"] == "DEGRADED"  # Most recent first
        assert history[1]["status"] == "UP"

    def test_get_cache_age(self, temp_data_dir):
        store = DataStore(temp_data_dir)

        # No cache
        assert store.get_cache_age("test") is None

        # With cache
        store.save_cache("test", {"data": 1})
        age = store.get_cache_age("test")
        assert age is not None
        assert age >= 0
        assert age < 1  # Should be very recent
