"""The API's description of itself.

One list, three consumers: ``GET /api/endpoints``, the in-app API page, and
the generated OpenAPI document. Documentation that lives somewhere else
rots — the committed spec had picked up two endpoints that never existed
and lost five that did — so the catalog sits next to the handlers, and a
test fails if it and the router disagree in either direction.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Every entry is (method, path). ``path`` uses {braces} for path segments
# the caller fills in; the router matches those by prefix. ``returns`` lists
# the top-level keys of the response, and ``shape: "array"`` marks the two
# endpoints that answer with a list of such objects rather than one.
ENDPOINTS: List[Dict[str, Any]] = [
    {
        "method": "GET",
        "path": "/api/status",
        "group": "Fleet",
        "summary": "Every system in the fleet, with its status",
        "description": (
            "The collector's raw payload: one row per system plus fleet-wide "
            "counts. This is what the Fleet status page renders."
        ),
        "returns": ["meta", "summary", "systems"],
        "notes": (
            "Answers 503 with an explanation while the first collection is "
            "still running."
        ),
    },
    {
        "method": "GET",
        "path": "/api/fleet/summary",
        "group": "Fleet",
        "summary": "Condensed fleet listing",
        "description": "The same systems, trimmed to the fields a table needs.",
        "returns": ["generated_at", "fleet_stats", "systems"],
    },
    {
        "method": "GET",
        "path": "/api/topology",
        "group": "Fleet",
        "summary": "The fleet as a graph: monitor, sites, systems",
        "description": (
            "Merges what a site reports about a machine with what the monitor "
            "observes by connecting to it. Always answers 200 — a fleet that "
            "has not been collected yet returns just the monitor node with "
            "meta.ready false."
        ),
        "returns": ["meta", "summary", "sites", "nodes", "edges"],
    },
    {
        "method": "GET",
        "path": "/api/history",
        "group": "Fleet",
        "summary": "Replayable frames of status and utilization",
        "description": (
            "Status transitions carried forward and queue depth bucketed onto "
            "evenly spaced instants — what the topology timeline plays back."
        ),
        "params": [
            {"name": "window", "type": "integer", "default": 24, "description": "Hours to replay"},
            {"name": "step", "type": "integer", "default": 15, "description": "Minutes between frames"},
        ],
        "returns": ["window_hours", "step_minutes", "from", "to", "systems", "frames"],
    },
    {
        "method": "GET",
        "path": "/api/events",
        "group": "Fleet",
        "summary": "Recent system state changes",
        "description": (
            "The transition log behind alerting, newest first. Populated "
            "whether or not a webhook is configured."
        ),
        "params": [
            {"name": "limit", "type": "integer", "default": 50, "description": "Maximum events (1-200)"},
        ],
        "returns": ["events", "alerting_enabled", "generated_at"],
    },
    {
        "method": "GET",
        "path": "/api/cluster-usage",
        "group": "Clusters",
        "summary": "Queues, nodes, allocations and storage per cluster",
        "description": (
            "Everything the monitor collects over its live sessions. Each "
            "queue also carries a wait_estimate once there is enough recorded "
            "history to derive one."
        ),
        "shape": "array",
        "returns": [
            "cluster_metadata",
            "usage_data",
            "queue_data",
            "gpu_data",
            "system_info",
            "storage_data",
        ],
        "notes": (
            "While the first sweep runs this returns an envelope with status "
            "'warming_up' or 'partial' and a progress object, rather than an "
            "error."
        ),
    },
    {
        "method": "GET",
        "path": "/api/cluster-usage/{cluster}",
        "group": "Clusters",
        "summary": "One cluster's profile",
        "description": "The same data for a single cluster, keyed by its slug.",
        "returns": ["cluster", "slug", "usage", "queues", "node_classes", "placement_hint"],
    },
    {
        "method": "GET",
        "path": "/api/storage",
        "group": "Clusters",
        "summary": "Filesystem capacity and usage",
        "description": "Storage for every connected cluster, sharing the warming-up envelope.",
        "shape": "array",
        "returns": ["cluster_metadata", "storage_data"],
    },
    {
        "method": "GET",
        "path": "/api/placement",
        "group": "Decisions",
        "summary": "Rank queues for a job",
        "description": (
            "Scores every queue that can run the job on idle capacity, measured "
            "wait, backlog, and allocation headroom. Queues that cannot run it "
            "come back under 'blocked' with the reason."
        ),
        "params": [
            {"name": "cores", "type": "integer", "default": 1, "description": "Cores the job needs"},
            {"name": "hours", "type": "number", "default": 1, "description": "Walltime in hours"},
            {"name": "gpus", "type": "integer", "default": 0, "description": "GPUs the job needs"},
            {"name": "queue_type", "type": "string", "description": "Restrict to a queue type or name"},
            {"name": "limit", "type": "integer", "default": 5, "description": "Maximum candidates"},
        ],
        "returns": ["request", "candidates", "blocked", "considered", "generated_at"],
    },
    {
        "method": "GET",
        "path": "/api/insights",
        "group": "Decisions",
        "summary": "Generated warnings and recommendations",
        "description": "Allocation, backlog, GPU and status findings, sorted by priority.",
        "returns": ["insights", "generated_at"],
    },
    {
        "method": "GET",
        "path": "/api/system-markdown/{system}",
        "group": "Reference",
        "summary": "A system's briefing",
        "description": "Markdown notes scraped for a system, keyed by slug.",
        "returns": ["slug", "content"],
    },
    {
        "method": "GET",
        "path": "/api/config",
        "group": "Reference",
        "summary": "Deployment configuration the frontend needs",
        "description": (
            "Branding, enabled tabs, feature flags and topology settings. "
            "Secrets are never included — the alert webhook URL in particular."
        ),
        "returns": ["deployment", "ui", "features", "topology"],
    },
    {
        "method": "GET",
        "path": "/api/endpoints",
        "group": "Reference",
        "summary": "This catalog",
        "description": "Describes every endpoint, including itself.",
        "returns": ["endpoints", "groups", "base_url", "generated_at"],
    },
    {
        "method": "GET",
        "path": "/api/v2/collectors/status",
        "group": "Reference",
        "summary": "Whether each collector has data yet",
        "description": "Readiness per collector, for health checks and debugging.",
        "returns": ["collectors"],
    },
    {
        "method": "GET",
        "path": "/app-config.js",
        "group": "Reference",
        "summary": "Frontend configuration as a script",
        "description": (
            "The same configuration as /api/config, delivered as JavaScript "
            "that assigns window.APP_CONFIG. Not JSON."
        ),
        "content_type": "application/javascript",
    },
    {
        "method": "POST",
        "path": "/api/refresh",
        "group": "Control",
        "summary": "Collect from source now",
        "description": (
            "Runs a fleet refresh synchronously and returns whether it "
            "succeeded. No request body."
        ),
        "returns": ["ok", "detail"],
    },
]


def catalog() -> List[Dict[str, Any]]:
    """The endpoint list, safe for the caller to mutate."""
    return [dict(entry) for entry in ENDPOINTS]


def groups() -> List[str]:
    """Group names in the order they first appear."""
    seen: List[str] = []
    for entry in ENDPOINTS:
        group = entry.get("group", "Other")
        if group not in seen:
            seen.append(group)
    return seen
