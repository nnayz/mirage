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

from datetime import datetime, timezone

from mirage.commands.builtin.utils.constants import (DEFAULT_MODES,
                                                     EPOCH_LS_TIME, MONTHS,
                                                     NUMERIC_PREFIX,
                                                     TYPE_CHARS)
from mirage.commands.builtin.utils.identity import (UNKNOWN_NAME, Identity,
                                                    group_name, owner_name)
from mirage.types import (DEVICE_NUMBERS_KEY, LINK_TARGET_KEY, FileStat,
                          FileType)


def human_scaled(n: int, base: int, units: tuple[str, ...]) -> str:
    """GNU's ``human_readable`` rounding, shared by ``-h`` and ``-H``.

    Three rules, none of which fall out of a plain divide-and-format.
    Below one unit GNU prints the count alone -- ``24``, never ``24B``.
    Above it the value is rounded *up* to the precision shown, so 1025
    bytes is ``1.1K`` rather than ``1.0K``. And the decimal is dropped
    once the scaled value reaches ten, giving ``10K`` rather than
    ``10.0K``. Rounding up can carry past the base (1048575 bytes ceils
    to 1024K, which GNU shows as ``1.0M``), so the unit is re-chosen
    after rounding instead of once up front.

    Args:
        n (int): byte count.
        base (int): 1024 for ``-h``, 1000 for ``-H``.
        units (tuple[str, ...]): suffixes indexed by power; index 0 is
            unused because a sub-unit count carries no suffix at all.

    Returns:
        str: the size as GNU would print it.
    """
    if n < base:
        return str(n)
    i, divisor = 1, base
    while True:
        tenths = -(-n * 10 // divisor)
        if tenths < 100:
            return f"{tenths // 10}.{tenths % 10}{units[i]}"
        whole = -(-n // divisor)
        if whole < base or i == len(units) - 1:
            return f"{whole}{units[i]}"
        i += 1
        divisor *= base


def human_size(n: int) -> str:
    return human_scaled(n, 1024, ("", "K", "M", "G", "T", "P", "E"))


def _perm_triplet(bits: int, special: str | None = None) -> str:
    if special is not None:
        execbit = special.lower() if bits & 1 else special.upper()
    else:
        execbit = "x" if bits & 1 else "-"
    return ("r" if bits & 4 else "-") + ("w" if bits & 2 else "-") + execbit


def ls_mode_string(s: FileStat) -> str:
    type_char = TYPE_CHARS.get(s.type, "-")
    mode = s.mode if s.mode is not None else DEFAULT_MODES.get(s.type, 0o644)
    perms = (_perm_triplet(mode >> 6, "s" if mode & 0o4000 else None) +
             _perm_triplet(mode >> 3, "s" if mode & 0o2000 else None) +
             _perm_triplet(mode, "t" if mode & 0o1000 else None))
    return f"{type_char}{perms}"


def _ls_time_string(modified: str | None) -> str:
    if not modified:
        return EPOCH_LS_TIME
    try:
        text = modified.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return EPOCH_LS_TIME
    month = MONTHS[dt.month - 1]
    day = f"{dt.day:>2}"
    return f"{month} {day} {dt.hour:02d}:{dt.minute:02d}"


def _ls_name(s: FileStat) -> str:
    """The name column: GNU appends ``-> target`` for a symlink row.

    Args:
        s (FileStat): the row being rendered.
    """
    if s.type != FileType.SYMLINK:
        return s.name
    target = s.extra.get(LINK_TARGET_KEY)
    return f"{s.name} -> {target}" if target else s.name


def _ls_size_and_time(s: FileStat, human: bool) -> tuple[str, str]:
    """The size and time columns of one ``ls -l`` row.

    A device row carries its major and minor numbers where GNU puts
    them. An entry with neither a size nor a time (a synthetic
    API-backend directory) shows ``-`` in both rather than inventing
    size 0 and the epoch.

    Args:
        s (FileStat): the row's stat.
        human (bool): render the size with ``-h`` units.
    """
    dev = s.extra.get(DEVICE_NUMBERS_KEY) if s.extra else None
    if dev:
        time = (UNKNOWN_NAME
                if s.modified is None else _ls_time_string(s.modified))
        return f"{dev[0]}, {dev[1]}", time
    if s.size is None and s.modified is None:
        return UNKNOWN_NAME, UNKNOWN_NAME
    size = human_size(s.size or 0) if human else str(s.size or 0)
    return size, _ls_time_string(s.modified)


def format_ls_long(
    stats: list[FileStat],
    *,
    human: bool = False,
    identity: Identity | None = None,
    size_width: int | None = None,
) -> list[str]:
    """Render ``ls -l`` rows: mode, links, owner, group, size, time, name.

    The owner is the entry's uid when a backend or the attr overlay
    reports one, else the workspace user; the group is the gid, else the
    session's profile; ``-`` when nothing names one.

    Args:
        stats (list[FileStat]): the rows to render.
        human (bool): render sizes with ``-h`` units.
        identity (Identity | None): who the session is; None outside a
            workspace, where both columns fall back to ``-``.
        size_width (int | None): the size column's width, computed from
            the rows when None.
    """
    columns = [_ls_size_and_time(s, human) for s in stats]
    width = size_width if size_width is not None else max(
        (len(size) for size, _ in columns), default=1)
    out: list[str] = []
    for s, (raw_size, time) in zip(stats, columns):
        mode = ls_mode_string(s)
        size = raw_size.rjust(width)
        who = owner_name(s.uid, identity)
        grp = group_name(s.gid, identity)
        out.append(f"{mode} 1 {who} {grp} {size} {time} {_ls_name(s)}")
    return out


def to_number(val: str) -> float:
    """Coerce a string to a number with GNU awk semantics.

    Args:
        val (str): raw token; the leading numeric prefix counts, else 0.
    """
    m = NUMERIC_PREFIX.match(val.strip())
    return float(m.group(0)) if m else 0.0


def format_number(val: float) -> str:
    """Render an awk numeric value, collapsing integral floats.

    Args:
        val (float): numeric value to render.
    """
    return str(int(val)) if val == int(val) else str(val)
