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

import type { S3Accessor } from '../../../accessor/s3.ts'
import { read as s3Read } from '../../../core/s3/read.ts'
import { readdir as s3Readdir } from '../../../core/s3/readdir.ts'
import { resolveGlob } from '../../../core/s3/glob.ts'
import { stat as s3Stat } from '../../../core/s3/stat.ts'
import { stream as s3Stream } from '../../../core/s3/stream.ts'
import { exitOnEmpty } from '../../../io/stream.ts'
import { IOResult, type ByteSource } from '../../../io/types.ts'
import { FileType, PathSpec, ResourceName, type FileStat } from '../../../types.ts'
import { command, type CommandFnResult, type CommandOpts } from '../../config.ts'
import { specOf } from '../../spec/builtins.ts'
import { compilePattern, grepLines, grepStream } from '../grep_helper.ts'
import { rgFolderFiletype, rgFull } from '../rg_helper.ts'
import { resolveSource } from '../utils/stream.ts'

const ENC = new TextEncoder()
const DEC = new TextDecoder('utf-8', { fatal: false })

interface RgFlags {
  ignoreCase: boolean
  invert: boolean
  lineNumbers: boolean
  countOnly: boolean
  filesOnly: boolean
  wholeWord: boolean
  fixedString: boolean
  onlyMatching: boolean
  maxCount: number | null
  afterContext: number
  beforeContext: number
  fileType: string | null
  globPattern: string | null
  hidden: boolean
}

function parseRgFlags(flags: Record<string, string | boolean>): RgFlags {
  const toInt = (v: string | boolean | undefined): number | null =>
    typeof v === 'string' ? Number.parseInt(v, 10) : null
  const a = toInt(flags.A)
  const b = toInt(flags.B)
  const c = toInt(flags.C)
  return {
    ignoreCase: flags.i === true,
    invert: flags.v === true,
    lineNumbers: flags.n === true,
    countOnly: flags.c === true,
    filesOnly: flags.args_l === true,
    wholeWord: flags.w === true,
    fixedString: flags.F === true,
    onlyMatching: flags.o === true,
    maxCount: toInt(flags.m),
    afterContext: a ?? c ?? 0,
    beforeContext: b ?? c ?? 0,
    fileType: typeof flags.type === 'string' ? flags.type : null,
    globPattern: typeof flags.glob === 'string' ? flags.glob : null,
    hidden: flags.hidden === true,
  }
}

function splitLinesNoTrailing(text: string): string[] {
  const stripped = text.endsWith('\n') ? text.slice(0, -1) : text
  return stripped === '' ? [] : stripped.split('\n')
}

function makeSpec(path: string, template: PathSpec): PathSpec {
  return new PathSpec({
    original: path,
    directory: path,
    resolved: false,
    prefix: template.prefix,
  })
}

async function rgCommand(
  accessor: S3Accessor,
  paths: PathSpec[],
  texts: string[],
  opts: CommandOpts,
): Promise<CommandFnResult> {
  const [exprText] = texts
  if (exprText === undefined) {
    return [
      null,
      new IOResult({ exitCode: 2, stderr: ENC.encode('rg: usage: rg [flags] pattern [path]\n') }),
    ]
  }
  const flags = parseRgFlags(opts.flags)

  if (paths.length === 0) {
    const source = resolveSource(opts.stdin, 'rg: usage: rg [flags] pattern path')
    const pat = compilePattern(exprText, flags.ignoreCase, flags.fixedString, flags.wholeWord)
    const stream = grepStream(source, pat, {
      invert: flags.invert,
      lineNumbers: flags.lineNumbers,
      countOnly: flags.countOnly,
      onlyMatching: flags.onlyMatching,
      maxCount: flags.maxCount,
      afterContext: flags.afterContext,
      beforeContext: flags.beforeContext,
    })
    const io = new IOResult()
    return [exitOnEmpty(stream, io), io]
  }

  const resolved = await resolveGlob(accessor, paths, opts.index ?? undefined)
  const first = resolved[0]
  if (first === undefined) return [null, new IOResult()]

  let isDir = false
  try {
    const s = await s3Stat(accessor, first)
    isDir = s.type === FileType.DIRECTORY
  } catch {
    try {
      await s3Readdir(accessor, first, opts.index ?? undefined)
      isDir = true
    } catch {
      // not readable
    }
  }

  if (isDir && opts.filetypeFns !== null && Object.keys(opts.filetypeFns).length > 0) {
    const readdirFn = (p: string): Promise<string[]> =>
      s3Readdir(accessor, makeSpec(p, first), opts.index ?? undefined)
    const statFn = (p: string): Promise<FileStat> =>
      s3Stat(accessor, makeSpec(p, first), opts.index ?? undefined)
    const readBytesFn = (p: string): Promise<Uint8Array> => s3Read(accessor, makeSpec(p, first))
    const warnings: string[] = []
    const results = await rgFolderFiletype(
      readdirFn,
      statFn,
      readBytesFn,
      first.original,
      exprText,
      {},
      {
        ignoreCase: flags.ignoreCase,
        invert: flags.invert,
        lineNumbers: flags.lineNumbers,
        countOnly: flags.countOnly,
        filesOnly: flags.filesOnly,
        onlyMatching: flags.onlyMatching,
        maxCount: flags.maxCount,
        fixedString: flags.fixedString,
        wholeWord: flags.wholeWord,
        fileType: flags.fileType,
        globPattern: flags.globPattern,
        hidden: flags.hidden,
      },
      warnings,
    )
    const stderr = warnings.length > 0 ? ENC.encode(warnings.join('\n')) : undefined
    if (results.length === 0) {
      const io = new IOResult({ exitCode: 1, ...(stderr !== undefined ? { stderr } : {}) })
      return [new Uint8Array(0), io]
    }
    const out: ByteSource = ENC.encode(results.join('\n'))
    const io = new IOResult(stderr !== undefined ? { stderr } : {})
    return [out, io]
  }

  const needsFull =
    flags.filesOnly ||
    flags.beforeContext > 0 ||
    flags.afterContext > 0 ||
    flags.fileType !== null ||
    flags.globPattern !== null
  if (needsFull) {
    const readdirFn = (p: string): Promise<string[]> =>
      s3Readdir(accessor, makeSpec(p, first), opts.index ?? undefined)
    const statFn = (p: string): Promise<FileStat> =>
      s3Stat(accessor, makeSpec(p, first), opts.index ?? undefined)
    const readBytesFn = (p: string): Promise<Uint8Array> => s3Read(accessor, makeSpec(p, first))
    const warnings: string[] = []
    const results = await rgFull(
      readdirFn,
      statFn,
      readBytesFn,
      first.original,
      exprText,
      {
        ignoreCase: flags.ignoreCase,
        invert: flags.invert,
        lineNumbers: flags.lineNumbers,
        countOnly: flags.countOnly,
        filesOnly: flags.filesOnly,
        fixedString: flags.fixedString,
        onlyMatching: flags.onlyMatching,
        maxCount: flags.maxCount,
        wholeWord: flags.wholeWord,
        contextBefore: flags.beforeContext,
        contextAfter: flags.afterContext,
        fileType: flags.fileType,
        globPattern: flags.globPattern,
        hidden: flags.hidden,
      },
      warnings,
    )
    const stderr = warnings.length > 0 ? ENC.encode(warnings.join('\n')) : undefined
    if (results.length === 0) {
      const io = new IOResult({ exitCode: 1, ...(stderr !== undefined ? { stderr } : {}) })
      return [new Uint8Array(0), io]
    }
    const out: ByteSource = ENC.encode(results.join('\n'))
    const io = new IOResult(stderr !== undefined ? { stderr } : {})
    return [out, io]
  }

  const pat = compilePattern(exprText, flags.ignoreCase, flags.fixedString, flags.wholeWord)

  if (resolved.length > 1) {
    const allResults: string[] = []
    for (const p of resolved) {
      const data = splitLinesNoTrailing(DEC.decode(await s3Read(accessor, p)))
      const hits = grepLines(p.original, data, pat, {
        invert: flags.invert,
        lineNumbers: flags.lineNumbers,
        countOnly: flags.countOnly,
        filesOnly: flags.filesOnly,
        onlyMatching: flags.onlyMatching,
        maxCount: flags.maxCount,
      })
      if (flags.countOnly) {
        if (hits.length > 0) allResults.push(`${p.original}:${hits[0] ?? ''}`)
      } else if (flags.filesOnly) {
        for (const h of hits) allResults.push(h)
      } else {
        for (const h of hits) allResults.push(`${p.original}:${h}`)
      }
    }
    if (allResults.length === 0) return [new Uint8Array(0), new IOResult({ exitCode: 1 })]
    const out: ByteSource = ENC.encode(allResults.join('\n'))
    return [out, new IOResult()]
  }

  const source = s3Stream(accessor, first)
  const stream = grepStream(source, pat, {
    invert: flags.invert,
    lineNumbers: flags.lineNumbers,
    countOnly: flags.countOnly,
    onlyMatching: flags.onlyMatching,
    maxCount: flags.maxCount,
    afterContext: flags.afterContext,
    beforeContext: flags.beforeContext,
  })
  const io = new IOResult()
  return [exitOnEmpty(stream, io), io]
}

export const S3_RG = command({
  name: 'rg',
  resource: ResourceName.S3,
  spec: specOf('rg'),
  fn: rgCommand,
})
