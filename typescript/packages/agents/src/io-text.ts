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

import { describeRefusal } from '@struktoai/mirage-core/policy/index'
import type { Refusal } from '@struktoai/mirage-core/types'
import type { ExecuteResult } from '@struktoai/mirage-core/workspace/workspace/workspace'

export function decode(value: Uint8Array | null | undefined): string {
  if (value === null || value === undefined) return ''
  return new TextDecoder('utf-8').decode(value)
}

/**
 * Append the refusal's reason as one more line after the shell's own
 * output, for a surface that hands the agent text. Only a
 * command-scoped refusal is described: its stderr is bash's bare
 * `Permission denied`, which says nothing. An operand-scoped one already
 * names the reason on the line, GNU-style.
 */
export function withRefusal(text: string, refusal: Refusal | null): string {
  if (refusal === null || refusal.scope === 'operand') return text
  const line = `${describeRefusal(refusal)}\n`
  if (text === '') return line
  return text.endsWith('\n') ? `${text}${line}` : `${text}\n${line}`
}

export function ioToStr(io: ExecuteResult): string {
  const stdout = io.stdoutText
  const stderr = io.stderrText
  let text = stdout
  if (stderr) text = stdout ? `${stdout}\n${stderr}` : stderr
  return withRefusal(text, io.refusal)
}
