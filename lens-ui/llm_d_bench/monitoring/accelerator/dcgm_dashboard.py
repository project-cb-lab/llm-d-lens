"""Grafana dashboard for the standalone DCGM exporter.

The dcgm-exporter Helm chart ships a ServiceMonitor but no Grafana dashboard,
so Lens creates this ConfigMap (labelled ``grafana_dashboard=1``) in the
cluster's monitoring namespace; the kube-prometheus-stack Grafana sidecar
watches ``NAMESPACE=ALL`` and provisions it. The datasource is a template
variable so the same JSON works against whatever UID the Prometheus
datasource gets in each cluster.
"""

from __future__ import annotations

import json

DASHBOARD_NAME = "dcgm-exporter-dashboard"
_DATASOURCE = {"type": "prometheus", "uid": "${datasource}"}


def _panel(
    panel_id: int,
    title: str,
    expr: str,
    unit: str,
    x: int,
    y: int,
    *,
    w: int = 12,
    h: int = 8,
    legend: str = "{{Hostname}} gpu {{gpu}}",
) -> dict:
    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "datasource": _DATASOURCE,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {"drawStyle": "line", "fillOpacity": 10, "lineWidth": 1},
            },
            "overrides": [],
        },
        "targets": [
            {"refId": "A", "datasource": _DATASOURCE, "expr": expr, "legendFormat": legend}
        ],
    }


DASHBOARD = {
    "title": "NVIDIA DCGM Exporter",
    "uid": DASHBOARD_NAME,
    "schemaVersion": 39,
    "editable": True,
    "time": {"from": "now-1h", "to": "now"},
    "refresh": "30s",
    "templating": {
        "list": [
            {
                "name": "datasource",
                "label": "Data source",
                "type": "datasource",
                "query": "prometheus",
                "current": {},
                "hide": 0,
            }
        ]
    },
    "panels": [
        _panel(1, "GPU utilization", "avg by (Hostname, gpu) (DCGM_FI_DEV_GPU_UTIL)", "percent", 0, 0),
        _panel(2, "GPU memory used", "avg by (Hostname, gpu) (DCGM_FI_DEV_FB_USED)", "mbytes", 12, 0),
        _panel(3, "GPU power usage", "avg by (Hostname, gpu) (DCGM_FI_DEV_POWER_USAGE)", "watt", 0, 8),
        _panel(4, "GPU temperature", "avg by (Hostname, gpu) (DCGM_FI_DEV_GPU_TEMP)", "celsius", 12, 8),
    ],
}


def dashboard_manifest(monitoring_namespace: str) -> dict:
    """The ConfigMap that the Grafana dashboard sidecar provisions."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": DASHBOARD_NAME,
            "namespace": monitoring_namespace,
            "labels": {"grafana_dashboard": "1"},
        },
        "data": {"dashboard.json": json.dumps(DASHBOARD)},
    }
