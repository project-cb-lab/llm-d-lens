"""Shared monitoring Service selection; domain installation and tunnel policies stay local."""

def service_name(item: dict) -> str:
    return str((item.get("metadata") or {}).get("name") or "").lower()


def service_port(item: dict, default: int) -> int:
    ports = (item.get("spec") or {}).get("ports") or []
    if not ports:
        return default
    for port in ports:
        if int(port.get("port", 0) or 0) == default:
            return default
    return int(ports[0].get("port", default) or default)


def find_service(items: list[dict], predicate) -> dict | None:
    for item in items:
        if predicate(service_name(item)):
            return item
    return None
