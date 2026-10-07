"""Resolve the ``ansible-playbook`` executable and the Kubespray checkout it
should run against (design §4.1, "Mode A").

Mirrors ``llm_d_bench.cluster.repo_downloads``'s "clone once, cache by
version, reuse forever" pattern rather than reinventing dependency
management: a pinned Kubespray tag is cloned into
``~/.cache/lens/kubespray/<tag>`` and a dedicated virtualenv is built
from its own ``requirements.txt`` (Kubespray pins a specific Ansible
version; reusing whatever Ansible happens to be on the host's PATH is
explicitly *not* supported upstream).

This module only resolves paths / ensures the checkout exists; it does not
run ``cluster.yml`` itself (see ``service.py``).
"""

from __future__ import annotations

from pathlib import Path

from llm_d_bench.utils.paths import storage_path
from llm_d_bench.utils.shell import CommandNotFoundError, run_sync

#: Matches the version referenced throughout ``docs/design/CLUSTER_BOOTSTRAP_DESIGN.md``
#: (the latest tag as of the design's research pass). Bump deliberately,
#: not automatically, so behaviour doesn't drift silently.
KUBESPRAY_TAG = "v2.31.0"
KUBESPRAY_REPO_URL = "https://github.com/kubernetes-sigs/kubespray.git"

_CLONE_TIMEOUT_SECONDS = 300.0
_VENV_TIMEOUT_SECONDS = 600.0


def cache_root() -> Path:
    return storage_path("cache", "kubespray")


def checkout_dir(tag: str = KUBESPRAY_TAG) -> Path:
    return cache_root() / tag


def venv_dir(tag: str = KUBESPRAY_TAG) -> Path:
    return checkout_dir(tag) / ".venv"


def ansible_playbook_path(tag: str = KUBESPRAY_TAG) -> Path:
    return venv_dir(tag) / "bin" / "ansible-playbook"


def cluster_playbook_path(tag: str = KUBESPRAY_TAG) -> Path:
    return checkout_dir(tag) / "cluster.yml"


def is_ready(tag: str = KUBESPRAY_TAG) -> bool:
    """Whether the pinned Kubespray checkout + venv are already provisioned."""
    return ansible_playbook_path(tag).is_file() and cluster_playbook_path(tag).is_file()


def ensure_ready(tag: str = KUBESPRAY_TAG) -> Path:
    """Clone the pinned Kubespray tag and build its dedicated venv if not
    already present; returns the checkout directory.

    This is a slow, blocking operation (git clone + pip install) -- callers
    running inside the event loop must wrap it in ``asyncio.to_thread``.
    Intentionally *not* invoked automatically as part of every bootstrap
    job: an operator is expected to provision this once via an explicit
    setup step/CLI command (see ``docs/design/CLUSTER_BOOTSTRAP_DESIGN.md`` §9, M1),
    since it requires outbound network access to GitHub/PyPI which may not
    be available at request-serving time in every deployment.
    """
    dest = checkout_dir(tag)
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        clone = run_sync(
            ["git", "clone", "--quiet", "--depth", "1", "--branch", tag, KUBESPRAY_REPO_URL, str(dest)],
            timeout=_CLONE_TIMEOUT_SECONDS,
        )
        if clone.returncode != 0:
            raise RuntimeError(clone.stderr.strip() or f"git clone of {KUBESPRAY_REPO_URL}@{tag} failed")

    venv = venv_dir(tag)
    if not ansible_playbook_path(tag).is_file():
        create_venv = run_sync(["python3", "-m", "venv", str(venv)], timeout=_VENV_TIMEOUT_SECONDS)
        if create_venv.returncode != 0:
            raise RuntimeError(create_venv.stderr.strip() or "failed to create Kubespray venv")
        requirements = dest / "requirements.txt"
        install = run_sync(
            [str(venv / "bin" / "pip"), "install", "-q", "-r", str(requirements)],
            timeout=_VENV_TIMEOUT_SECONDS,
        )
        if install.returncode != 0:
            raise RuntimeError(install.stderr.strip() or "failed to install Kubespray's requirements.txt")
    return dest


def require_ready(tag: str = KUBESPRAY_TAG) -> Path:
    """Like :func:`ensure_ready` but never clones/installs -- raises
    :class:`CommandNotFoundError` if the pinned checkout isn't already
    provisioned, for use on the request-serving path (see M1 in the design
    doc: provisioning happens out-of-band, not on-demand per bootstrap)."""
    if not is_ready(tag):
        raise CommandNotFoundError(
            f"Kubespray {tag} is not provisioned at {checkout_dir(tag)}; "
            "run llm_d_bench.cluster.bootstrap.kubespray.ensure_ready() once "
            "(e.g. via an ops setup step) before starting bootstrap jobs."
        )
    return checkout_dir(tag)
