"""LDAP identity provider (design section 20.5.1).

``ldap3`` is imported lazily so the package imports without the optional
dependency and tests can exercise the search/escape logic without a server.
Nested groups are flattened at sync time by the service, not here.
"""

from __future__ import annotations

from typing import Any, ClassVar

from llm_d_bench.auth.providers.base import (
    ConnectionResult,
    ExternalIdentity,
    IdentityProvider,
    IdentityProviderError,
    ProviderCapabilities,
)

#: RFC 4515 filter escaping for the ``{username}`` placeholder.
_FILTER_ESCAPES = {"\\": "\\5c", "*": "\\2a", "(": "\\28", ")": "\\29", "\x00": "\\00"}

#: Default filter selecting group entries; overridable via ``group_filter``.
#: Excludes containers such as ``organizationalUnit`` that a ``(objectClass=*)``
#: search under ``group_base_dn`` would otherwise import as empty groups.
_DEFAULT_GROUP_FILTER = (
    "(|(objectClass=groupOfNames)(objectClass=groupOfUniqueNames)(objectClass=posixGroup))"
)


def escape_filter_value(value: str) -> str:
    return "".join(_FILTER_ESCAPES.get(char, char) for char in value)


class LdapProvider(IdentityProvider):
    type: ClassVar[str] = "ldap"
    capabilities: ClassVar[ProviderCapabilities] = ProviderCapabilities(
        password_auth=True,
        browser_redirect=False,
        group_sync=True,
        writable=False,
        jit_provisioning=True,
    )

    def _server(self) -> Any:
        try:
            from ldap3 import Server  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on extra
            raise IdentityProviderError("ldap3 is not installed; install the [ldap] extra") from exc
        url = str(self.config.get("server_url") or "")
        use_ssl = url.lower().startswith("ldaps://")
        host = url.split("://", 1)[-1]
        port = int(self.config.get("port") or (636 if use_ssl else 389))
        return Server(host, port=port, use_ssl=use_ssl, get_info=None)

    def _connect(self, user: str | None, password: str | None) -> Any:
        try:
            from ldap3 import Connection  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise IdentityProviderError("ldap3 is not installed; install the [ldap] extra") from exc
        connection = Connection(
            self._server(),
            user=user,
            password=password,
            auto_bind=True,
            receive_timeout=int(self.config.get("read_timeout_seconds", 10)),
        )
        if bool(self.config.get("start_tls")) and not str(self.config.get("server_url", "")).lower().startswith(
            "ldaps://"
        ):
            connection.start_tls()
        return connection

    def _user_filter(self, username: str) -> str:
        template = str(self.config.get("user_filter") or "({username_attribute}={username})")
        attribute = self.config.get("username_attribute", "uid")
        return template.format(username_attribute=attribute, username=escape_filter_value(username))

    async def authenticate(self, username: str, password: str) -> ExternalIdentity | None:
        if not password:
            return None
        user_base = str(self.config.get("user_base_dn") or "")
        # Resolve the user entry and their groups with the service bind: ordinary
        # users are usually not allowed to read group entries (osixia's default
        # ACL restricts reads to self), so the bind connection is used for both.
        admin = self._connect(self.config.get("bind_dn"), self.secret)
        try:
            admin.search(user_base, self._user_filter(username), attributes=["*"])
            entries = list(admin.entries)
            if not entries:
                return None
            entry = entries[0]
            user_dn = entry.entry_dn
            groups = self._groups_for(admin, user_dn)
        finally:
            admin.unbind()
        # Verify the user's own password with a second bind.
        try:
            user_connection = self._connect(user_dn, password)
        except Exception:  # noqa: BLE001 - any bind failure means invalid credentials
            return None
        user_connection.unbind()
        return ExternalIdentity(
            external_id=user_dn,
            username=username,
            display_name=str(self._attribute(entry, self.config.get("display_name_attribute", "cn")) or username),
            email=str(self._attribute(entry, self.config.get("email_attribute", "mail")) or ""),
            groups=tuple(groups),
        )

    def _groups_for(self, connection: Any, user_dn: str) -> list[str]:
        """Return the DNs of the groups the user belongs to (design section 20.5.1)."""
        group_base = str(self.config.get("group_base_dn") or "")
        if not group_base:
            return []
        member_attribute = str(self.config.get("group_member_attribute", "member"))
        name_attribute = str(self.config.get("group_name_attribute", "cn"))
        group_filter = str(self.config.get("group_filter") or _DEFAULT_GROUP_FILTER)
        connection.search(group_base, group_filter, attributes=[member_attribute, name_attribute])
        groups: list[str] = []
        for entry in connection.entries:
            members = entry[member_attribute].values if member_attribute in entry else []
            if user_dn in members:
                groups.append(str(entry.entry_dn))
        return groups

    async def list_groups(self) -> list[dict[str, str]]:
        """Enumerate every group under ``group_base_dn`` (design section 20.5.1)."""
        group_base = str(self.config.get("group_base_dn") or "")
        if not group_base:
            return []
        name_attribute = str(self.config.get("group_name_attribute", "cn"))
        group_filter = str(self.config.get("group_filter") or _DEFAULT_GROUP_FILTER)
        connection = self._connect(self.config.get("bind_dn"), self.secret)
        try:
            connection.search(group_base, group_filter, attributes=[name_attribute])
            return [
                {
                    "external_id": str(entry.entry_dn),
                    "name": str(self._attribute(entry, name_attribute) or entry.entry_dn),
                }
                for entry in connection.entries
            ]
        finally:
            connection.unbind()

    @staticmethod
    def _attribute(entry: Any, name: str) -> Any:
        try:
            return entry[name].value if name in entry else None
        except Exception:  # noqa: BLE001
            return None

    async def list_group_members(self, external_group: str) -> list[ExternalIdentity]:
        """Resolve direct members of a group during a manual directory sync."""
        group_base = str(self.config.get("group_base_dn") or "")
        member_attribute = str(self.config.get("group_member_attribute", "member"))
        name_attribute = str(self.config.get("group_name_attribute", "cn"))
        connection = self._connect(self.config.get("bind_dn"), self.secret)
        try:
            if "=" in external_group:
                # Treat the mapping value as a group DN.
                connection.search(external_group, "(objectClass=*)", attributes=[member_attribute])
            elif group_base:
                connection.search(
                    group_base,
                    f"({name_attribute}={escape_filter_value(external_group)})",
                    attributes=[member_attribute],
                )
            else:
                return []
            entries = list(connection.entries)
            if not entries:
                return []
            members = list(entries[0][member_attribute].values) if member_attribute in entries[0] else []
            identities: list[ExternalIdentity] = []
            for member_dn in members:
                identity = self._identity_for_dn(connection, str(member_dn))
                if identity is not None:
                    identities.append(identity)
            return identities
        finally:
            connection.unbind()

    def _identity_for_dn(self, connection: Any, member_dn: str) -> ExternalIdentity | None:
        try:
            from ldap3 import BASE  # noqa: PLC0415

            connection.search(member_dn, "(objectClass=*)", search_scope=BASE, attributes=["*"])
        except Exception:  # noqa: BLE001
            return None
        if not connection.entries:
            return None
        entry = connection.entries[0]
        username = str(
            self._attribute(entry, self.config.get("username_attribute", "uid"))
            or self._attribute(entry, self.config.get("display_name_attribute", "cn"))
            or ""
        )
        if not username:
            return None
        return ExternalIdentity(
            external_id=member_dn,
            username=username,
            display_name=str(self._attribute(entry, self.config.get("display_name_attribute", "cn")) or username),
            email=str(self._attribute(entry, self.config.get("email_attribute", "mail")) or ""),
        )

    async def test_connection(self) -> ConnectionResult:
        try:
            connection = self._connect(self.config.get("bind_dn"), self.secret)
        except Exception as error:  # noqa: BLE001
            return ConnectionResult(ok=False, detail=str(error))
        connection.unbind()
        return ConnectionResult(ok=True, detail="bind succeeded")
