# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Configuration processing backend for Prism."""

from .router import router
from .service import render_configuration, resolve_configurations, save_configuration

__all__ = [
    "render_configuration",
    "resolve_configurations",
    "router",
    "save_configuration",
]
