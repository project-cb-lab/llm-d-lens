"""Monitoring adapter contracts without cluster or network access."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from llm_d_bench.monitoring.command_output import parse_command_json
from llm_d_bench.monitoring.service_links import find_service, service_port
from llm_d_bench.monitoring.prometheus import query_vector
from llm_d_bench.monitoring.operation_runtime import run_install_command, installation_environment


def test_command_parser_retains_default_and_domain_failure():
    class QueryError(Exception):
        def __init__(self, code, message, **details):
            self.code, self.details = code, details
            super().__init__(message)
    fallback = []
    assert parse_command_json(SimpleNamespace(returncode=1, stdout='bad'), fallback, error_factory=QueryError, code='QUERY') is fallback
    with pytest.raises(QueryError) as failure:
        parse_command_json(SimpleNamespace(returncode=0, stdout='{bad', argv=['kubectl']), None, error_factory=QueryError, code='QUERY')
    assert failure.value.code == 'QUERY'
    assert failure.value.details == {'retryable': True, 'status_code': 502}


def test_service_selection_preserves_preferred_port_and_case_folding():
    service = {'metadata': {'name': 'MON-Prometheus'}, 'spec': {'ports': [{'port': 8080}, {'port': 9090}]}}
    assert find_service([service], lambda name: name.endswith('prometheus')) is service
    assert service_port(service, 9090) == 9090
    assert service_port(service, 3000) == 8080
    assert service_port({}, 3000) == 3000


@pytest.mark.asyncio
async def test_prometheus_keeps_vector_and_empty_failure_contract():
    client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, json={'status':'success','data':{'result':[{'value':[1,'0']}] }}, request=httpx.Request('GET','http://local/api/v1/query'))))
    assert await query_vector(client, 'up') == [{'value':[1,'0']}]
    client.get.assert_awaited_with('/api/v1/query', params={'query':'up'})
    client.get.side_effect = httpx.ConnectError('offline')
    assert await query_vector(client, 'up') == []


def test_install_environment_filters_and_overrides_cluster_config(monkeypatch):
    monkeypatch.setenv('UNRELATED_SECRET', 'omit')
    monkeypatch.setenv('KUBECONFIG', 'ambient')
    env = installation_environment('cluster', lambda _: {'KUBECONFIG':'selected'})
    assert env['KUBECONFIG'] == 'selected'
    assert 'UNRELATED_SECRET' not in env


@pytest.mark.asyncio
async def test_install_transport_kills_and_reaps_on_timeout():
    class Process:
        returncode = None
        killed = False
        async def wait(self):
            if not self.killed:
                await asyncio.Future()
            self.returncode = -9
            return self.returncode
        def kill(self):
            self.killed = True
        @property
        def stdout(self):
            async def lines():
                yield b'waiting for pods\n'
                await asyncio.Future()
            return lines()
    process = Process()
    lines = []
    spawn = AsyncMock(return_value=process)
    with pytest.raises(TimeoutError):
        await run_install_command(['installer'], spawn=spawn, env={}, timeout=0.01, on_line=lines.append)
    assert process.killed and process.returncode == -9
    assert lines == ['waiting for pods\n']
