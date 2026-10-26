import json

import pytest

from llmd import inventory
from llmd.errors import ConflictError, NotFoundError
from llmd.kube import Kube

from .fakes import FakeCluster


def record(**values):
    return {"schema": inventory.SCHEMA, "instance": "r1", "namespace": "team-a", "objects": [], **values}


def test_write_read_and_list():
    cluster = FakeCluster()
    kube = Kube(runner=cluster)
    assert inventory.read(kube, "team-a", "r1") is None
    inventory.write(kube, record(phase="Applied"))
    found = inventory.require(kube, "team-a", "r1")
    assert found["phase"] == "Applied" and found["updatedAt"]
    assert [item["instance"] for item in inventory.list_records(kube, "team-a")] == ["r1"]
    assert inventory.management(found) == inventory.MANAGED
    assert inventory.management(record(management={"mode": "imported"})) == inventory.IMPORTED
    with pytest.raises(NotFoundError) as missing:
        inventory.require(kube, "team-a", "other")
    assert "llmd list" in missing.value.hint


def test_a_record_that_does_not_match_its_labels_is_refused():
    item = inventory.configmap(record())
    tampered = json.loads(item["data"][inventory.DATA_KEY])
    tampered["instance"] = "someone-else"
    item["data"][inventory.DATA_KEY] = json.dumps(tampered)
    with pytest.raises(ConflictError, match="refusing to use it"):
        inventory.parse(item, namespace="team-a")
    with pytest.raises(ConflictError, match="not valid JSON"):
        inventory.parse({"metadata": {"name": "x"}, "data": {inventory.DATA_KEY: "{"}}, namespace="team-a")
    with pytest.raises(ConflictError):
        inventory.parse(inventory.configmap(record()), namespace="team-b")


def test_an_invalid_record_is_listed_not_hidden():
    cluster = FakeCluster()
    item = inventory.configmap(record())
    item["data"][inventory.DATA_KEY] = "{"
    cluster.put("team-a", item)
    [listed] = inventory.list_records(Kube(runner=cluster), "team-a")
    assert listed["phase"] == "Invalid"
