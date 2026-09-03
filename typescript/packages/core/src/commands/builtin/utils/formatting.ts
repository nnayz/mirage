// ========= Copyright 2026 @ Strukto.AI All Rights Reserved. =========
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
// ========= Copyright 2026 @ Strukto.AI All Rights Reserved. =========

import { DEVICE_NUMBERS_KEY, FileType, LINK_TARGET_KEY, type FileStat } from '../../../types.ts'
import { DEFAULT_MODES, EPOCH_LS_TIME, MONTHS, NUMERIC_PREFIX, TYPE_CHARS } from './constants.ts'
import { UNKNOWN_NAME, groupName, ownerName, type Identity } from './identity.ts'

/**
 * GNU's `human_readable` rounding, shared by `-h` and `-H`.
 *
 * Three rules, none of which fall out of a plain divide-and-format.
 * Below one unit GNU prints the count alone -- `24`, never `24B`. Above
 * it the value is rounded *up* to the precision shown, so 1025 bytes is
 * `1.1K` rather than `1.0K`. And the decimal is dropped once the scaled
 * value reaches ten, giving `10K` rather than `10.0K`. Rounding up can
 * carry past the base (1048575 bytes ceils to 1024K, which GNU shows as
 * `1.0M`), so the unit is re-chosen after rounding instead of once up
 * front.
 *
 * @param n byte count
 * @param base 1024 for `-h`, 1000 for `-H`
 * @param units suffixes indexed by power; index 0 is unused because a
 *   sub-unit count carries no suffix at all
 */
function ceilDiv(a: bigint, b: bigint): bigint {
  return (a + b - 1n) / b
}

export function humanScaled(n: number, base: number, units: readonly string[]): string {
  if (n < base) return String(n)
  // BigInt, not number: `n * 10` leaves the safe-integer range a little
  // under a petabyte, and the product silently rounds down before the
  // ceiling, which then lands on the wrong tenth -- 1914029841632461
  // bytes read `1.7P` where GNU and Python say `1.8P`. Python does this
  // in arbitrary-precision ints, so BigInt is the faithful mirror rather
  // than a workaround. A count this large is integral; truncating guards
  // the BigInt conversion against a fractional caller.
  const value = BigInt(Math.trunc(n))
  const big = BigInt(base)
  let i = 1
  let divisor = big
  for (;;) {
    const tenths = ceilDiv(value * 10n, divisor)
    if (tenths < 100n) {
      const unit = (tenths / 10n).toString()
      const decimal = (tenths % 10n).toString()
      return `${unit}.${decimal}${units[i] ?? ''}`
    }
    const whole = ceilDiv(value, divisor)
    if (whole < big || i === units.length - 1) return `${whole.toString()}${units[i] ?? ''}`
    i += 1
    divisor *= big
  }
}

export function humanSize(n: number): string {
  return humanScaled(n, 1024, ['', 'K', 'M', 'G', 'T', 'P', 'E'])
}

function permTriplet(bits: number, special?: string): string {
  const execBit =
    special !== undefined
      ? bits & 1
        ? special.toLowerCase()
        : special.toUpperCase()
      : bits & 1
        ? 'x'
        : '-'
  return (bits & 4 ? 'r' : '-') + (bits & 2 ? 'w' : '-') + execBit
}

export function lsModeString(s: FileStat): string {
  const typeChar = TYPE_CHARS[s.type] ?? '-'
  const mode = s.mode ?? DEFAULT_MODES[s.type] ?? 0o644
  return (
    typeChar +
    permTriplet(mode >> 6, mode & 0o4000 ? 's' : undefined) +
    permTriplet(mode >> 3, mode & 0o2000 ? 's' : undefined) +
    permTriplet(mode, mode & 0o1000 ? 't' : undefined)
  )
}

function padLeft(s: string, width: number): string {
  return s.length >= width ? s : ' '.repeat(width - s.length) + s
}

function lsTimeString(modified: string | null | undefined): string {
  if (modified === null || modified === undefined || modified === '') {
    return EPOCH_LS_TIME
  }
  const t = Date.parse(modified)
  if (Number.isNaN(t)) return EPOCH_LS_TIME
  const d = new Date(t)
  const month = MONTHS[d.getUTCMonth()] ?? 'Jan'
  const day = padLeft(String(d.getUTCDate()), 2)
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mm = String(d.getUTCMinutes()).padStart(2, '0')
  return `${month} ${day} ${hh}:${mm}`
}

export interface LsLongOptions {
  human?: boolean
  // Who the session is; null outside a workspace, where both the owner
  // and the group column fall back to `-`.
  identity?: Identity | null
  sizeWidth?: number
}

// The name column: GNU appends `-> target` for a symlink row.
function lsName(s: FileStat): string {
  if (s.type !== FileType.SYMLINK) return s.name
  const target = s.extra[LINK_TARGET_KEY]
  return typeof target === 'string' && target !== '' ? `${s.name} -> ${target}` : s.name
}

// The size and time columns of one `ls -l` row. A device row carries its
// major and minor numbers where GNU puts them. An entry with neither a
// size nor a time (a synthetic API-backend directory) shows `-` in both
// rather than inventing size 0 and the epoch, mirroring the python
// formatter.
function lsSizeAndTime(s: FileStat, human: boolean): [string, string] {
  const device = s.extra[DEVICE_NUMBERS_KEY]
  if (Array.isArray(device) && device.length === 2) {
    const time = s.modified == null ? UNKNOWN_NAME : lsTimeString(s.modified)
    return [`${String(device[0])}, ${String(device[1])}`, time]
  }
  if (s.size == null && s.modified == null) return [UNKNOWN_NAME, UNKNOWN_NAME]
  const size = human ? humanSize(s.size ?? 0) : String(s.size ?? 0)
  return [size, lsTimeString(s.modified)]
}

// `ls -l` rows: mode, links, owner, group, size, time, name. The owner is
// the entry's uid when a backend or the attr overlay reports one, else
// the workspace user; the group is the gid, else the session's profile;
// `-` when nothing names one.
export function formatLsLong(stats: readonly FileStat[], opts: LsLongOptions = {}): string[] {
  const identity = opts.identity ?? null
  const human = opts.human ?? false
  const columns = stats.map((s) => lsSizeAndTime(s, human))
  const width = opts.sizeWidth ?? columns.reduce((m, [size]) => Math.max(m, size.length), 1)
  return stats.map((s, i) => {
    const [rawSize, time] = columns[i] ?? [UNKNOWN_NAME, UNKNOWN_NAME]
    const mode = lsModeString(s)
    const size = padLeft(rawSize, width)
    const who = ownerName(s.uid, identity)
    const grp = groupName(s.gid, identity)
    return `${mode} 1 ${who} ${grp} ${size} ${time} ${lsName(s)}`
  })
}

export function toNumber(val: string): number {
  const m = NUMERIC_PREFIX.exec(val.trim())
  return m === null ? 0 : Number.parseFloat(m[0])
}
