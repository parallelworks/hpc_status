# HPC Cross-Site Status Monitor - Refactor Implementation Plan

> **Goal**: Transform the HPCMP Status Dashboard into a generalized, modern HPC cross-site status monitor that provides clear, actionable insights on queue availability, storage capacity, and allocation data for researchers, scientists, engineers, and automated workflows—enabling intelligent load balancing of compute tasks across connected systems.

---

## Design Principles

### Target Audience
This tool is designed for **researchers, scientists, engineers, and computing practitioners**—not system administrators. The UI and terminology should:
- Use plain language over technical jargon
- Answer "Where can I run my job?" not "What's the node utilization?"
- Show actionable insights, not raw metrics
- Surface recommendations proactively

### Primary Use Cases
1. **Interactive Decision Support** - Help users quickly identify the best system/queue for their workload
2. **Automated Load Balancing** - Provide APIs for workflow engines to distribute jobs across systems
3. **Capacity Planning** - Track allocation burn-down and storage usage to avoid surprises
4. **Cross-Site Visibility** - Single view across all connected HPC and cloud resources

### Target Deployments
| Platform | Systems | Status Source | Notes |
|----------|---------|---------------|-------|
| **HPCMP (DoD)** | ~10 systems | centers.hpc.mil scraping + PW CLI | Current deployment |
| **NOAA RDHPCS** | 6-7 systems | PW CLI primary, docs.rdhpcs.noaa.gov supplementary | Planned deployment |
| **General/Cloud** | Variable | PW CLI only | Any PW-connected clusters |

The tool must work **without** an external status page—PW CLI data is the universal source.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Repository Restructure](#2-repository-restructure)
3. [Generalization Architecture](#3-generalization-architecture)
4. [Data Persistence & Startup Behavior](#4-data-persistence--startup-behavior)
5. [UI/UX Modernization](#5-uiux-modernization)
6. [Enhanced Data Collection](#6-enhanced-data-collection)
7. [Insights & Recommendations Engine](#7-insights--recommendations-engine)
8. [Testing Strategy](#8-testing-strategy)
9. [Documentation Overhaul](#9-documentation-overhaul)
10. [Implementation Phases](#10-implementation-phases)

---

## 1. Executive Summary

### Current State
- Tightly coupled to HPCMP (DoD HPC) systems
- Vanilla JavaScript frontend with basic charts
- No persistence across restarts
- No storage ($HOME/$WORKDIR) monitoring
- No automated recommendations or load balancing
- Minimal documentation and no tests

### Target State
- **Multi-platform**: Works with HPCMP, NOAA RDHPCS, or any PW CLI-connected systems
- **PW CLI as core**: Home page works without external scraping; status pages are optional supplements
- Modern, research-focused UI with GeistSans typography (designed for researchers, not sysadmins)
- Persistent data with instant startup from cached data (`~/.hpc_status/`)
- $HOME and $WORKDIR storage monitoring across systems
- Smart recommendations for load balancing and queue selection
- Load balancing API for automated workflow distribution
- Rate-limited data collection (won't flood HPC systems)
- Platform-specific configuration via `--config` flag
- Comprehensive docs and test coverage

---

## 2. Repository Restructure

### Current Structure (Flat)
```
/
├── dashboard_server.py
├── cluster_monitor.py
├── hpc_status_scraper.py
├── ...
└── public/
    ├── index.html
    ├── app.js
    └── styles.css
```

### Proposed Structure (Organized)
```
/
├── src/
│   ├── server/
│   │   ├── __init__.py
│   │   ├── main.py                 # Entry point (was dashboard_server.py)
│   │   ├── routes.py               # API route handlers
│   │   ├── workers.py              # Background workers (refresh, monitor)
│   │   └── config.py               # Configuration management
│   │
│   ├── collectors/
│   │   ├── __init__.py
│   │   ├── base.py                 # Abstract collector interface
│   │   ├── pw_cluster.py           # PW CLI cluster collector (core)
│   │   ├── hpcmp.py                # HPCMP scraper (optional, DoD only)
│   │   ├── noaa_docs.py            # NOAA docs scraper (optional, NOAA only)
│   │   └── cloud.py                # Cloud provider collector (future)
│   │
│   ├── data/
│   │   ├── __init__.py
│   │   ├── models.py               # Data models (System, Cluster, Queue)
│   │   ├── aggregator.py           # Data aggregation logic
│   │   ├── persistence.py          # Data persistence layer
│   │   └── cache.py                # In-memory caching
│   │
│   ├── insights/
│   │   ├── __init__.py
│   │   ├── recommendations.py      # Queue/system recommendations
│   │   ├── load_balancer.py        # Load balancing suggestions
│   │   └── analytics.py            # Usage analytics
│   │
│   └── utils/
│       ├── __init__.py
│       ├── cli.py                  # CLI argument parsing
│       └── logging.py              # Logging configuration
│
├── web/                            # Frontend (was public/)
│   ├── index.html                  # Main dashboard
│   ├── queues.html
│   ├── quota.html
│   ├── assets/
│   │   ├── css/
│   │   │   ├── main.css            # Core styles
│   │   │   ├── theme.css           # Theme variables
│   │   │   └── components.css      # Component styles
│   │   ├── js/
│   │   │   ├── app.js
│   │   │   ├── queues.js
│   │   │   ├── quota.js
│   │   │   ├── components/         # Reusable UI components
│   │   │   │   ├── charts.js
│   │   │   │   ├── tables.js
│   │   │   │   └── cards.js
│   │   │   └── utils/
│   │   │       ├── api.js          # API client
│   │   │       ├── theme.js
│   │   │       └── formatting.js
│   │   └── fonts/                  # GeistSans font files
│   └── static/                     # Static assets only (no generated data)
│
├── tests/
│   ├── unit/
│   │   ├── test_collectors.py
│   │   ├── test_aggregator.py
│   │   ├── test_recommendations.py
│   │   └── test_models.py
│   ├── integration/
│   │   ├── test_api.py
│   │   ├── test_data_flow.py
│   │   └── test_persistence.py
│   ├── e2e/
│   │   └── test_dashboard.py       # Playwright/Selenium tests
│   ├── fixtures/
│   │   ├── sample_status.json
│   │   ├── sample_cluster_usage.json
│   │   └── mock_pw_output.txt
│   └── conftest.py
│
├── docs/
│   ├── getting-started.md
│   ├── configuration.md
│   ├── api-reference.md
│   ├── architecture.md
│   ├── deployment.md
│   ├── extending.md                # Adding new collectors
│   └── images/
│
├── scripts/
│   ├── run.sh                      # Main launch script
│   ├── dev.sh                      # Development mode
│   └── test.sh                     # Test runner
│
├── README.md                       # Short overview + links
├── pyproject.toml                  # Modern Python config (uv compatible)
├── uv.lock                         # Lockfile for reproducible installs
├── workflow.yaml
└── .gitignore
```

### Migration Steps

1. Create new directory structure
2. Migrate to `pyproject.toml` with uv (see below)
3. Split `dashboard_server.py` into `server/main.py`, `server/routes.py`, `server/workers.py`
4. Extract scraper into `collectors/hpcmp.py` with base interface
5. Move `cluster_monitor.py` logic into `collectors/pw_cluster.py`
6. Create data models in `data/models.py`
7. Reorganize frontend into `web/` with component structure
8. Update import paths and entry points

### pyproject.toml Configuration

```toml
[project]
name = "hpc-status-monitor"
version = "2.0.0"
description = "Cross-site HPC status monitoring dashboard"
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
    "requests>=2.31",
    "beautifulsoup4>=4.12",
    "urllib3>=2.0",
    "certifi>=2024.2",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov>=4.0",
    "pytest-asyncio>=0.23",
    "playwright>=1.40",
    "httpx>=0.26",  # For testing HTTP
]

[project.scripts]
hpc-status = "src.server.main:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "pytest-cov>=4.0",
]
```

---

## 3. Generalization Architecture

### 3.1 Multi-Source Collector System

```python
# src/collectors/base.py
from abc import ABC, abstractmethod
from typing import List, Optional
from src.data.models import System, ClusterData

class BaseCollector(ABC):
    """Abstract base class for data collectors."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this collector."""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name for UI."""
        pass

    @abstractmethod
    async def collect(self) -> List[System]:
        """Fetch current system/cluster data."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this collector can run (dependencies met)."""
        pass
```

### 3.2 Collector Implementations

| Collector | Source | Data Provided | Required |
|-----------|--------|---------------|----------|
| `PWClusterCollector` | PW CLI (`pw clusters ls`, `pw ssh`) | Queue, quota, usage, storage | **Yes (core)** |
| `HPCMPCollector` | centers.hpc.mil scraping | Fleet status, DSRC info, system briefs | No (HPCMP only) |
| `NOAADocsCollector` | docs.rdhpcs.noaa.gov | System descriptions, documentation links | No (NOAA only) |
| `CloudCollector` | Cloud provider APIs (AWS, GCP, Azure) | Cloud cluster status | No (future) |

**Key insight**: PW CLI is the **universal data source**. External scrapers (HPCMP, NOAA docs) provide supplementary context but are not required for core functionality.

### 3.3 Configuration-Based Enabling

Configuration varies by deployment. Here are examples for different platforms:

**HPCMP Deployment** (`config.hpcmp.yaml`):
```yaml
deployment:
  name: "HPCMP Status Monitor"
  platform: hpcmp

collectors:
  pw_cluster:
    enabled: true
    refresh_interval: 120

  hpcmp:
    enabled: true
    url: "https://centers.hpc.mil/systems/unclassified.html"
    refresh_interval: 180

ui:
  home_page: "fleet"          # Show HPCMP fleet overview as home
  tabs:
    fleet: true               # HPCMP-specific fleet status
    queues: true
    quota: true
    storage: true
```

**NOAA RDHPCS Deployment** (`config.noaa.yaml`):
```yaml
deployment:
  name: "NOAA RDHPCS Status Monitor"
  platform: noaa

collectors:
  pw_cluster:
    enabled: true
    refresh_interval: 120

  noaa_docs:
    enabled: true
    url: "https://docs.rdhpcs.noaa.gov/systems/index.html"
    refresh_interval: 3600    # Docs change infrequently

ui:
  home_page: "overview"       # Generalized multi-site overview
  tabs:
    overview: true            # Multi-site dashboard (no external scraping required)
    queues: true
    quota: true
    storage: true
    docs: true                # Link to NOAA docs
```

**Generic PW Deployment** (`config.yaml`):
```yaml
deployment:
  name: "HPC Status Monitor"
  platform: generic

collectors:
  pw_cluster:
    enabled: true
    refresh_interval: 120

ui:
  home_page: "overview"       # Works with just PW CLI data
  tabs:
    overview: true
    queues: true
    quota: true
    storage: true
```

**Shared Rate Limiting Config** (all deployments):
```yaml
rate_limiting:
  max_concurrent_ssh: 3
  ssh_timeout: 30
  retry_backoff: [5, 15, 60]
  per_cluster:
    min_interval: 60
    max_commands_per_poll: 5
  circuit_breaker:
    failure_threshold: 3
    pause_duration: 300
```

### 3.4 Rate Limiting Implementation

To avoid flooding HPC systems with requests, the collector implements:

```python
# src/collectors/rate_limiter.py
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict

class RateLimiter:
    """Prevents overwhelming HPC systems with too many requests."""

    def __init__(self, config: dict):
        self.max_concurrent = config.get('max_concurrent_ssh', 3)
        self.min_interval = config.get('per_cluster', {}).get('min_interval', 60)
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._last_poll: dict[str, datetime] = defaultdict(lambda: datetime.min)
        self._failure_counts: dict[str, int] = defaultdict(int)

    async def acquire(self, cluster_id: str) -> bool:
        """Acquire permission to poll a cluster. Returns False if rate limited."""
        # Check circuit breaker
        if self._failure_counts[cluster_id] >= 3:
            return False

        # Check minimum interval
        elapsed = (datetime.utcnow() - self._last_poll[cluster_id]).total_seconds()
        if elapsed < self.min_interval:
            return False

        # Acquire semaphore (limits concurrent connections)
        await self._semaphore.acquire()
        self._last_poll[cluster_id] = datetime.utcnow()
        return True

    def release(self, cluster_id: str, success: bool):
        """Release the semaphore and update failure tracking."""
        self._semaphore.release()
        if success:
            self._failure_counts[cluster_id] = 0
        else:
            self._failure_counts[cluster_id] += 1
```

**Key Behaviors:**
- **Concurrent limit**: Max 3 simultaneous SSH connections (configurable)
- **Per-cluster throttle**: Minimum 60s between polls to same cluster
- **Circuit breaker**: Pauses collection after 3 consecutive failures
- **Graceful degradation**: Uses cached data when rate limited

### 3.5 Conditional Tab Rendering

```javascript
// web/assets/js/utils/config.js
export async function loadConfig() {
    const response = await fetch('/api/config');
    return response.json();
}

// Navigation renders based on config
function renderNavigation(config) {
    const tabs = [];

    if (config.ui.tabs.queues) {
        tabs.push({ id: 'queues', label: 'Queue Health', href: '/queues.html' });
    }
    if (config.ui.tabs.quota) {
        tabs.push({ id: 'quota', label: 'Allocations', href: '/quota.html' });
    }
    if (config.ui.tabs.fleet_status && config.collectors.hpcmp.enabled) {
        tabs.push({ id: 'fleet', label: 'HPCMP Fleet', href: '/index.html' });
    }
    // ...
}
```

### 3.6 Launch Modes

```bash
# HPCMP deployment (DoD)
python -m src.server.main --config config.hpcmp.yaml

# NOAA RDHPCS deployment
python -m src.server.main --config config.noaa.yaml

# Generic deployment (PW CLI only, no external scraping)
python -m src.server.main --config config.yaml

# Development mode with mock data
python -m src.server.main --mock-data
```

The `--config` flag selects the deployment configuration, which determines:
- Which collectors are enabled
- What the home page shows
- Which tabs are available
- Platform-specific branding/terminology

---

## 4. Data Persistence & Startup Behavior

### 4.1 Persistence Layer

All data persists in the user's home directory to survive across workflow executions and dashboard restarts:

```
~/.hpc_status/
├── status.db              # SQLite database (structured data, historical snapshots)
├── config.yaml            # User config overrides (optional)
│
├── cache/                 # JSON cache files (fast reads for dashboard)
│   ├── fleet_status.json      # HPCMP fleet status
│   ├── cluster_usage.json     # PW cluster usage/quota data
│   ├── queue_status.json      # Queue health across all systems
│   └── storage_status.json    # $HOME/$WORKDIR capacity
│
├── user_data/             # User-specific data from PW SSH
│   ├── groups.json            # User's groups per system
│   ├── jobs.json              # Running/pending jobs
│   └── quotas.json            # Disk quotas per system
│
├── markdown/              # Generated system briefings
│   ├── nautilus.md
│   ├── jean.md
│   └── ...
│
└── logs/
    └── dashboard.log
```

**Key benefit**: When dashboard restarts, it immediately loads from `~/.hpc_status/cache/` before any network requests. Users see data instantly, even if the first poll takes time.

```python
# src/data/persistence.py
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
import os

def get_data_dir() -> Path:
    """Get user-persistent data directory."""
    data_dir = Path(os.environ.get('HPC_STATUS_DATA_DIR', Path.home() / '.hpc_status'))
    # Create all subdirectories
    for subdir in ['cache', 'user_data', 'markdown', 'logs']:
        (data_dir / subdir).mkdir(parents=True, exist_ok=True)
    return data_dir

class DataStore:
    """Persistent storage for status data."""

    def __init__(self, data_dir: Path = None):
        self.data_dir = data_dir or get_data_dir()
        self.db_path = self.data_dir / 'status.db'
        self.cache_dir = self.data_dir / 'cache'
        self.user_data_dir = self.data_dir / 'user_data'
        self._init_db()

    # --- JSON Cache (fast reads) ---

    def save_cache(self, name: str, data: dict):
        """Save data to JSON cache file."""
        cache_file = self.cache_dir / f'{name}.json'
        cache_file.write_text(json.dumps(data, indent=2, default=str))

    def load_cache(self, name: str, max_age: timedelta = None) -> Optional[dict]:
        """Load data from JSON cache file."""
        cache_file = self.cache_dir / f'{name}.json'
        if not cache_file.exists():
            return None

        # Check age if max_age specified
        if max_age:
            mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
            if datetime.now() - mtime > max_age:
                return None

        return json.loads(cache_file.read_text())

    # --- SQLite (historical data, queries) ---

    def _init_db(self):
        """Create tables if not exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY,
                    collector TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    data JSON NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_collector_timestamp
                ON snapshots(collector, timestamp DESC)
            """)

    def save_snapshot(self, collector: str, data: dict):
        """Save a data snapshot."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO snapshots (collector, timestamp, data) VALUES (?, ?, ?)",
                (collector, datetime.utcnow().isoformat(), json.dumps(data))
            )

    def get_latest(self, collector: str, max_age: timedelta = None) -> Optional[dict]:
        """Get most recent snapshot, optionally filtered by age."""
        with sqlite3.connect(self.db_path) as conn:
            query = "SELECT data, timestamp FROM snapshots WHERE collector = ? ORDER BY timestamp DESC LIMIT 1"
            row = conn.execute(query, (collector,)).fetchone()

            if row:
                data, ts = row
                if max_age:
                    snapshot_time = datetime.fromisoformat(ts)
                    if datetime.utcnow() - snapshot_time > max_age:
                        return None
                return json.loads(data)
        return None
```

### 4.2 Startup Behavior

On startup, the dashboard immediately loads from `~/.hpc_status/cache/` before making any network requests:

```python
# src/server/main.py
class DashboardState:
    def __init__(self, store: DataStore):
        self.store = store
        self._fleet_status = None
        self._cluster_usage = None
        self._is_loading = False
        self._load_initial_data()

    def _load_initial_data(self):
        """Load all cached data immediately on startup."""
        # Load fleet status (HPCMP systems)
        self._fleet_status = self.store.load_cache('fleet_status', max_age=timedelta(hours=24))
        if self._fleet_status:
            self._fleet_status['meta']['from_cache'] = True
            self._fleet_status['meta']['cache_file'] = '~/.hpc_status/cache/fleet_status.json'

        # Load cluster usage (PW clusters)
        self._cluster_usage = self.store.load_cache('cluster_usage', max_age=timedelta(hours=24))

        # Load queue status
        self._queue_status = self.store.load_cache('queue_status', max_age=timedelta(hours=24))

        # Load storage status
        self._storage_status = self.store.load_cache('storage_status', max_age=timedelta(hours=24))

        # If nothing cached, mark as loading
        if not any([self._fleet_status, self._cluster_usage]):
            self._is_loading = True

    def get_status(self) -> dict:
        """Return current status, from cache or live."""
        if self._is_loading and not self._fleet_status:
            return {
                'meta': {
                    'status': 'loading',
                    'message': 'Collecting data from HPC systems...',
                    'first_poll_pending': True
                },
                'systems': [],
                'summary': None
            }
        return self._fleet_status

    def on_refresh_complete(self, data: dict, source: str):
        """Called when a collector finishes. Saves to cache."""
        self.store.save_cache(source, data)
        setattr(self, f'_{source}', data)
        self._is_loading = False
```

**Startup sequence:**
1. Dashboard process starts
2. Immediately reads `~/.hpc_status/cache/*.json` (milliseconds)
3. Serves cached data to UI
4. Background workers begin polling systems
5. As fresh data arrives, cache files are updated
6. UI refreshes with live data

### 4.3 Frontend Loading States

```javascript
// web/assets/js/app.js
async function loadData() {
    const data = await api.fetchStatus();

    if (data.meta.first_poll_pending) {
        showLoadingState({
            title: 'Initializing Dashboard',
            message: 'Collecting data from HPC systems. This may take a moment...',
            showSpinner: true
        });
        scheduleRetry(5000);  // Retry in 5 seconds
        return;
    }

    if (data.meta.from_cache) {
        showCacheBanner({
            message: `Showing cached data from ${formatAge(data.meta.cache_age)} ago`,
            refreshing: true
        });
    }

    renderDashboard(data);
}

function showLoadingState({ title, message, showSpinner }) {
    document.getElementById('dashboard-content').innerHTML = `
        <div class="loading-state">
            ${showSpinner ? '<div class="spinner"></div>' : ''}
            <h2>${title}</h2>
            <p>${message}</p>
        </div>
    `;
}
```

---

## 5. UI/UX Modernization

### 5.1 Typography Update (GeistSans)

```css
/* web/assets/css/theme.css */
@font-face {
    font-family: 'GeistSans';
    src: url('../fonts/GeistSans-Regular.woff2') format('woff2');
    font-weight: 400;
    font-style: normal;
    font-display: swap;
}

@font-face {
    font-family: 'GeistSans';
    src: url('../fonts/GeistSans-Medium.woff2') format('woff2');
    font-weight: 500;
    font-style: normal;
    font-display: swap;
}

@font-face {
    font-family: 'GeistSans';
    src: url('../fonts/GeistSans-SemiBold.woff2') format('woff2');
    font-weight: 600;
    font-style: normal;
    font-display: swap;
}

@font-face {
    font-family: 'GeistSans';
    src: url('../fonts/GeistSans-Bold.woff2') format('woff2');
    font-weight: 700;
    font-style: normal;
    font-display: swap;
}

/* Fallback system font stack */
@font-face {
    font-family: 'GeistSans Fallback';
    src: local('Arial');
    ascent-override: 90%;
    descent-override: 20%;
    line-gap-override: 0%;
    size-adjust: 105%;
}

:root {
    --font-sans: 'GeistSans', 'GeistSans Fallback', -apple-system,
                 BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    --font-mono: 'GeistMono', 'Fira Code', 'Monaco', monospace;
}

body {
    font-family: var(--font-sans);
    font-feature-settings: 'ss01' on, 'ss02' on;  /* Stylistic sets */
}
```

### 5.2 Dashboard Redesign Principles

**Design for researchers, not sysadmins.** Every element should answer user questions:
- "Where can I run my job right now?" → Queue availability cards
- "Do I have enough hours left?" → Allocation health with burn-down
- "Is there space for my output?" → Storage capacity indicators
- "Which system is fastest?" → Wait time estimates and recommendations

| Current Issue | Proposed Solution |
|---------------|-------------------|
| Dense tables hard to scan | Card-based layout with key metrics highlighted |
| Charts not actionable | Interactive charts with tooltips showing recommendations |
| Status colors unclear | Clear status indicators with legends |
| No visual hierarchy | Progressive disclosure - summary → detail on demand |
| Technical terminology | Plain language (e.g., "cores free" not "idle node count") |
| Raw numbers without context | Comparative indicators ("faster than average", "low wait") |
| No proactive guidance | Surface recommendations without requiring user action |

### 5.3 Generalized Home Page (Multi-Site Overview)

The home page must work **without** external status page scraping—using only PW CLI data. This is the default for NOAA and generic deployments.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  HPC Status Monitor                                    [NOAA RDHPCS]  🌙    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Connected Systems: 6    │  Ready: 5  │  Busy: 1  │  Offline: 0    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐               │
│  │ 🟢 Hera         │ │ 🟢 Orion        │ │ 🟡 Jet          │               │
│  │ Ready           │ │ Ready           │ │ High Load       │               │
│  │                 │ │                 │ │                 │               │
│  │ Queues: 4 avail │ │ Queues: 3 avail │ │ Queues: 1 avail │               │
│  │ Wait: ~5 min    │ │ Wait: ~2 min    │ │ Wait: ~45 min   │               │
│  │ Alloc: 85% left │ │ Alloc: 62% left │ │ Alloc: 91% left │               │
│  │ $WORK: 1.2 TB   │ │ $WORK: 800 GB   │ │ $WORK: 2.1 TB   │               │
│  │                 │ │                 │ │                 │               │
│  │ [Details]       │ │ [Details]       │ │ [Details]       │               │
│  └─────────────────┘ └─────────────────┘ └─────────────────┘               │
│                                                                             │
│  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐               │
│  │ 🟢 Gaea         │ │ 🟢 PPAN         │ │ 🟢 Niagara      │               │
│  │ Ready           │ │ Ready           │ │ Ready           │               │
│  │ ...             │ │ ...             │ │ ...             │               │
│  └─────────────────┘ └─────────────────┘ └─────────────────┘               │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  💡 Recommendation: Orion has the shortest wait time right now.     │   │
│  │     For a 32-core, 4-hour job → use 'batch' queue on Orion.        │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  [Overview]  [Queues]  [Allocations]  [Storage]                  [Docs ↗]  │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Key differences from HPCMP fleet view:**
- No dependency on external status page scraping
- System status derived from PW CLI data (connection state, queue health)
- Works immediately with just `pw clusters ls` and `pw ssh` data
- Status indicators: 🟢 Ready, 🟡 High Load, 🔴 Offline, ⚪ Unknown

**System card data sources (all from PW CLI):**
| Field | Source |
|-------|--------|
| Status | `pw clusters ls` (on/off) + queue health |
| Queues available | `pw ssh show_queues` |
| Wait estimate | Derived from pending jobs / throughput |
| Allocation | `pw ssh show_usage` |
| $WORK space | `pw ssh df -h $WORKDIR` |

### 5.4 Platform-Specific Components

#### HPCMP: Fleet Status Tab
Shows DSRC breakdown, scheduler types, login nodes—scraped from centers.hpc.mil.
Only shown when `collectors.hpcmp.enabled: true`.

#### NOAA: Documentation Links Tab
Links to system-specific docs from docs.rdhpcs.noaa.gov.
Only shown when `collectors.noaa_docs.enabled: true`.

### 5.5 New Dashboard Components

#### Queue Availability Card
```
┌─────────────────────────────────────────────────┐
│ 🟢 System: Nautilus (NAVY)                      │
│ ─────────────────────────────────────────────── │
│ Available Now                                   │
│ ┌─────────────────────────────────────────────┐ │
│ │ standard    [████████░░]  23 cores free     │ │
│ │ debug       [██████████]  Available         │ │
│ │ gpu         [██░░░░░░░░]  2 GPUs waiting    │ │
│ └─────────────────────────────────────────────┘ │
│                                                 │
│ 💡 Recommendation: Best for small jobs (<4hr)   │
└─────────────────────────────────────────────────┘
```

#### Allocation Health Donut
```
┌─────────────────────────────────────────────────┐
│        Allocation Usage                         │
│                                                 │
│            ┌─────────┐                          │
│           ╱           ╲                         │
│          │   72%      │   250K hrs allocated    │
│          │   used     │   180K hrs used         │
│           ╲           ╱   70K hrs remaining     │
│            └─────────┘                          │
│                                                 │
│  ⚠️ At current rate, allocation depletes in    │
│     ~45 days. Consider requesting more hours.   │
└─────────────────────────────────────────────────┘
```

#### Storage Capacity Card
```
┌─────────────────────────────────────────────────┐
│  Your Storage                                   │
│  ─────────────────────────────────────────────  │
│                                                 │
│  Nautilus                                       │
│    Home      [████████░░]  80 GB / 100 GB      │
│    Work      [██░░░░░░░░]  150 GB / 1 TB  ✓    │
│                                                 │
│  Jean                                           │
│    Home      [█████████░]  95 GB / 100 GB  ⚠️  │
│    Work      [███░░░░░░░]  300 GB / 2 TB  ✓    │
│                                                 │
│  💡 Jean home is nearly full. Clean up old     │
│     files or move data to $WORKDIR.            │
└─────────────────────────────────────────────────┘
```

#### Quick Action Panel (for researchers)
```
┌─────────────────────────────────────────────────┐
│  Where should I run my job?                     │
│  ─────────────────────────────────────────────  │
│                                                 │
│  Cores needed:  [32    ▾]                       │
│  Runtime:       [4 hours▾]                      │
│  Need GPUs?     [ ] Yes                         │
│                                                 │
│  ┌─────────────────────────────────────────┐   │
│  │  Best option: Nautilus (standard queue) │   │
│  │  Est. wait: ~5 minutes                  │   │
│  │  Your allocation: 70K hours remaining   │   │
│  └─────────────────────────────────────────┘   │
│                                                 │
│  [Copy SSH command]  [View all options]         │
└─────────────────────────────────────────────────┘
```

### 5.6 Responsive Design

```css
/* Mobile-first responsive breakpoints */
:root {
    --breakpoint-sm: 640px;
    --breakpoint-md: 768px;
    --breakpoint-lg: 1024px;
    --breakpoint-xl: 1280px;
}

.dashboard-grid {
    display: grid;
    gap: var(--space-4);
    grid-template-columns: 1fr;
}

@media (min-width: 768px) {
    .dashboard-grid {
        grid-template-columns: repeat(2, 1fr);
    }
}

@media (min-width: 1024px) {
    .dashboard-grid {
        grid-template-columns: repeat(3, 1fr);
    }
}
```

---

## 6. Enhanced Data Collection

### 6.1 Additional PW SSH Data Points

| Data Point | PW Command | User Value |
|------------|------------|------------|
| User Groups | `pw ssh <uri> groups` | Shows accessible queues/projects |
| $HOME Storage | `pw ssh <uri> df -h $HOME` | Home directory capacity |
| $WORKDIR Storage | `pw ssh <uri> df -h $WORKDIR` | Work directory capacity |
| Disk Quota | `pw ssh <uri> quota -s` | User quota usage vs limit |
| Running Jobs | `pw ssh <uri> squeue -u $USER` | Personal job status |
| System Load | `pw ssh <uri> uptime` | Real-time load average |
| Scratch Space | `pw ssh <uri> df -h /scratch` | Temp storage availability |

### 6.2 Storage Monitoring

Storage availability is critical for job success. The dashboard tracks:

```
┌─────────────────────────────────────────────────────────────────┐
│  Storage Overview                                               │
│  ─────────────────────────────────────────────────────────────  │
│                                                                 │
│  $HOME (nautilus)     [████████░░]  82% used   18 GB free      │
│  $WORKDIR (nautilus)  [██░░░░░░░░]  15% used   850 GB free     │
│  /scratch (nautilus)  [█████░░░░░]  48% used   2.1 TB free     │
│                                                                 │
│  ⚠️ Home directory on Jean is 95% full - clean up recommended  │
└─────────────────────────────────────────────────────────────────┘
```

**Storage Data Model:**
```python
@dataclass
class StorageInfo:
    """Storage capacity for a filesystem."""
    mount_point: str          # $HOME, $WORKDIR, /scratch
    filesystem: str           # Physical device/path
    total_gb: float
    used_gb: float
    available_gb: float
    percent_used: float

    @property
    def status(self) -> str:
        if self.percent_used >= 95:
            return 'critical'
        elif self.percent_used >= 80:
            return 'warning'
        return 'healthy'
```

### 6.3 Extended Data Model

```python
# src/data/models.py
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime

@dataclass
class UserContext:
    """User-specific information on a system."""
    username: str
    groups: List[str] = field(default_factory=list)
    storage: List['StorageInfo'] = field(default_factory=list)  # $HOME, $WORKDIR, etc.
    active_jobs: int = 0
    pending_jobs: int = 0

    def home_storage(self) -> Optional['StorageInfo']:
        return next((s for s in self.storage if s.mount_point == '$HOME'), None)

    def workdir_storage(self) -> Optional['StorageInfo']:
        return next((s for s in self.storage if s.mount_point == '$WORKDIR'), None)

@dataclass
class QueueHealth:
    """Queue status with actionable metrics."""
    name: str
    status: str  # 'available', 'busy', 'draining', 'offline'
    max_walltime_hours: float
    running_jobs: int
    pending_jobs: int
    available_cores: int
    total_cores: int
    estimated_wait_minutes: Optional[int] = None
    recommended_for: List[str] = field(default_factory=list)  # ['small_jobs', 'gpu', 'long_running']

@dataclass
class SystemInsight:
    """AI-generated insight about a system."""
    type: str  # 'recommendation', 'warning', 'info'
    message: str
    priority: int  # 1-5, higher = more important
    related_metric: Optional[str] = None
```

### 6.4 New API Endpoints

```
GET /api/v2/systems
    - All systems with extended metadata

GET /api/v2/systems/{slug}/user-context
    - User-specific data (groups, quotas, jobs)

GET /api/v2/queues/recommendations
    - Smart queue recommendations based on job requirements

GET /api/v2/load-balance
    - Suggested system distribution for workload

GET /api/v2/insights
    - All current insights and recommendations
```

---

## 7. Insights & Recommendations Engine

### 7.1 Recommendation Types

| Recommendation | Trigger | Example Output |
|----------------|---------|----------------|
| Best Queue | User specifies job requirements | "For a 2-hour, 32-core job, use `standard` queue on Nautilus (est. wait: 5 min)" |
| Load Balance | Multiple systems available | "Distribute across ERDC and NAVY for fastest completion" |
| Allocation Warning | Usage > 80% | "Your ABC12345 allocation is 85% used. Consider requesting additional hours." |
| Storage Warning | $HOME or $WORKDIR > 90% | "Your home directory on Jean is 95% full. Clean up or move files to $WORKDIR." |
| Off-Peak Suggestion | High queue depth | "Queue depth is high. Jobs submitted after 6pm typically start 40% faster." |
| System Alert | Degraded/maintenance | "Barfoot entering maintenance in 2 hours. Migrate pending jobs to Nautilus." |
| Storage-Aware Routing | Job needs space | "Nautilus excluded: insufficient $WORKDIR space. Recommending Jean instead." |

### 7.2 Recommendation Engine

```python
# src/insights/recommendations.py
from typing import List, Optional
from dataclasses import dataclass
from src.data.models import QueueHealth, SystemInsight

@dataclass
class JobRequirements:
    cores: int
    walltime_hours: float
    memory_gb: Optional[float] = None
    gpus: int = 0
    priority: str = 'normal'  # 'low', 'normal', 'high'

class RecommendationEngine:
    def __init__(self, systems_data: dict, user_context: dict):
        self.systems = systems_data
        self.user = user_context

    def recommend_queue(self, requirements: JobRequirements) -> List[dict]:
        """Return ranked list of queue recommendations."""
        candidates = []

        for system in self.systems:
            for queue in system['queues']:
                score = self._score_queue(queue, requirements)
                if score > 0:
                    candidates.append({
                        'system': system['name'],
                        'queue': queue['name'],
                        'score': score,
                        'estimated_wait': self._estimate_wait(queue, requirements),
                        'reason': self._explain_recommendation(queue, requirements)
                    })

        return sorted(candidates, key=lambda x: x['score'], reverse=True)[:5]

    def suggest_load_balance(self, total_jobs: int, requirements: JobRequirements) -> dict:
        """Suggest distribution across systems for parallel submission."""
        available_systems = [s for s in self.systems if s['status'] == 'UP']

        # Calculate capacity and current load for each system
        distribution = {}
        total_capacity = sum(self._available_capacity(s) for s in available_systems)

        for system in available_systems:
            capacity_ratio = self._available_capacity(system) / total_capacity
            distribution[system['name']] = {
                'suggested_jobs': int(total_jobs * capacity_ratio),
                'queue': self._best_queue(system, requirements),
                'reason': f"{int(capacity_ratio * 100)}% of available capacity"
            }

        return distribution

    def generate_insights(self) -> List[SystemInsight]:
        """Generate all current insights."""
        insights = []

        # Check allocation health
        insights.extend(self._allocation_insights())

        # Check queue health
        insights.extend(self._queue_insights())

        # Check system status
        insights.extend(self._system_insights())

        return sorted(insights, key=lambda x: x.priority, reverse=True)
```

### 7.3 Load Balancing API for Automated Workflows

A primary use case is enabling automated workflows to intelligently distribute compute tasks across available systems. The load balancing API provides this capability.

**Use Case Example:**
A researcher has 500 simulation jobs to run. Rather than manually checking each system, their workflow script calls the load balance API to automatically distribute jobs across available systems based on current queue depth, allocation remaining, and storage availability.

```python
# Example: Load balancing API for automated job submission
@app.route('/api/v2/load-balance', methods=['POST'])
def load_balance():
    """
    Request body:
    {
        "job_count": 100,
        "cores_per_job": 32,
        "walltime_hours": 4,
        "memory_gb_per_job": 64,           // optional
        "storage_required_gb": 100,         // optional - filters systems with enough space
        "prefer_systems": ["nautilus", "jean"],  // optional
        "avoid_systems": ["barfoot"]              // optional
    }

    Response:
    {
        "distribution": {
            "nautilus": {
                "jobs": 60,
                "queue": "standard",
                "reason": "Lowest queue depth, 85% allocation remaining"
            },
            "jean": {
                "jobs": 40,
                "queue": "compute",
                "reason": "Good availability, sufficient $WORKDIR space"
            }
        },
        "systems_excluded": {
            "barfoot": "User requested exclusion",
            "onyx": "Insufficient $WORKDIR space (need 100GB, have 45GB)"
        },
        "estimated_completion": "2026-01-22T18:30:00Z",
        "confidence": 0.85
    }
    """
    requirements = JobRequirements(**request.json)
    engine = RecommendationEngine(get_systems_data(), get_user_context())
    return jsonify(engine.suggest_load_balance(
        request.json['job_count'],
        requirements
    ))
```

**Integration with PW Workflows:**
```python
# Example workflow script using load balance API
import requests

def submit_jobs(job_count, cores, walltime):
    # Get optimal distribution from status monitor
    resp = requests.post('http://localhost:8080/api/v2/load-balance', json={
        'job_count': job_count,
        'cores_per_job': cores,
        'walltime_hours': walltime,
        'storage_required_gb': 50
    })
    distribution = resp.json()['distribution']

    # Submit jobs according to recommended distribution
    for system, config in distribution.items():
        for i in range(config['jobs']):
            pw_submit(system, config['queue'], job_script)
```

---

## 8. Testing Strategy

### 8.1 Test Structure

```
tests/
├── unit/                       # Fast, isolated tests
│   ├── test_collectors.py      # Collector parsing logic
│   ├── test_models.py          # Data model validation
│   ├── test_aggregator.py      # Data aggregation
│   ├── test_recommendations.py # Recommendation engine
│   └── test_persistence.py     # Storage layer
│
├── integration/                # Tests with real dependencies
│   ├── test_api.py             # API endpoint tests
│   ├── test_data_flow.py       # End-to-end data pipeline
│   └── test_workers.py         # Background worker tests
│
├── e2e/                        # Browser-based tests
│   ├── test_dashboard.py       # Main dashboard interactions
│   └── test_navigation.py      # Tab navigation, filtering
│
├── fixtures/                   # Test data
│   ├── sample_hpcmp_html.html  # Scraped HTML sample
│   ├── sample_pw_output.txt    # PW CLI output samples
│   └── sample_status.json      # Expected output
│
└── conftest.py                 # Pytest configuration
```

### 8.2 Unit Test Examples

```python
# tests/unit/test_collectors.py
import pytest
from src.collectors.hpcmp import HPCMPCollector
from src.collectors.pw_cluster import PWClusterCollector

class TestHPCMPCollector:
    def test_parse_status_image(self, sample_html):
        """Test parsing status from HTML image elements."""
        collector = HPCMPCollector()
        systems = collector._parse_html(sample_html)

        assert len(systems) == 10
        assert systems[0].status in ['UP', 'DOWN', 'DEGRADED', 'MAINTENANCE']

    def test_infer_dsrc(self, sample_html):
        """Test DSRC inference from context."""
        collector = HPCMPCollector()
        systems = collector._parse_html(sample_html)

        assert all(s.dsrc in ['AFRL', 'NAVY', 'ERDC', 'ARL'] for s in systems)

    def test_handles_malformed_html(self):
        """Test graceful handling of malformed input."""
        collector = HPCMPCollector()
        result = collector._parse_html("<html><body>Invalid</body></html>")

        assert result == []


class TestPWClusterCollector:
    def test_parse_cluster_list(self, mock_pw_output):
        """Test parsing pw clusters ls output."""
        collector = PWClusterCollector()
        clusters = collector._parse_cluster_list(mock_pw_output['clusters_ls'])

        assert len(clusters) == 3
        assert all(c.status == 'on' for c in clusters)

    def test_parse_usage_output(self, mock_pw_output):
        """Test parsing show_usage command output."""
        collector = PWClusterCollector()
        usage = collector._parse_usage(mock_pw_output['show_usage'])

        assert usage.hours_allocated == 250000
        assert usage.hours_used == 180000
        assert usage.percent_remaining == 28.0
```

### 8.3 Integration Test Examples

```python
# tests/integration/test_api.py
import pytest
from src.server.main import create_app

@pytest.fixture
def client():
    app = create_app(testing=True)
    return app.test_client()

class TestStatusAPI:
    def test_get_status(self, client):
        """Test /api/status returns valid payload."""
        response = client.get('/api/status')

        assert response.status_code == 200
        data = response.json
        assert 'systems' in data
        assert 'summary' in data
        assert 'meta' in data

    def test_get_recommendations(self, client):
        """Test /api/v2/queues/recommendations."""
        response = client.post('/api/v2/queues/recommendations', json={
            'cores': 32,
            'walltime_hours': 4
        })

        assert response.status_code == 200
        data = response.json
        assert len(data['recommendations']) > 0
        assert 'estimated_wait' in data['recommendations'][0]
```

### 8.4 Test Fixtures

```python
# tests/conftest.py
import pytest
from pathlib import Path

@pytest.fixture
def sample_html():
    """Load sample HPCMP HTML for parsing tests."""
    return (Path(__file__).parent / 'fixtures/sample_hpcmp_html.html').read_text()

@pytest.fixture
def mock_pw_output():
    """Load sample PW CLI outputs."""
    fixtures = Path(__file__).parent / 'fixtures'
    return {
        'clusters_ls': (fixtures / 'pw_clusters_ls.txt').read_text(),
        'show_usage': (fixtures / 'pw_show_usage.txt').read_text(),
        'show_queues': (fixtures / 'pw_show_queues.txt').read_text()
    }

@pytest.fixture
def mock_pw_cli(mocker):
    """Mock PW CLI subprocess calls."""
    def mock_run(cmd, *args, **kwargs):
        if 'clusters ls' in ' '.join(cmd):
            return MockResult(stdout=mock_pw_output()['clusters_ls'])
        elif 'show_usage' in ' '.join(cmd):
            return MockResult(stdout=mock_pw_output()['show_usage'])
        # ... etc

    return mocker.patch('subprocess.run', side_effect=mock_run)
```

### 8.5 E2E Tests with Playwright

```python
# tests/e2e/test_dashboard.py
import pytest
from playwright.sync_api import Page, expect

class TestDashboard:
    def test_loads_with_data(self, page: Page, live_server):
        """Test dashboard loads and displays data."""
        page.goto(live_server.url)

        # Wait for data to load
        expect(page.locator('.system-card')).to_have_count_greater_than(0)

        # Check summary cards rendered
        expect(page.locator('.summary-card')).to_be_visible()

    def test_theme_toggle(self, page: Page, live_server):
        """Test theme switching persists."""
        page.goto(live_server.url)

        # Click theme toggle
        page.click('[data-testid="theme-toggle"]')

        # Verify theme changed
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')

        # Reload and verify persistence
        page.reload()
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')

    def test_filter_systems(self, page: Page, live_server):
        """Test filtering systems by status."""
        page.goto(live_server.url)

        # Select 'UP' filter
        page.select_option('[data-testid="status-filter"]', 'UP')

        # Verify only UP systems shown
        cards = page.locator('.system-card')
        for card in cards.all():
            expect(card.locator('.status-badge')).to_contain_text('UP')
```

---

## 9. Documentation Overhaul

### 9.1 New README.md (Concise)

```markdown
# HPC Cross-Site Status Monitor

Real-time monitoring dashboard for HPC systems, queues, and allocations.

![Dashboard Screenshot](docs/images/dashboard-screenshot.png)

## Quick Start

```bash
# Option 1: Using the run script (handles venv + deps automatically)
./scripts/run.sh

# Option 2: Manual setup with uv
uv venv ~/.venvs/hpc-status
source ~/.venvs/hpc-status/bin/activate
uv pip install -e .
python -m src.server.main

# Open http://localhost:8080
```

## Features

- **Multi-System Monitoring** - Track status across HPCMP, cloud, and on-prem clusters
- **Queue Insights** - Real-time queue availability with wait time estimates
- **Allocation Tracking** - Visual allocation usage with depletion warnings
- **Smart Recommendations** - AI-powered suggestions for queue selection and load balancing
- **REST API** - Programmatic access for automated workflows

## Documentation

- [Getting Started](docs/getting-started.md)
- [Configuration](docs/configuration.md)
- [API Reference](docs/api-reference.md)
- [Architecture](docs/architecture.md)
- [Deployment Guide](docs/deployment.md)
- [Extending (Custom Collectors)](docs/extending.md)

## API Example

```bash
# Get queue recommendations
curl -X POST http://localhost:8080/api/v2/queues/recommendations \
  -H "Content-Type: application/json" \
  -d '{"cores": 32, "walltime_hours": 4}'
```

## License

[LICENSE](LICENSE)
```

### 9.2 Documentation Structure

| Document | Purpose | Audience |
|----------|---------|----------|
| `getting-started.md` | Installation (uv setup), first run, basic usage | New users |
| `configuration.md` | All config options, environment vars, shared venv setup | Administrators |
| `api-reference.md` | Complete API documentation | Developers |
| `architecture.md` | System design, data flow diagrams | Contributors |
| `deployment.md` | Production deployment, PW workflow setup, shared venv strategy | DevOps |
| `extending.md` | Adding custom collectors, plugins | Developers |

### 9.3 Getting Started Documentation (uv focus)

```markdown
# Getting Started

## Prerequisites

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

## Installing uv

uv is a fast Python package manager. Install it once:

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or with pip
pip install uv
```

## Installation

### Quick Start (Recommended)

The run script handles everything automatically:

```bash
./scripts/run.sh
```

This will:
1. Install uv if not present
2. Create/reuse shared venv at `~/.venvs/hpc-status`
3. Sync dependencies (fast - only installs changes)
4. Start the dashboard server

### Manual Installation

```bash
# Create shared venv (reused across workflow runs)
uv venv ~/.venvs/hpc-status

# Activate
source ~/.venvs/hpc-status/bin/activate

# Install dependencies
uv pip install -e .

# Or install from lockfile for exact versions
uv pip sync uv.lock

# Run
python -m src.server.main
```

## Shared Virtual Environment

This project uses a **shared venv** strategy for PW workflows:

- Location: `~/.venvs/hpc-status/`
- Benefit: Dependencies persist across workflow executions
- Result: Near-instant startup after first run

To reset the environment:
```bash
rm -rf ~/.venvs/hpc-status
./scripts/run.sh  # Recreates fresh
```
```

### 9.4 API Reference Example

```markdown
# API Reference

## Authentication

Currently, no authentication is required. All endpoints are publicly accessible.

---

## Endpoints

### GET /api/status

Returns the current fleet status.

**Response**
```json
{
  "meta": {
    "generated_at": "2026-01-22T15:30:00Z",
    "source": "hpcmp",
    "from_cache": false
  },
  "summary": {
    "total_systems": 10,
    "status_counts": {"UP": 9, "DEGRADED": 1},
    "uptime_ratio": 0.9
  },
  "systems": [...]
}
```

### POST /api/v2/queues/recommendations

Get smart queue recommendations for a job.

**Request Body**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| cores | int | Yes | Number of cores needed |
| walltime_hours | float | Yes | Maximum job duration |
| memory_gb | float | No | Memory requirement per node |
| gpus | int | No | Number of GPUs needed |

**Response**
```json
{
  "recommendations": [
    {
      "system": "nautilus",
      "queue": "standard",
      "score": 0.95,
      "estimated_wait_minutes": 5,
      "reason": "Low queue depth, sufficient cores available"
    }
  ]
}
```
```

---

## 10. Implementation Phases

### Phase 1: Foundation (Week 1-2)
**Goal**: Restructure repo, migrate to uv, and establish testing foundation

- [ ] Create new directory structure
- [ ] Migrate from `requirements.txt` to `pyproject.toml`
- [ ] Generate `uv.lock` for reproducible installs
- [ ] Update `scripts/run.sh` for shared venv with uv
- [ ] Split monolithic files into modules
- [ ] Set up pytest configuration
- [ ] Write initial unit tests for existing functionality
- [ ] Update imports and entry points
- [ ] Verify existing functionality works

### Phase 2: Data Persistence (Week 2-3)
**Goal**: Implement persistent storage and startup improvements

- [ ] Implement SQLite-based DataStore
- [ ] Add snapshot saving on each data refresh
- [ ] Load cached data on startup
- [ ] Implement frontend loading states
- [ ] Add cache age indicators in UI
- [ ] Write tests for persistence layer

### Phase 3: Generalization (Week 3-4)
**Goal**: Abstract collectors and add configuration system

- [ ] Create BaseCollector interface
- [ ] Refactor HPCMP scraper as collector plugin
- [ ] Refactor PW cluster monitor as collector plugin
- [ ] Implement YAML-based configuration
- [ ] Add conditional tab rendering
- [ ] Add launch mode flags (full, pw-only, minimal)
- [ ] Write tests for collector abstraction

### Phase 4: UI Modernization (Week 4-5)
**Goal**: Update typography, improve dashboard clarity

- [ ] Integrate GeistSans fonts
- [ ] Redesign summary cards with clearer metrics
- [ ] Implement new queue availability visualization
- [ ] Add responsive grid layout
- [ ] Improve chart interactivity
- [ ] Simplify technical language
- [ ] Add tooltips for terminology
- [ ] Write E2E tests for UI

### Phase 5: Recommendations Engine (Week 5-6)
**Goal**: Implement smart insights and recommendations

- [ ] Create recommendation engine module
- [ ] Implement queue recommendation algorithm
- [ ] Implement load balancing suggestions
- [ ] Add allocation warnings
- [ ] Create `/api/v2/` endpoints
- [ ] Integrate recommendations into UI
- [ ] Write tests for recommendation logic

### Phase 6: Extended Data Collection (Week 6-7)
**Goal**: Add storage monitoring, user context, and enhanced metrics

- [ ] Implement $HOME storage monitoring (`pw ssh df -h $HOME`)
- [ ] Implement $WORKDIR storage monitoring (`pw ssh df -h $WORKDIR`)
- [ ] Add user groups collection (`pw ssh groups`)
- [ ] Add disk quota collection (`pw ssh quota -s`)
- [ ] Add running jobs summary
- [ ] Create storage capacity UI components
- [ ] Add storage warnings to insights engine
- [ ] Write tests for new collectors

### Phase 7: Documentation & Polish (Week 7-8)
**Goal**: Complete documentation and final improvements

- [ ] Write concise README
- [ ] Create docs/ directory with detailed guides
- [ ] Add architecture diagrams
- [ ] Complete API reference
- [ ] Add deployment guide (HPCMP, NOAA, generic)
- [ ] Final UI polish and accessibility review
- [ ] Comprehensive test coverage review

### Phase 8: NOAA Deployment (Week 8-9)
**Goal**: Deploy to NOAA RDHPCS platform

- [ ] Create `config.noaa.yaml` configuration
- [ ] Implement `NOAADocsCollector` (scrape docs.rdhpcs.noaa.gov for system info)
- [ ] Test with NOAA's 6-7 connected systems
- [ ] Verify PW CLI commands work on NOAA systems (`show_usage`, `show_queues`)
- [ ] Customize branding/terminology for NOAA users
- [ ] Add NOAA-specific documentation links
- [ ] Deploy and gather user feedback

---

## Appendix: Technology Decisions

### Is Python the Right Choice?

**Current choice: Python** — but worth evaluating alternatives.

#### Python Strengths (for this project)
| Strength | Relevance |
|----------|-----------|
| PW CLI integration | Subprocess calls, text parsing are natural in Python |
| HPC ecosystem familiarity | Target users (researchers) likely know Python |
| BeautifulSoup/requests | Mature web scraping libraries |
| Rapid prototyping | Quick iteration on data models and APIs |
| No build step | Simpler deployment in HPC environments |

#### Python Weaknesses
| Weakness | Impact |
|----------|--------|
| Performance | Not critical—this is I/O bound (SSH, HTTP), not CPU bound |
| Concurrency model | asyncio works but adds complexity; threading has GIL |
| Type safety | Runtime errors possible; mitigated with type hints + mypy |
| Binary distribution | Requires Python runtime; mitigated with shared venv strategy |

#### Alternative Considerations

**Go**
- Pros: Single binary, excellent concurrency, fast startup
- Cons: Less familiar to HPC researchers, more verbose, weaker HTML parsing
- Verdict: Good choice if we needed a standalone distributable binary

**Rust**
- Pros: Performance, safety, single binary
- Cons: Steeper learning curve, slower development, overkill for I/O-bound work
- Verdict: Over-engineered for this use case

**Node.js/TypeScript**
- Pros: Same language frontend/backend, good async model
- Cons: Another runtime to manage, npm complexity, less HPC ecosystem fit
- Verdict: Reasonable but no compelling advantage over Python

**Recommendation: Stay with Python**

The project is I/O-bound (waiting on SSH commands and HTTP requests), not CPU-bound. Python's weaknesses don't significantly impact this workload, and its strengths (ecosystem fit, researcher familiarity, rapid development) are valuable.

If we later need:
- **Standalone binary distribution** → Consider rewriting core in Go
- **High-frequency polling** → Consider Go or Rust for the collector daemon
- **Complex frontend interactivity** → Consider TypeScript with a proper framework

For now, Python with `uv` (fast installs) and shared venv (fast startup) addresses the practical deployment concerns.

---

### Why `uv` for Package Management?

[uv](https://github.com/astral-sh/uv) is a fast Python package installer and resolver written in Rust, offering 10-100x speedups over pip.

**Benefits:**
- Extremely fast dependency resolution and installation
- Built-in virtual environment management
- Compatible with `pyproject.toml` and standard Python packaging
- Lockfile support (`uv.lock`) for reproducible builds
- Drop-in replacement for pip/pip-tools workflows

### Shared Virtual Environment Strategy

For PW workflow deployments, we use **persistent user directories** rather than per-repo storage:

```
~/
├── .venvs/
│   └── hpc-status/          # Shared venv across all workflow executions
│       ├── bin/
│       ├── lib/
│       └── pyvenv.cfg
│
└── .hpc_status/             # ALL application data (survives restarts)
    ├── status.db            # SQLite (historical data, analytics)
    ├── config.yaml          # User config overrides
    ├── cache/               # JSON cache (fast dashboard reads)
    │   ├── fleet_status.json
    │   ├── cluster_usage.json
    │   ├── queue_status.json
    │   └── storage_status.json
    ├── user_data/           # User-specific PW SSH data
    │   ├── groups.json
    │   ├── jobs.json
    │   └── quotas.json
    ├── markdown/            # System briefings
    └── logs/
```

**No data in repo directory** — code is separate from data. This means:
- Multiple workflow runs share the same cache
- Dashboard restart = instant data (no waiting for first poll)
- Clean git status (no generated files)

**Rationale:**
- **Faster startup** - Dependencies already installed from previous runs
- **Reduced disk usage** - Single venv vs. duplicated per workflow execution
- **Simplified updates** - One location to manage/upgrade dependencies
- **Workflow efficiency** - No pip install delay on each workflow launch

**Implementation:**
```bash
# scripts/run.sh
VENV_DIR="${HOME}/.venvs/hpc-status"
DATA_DIR="${HOME}/.hpc_status"
UV_BIN="${HOME}/.local/bin/uv"

# Install uv if not present (one-time, fast)
if [ ! -f "$UV_BIN" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Create shared venv if not exists
if [ ! -d "$VENV_DIR" ]; then
    $UV_BIN venv "$VENV_DIR"
fi

# Create data directory if not exists
mkdir -p "$DATA_DIR/logs"

# Sync dependencies (fast - only installs missing/changed)
$UV_BIN pip sync --python "$VENV_DIR/bin/python" pyproject.toml

# Activate and run with persistent data directory
source "$VENV_DIR/bin/activate"
export HPC_STATUS_DATA_DIR="$DATA_DIR"
python -m src.server.main "$@"
```

**Version Pinning:**
The `uv.lock` file ensures reproducible installs across all users and executions:
```bash
# Generate/update lockfile (developer action)
uv lock

# Install from lockfile (workflow action - exact versions)
uv pip sync uv.lock
```

### Why SQLite for Persistence?
- Zero configuration, file-based
- Included in Python standard library
- Sufficient for single-server deployment
- Easy to backup (single file)
- Can migrate to PostgreSQL later if needed

### Why Keep Vanilla JavaScript?
- No build step required
- Fast initial load
- Simpler deployment
- Sufficient for dashboard complexity
- Could consider Lit or Preact for future component architecture

### Why YAML Configuration?
- Human-readable
- Supports comments
- Easy to version control
- Well-supported in Python (PyYAML)

### Workflow Integration (workflow.yaml)

The PW workflow leverages the shared venv for fast startup:

```yaml
# workflow.yaml
jobs:
  dashboard:
    steps:
      - name: Start Dashboard
        run: |
          # Shared venv ensures deps are cached across runs
          ./scripts/run.sh --port ${PORT:-8080}
        env:
          VENV_DIR: ~/.venvs/hpc-status
          UV_CACHE_DIR: ~/.cache/uv  # Shared uv cache
```

**First Run**: ~15-30s (uv install + dependency sync)
**Subsequent Runs**: <2s (venv exists, deps unchanged)

---

## Success Metrics

| Metric | Target |
|--------|--------|
| Test Coverage | >80% |
| Dependency Install (first run) | <30s with uv |
| Dependency Install (subsequent) | <2s (shared venv) |
| Server Startup (cached data) | <500ms |
| First Meaningful Paint | <2s |
| API Response Time (p95) | <200ms |
| Documentation Coverage | All endpoints documented |
| User Satisfaction | Qualitative feedback positive |

---

*Document Version: 1.5*
*Created: 2026-01-22*
*Last Updated: 2026-01-22*

---

## Changelog

### v1.5 (2026-01-22)
- Added NOAA RDHPCS as target deployment platform
- Generalized home page to work without external status page scraping
- PW CLI is now the core/universal data source; external scrapers are optional
- Added platform-specific configuration examples (HPCMP, NOAA, generic)
- Added `NOAADocsCollector` for docs.rdhpcs.noaa.gov
- Home page derived from PW CLI data only (queues, allocations, storage)
- Launch via `--config` flag for different deployments

### v1.4 (2026-01-22)
- Expanded `~/.hpc_status/` to include all data: cache/, user_data/, markdown/
- Added JSON cache layer for fast dashboard startup
- No data stored in repo directory (clean separation of code and data)
- Dashboard loads from cache immediately on restart before polling

### v1.3 (2026-01-22)
- Moved SQLite database to user-persistent directory (`~/.hpc_status/`)
- Added "Is Python the Right Choice?" technology assessment
- Updated run.sh to set `HPC_STATUS_DATA_DIR` environment variable
- Unified persistent storage strategy (venv + data in user home)

### v1.2 (2026-01-22)
- Added Design Principles section emphasizing researcher/scientist audience
- Added $HOME and $WORKDIR storage monitoring
- Added rate limiting configuration to avoid flooding HPC systems
- Enhanced load balancing API with storage requirements
- Added storage capacity UI mockups
- Added "Quick Action Panel" for researcher-friendly job placement
- Strengthened focus on automated load balancing use case

### v1.1 (2026-01-22)
- Added `uv` package manager integration
- Added shared virtual environment strategy (`~/.venvs/hpc-status`)
- Updated `pyproject.toml` configuration example
- Added workflow.yaml integration notes for fast startup
