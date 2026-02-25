"""Pytest configuration and shared fixtures."""

import json
import pytest
from pathlib import Path
from tempfile import TemporaryDirectory


@pytest.fixture
def temp_data_dir():
    """Create a temporary data directory for tests."""
    with TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_hpcmp_html():
    """Sample HPCMP status page HTML for testing."""
    return '''
    <html>
    <body>
        <div class="navy-dsrc">
            <h3>Navy DSRC</h3>
            <img class="statusImg" alt="Nautilus is currently Up." src="/images/up.png">
            <a href="/guides/nautilusSlurmGuide.html">Nautilus Slurm Guide</a>
        </div>
        <div class="afrl-dsrc">
            <h3>AFRL DSRC</h3>
            <img class="statusImg" alt="Raider is currently Degraded." src="/images/degraded.png">
            <a href="/guides/raiderPbsGuide.html">Raider PBS Guide</a>
        </div>
        <div class="erdc-dsrc">
            <h3>ERDC DSRC</h3>
            <img class="statusImg" alt="Onyx is currently Down." src="/images/down.png">
        </div>
    </body>
    </html>
    '''


@pytest.fixture
def sample_pw_clusters_output():
    """Sample output from pw clusters ls command (space-separated format)."""
    return '''
URI                    STATUS  TYPE
pw://user/nautilus     active  existing
pw://user/jean         active  existing
pw://user/onyx         off     existing
'''


@pytest.fixture
def sample_pw_clusters_output_pipe():
    """Sample output from pw clusters ls command (legacy pipe-delimited format)."""
    return '''
+-----------------------------------+--------+-----------+
| URI                               | STATUS | TYPE      |
+-----------------------------------+--------+-----------+
| pw://user/nautilus                | on     | existing  |
| pw://user/jean                    | on     | existing  |
| pw://user/onyx                    | off    | existing  |
+-----------------------------------+--------+-----------+
'''


@pytest.fixture
def sample_usage_output():
    """Sample output from show_usage command."""
    return '''
Usage Information for user

Fiscal Year 2026 - Hours Remaining: 250000

System       Subproject        Allocated     Used  Remaining     %Rem  Background
===================================================================================
nautilus     PROJECT123           250000        0     250000  100.00%          0
jean         PROJECT456           100000    50000      50000   50.00%          0
'''


@pytest.fixture
def sample_queue_output():
    """Sample output from show_queues command."""
    return '''
QUEUE INFORMATION:
Queue Name   Max Time    Max Jobs  Max Cores  Running  Pending  Cores Run  Cores Pend  Type
==============================================================================================
standard     24:00:00    -         -          4        0        384        0           Exe
debug        01:00:00    2         64         0        0        0          0           Exe
gpu          12:00:00    -         -          1        2        64         128         GPU

NODE INFORMATION:
Node Type     Nodes  Cores/Node  Total Cores  Running  Free
================================================================
Standard      494    96          47424        10080    37344
GPU           32     64          2048         64       1984
'''


@pytest.fixture
def sample_cluster_usage_json():
    """Sample cluster usage JSON data."""
    return [
        {
            "cluster_metadata": {
                "name": "nautilus",
                "uri": "pw://user/nautilus",
                "status": "active",
                "type": "existing",
                "timestamp": "2026-01-22T12:00:00Z"
            },
            "usage_data": {
                "header": "Usage Information",
                "fiscal_year_info": "FY2026",
                "systems": [
                    {
                        "system": "nautilus",
                        "subproject": "PROJECT123",
                        "hours_allocated": 250000,
                        "hours_used": 0,
                        "hours_remaining": 250000,
                        "percent_remaining": 100.0,
                        "background_hours_used": 0
                    }
                ]
            },
            "queue_data": {
                "queues": [
                    {
                        "queue_name": "standard",
                        "max_walltime": "24:00:00",
                        "jobs_running": "4",
                        "jobs_pending": "0",
                        "cores_running": "384",
                        "cores_pending": "0",
                        "queue_type": "Exe"
                    }
                ],
                "nodes": [
                    {
                        "node_type": "Standard",
                        "nodes_available": "494",
                        "cores_per_node": "96",
                        "cores_available": "47424",
                        "cores_running": "10080",
                        "cores_free": "37344"
                    }
                ]
            }
        }
    ]
