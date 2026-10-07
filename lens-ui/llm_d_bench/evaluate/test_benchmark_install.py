"""Regression coverage for installing benchmark's unpublished workspace package."""

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

router = importlib.import_module("llm_d_bench.evaluate.router")


@pytest.mark.parametrize("has_report_package", [True, False])
def test_install_supplies_bundled_report_to_pip(monkeypatch, tmp_path, has_report_package):
    root = tmp_path / "benchmark"
    root.mkdir()
    report = root / "benchmark-report"
    if has_report_package:
        report.mkdir()
        (report / "pyproject.toml").write_text('[project]\nname = "llmd-benchmark-report"\n')
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if "pip" in command:
            if has_report_package and str(report) not in command:
                return SimpleNamespace(returncode=1, stderr="No matching distribution found for llmd-benchmark-report")
            assert (str(report) in command) == has_report_package
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(router.subprocess, "run", run)
    router._install_benchmark_checkout(root)
    assert any("-c" in command for command in commands)


def _write_buggy_kubernetes_configuration(venv: Path) -> Path:
    configuration_file = venv / "lib" / "python3.12" / "site-packages" / "kubernetes" / "client" / "configuration.py"
    configuration_file.parent.mkdir(parents=True, exist_ok=True)
    configuration_file.write_text(
        "class Configuration(object):\n"
        "    def __init__(self):\n"
        "        self.proxy = None\n"
        '        if os.getenv("HTTPS_PROXY"): self.proxy = os.getenv("HTTPS_PROXY")\n'
        "        self.no_proxy = None\n"
        '        if os.getenv("NO_PROXY"): self.no_proxy = os.getenv("NO_PROXY")\n'
        '        if os.getenv("no_proxy"): self.no_proxy = os.getenv("no_proxy")\n'
        '        """Proxy URL\n'
        '        """\n'
        "        self.no_proxy = None\n"
        '        """bypass proxy for host in the no_proxy list.\n'
        '        """\n'
        "        self.proxy_headers = None\n",
        encoding="utf-8",
    )
    return configuration_file


def test_patch_kubernetes_client_no_proxy_bug_removes_clobbering_assignment(tmp_path):
    venv = tmp_path / ".venv"
    configuration_file = _write_buggy_kubernetes_configuration(venv)

    router._patch_kubernetes_client_no_proxy_bug(venv)

    patched = configuration_file.read_text(encoding="utf-8")
    assert patched.count("self.no_proxy = None") == 1
    assert 'if os.getenv("no_proxy"): self.no_proxy = os.getenv("no_proxy")' in patched


def test_patch_kubernetes_client_no_proxy_bug_is_idempotent_and_tolerates_missing_file(tmp_path):
    venv = tmp_path / ".venv"
    configuration_file = _write_buggy_kubernetes_configuration(venv)

    router._patch_kubernetes_client_no_proxy_bug(venv)
    once_patched = configuration_file.read_text(encoding="utf-8")
    router._patch_kubernetes_client_no_proxy_bug(venv)
    assert configuration_file.read_text(encoding="utf-8") == once_patched

    router._patch_kubernetes_client_no_proxy_bug(tmp_path / "does-not-exist")

