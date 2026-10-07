"""Expand a user-supplied "host spec" string into one or more literal IPs
(or hostnames), so the bootstrap node table can accept:

* a single IP/hostname: ``10.0.0.5``
* a comma-separated list: ``10.0.0.5, 10.0.0.6, 10.0.0.9``
* an IP range, either shorthand (only the last octet varies):
  ``10.0.0.10-20`` -> ``10.0.0.10`` .. ``10.0.0.20``
* or a full "from IP - to IP" range (may span octet boundaries):
  ``10.0.0.250-10.0.1.5``

Every worker (and control-plane) node in a batch expanded from one range
shares the same role selection / SSH credentials -- see
``docs/design/CLUSTER_BOOTSTRAP_DESIGN.md`` §3.1 and ``service.py``.
"""

from __future__ import annotations

import ipaddress
import re

#: Hard ceiling on how many hosts a single bootstrap request may expand to
#: (across every row combined), to keep obviously-mistyped ranges (e.g. a
#: typo'd ``/8``) from silently trying to SSH into thousands of hosts.
MAX_EXPANDED_HOSTS = 256

_SHORTHAND_RANGE = re.compile(r"^(?P<prefix>\d{1,3}\.\d{1,3}\.\d{1,3}\.)(?P<start>\d{1,3})-(?P<end>\d{1,3})$")


def _expand_shorthand_range(spec: str) -> list[str] | None:
    match = _SHORTHAND_RANGE.match(spec)
    if not match:
        return None
    prefix = match.group("prefix")
    start, end = int(match.group("start")), int(match.group("end"))
    if not (0 <= start <= 255) or not (0 <= end <= 255):
        raise ValueError(f"invalid host range {spec!r}: octet values must be between 0 and 255")
    if start > end:
        raise ValueError(f"invalid host range {spec!r}: start octet is greater than end octet")
    candidates = [f"{prefix}{value}" for value in range(start, end + 1)]
    try:
        for candidate in (candidates[0], candidates[-1]):
            ipaddress.IPv4Address(candidate)
    except ipaddress.AddressValueError as error:
        raise ValueError(f"invalid host range {spec!r}: {error}") from error
    return candidates


def _expand_full_range(spec: str) -> list[str] | None:
    if spec.count("-") != 1:
        return None
    start_text, end_text = (part.strip() for part in spec.split("-", 1))
    try:
        start = ipaddress.IPv4Address(start_text)
        end = ipaddress.IPv4Address(end_text)
    except ipaddress.AddressValueError:
        return None
    if int(end) < int(start):
        raise ValueError(f"invalid host range {spec!r}: start address is greater than end address")
    return [str(ipaddress.IPv4Address(value)) for value in range(int(start), int(end) + 1)]


def expand_host_token(token: str) -> list[str]:
    """Expand a single (non-comma-containing) host spec token.

    Only tokens that actually *look* like an IP range (either the
    shorthand ``a.b.c.start-end`` form or a full ``ip-ip`` form) are
    expanded; anything else -- including ordinary hostnames that happen to
    contain a dash, e.g. ``worker-1.internal`` -- is treated as a single
    literal host. A range-*shaped* token with an inverted start/end is
    still rejected, since that's almost certainly a typo rather than an
    intentional hostname.
    """
    spec = token.strip()
    if not spec:
        return []
    if "-" in spec:
        shorthand = _expand_shorthand_range(spec)
        if shorthand is not None:
            return shorthand
        full = _expand_full_range(spec)
        if full is not None:
            return full
    return [spec]


def expand_host_spec(spec: str) -> list[str]:
    """Expand a full host-spec string (comma-separated tokens, each of
    which may itself be a single host or a range) into an ordered list of
    unique literal hosts."""
    hosts: list[str] = []
    seen: set[str] = set()
    for token in spec.split(","):
        for host in expand_host_token(token):
            if host not in seen:
                seen.add(host)
                hosts.append(host)
    if not hosts:
        raise ValueError("host spec did not expand to any hosts")
    return hosts
