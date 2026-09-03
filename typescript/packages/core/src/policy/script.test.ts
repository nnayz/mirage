import { afterEach, describe, expect, it } from 'vitest'
import { PrefixResolver } from '../runtime/resolver.ts'
import { ScriptSource } from '../runtime/routing/types.ts'
import type { BridgeDispatchFn } from '../runtime/types.ts'
import { ContentType, FileStat, FileType, PathSpec } from '../types.ts'
import { DEFAULT_ASK_REASON, DEFAULT_DENY_REASON } from './constants.ts'
import { ScriptPolicy, scriptAction, scriptContext } from './script.ts'
import type { CommandContext, ProfileScript } from './types.ts'

// A policy is a program defining the hook it answers at, the way a
// coded Policy does, and it answers with return.
const JUDGE = `\
function preCommand(ctx) {
  const c = ctx.command
  if (c.name === 'cat' && c.paths.some((p) => p.startsWith('/repo/sealed/'))) {
    return { deny: 'sealed by ' + ctx.profile }
  }
  return c.name === 'shred' ? { ask: 'sign-off' } : null
}
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

function ctx(command = 'cat', sessionId = 's'): CommandContext {
  return {
    command,
    paths: [path('/repo/sealed/k')],
    operands: [path('/repo/sealed/k')],
    argv: ['/repo/sealed/k'],
    cwd: '/repo',
    registry: { isMountRoot: () => false },
    sessionId,
    agentId: 'agent-1',
    tokens: [command, '/repo/sealed/k'],
    program: [command],
  }
}

// A python judge that reads what the operand holds, not what it is
// called: the shape a content policy takes when it is a program.
const READER_PY = `\
def pre_command(ctx):
    for p in ctx['command']['paths']:
        try:
            body = open(p).read()
        except OSError:
            continue
        if 'payload' in body:
            return {'ask': 'sign-off on payload'}
    return None
`

function entry(
  source = JUDGE,
  runtime = 'quickjs',
  language: 'js' | 'python' = 'js',
): ProfileScript {
  return { profile: 'release', script: new ScriptSource(source, language), runtime }
}

/**
 * A read-only door over a few files, answering ENOENT for the rest. An
 * open lists the directory and stats the file before it reads, so the
 * door answers all three.
 */
function doorsOver(files: Record<string, string>): BridgeDispatchFn {
  return (op, path) => {
    if (op === 'readdir') {
      const names = Object.keys(files).filter(
        (p) => p.startsWith(path) && !p.slice(path.length).includes('/'),
      )
      if (names.length === 0) {
        return Promise.reject(Object.assign(new Error(path), { code: 'ENOENT' }))
      }
      return Promise.resolve(names)
    }
    const body = files[path]
    if (body === undefined) {
      return Promise.reject(Object.assign(new Error(path), { code: 'ENOENT' }))
    }
    const bytes = new TextEncoder().encode(body)
    if (op === 'read') return Promise.resolve(bytes)
    if (op === 'stat') {
      return Promise.resolve(
        new FileStat({
          name: path,
          size: bytes.length,
          type: FileType.FILE,
          content: ContentType.TEXT,
        }),
      )
    }
    return Promise.reject(Object.assign(new Error(`${op} ${path}`), { code: 'EROFS' }))
  }
}

function policyOf(script: ProfileScript | null): ScriptPolicy {
  return new ScriptPolicy({ scriptOf: () => script }, () => ['/repo/', '/scratch/'])
}

const open: ScriptPolicy[] = []

function track(policy: ScriptPolicy): ScriptPolicy {
  open.push(policy)
  return policy
}

afterEach(async () => {
  for (const policy of open.splice(0)) await policy.close()
})

describe('scriptContext', () => {
  it('is the command context as data', () => {
    expect(scriptContext('release', ctx(), ['/repo/', '/scratch/'])).toEqual({
      profile: 'release',
      command: {
        name: 'cat',
        argv: ['/repo/sealed/k'],
        tokens: ['cat', '/repo/sealed/k'],
        program: ['cat'],
        paths: ['/repo/sealed/k'],
        operands: ['/repo/sealed/k'],
        tool: true,
        walks: false,
      },
      session: { id: 's', agent: 'agent-1', cwd: '/repo' },
      mounts: ['/repo/', '/scratch/'],
    })
  })
})

describe('scriptAction', () => {
  it.each([[null], ['allow']])('reads %j as no opinion', (value) => {
    expect(scriptAction(value)).toBeNull()
  })

  it('turns a deny answer into a whole-command deny', () => {
    expect(scriptAction({ deny: 'sealed' })).toEqual({ kind: 'deny', reason: 'sealed' })
  })

  it('takes an ask answer to the approval door', () => {
    const action = scriptAction({ ask: 'sign-off' })
    expect(action).toEqual({ kind: 'ask', reason: 'sign-off' })
  })

  it("gives the bare verbs the document's default reasons", () => {
    expect(scriptAction('deny')).toEqual({ kind: 'deny', reason: DEFAULT_DENY_REASON })
    expect(scriptAction('ask')).toEqual({ kind: 'ask', reason: DEFAULT_ASK_REASON })
  })

  it.each([
    [[1, 2]],
    [7],
    ['nope'],
    [{}],
    [{ deny: '' }],
    [{ deny: 3 }],
    [{ allow: true }],
    [{ deny: 'a', ask: 'b' }],
  ])('refuses %j', (value) => {
    expect(() => scriptAction(value)).toThrow(/must answer allow, deny or ask/)
  })
})

describe('ScriptPolicy', () => {
  it('does not judge a session without a script', async () => {
    const policy = track(policyOf(null))
    expect(await policy.preCommand(ctx())).toBeNull()
  })

  it('refuses a command with a deny it computed', async () => {
    const policy = track(policyOf(entry()))
    expect(await policy.preCommand(ctx('cat'))).toEqual({
      kind: 'deny',
      reason: 'sealed by release',
    })
  })

  it('stays silent on a command the script allows', async () => {
    const policy = track(policyOf(entry()))
    expect(await policy.preCommand(ctx('ls'))).toBeNull()
  })

  it('takes an ask it computed to the door', async () => {
    const policy = track(policyOf(entry()))
    expect(await policy.preCommand(ctx('shred'))).toEqual({ kind: 'ask', reason: 'sign-off' })
  })

  it('fails closed when the script throws', async () => {
    // Silence on failure would run exactly the commands the script
    // existed to judge, so every failure arm refuses instead.
    const policy = track(policyOf(entry("function preCommand() { throw new Error('boom') }")))
    const action = await policy.preCommand(ctx())
    expect(action).toMatchObject({ kind: 'deny' })
    expect((action as { reason: string }).reason).toMatch(/profile 'release' policy failed/)
  })

  it('fails closed on a wrong answer shape', async () => {
    const policy = track(policyOf(entry('function preCommand() { return [1, 2] }')))
    const action = await policy.preCommand(ctx())
    expect((action as { reason: string }).reason).toMatch(/profile 'release' policy must answer/)
  })

  it('fails closed on a program that defines no hook', async () => {
    // A verdict as a bare last expression was the old contract; a policy
    // defines pre_command / preCommand, and a program without one is
    // refused at the call rather than read for a value it never meant.
    const policy = track(policyOf(entry('null')))
    const action = await policy.preCommand(ctx())
    expect(action).toMatchObject({ kind: 'deny' })
    expect((action as { reason: string }).reason).toMatch(/profile 'release' policy failed/)
    expect((action as { reason: string }).reason).toMatch(/preCommand/)
  })

  it('fails closed on an engine it cannot build', async () => {
    const policy = track(policyOf(entry(JUDGE, 'ghost')))
    const action = await policy.preCommand(ctx())
    expect((action as { reason: string }).reason).toMatch(
      /profile 'release' policy names runtime 'ghost'/,
    )
  })

  it('fails closed on an engine that cannot evaluate', async () => {
    const policy = track(policyOf(entry(JUDGE, 'vfs')))
    const action = await policy.preCommand(ctx())
    expect((action as { reason: string }).reason).toMatch(/cannot evaluate one/)
  })

  it('reuses one engine across commands and closes it', async () => {
    const policy = policyOf(entry())
    expect(await policy.preCommand(ctx('cat'))).not.toBeNull()
    expect(await policy.preCommand(ctx('ls'))).toBeNull()
    await policy.close()
  })
})

describe('ScriptPolicy wiring', () => {
  it('reads the workspace through the doors it is wired to', async () => {
    // The facts name the path; the engine opens it. The read arrives
    // on the bridge the workspace handed over, the way an agent's own
    // program reaches a mount.
    const policy = track(
      new ScriptPolicy({ scriptOf: () => entry(READER_PY, 'monty', 'python') }, () => ['/repo/'], {
        bridge: () => doorsOver({ '/repo/sealed/k': 'subject: invoice\n\na payload\n' }),
        resolver: new PrefixResolver(() => ['/repo/']),
      }),
    )
    expect(await policy.preCommand(ctx('cat'))).toEqual({
      kind: 'ask',
      reason: 'sign-off on payload',
    })
  }, 60_000)

  it('a bare policy has no door, and its program reads no file', async () => {
    // Unwired, the engine sees no mount: the open misses, the policy's
    // own except arm runs, and nothing is judged on content it never
    // saw. The workspace is what supplies the doors.
    const policy = track(policyOf(entry(READER_PY, 'monty', 'python')))
    expect(await policy.preCommand(ctx('cat'))).toBeNull()
  }, 60_000)
})
