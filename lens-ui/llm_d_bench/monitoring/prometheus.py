"""Monitoring query transport retaining legacy empty-on-failure semantics."""
import httpx

async def query_vector(client: httpx.AsyncClient, promql: str) -> list[dict]:
    try:
        response = await client.get("/api/v1/query", params={"query": promql})
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    if payload.get("status") != "success":
        return []
    return (payload.get("data") or {}).get("result") or []
