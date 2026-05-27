# ========= Copyright 2026 @ Strukto.AI All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2026 @ Strukto.AI All Rights Reserved. =========


def normalize(raw: str | None) -> str:
    """Normalize a key prefix.

    Args:
        raw: The raw prefix string, or None.

    Returns:
        Empty string if input was None/empty; otherwise the prefix with
        leading slashes stripped and a trailing slash ensured.
    """
    if not raw:
        return ""
    v = raw.lstrip("/")
    return v if v.endswith("/") else v + "/"


def apply(prefix: str, path: str) -> str:
    """Prepend a normalized prefix to a virtual path.

    Args:
        prefix: A normalized prefix (use ``normalize()`` first if unsure).
        path: The virtual path to scope.

    Returns:
        The backend key: ``prefix + path`` with the leading slash of
        ``path`` stripped.
    """
    return prefix + path.lstrip("/")


def apply_dir(prefix: str, path: str) -> str:
    """Same as ``apply()`` but guarantees a trailing slash for LIST-style ops.

    Args:
        prefix: A normalized prefix.
        path: The virtual path to scope.

    Returns:
        The backend key with a trailing slash, suitable for use as a LIST
        ``Prefix`` argument.
    """
    key = apply(prefix, path)
    return key if not key or key.endswith("/") else key + "/"


def strip(prefix: str, key: str) -> str:
    """Strip a normalized prefix from a backend-returned key.

    Args:
        prefix: A normalized prefix.
        key: The backend-returned key.

    Returns:
        The key with the prefix removed if present; otherwise unchanged.
    """
    return key[len(prefix):] if prefix and key.startswith(prefix) else key
