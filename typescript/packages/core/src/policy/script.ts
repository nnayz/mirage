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

import { CommandTimeoutError } from '../commands/errors.ts'
import type { Runtime } from '../runtime/base.ts'
import { EvalError } from '../runtime/errors.ts'
import { LanguageRuntime } from '../runtime/language.ts'
import type { Evaluator } from '../runtime/mixin.ts'
import type { MountResolver } from '../runtime/resolver.ts'
import type { ScriptSource } from '../runtime/routing/types.ts'
import { evalWithCtx, scriptEngine } from '../runtime/script.ts'
import type { BridgeDispatchFn, EvalValue } from '../runtime/types.ts'
import type { Policy } from './base.ts'
import {
  DEFAULT_ASK_REASON,
  DEFAULT_DENY_REASON,
  SCRIPT_EVAL_TIMEOUT_SECONDS,
} from './constants.ts'
import type {
  Action,
  Ask,
  CommandContext,
  Deny,
  ProfileScript,
  SessionScriptsQuery,
} from './types.ts'

/**
 * What a profile's script is told about one command: the
 * `CommandContext` the coded hooks read, as plain data.
 *
 * The same facts on both hosts, JSON-shaped because the script runs
 * inside a sandboxed engine that a live object cannot cross into.
 * Paths are spelled as resolved virtual paths, so a script matches what
 * the command will actually touch, not what was typed; the raw words
 * are in `argv` for a script that wants them.
 */
export function scriptContext(
  profile: string,
  ctx: CommandContext,
  mounts: readonly string[],
): Record<string, EvalValue> {
  return {
    profile,
    command: {
      name: ctx.command,
      argv: [...ctx.argv],
      tokens: [...(ctx.tokens ?? [])],
      program: [...(ctx.program ?? [])],
      paths: ctx.paths.map((path) => path.virtual),
      operands: (ctx.operands ?? []).map((path) => path.virtual),
      tool: ctx.tool ?? true,
      walks: ctx.walks ?? false,
    },
    session: {
      id: ctx.sessionId ?? '',
      agent: ctx.agentId ?? '',
      cwd: ctx.cwd,
    },
    mounts: [...mounts],
  }
}

/**
 * The policy answer a policy's hook returns.
 *
 * The vocabulary is the `preCommand` hook's own, spelled as data: null
 * or `'allow'` is no opinion (the command runs unless another rule
 * refuses it, and can never override one that does), `'deny'` /
 * `{deny: reason}` refuses, `'ask'` / `{ask: reason}` takes the line to
 * the approval door. The bare strings carry the document's default
 * reasons, the same ones a rule stating no reason gets.
 *
 * Throws a plain Error whose message is a clause about "script", for
 * the caller to prefix with whose policy it is.
 */
export function scriptAction(value: EvalValue): Deny | Ask | null {
  if (value === null || value === 'allow') return null
  if (value === 'deny') return { kind: 'deny', reason: DEFAULT_DENY_REASON }
  if (value === 'ask') return { kind: 'ask', reason: DEFAULT_ASK_REASON }
  if (typeof value === 'object' && !Array.isArray(value) && !(value instanceof Uint8Array)) {
    const entries = Object.entries(value)
    if (entries.length === 1) {
      const first = entries[0]
      if (first !== undefined) {
        const [verb, reason] = first
        if ((verb === 'deny' || verb === 'ask') && typeof reason === 'string' && reason !== '') {
          return { kind: verb, reason }
        }
      }
    }
  }
  throw new Error(
    `script must answer allow, deny or ask: null or 'allow', 'deny', 'ask', ` +
      `{deny: reason} or {ask: reason}; got ${JSON.stringify(value)}`,
  )
}

/**
 * The workspace's file doors, for the engine a profile policy runs on.
 *
 * A policy judges a line before it runs, and some judgments are about
 * what a file holds rather than what it is called. The engine is
 * attached with these exactly as `Runtimes` attaches an agent's, so
 * the policy's `open()` reads the mounts through the same door an
 * agent's program would, and a read from a policy clears the op door
 * like any other. The workspace supplies them; a bare ScriptPolicy
 * (outside a workspace) has none, and its programs see no file.
 */
export interface ScriptWiring {
  bridge: () => BridgeDispatchFn
  resolver: MountResolver
}

/**
 * The call that runs a policy's hook, in its language's own spelling.
 *
 * A policy program defines the hook it answers at, the way a coded
 * Policy does: `pre_command(ctx)` in python, `preCommand(ctx)` in
 * JavaScript, returning the verdict. The program is evaluated whole,
 * with this call appended as its last expression, so the definitions
 * run and the call's return is what the evaluator hands back. A program
 * defining no hook fails at the call (a NameError), and fails closed.
 */
export function hookCall(script: ScriptSource): string {
  return script.language === 'js' ? 'preCommand(ctx)' : 'pre_command(ctx)'
}

/**
 * Each profile's policy, enforced at the admission gate.
 *
 * The scripted twin of `PermissionsPolicy`, registered right after it:
 * where that policy evaluates the document's declarative rules, this
 * one calls the profile's policy program, per command, with the same
 * facts (`scriptContext`). It reads the session's policy through the
 * narrow `SessionScriptsQuery` by the session id the door put in the
 * context, so a session whose profile states no policy costs one lookup
 * and nothing else.
 *
 * The facts name the paths; the engine can open them. It is wired to
 * the workspace's files the way an agent's runtime is (`ScriptWiring`),
 * so a policy may read what an operand holds and answer for its
 * content, not only its name.
 *
 * Every failure fails closed: a policy that threw, timed out, answered
 * with the wrong shape, defines no hook, or names an engine that cannot
 * be built refuses the command with a reason naming the profile.
 * Silence on failure would run exactly the commands the policy existed
 * to judge.
 *
 * Engines are built lazily on the first command that needs one, shared
 * per engine name, and closed by the workspace's own close.
 * Evaluations are serialized: the engines are workers, and two
 * concurrent evals on one would interleave.
 */
export class ScriptPolicy implements Policy {
  private readonly sessions: SessionScriptsQuery
  private readonly mounts: () => readonly string[]
  private readonly wiring: ScriptWiring | null
  private readonly engines = new Map<string, Runtime & Evaluator>()
  private queue: Promise<unknown> = Promise.resolve()

  constructor(
    sessions: SessionScriptsQuery,
    mounts: () => readonly string[],
    wiring: ScriptWiring | null = null,
  ) {
    this.sessions = sessions
    this.mounts = mounts
    this.wiring = wiring
  }

  async preCommand(ctx: CommandContext): Promise<Action | null> {
    const entry = this.sessions.scriptOf(ctx.sessionId ?? '')
    if (entry === null) return null
    let value: EvalValue
    try {
      value = await this.evaluate(entry, ctx)
    } catch (err) {
      if (err instanceof CommandTimeoutError) {
        return failed(entry, `timed out after ${String(SCRIPT_EVAL_TIMEOUT_SECONDS)}s`)
      }
      if (err instanceof EvalError) {
        return failed(entry, `${err.syntax ? 'syntax error' : 'failed'}: ${err.message}`)
      }
      // scriptEngine's refusal (the engine cannot be built) is a clause
      // about "script"; the profile's word for it is policy.
      return failed(entry, clause(err))
    }
    try {
      return scriptAction(value)
    } catch (err) {
      return failed(entry, clause(err))
    }
  }

  /** Close every engine a script was evaluated on. */
  async close(): Promise<void> {
    const engines = [...this.engines.values()]
    this.engines.clear()
    for (const engine of engines) await engine.close()
  }

  /** One command through one profile's policy, serialized. */
  private async evaluate(entry: ProfileScript, ctx: CommandContext): Promise<EvalValue> {
    const run = this.queue.then(async () => {
      let engine = this.engines.get(entry.runtime)
      if (engine === undefined) {
        engine = scriptEngine(entry.script, entry.runtime)
        // Attached before the first eval, as `Runtimes` attaches an
        // agent's engine: the script's `open()` then reads the mounts
        // through the same door, and an unattached engine sees no file.
        if (this.wiring !== null && engine instanceof LanguageRuntime) {
          engine.attach(this.wiring.bridge(), this.wiring.resolver)
        }
        this.engines.set(entry.runtime, engine)
      }
      return evalWithCtx(
        `${entry.script.source}\n\n${hookCall(entry.script)}\n`,
        scriptContext(entry.profile, ctx, this.mounts()),
        engine,
        SCRIPT_EVAL_TIMEOUT_SECONDS,
        `profile '${entry.profile}' policy`,
      )
    })
    this.queue = run.catch(() => undefined)
    return run
  }
}

/** The fail-closed refusal: one wording however the policy broke. */
function failed(entry: ProfileScript, detail: string): Deny {
  return { kind: 'deny', reason: `profile '${entry.profile}' policy ${detail}` }
}

/**
 * An error's message as the clause after "policy": the engine door and
 * the answer reader both speak of "script", which is the program's
 * generic name, and the profile's word for its program is policy.
 */
function clause(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err)
  return message.replace(/^script /, '')
}
