import { describe, expect, it } from 'vitest'
import { RAMResource } from '../resource/ram/ram.ts'
import { ScriptSource } from '../runtime/routing/types.ts'
import { MountMode } from '../types.ts'
import { getTestParser } from './fixtures/workspace_fixture.ts'
import { Workspace } from './workspace/workspace.ts'

// A per-command judge: deny cat under /data/sealed/ with a computed
// reason, take shred to the approval door, stay silent otherwise. A
// policy defines the hook it answers at, and answers with return.
const JUDGE = `\
function preCommand(ctx) {
  const c = ctx.command
  if (c.name === 'cat' && c.paths.some((p) => p.startsWith('/data/sealed/'))) {
    return { deny: 'sealed by ' + ctx.profile }
  }
  return c.name === 'shred' ? { ask: 'sign-off' } : null
}
`

// The python spelling of the same judge, for the engines that speak it.
const JUDGE_PY = `\
def pre_command(ctx):
    c = ctx['command']
    for p in c['paths']:
        if c['name'] == 'cat' and p.startswith('/data/sealed/'):
            return {'deny': 'sealed by ' + ctx['profile']}
    if c['name'] == 'shred':
        return {'ask': 'sign-off'}
    return None
`

// A judge that reads what the operand holds, not what it is called:
// the shape a content policy takes when it is a program.
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

// One profile named release with a policy and nothing else, so what
// runs is purely the policy's decision.
function scripted(source = JUDGE, runtime = 'quickjs', language: 'js' | 'python' = 'js') {
  return {
    release: {
      policy: { script: new ScriptSource(source, language), runtime },
    },
  }
}

async function build(
  profiles: Record<string, unknown>,
  runtimes?: string[],
  profile?: string,
): Promise<Workspace> {
  const shellParser = await getTestParser()
  return new Workspace(
    { '/data/': new RAMResource() },
    {
      mode: MountMode.WRITE,
      shellParser,
      profiles: profiles as never,
      ...(runtimes !== undefined ? { runtimes } : {}),
      ...(profile !== undefined ? { profile } : {}),
    },
  )
}

describe('profile policies', () => {
  it('judges each command, with the facts as ctx', async () => {
    const ws = await build(scripted())
    try {
      await ws.execute('mkdir -p /data/sealed && echo k > /data/sealed/k')
      ws.createSession('s', { profile: 'release' })
      expect((await ws.execute('echo hi', { sessionId: 's' })).exitCode).toBe(0)
      const denied = await ws.execute('cat /data/sealed/k', { sessionId: 's' })
      expect(denied.exitCode).toBe(126)
      expect(denied.stderrText).toBe('cat: Permission denied\n')
      expect(denied.refusal).toMatchObject({ kind: 'deny', reason: 'sealed by release' })
    } finally {
      await ws.close()
    }
  })

  it('reads resolved paths, not typed words', async () => {
    // `cd /data && cat sealed/k` names no /data/sealed word; the gate
    // hands the script the resolved operand, so the deny still lands.
    const ws = await build(scripted())
    try {
      ws.createSession('s', { profile: 'release' })
      const denied = await ws.execute('cd /data && cat sealed/k', { sessionId: 's' })
      expect(denied.exitCode).toBe(126)
      expect(denied.stderrText).toBe('cat: Permission denied\n')
    } finally {
      await ws.close()
    }
  })

  it('a script-only profile installs everything', async () => {
    // No allow list, so nothing is hidden: a command no document names
    // runs whenever the script stays silent on it.
    const ws = await build(scripted())
    try {
      await ws.execute('echo x > /data/x')
      ws.createSession('s', { profile: 'release' })
      expect((await ws.execute('rm /data/x', { sessionId: 's' })).exitCode).toBe(0)
    } finally {
      await ws.close()
    }
  })

  it('a document may ride beside the script', async () => {
    // Optional, not required: a profile stating both keeps the allow
    // list's hiding, and the script adds its verdicts beside it.
    const profiles = scripted()
    const ws = await build({
      release: { ...profiles.release, commands: { allow: ['ls', 'cat', 'echo'] } },
    })
    try {
      ws.createSession('s', { profile: 'release' })
      expect((await ws.execute('rm /data/x', { sessionId: 's' })).exitCode).toBe(127)
      const denied = await ws.execute('cat /data/sealed/k', { sessionId: 's' })
      expect(denied.exitCode).toBe(126)
      expect(denied.stderrText).toBe('cat: Permission denied\n')
    } finally {
      await ws.close()
    }
  })

  it('takes an ask it computed to the approval door', async () => {
    const ws = await build(scripted())
    try {
      ws.createSession('s', { profile: 'release' })
      const held = await ws.execute('shred /data/x', { sessionId: 's' })
      expect(held.exitCode).toBe(126)
      expect(held.stderrText).toBe('shred: Permission denied\n')
      expect(held.refusal).toMatchObject({ kind: 'pending', reason: 'sign-off' })
      expect(held.refusal?.askId).toBeTruthy()
    } finally {
      await ws.close()
    }
  })

  it('leaves other sessions alone', async () => {
    const ws = await build(scripted())
    try {
      await ws.execute('mkdir -p /data/sealed && echo k > /data/sealed/k')
      ws.createSession('s', { profile: 'release' })
      const read = await ws.execute('cat /data/sealed/k')
      expect(read.exitCode).toBe(0)
    } finally {
      await ws.close()
    }
  })

  it('a scripted default profile shapes the default session', async () => {
    const ws = await build(scripted(), undefined, 'release')
    try {
      expect((await ws.execute('echo hi')).exitCode).toBe(0)
      const denied = await ws.execute('cat /data/sealed/k')
      expect(denied.exitCode).toBe(126)
      expect(denied.stderrText).toBe('cat: Permission denied\n')
    } finally {
      await ws.close()
    }
  })

  it('runs a python judge on monty, the engine both hosts carry', async () => {
    const ws = await build(scripted(JUDGE_PY, 'monty', 'python'))
    try {
      ws.createSession('s', { profile: 'release' })
      const denied = await ws.execute('cat /data/sealed/k', { sessionId: 's' })
      expect(denied.exitCode).toBe(126)
      expect(denied.stderrText).toBe('cat: Permission denied\n')
    } finally {
      await ws.close()
    }
  }, 120000)

  it('a python judge reads what the line names, through the mounts', async () => {
    // Content, not names: the script opens each operand through the
    // same door an agent's program would, so it can ask about what a
    // file holds. A directory operand is not its business, and a file
    // without the marker runs.
    const ws = await build(scripted(READER_PY, 'monty', 'python'))
    try {
      await ws.execute(
        "mkdir -p /data/in && printf 'subject: invoice\\n\\na payload\\n' > /data/in/mail.txt && echo plain > /data/in/note.txt",
      )
      ws.createSession('s', { profile: 'release' })
      const held = await ws.execute('cat /data/in/mail.txt', { sessionId: 's' })
      expect(held.exitCode).toBe(126)
      expect(held.refusal).toMatchObject({ kind: 'pending', reason: 'sign-off on payload' })
      expect((await ws.execute('cat /data/in/note.txt', { sessionId: 's' })).exitCode).toBe(0)
      expect((await ws.execute('ls /data/in', { sessionId: 's' })).exitCode).toBe(0)
    } finally {
      await ws.close()
    }
  }, 120000)

  it('runs in a world with no evaluator', async () => {
    // A profile is operator configuration, so the engine that judges
    // for it is a property of the profile, built fresh and never
    // resolved out of the runtime world.
    const ws = await build(scripted(), ['vfs'])
    try {
      ws.createSession('s', { profile: 'release' })
      const denied = await ws.execute('cat /data/sealed/k', { sessionId: 's' })
      expect(denied.exitCode).toBe(126)
      expect(denied.stderrText).toBe('cat: Permission denied\n')
    } finally {
      await ws.close()
    }
  })

  it('a broken policy fails closed per command', async () => {
    // Silence on failure would run exactly the commands the policy
    // existed to judge.
    const ws = await build(scripted("function preCommand() { throw new Error('boom') }"))
    try {
      ws.createSession('s', { profile: 'release' })
      const refused = await ws.execute('echo hi', { sessionId: 's' })
      expect(refused.exitCode).toBe(126)
      expect(refused.stderrText).toBe('echo: Permission denied\n')
      expect(refused.refusal?.reason).toMatch(/profile 'release' policy failed/)
      expect((await ws.execute('echo hi')).exitCode).toBe(0)
    } finally {
      await ws.close()
    }
  })

  it('an engine that cannot evaluate fails closed', async () => {
    const ws = await build(scripted(JUDGE, 'vfs'))
    try {
      ws.createSession('s', { profile: 'release' })
      const refused = await ws.execute('echo hi', { sessionId: 's' })
      expect(refused.exitCode).toBe(126)
      expect(refused.stderrText).toBe('echo: Permission denied\n')
      expect(refused.refusal?.reason).toMatch(/cannot evaluate one/)
    } finally {
      await ws.close()
    }
  })

  it('a profile policy states its runtime', async () => {
    await expect(
      build({ release: { policy: { script: new ScriptSource(JUDGE, 'js') } } }),
    ).rejects.toThrow(/runtime names the engine the policy/)
  })

  it('the old script and runtime keys are told the new block', async () => {
    // Shipped first as `script` with `runtime` beside it, a word for
    // what the file is rather than what it does and an engine that
    // read as the profile's own; the refusal says where they went.
    await expect(
      build({ release: { script: new ScriptSource(JUDGE, 'js'), runtime: 'quickjs' } }),
    ).rejects.toThrow(/now one policy block/)
  })

  it('an inline document may not add a policy', async () => {
    const ws = await build(scripted())
    try {
      expect(() =>
        ws.createSession('s', {
          permissions: { policy: { script: new ScriptSource(JUDGE, 'js'), runtime: 'quickjs' } },
        }),
      ).toThrow(/not a policy/)
    } finally {
      await ws.close()
    }
  })
})
