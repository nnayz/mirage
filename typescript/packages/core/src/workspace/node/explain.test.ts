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

import { afterEach, describe, expect, it } from 'vitest'

import { Outcome, Scope } from '../../policy/index.ts'
import type { Action, CommandContext, Policy } from '../../policy/index.ts'
import { RAMResource } from '../../resource/ram/ram.ts'
import { MountMode } from '../../types.ts'
import { getTestParser } from '../fixtures/workspace_fixture.ts'
import { parseSessionProfile } from '../../policy/profile.ts'
import { Workspace } from '../workspace/workspace.ts'

const DEC = new TextDecoder()

const PROFILE = parseSessionProfile({
  commands: {
    allow: ['ls', 'cat', 'git', 'rm', 'mkdir', 'cd', 'echo'],
    deny: [{ reason: 'production data is protected', commands: { rm: ['/data/prod/*'] } }],
    ask: [
      { reason: 'pushes need sign-off', commands: ['git push'] },
      { reason: 'secrets need sign-off', commands: { cat: ['/data/secret.txt'] } },
    ],
  },
})

const open: Workspace[] = []
afterEach(async () => {
  for (const w of open.splice(0)) await w.close()
})

async function ws(): Promise<Workspace> {
  const parser = await getTestParser()
  const w = new Workspace(
    { '/data': new RAMResource() },
    { mode: MountMode.WRITE, shellParser: parser, profiles: { r: PROFILE } },
  )
  open.push(w)
  await w.execute('mkdir -p /data/prod')
  await w.execute('echo x > /data/prod/x.txt')
  await w.execute('echo a > /data/a.txt')
  await w.execute('echo s > /data/secret.txt')
  w.createSession('s', { profile: 'r' })
  return w
}

describe('explain', () => {
  it('answers each verb and names the rule', async () => {
    const w = await ws()
    const [allowed] = await w.explain('cat /data/a.txt', 's')
    expect(allowed?.outcome).toBe(Outcome.ALLOW)
    expect(allowed?.exitCode).toBe(0)
    expect(allowed?.rule).toBeNull()

    const [denied] = await w.explain('rm /data/prod/x.txt', 's')
    expect(denied?.outcome).toBe(Outcome.DENY)
    expect(denied?.reason).toBe('production data is protected')
    expect(denied?.source).toBe('top')
    expect(denied?.matchedPath).toBe('/data/prod/x.txt')

    const [asked] = await w.explain('git push origin main', 's')
    expect(asked?.outcome).toBe(Outcome.ASK)
    expect(asked?.reason).toBe('pushes need sign-off')
  })

  it('reports a word the session cannot see as deny at 127', async () => {
    // Both refusals the allow list produces are DENY with no rule; the
    // exit code is what separates a head word the session cannot see
    // from a line no allow entry covers.
    const w = await ws()
    const [missing] = await w.explain('gerp x', 's')
    expect(missing?.outcome).toBe(Outcome.DENY)
    expect(missing?.rule).toBeNull()
    expect(missing?.source).toBe('commands.allow')
    expect(missing?.exitCode).toBe(127)
    expect(missing?.stderr).toBe('gerp: command not found\n')
  })

  it('reads every command of a line', async () => {
    const w = await ws()
    const [first, second] = await w.explain('cat /data/a.txt && rm /data/prod/x.txt', 's')
    expect([first?.command, first?.outcome]).toEqual(['cat', Outcome.ALLOW])
    expect([second?.command, second?.outcome]).toEqual(['rm', Outcome.DENY])
  })

  it('says exactly what the run would say', async () => {
    const w = await ws()
    for (const line of ['rm /data/prod/x.txt', 'git push origin main', 'gerp x']) {
      const ran = await w.execute(line, { sessionId: 's' })
      const [said] = await w.explain(line, 's')
      expect(said?.exitCode).toBe(ran.exitCode)
      expect(said?.stderr).toBe(DEC.decode(ran.stderr))
    }
  })

  it('spends nothing', async () => {
    // A dry run of an ask must not put the question to anyone, or the
    // host would field requests for lines nobody typed, and must not
    // spend a grant, or explaining a line would use up its answer.
    const w = await ws()
    for (let i = 0; i < 3; i += 1) await w.explain('git push origin main', 's')
    expect(w.decisions.pending()).toEqual([])
    expect(w.getSession('s').decisions).toEqual([])
    await w.explain('rm /data/prod/x.txt', 's')
    expect((await w.fs.readdir('/data')).sort()).toEqual([
      '/data/a.txt',
      '/data/prod',
      '/data/secret.txt',
    ])
  })

  it('stops the whole line when a rule denies one command', async () => {
    // The agent composed the line as one intent, so a rule refusing
    // part of it refuses the intent: judging each command as the
    // dispatcher reached it deleted the first file and refused the
    // second.
    const w = await ws()
    const ran = await w.execute('rm /data/a.txt && rm /data/prod/x.txt', { sessionId: 's' })
    expect(ran.exitCode).toBe(1)
    expect(DEC.decode(ran.stderr)).toBe('rm: /data/prod/x.txt: production data is protected\n')
    expect(await w.fs.readdir('/data')).toContain('/data/a.txt')
  })

  it('leaves a line alone when a word is simply not installed', async () => {
    // A head word the session cannot see is a routing miss, not a
    // verdict, so it stays bash. A typo must not cost an agent the work
    // the line already did.
    const w = await ws()
    const ran = await w.execute('rm /data/a.txt && gerp x', { sessionId: 's' })
    expect(ran.exitCode).toBe(127)
    expect(DEC.decode(ran.stderr)).toBe('gerp: command not found\n')
    expect(await w.fs.readdir('/data')).not.toContain('/data/a.txt')
  })

  it('holds a line with an asked command until it is answered', async () => {
    const w = await ws()
    const line = 'rm /data/a.txt && cat /data/secret.txt'
    const ran = await w.execute(line, { sessionId: 's' })
    expect(ran.exitCode).toBe(126)
    expect(await w.fs.readdir('/data')).toContain('/data/a.txt')
    // Exactly one request, from the one pass that judged the line.
    const [pending] = w.decisions.pending()
    expect(w.decisions.pending()).toHaveLength(1)
    await w.decisions.answer(pending?.id ?? '', Outcome.ALLOW, Scope.ONCE)
    // The whole line replays, which is only sound because none of it
    // ran the first time, and the grant is spent exactly once even
    // though two passes now read it.
    const again = await w.execute(line, { sessionId: 's' })
    expect(again.exitCode).toBe(0)
    expect(await w.fs.readdir('/data')).not.toContain('/data/a.txt')
    expect(w.decisions.pending()).toEqual([])
  })

  it('moves what later rules read when the line begins with a cd', async () => {
    // The line is judged before it runs, so the pass has to walk a
    // literal `cd` itself or a rule about /data/prod would answer about
    // whatever directory the session happened to be in.
    const w = await ws()
    const ran = await w.execute('cd /data/prod && rm x.txt', { sessionId: 's' })
    expect(ran.exitCode).toBe(1)
    expect(DEC.decode(ran.stderr)).toBe('rm: x.txt: production data is protected\n')
  })

  it('reads a cd the same way the run does', async () => {
    // explain and the pass that decides the line share one walk, so a
    // host asking about a line and the agent typing it cannot be told
    // different things about where the line ends up.
    const w = await ws()
    const line = 'cd /data/prod && rm x.txt'
    const [, removed] = await w.explain(line, 's')
    expect(removed?.outcome).toBe(Outcome.DENY)
    const ran = await w.execute(line, { sessionId: 's' })
    expect(removed?.exitCode).toBe(ran.exitCode)
    expect(removed?.stderr).toBe(DEC.decode(ran.stderr))
  })

  it('holds a line only as far as the text reaches', async () => {
    // The pass reads the text of a line and the gate reads its values,
    // so a path the runtime computes is invisible here and the hold
    // lapses. The ask still fires, at the gate, once the earlier
    // commands have run. Pinned rather than only documented, because
    // the cost lands on the replay: approving this re-runs a line whose
    // first half is already done.
    const w = await ws()
    const ran = await w.execute('S=/data/secret.txt; rm /data/a.txt && cat $S', {
      sessionId: 's',
    })
    expect(ran.exitCode).toBe(126)
    expect(await w.fs.readdir('/data')).not.toContain('/data/a.txt')
    expect(w.decisions.pending()).toHaveLength(1)
  })

  it('does not move later commands for a cd inside a subshell', async () => {
    // bash restores the cwd when the subshell exits, so carrying the cd
    // past it refused a line that was never going to touch /data/prod.
    const w = await ws()
    const ran = await w.execute('(cd /data/prod && ls) && rm x.txt', { sessionId: 's' })
    expect(ran.exitCode).toBe(1)
    expect(DEC.decode(ran.stderr)).not.toContain('production data is protected')
    expect(await w.fs.readdir('/data/prod')).toContain('/data/prod/x.txt')
  })

  it('shows a line running once the session holds a grant', async () => {
    const w = await ws()
    const ran = await w.execute('git push origin main', { sessionId: 's' })
    expect(ran.exitCode).toBe(126)
    const [pending] = w.decisions.pending()
    await w.decisions.answer(pending?.id ?? '', Outcome.ALLOW, Scope.SESSION)
    // The document still says ask, because that is what it says; the
    // exit code says 0, because that is what the line would now do.
    const [asked] = await w.explain('git push origin main', 's')
    expect(asked?.outcome).toBe(Outcome.ASK)
    expect(asked?.exitCode).toBe(0)
    expect(asked?.stderr).toBe('')
  })
})

const SEALED = parseSessionProfile({
  commands: {
    allow: ['ls', 'cat', 'rm', 'mkdir', 'echo'],
    deny: [{ reason: 'sealed until review', paths: ['/data/prod/*'] }],
  },
})

async function sealedWs(): Promise<Workspace> {
  const parser = await getTestParser()
  const w = new Workspace(
    { '/data': new RAMResource() },
    { mode: MountMode.WRITE, shellParser: parser, profiles: { r: SEALED } },
  )
  open.push(w)
  await w.execute('mkdir -p /data/prod')
  await w.execute('echo x > /data/prod/x.txt')
  await w.execute('echo a > /data/a.txt')
  w.createSession('s', { profile: 'r' })
  return w
}

class NoCat implements Policy {
  preCommand(ctx: CommandContext): Action | null {
    if (ctx.command !== 'cat') return null
    return { kind: 'deny', reason: 'cat is refused by policy', scope: 'command' }
  }
}

describe('prejudge', () => {
  it('reads the statement’s redirect target', async () => {
    // The shell opens a redirect on its own fd, outside the window the
    // command's own gate covers, so admission reads the target as a word
    // of the command. The dry run has to read it the same way or it
    // answers ALLOW for a line the run refuses.
    const w = await sealedWs()
    const [said] = await w.explain('echo x > /data/prod/x.txt', 's')
    expect(said?.outcome).toBe(Outcome.DENY)
    expect(said?.reason).toBe('sealed until review')
  })

  it('holds the whole line for a rule on a redirect target', async () => {
    // `a && b > f` parses as one redirected statement wrapping the list,
    // so the target belongs to the last command of the chain, not the
    // statement's first child.
    const w = await sealedWs()
    const ran = await w.execute('rm /data/a.txt && echo x > /data/prod/x.txt', { sessionId: 's' })
    expect(ran.exitCode).not.toBe(0)
    expect(DEC.decode(ran.stderr)).toContain('sealed until review')
    expect(await w.fs.readdir('/data')).toContain('/data/a.txt')
  })

  it('holds the line for a coded policy with no document', async () => {
    // A session with no permissions document still runs under whatever
    // policies the deployment registered, and one is always registered
    // (MountRootPolicy). Returning early on `commands === null` held the
    // line for a document and left a policy with the half-line behavior
    // the pass exists to remove.
    const parser = await getTestParser()
    const w = new Workspace(
      { '/data': new RAMResource() },
      { mode: MountMode.WRITE, shellParser: parser, policies: [new NoCat()] },
    )
    open.push(w)
    await w.execute('echo a > /data/a.txt')
    await w.execute('echo b > /data/b.txt')
    const ran = await w.execute('rm /data/a.txt && cat /data/b.txt')
    expect(ran.exitCode).not.toBe(0)
    expect(DEC.decode(ran.stderr)).toBe('cat: Permission denied\n')
    expect(ran.refusal?.reason).toBe('cat is refused by policy')
    expect(await w.fs.readdir('/data')).toContain('/data/a.txt')
  })

  it('does not end the scan when an ask is answered inline', async () => {
    // The host answers the cat inline, so admit admits it. The rest of
    // the line has not been judged yet: reading that admission as "the
    // line is fine" let the later deny run behind an approval, which is
    // the half-line behavior in its worst form, since the agent was told
    // yes.
    const parser = await getTestParser()
    const w = new Workspace(
      { '/data': new RAMResource() },
      {
        mode: MountMode.WRITE,
        shellParser: parser,
        profiles: { r: PROFILE },
        onAsk: (record) =>
          Promise.resolve({ ...record, outcome: Outcome.ALLOW, scope: Scope.ONCE }),
      },
    )
    open.push(w)
    await w.execute('mkdir -p /data/prod')
    await w.execute('echo x > /data/prod/x.txt')
    await w.execute('echo s > /data/secret.txt')
    w.createSession('s', { profile: 'r' })
    const ran = await w.execute('cat /data/secret.txt && rm /data/prod/x.txt', { sessionId: 's' })
    expect(ran.exitCode).not.toBe(0)
    expect(DEC.decode(ran.stderr)).toContain('production data is protected')
    expect(DEC.decode(ran.stdout)).not.toContain('s')
    expect(await w.fs.readdir('/data/prod')).toContain('/data/prod/x.txt')
  })
})

describe('prejudge scope', () => {
  it('moves the commands inside a subshell when it begins with a cd', async () => {
    // The sibling test pins that the cd does not escape the subshell.
    // This one pins the other half: inside it, the cd still applies, so
    // the rule about /data/prod reads x.txt as the file it is. Reading a
    // subshell as "no cd applies" judged the command at the session cwd.
    const w = await ws()
    const ran = await w.execute('(cd /data/prod && rm x.txt)', { sessionId: 's' })
    expect(ran.exitCode).toBe(1)
    expect(DEC.decode(ran.stderr)).toBe('rm: x.txt: production data is protected\n')
    expect(await w.fs.readdir('/data/prod')).toContain('/data/prod/x.txt')
  })

  it('refuses a one-command line from inside the shell', async () => {
    // Nothing runs before a single command, so there is no hold to buy
    // and the per-command gate answers instead. That is the more
    // faithful answer, not just the cheaper one: the gate refuses from
    // inside the shell, so `2>&1` still moves the message, where this
    // pass answers above the redirect layer and would leave it on
    // stderr.
    const w = await ws()
    const ran = await w.execute('rm /data/prod/x.txt 2>&1', { sessionId: 's' })
    expect(ran.exitCode).toBe(1)
    expect(DEC.decode(ran.stdout)).toBe('rm: /data/prod/x.txt: production data is protected\n')
    expect(DEC.decode(ran.stderr)).toBe('')
  })
})
