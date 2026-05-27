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

import type { FindOptions } from '../../resource/base.ts'
import type { PathSpec } from '../../types.ts'
import type { S3Accessor } from '../../accessor/s3.ts'
import {
  fnmatch,
  loadS3Module,
  rawPathOf,
  s3Prefix,
  stripKeyPrefix,
  withClient,
} from './_client.ts'

export async function find(
  accessor: S3Accessor,
  path: PathSpec,
  options: FindOptions = {},
): Promise<string[]> {
  const { ListObjectsV2Command } = await loadS3Module(accessor.config)
  const raw = rawPathOf(path)
  const pfx = s3Prefix(raw, accessor.config)
  const results: string[] = []
  await withClient(accessor.config, async (client) => {
    let continuationToken: string | undefined
    do {
      const input: Record<string, unknown> = {
        Bucket: accessor.config.bucket,
        Prefix: pfx,
      }
      if (continuationToken !== undefined) input.ContinuationToken = continuationToken
      const resp = (await client.send(new ListObjectsV2Command(input))) as {
        Contents?: { Key?: string; Size?: number; LastModified?: Date }[]
        IsTruncated?: boolean
        NextContinuationToken?: string
      }
      for (const obj of resp.Contents ?? []) {
        const key = obj.Key
        if (key === undefined || key === pfx) continue
        const relative = key.slice(pfx.length)
        const depth = (relative.match(/\//g) ?? []).length
        if (
          options.maxDepth !== null &&
          options.maxDepth !== undefined &&
          depth > options.maxDepth
        ) {
          continue
        }
        if (
          options.minDepth !== null &&
          options.minDepth !== undefined &&
          depth < options.minDepth
        ) {
          continue
        }
        const entryName = key.split('/').pop() ?? ''
        if (
          options.orNames !== null &&
          options.orNames !== undefined &&
          options.orNames.length > 0
        ) {
          if (!options.orNames.some((pat) => fnmatch(entryName, pat))) continue
        } else if (options.name !== null && options.name !== undefined) {
          if (!fnmatch(entryName, options.name)) continue
        }
        if (options.iname !== null && options.iname !== undefined) {
          if (!fnmatch(entryName.toLowerCase(), options.iname.toLowerCase())) continue
        }
        const fullPath = '/' + stripKeyPrefix(key, accessor.config)
        if (options.pathPattern !== null && options.pathPattern !== undefined) {
          if (!fnmatch(fullPath, options.pathPattern)) continue
        }
        if (options.nameExclude !== null && options.nameExclude !== undefined) {
          if (fnmatch(entryName, options.nameExclude)) continue
        }
        if (options.type === 'f' && key.endsWith('/')) continue
        if (options.type === 'd' && !key.endsWith('/')) continue
        const size = obj.Size ?? 0
        if (options.minSize !== null && options.minSize !== undefined && size < options.minSize) {
          continue
        }
        if (options.maxSize !== null && options.maxSize !== undefined && size > options.maxSize) {
          continue
        }
        results.push(fullPath)
      }
      continuationToken = resp.IsTruncated === true ? resp.NextContinuationToken : undefined
    } while (continuationToken !== undefined)
  })
  return results.sort()
}
