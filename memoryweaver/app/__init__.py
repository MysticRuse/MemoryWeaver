# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lazy re-export (PEP 562). app.agent imports the specialist agents, whose
# tools import back into app.app_utils.* - an eager `from .agent import app`
# here would make that a circular import for any entry point that touches
# agents.* before app.* (e.g. the unit tests). Deferring the import until
# someone actually asks for `app.app` breaks the cycle while keeping the
# public surface identical.

__all__ = ["app"]


def __getattr__(name):
    if name == "app":
        from .agent import app
        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
