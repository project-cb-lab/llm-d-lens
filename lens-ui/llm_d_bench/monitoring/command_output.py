"""Command-result JSON decoding with domain-specific parse failures."""
import json


def parse_command_json(result, default, *, error_factory, code):
    if result.returncode != 0 or not result.stdout.strip():
        return default
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise error_factory(code, f"Unable to parse {result.argv[0]} response", retryable=True, status_code=502) from error
