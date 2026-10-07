# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Configuration-specific exceptions."""


class ConfigurationError(Exception):
    """Base configuration processing error."""


class ConfigurationPersistenceError(ConfigurationError):
    """A deployable configuration could not be persisted."""
