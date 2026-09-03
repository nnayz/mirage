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

from mirage.io.types import IOResult
from mirage.policy import describe_refusal
from mirage.types import Refusal


def decode(value: bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace")


def with_refusal(text: str, refusal: Refusal | None) -> str:
    """Append the refusal's reason as one more line after the shell's
    own output, for a surface that hands the agent text.

    Only a command-scoped refusal is described: its stderr is bash's
    bare ``Permission denied``, which says nothing. An operand-scoped
    one already names the reason on the line, GNU-style.

    Args:
        text (str): the joined stdout and stderr.
        refusal (Refusal | None): the record off the result; None
            returns the text unchanged.
    """
    if refusal is None or refusal.scope == "operand":
        return text
    line = describe_refusal(refusal) + "\n"
    if not text:
        return line
    return text + line if text.endswith("\n") else f"{text}\n{line}"


def io_to_str(io: IOResult) -> str:
    stdout = decode(io.stdout if isinstance(io.stdout, bytes) else None)
    stderr = decode(io.stderr if isinstance(io.stderr, bytes) else None)
    text = stdout
    if stderr:
        text = f"{stdout}\n{stderr}" if stdout else stderr
    return with_refusal(text, io.refusal)
