import pytest

from llm_d_bench.utils.kubernetes import validate_namespace


@pytest.mark.parametrize("value", ["a", "0", "llm-d-monitoring", "a" * 63])
def test_valid_namespace(value):
    assert validate_namespace(value) == value


@pytest.mark.parametrize("value", ["", "a" * 64, "Invalid_Name", "-abc", "abc-", "a.b", "a\nb"])
def test_invalid_namespace(value):
    with pytest.raises(ValueError, match="namespace must be a valid Kubernetes DNS label"):
        validate_namespace(value)
