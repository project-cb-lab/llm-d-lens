import pytest

from llmd import yamlio
from llmd.cli import main
from llmd.errors import CommandError, UsageError
from llmd.process import checked, subprocess_runner


def test_version(capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["--version"])
    assert stopped.value.code == 0
    assert capsys.readouterr().out.startswith("llmd ")


def test_no_command_prints_help(capsys):
    assert main([]) == 2
    assert "usage: llmd" in capsys.readouterr().out


def test_duplicate_yaml_keys_are_refused():
    with pytest.raises(UsageError, match="duplicate YAML key"):
        yamlio.load_documents("a: 1\na: 2\n")


def test_lists_are_expanded_and_empty_documents_dropped():
    text = "kind: List\nitems:\n- {kind: A}\n- {kind: B}\n---\n---\nkind: C\n"
    assert [doc["kind"] for doc in yamlio.load_documents(text)] == ["A", "B", "C"]


def test_kubectl_objects_reads_one_document_per_object():
    text = '{"kind": "ConfigMap", "metadata": {"name": "a"}}\n{"kind": "List", "items": [{"kind": "B"}]}'
    assert [item["kind"] for item in yamlio.kubectl_objects(text)] == ["ConfigMap", "B"]


def test_digest_is_stable_across_key_order():
    assert yamlio.digest({"a": 1, "b": [1, 2]}) == yamlio.digest({"b": [1, 2], "a": 1})


def test_atomic_write(tmp_path):
    target = tmp_path / "deep" / "file.txt"
    yamlio.write_text_atomic(target, "hello")
    assert target.read_text() == "hello"
    assert list(target.parent.iterdir()) == [target]


def test_a_missing_tool_is_a_command_error():
    with pytest.raises(CommandError, match="not installed"):
        subprocess_runner(["llmd-no-such-tool-anywhere"])


def test_checked_raises_on_failure():
    result = subprocess_runner(["git", "--no-such-flag"])
    with pytest.raises(CommandError):
        checked(result, "git")
