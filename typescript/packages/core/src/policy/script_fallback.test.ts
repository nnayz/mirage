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

import { describe, expect, it, vi } from 'vitest'
import { PrefixResolver } from '../runtime/resolver.ts'
import { ScriptSource } from '../runtime/routing/types.ts'
import type { BridgeDispatchFn } from '../runtime/types.ts'
import { ContentType, FileStat, FileType, PathSpec } from '../types.ts'
import type * as asyncContextModule from '../utils/async_context.ts'
import { ScriptPolicy } from './script.ts'
import type { CommandContext, OpsContext, ProfileScript } from './types.ts'

// The browser-runtime branch under node's test runner: the real
// FallbackStorage, no task isolation.
vi.mock('../utils/async_context.ts', async (importOriginal) => {
  const real = await importOriginal<typeof asyncContextModule>()
  return {
    ...real,
    asyncContextIsolatesTasks: false,
    createAsyncContext<T>() {
      return new real.FallbackStorage<T>()
    },
  }
})

// A program that reads at the command door and refuses at the op door,
// so its own read is exactly what its op hook would deadlock on.
const READER_AND_GATE = `\
def pre_command(ctx):
    try:
        open('/repo/a').read()
    except OSError:
        pass
    return None

def pre_ops(ctx):
    return {'deny': 'judged ' + ctx['op']['path']}
`

function path(virtual: string): PathSpec {
  return new PathSpec({
    virtual,
    directory: virtual,
    resourcePath: '',
    rawPath: virtual,
    resolved: true,
  })
}

function commandCtx(): CommandContext {
  return {
    command: 'cat',
    paths: [path('/repo/a')],
    operands: [path('/repo/a')],
    argv: ['/repo/a'],
    cwd: '/repo',
    registry: { isMountRoot: () => false },
    sessionId: 's',
    agentId: 'agent-1',
    tokens: ['cat', '/repo/a'],
    program: ['cat'],
  }
}

function opsCtx(op: string, virtual: string): OpsContext {
  return { op, path: path(virtual), write: false, prefix: '/repo', sessionId: 's' }
}

interface HeldDoor {
  door: BridgeDispatchFn
  /** Settles when the engine's read of `/repo/a` reaches the door. */
  arrived: Promise<void>
  /** Lets that read answer. */
  release: () => void
}

/**
 * A door over one file, `/repo/a`, whose read waits until the test
 * releases it: the window in which the policy's own read is in flight.
 */
function heldDoor(): HeldDoor {
  let arrive!: () => void
  let release!: () => void
  const arrived = new Promise<void>((resolve) => {
    arrive = resolve
  })
  const held = new Promise<void>((resolve) => {
    release = resolve
  })
  const body = new TextEncoder().encode('hello')
  const door: BridgeDispatchFn = async (op, p) => {
    if (op === 'readdir') return ['/repo/a']
    if (p !== '/repo/a') throw Object.assign(new Error(p), { code: 'ENOENT' })
    if (op === 'stat') {
      return new FileStat({
        name: p,
        size: body.length,
        type: FileType.FILE,
        content: ContentType.TEXT,
      })
    }
    if (op === 'read') {
      arrive()
      await held
      return body
    }
    throw Object.assign(new Error(`${op} ${p}`), { code: 'EROFS' })
  }
  return { door, arrived, release }
}

describe('ScriptPolicy on the fallback storage', () => {
  it('exempts only the read its own engine has in flight', async () => {
    const entry: ProfileScript = {
      profile: 'release',
      script: new ScriptSource(READER_AND_GATE, 'python'),
      runtime: 'monty',
    }
    const { door, arrived, release } = heldDoor()
    const policy = new ScriptPolicy({ scriptOf: () => entry }, () => ['/repo/'], {
      bridge: () => door,
      resolver: new PrefixResolver(() => ['/repo/']),
    })
    try {
      const judging = policy.preCommand(commandCtx())
      await arrived
      // The engine's own read, by name and path, is let through.
      expect(await policy.preOps(opsCtx('read', '/repo/a'))).toBeNull()
      // A concurrent op on another path is judged, not handed the
      // exemption because a read happens to be live: it waits for the
      // evaluation the read belongs to, then gets the hook's answer.
      const other = policy.preOps(opsCtx('read', '/repo/b'))
      release()
      expect(await judging).toBeNull()
      expect(await other).toEqual({ kind: 'deny', reason: 'judged /repo/b' })
    } finally {
      await policy.close()
    }
  }, 60_000)
})
