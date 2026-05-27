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

import type { PathSpec } from '../../types.ts'
import type { S3Accessor } from '../../accessor/s3.ts'
import { loadS3Module, rawPathOf, s3Prefix, withClient } from './_client.ts'

export async function rmR(accessor: S3Accessor, path: PathSpec): Promise<void> {
  // Recursively delete every object under the prefix.
  // Identical to rmdir in S3 semantics — S3 has no real directories, so
  // "rm -r" and "rmdir" both mean "delete every object whose key starts
  // with this prefix".
  const { DeleteObjectsCommand, ListObjectsV2Command } = await loadS3Module(accessor.config)
  const raw = rawPathOf(path)
  const pfx = s3Prefix(raw, accessor.config)
  await withClient(accessor.config, async (client) => {
    let continuationToken: string | undefined
    do {
      const input: Record<string, unknown> = {
        Bucket: accessor.config.bucket,
        Prefix: pfx,
      }
      if (continuationToken !== undefined) input.ContinuationToken = continuationToken
      const resp = (await client.send(new ListObjectsV2Command(input))) as {
        Contents?: { Key?: string }[]
        IsTruncated?: boolean
        NextContinuationToken?: string
      }
      const keys = (resp.Contents ?? [])
        .map((obj) => obj.Key)
        .filter((k): k is string => k !== undefined)
        .map((k) => ({ Key: k }))
      if (keys.length > 0) {
        await client.send(
          new DeleteObjectsCommand({
            Bucket: accessor.config.bucket,
            Delete: { Objects: keys },
          }),
        )
      }
      continuationToken = resp.IsTruncated === true ? resp.NextContinuationToken : undefined
    } while (continuationToken !== undefined)
  })
}
