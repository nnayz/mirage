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

import { describe, expect, it } from 'vitest'

import { resolveQdrantConfig } from '../../resource/qdrant/config.ts'
import { fieldValue, groupName, pointIdFromStem, rowStem, withoutField } from './fields.ts'

describe('qdrant payload fields', () => {
  it('reads dotted keys with Qdrant nested-field semantics', () => {
    expect(fieldValue({ metadata: { source: 'nested' } }, 'metadata.source')).toBe('nested')
    expect(
      fieldValue(
        { 'metadata.source': 'literal', metadata: { source: 'nested' } },
        'metadata.source',
      ),
    ).toBe('nested')
  })

  it('renders a source URL as its basename', () => {
    expect(groupName('s3://docs/policies/refund-2026.pdf', true)).toBe('refund-2026.pdf')
  })

  it('keeps the point id in a payload-derived filename', () => {
    const config = resolveQdrantConfig({ nameField: 'metadata.page' })
    const stem = rowStem({ id: 17, metadata: { page: '004' } }, config)
    expect(stem).toBe('004__17')
    expect(pointIdFromStem(stem, config)).toBe('17')
    expect(pointIdFromStem('18', config)).toBe('18')
  })

  it('removes nested render-only fields without mutating the row', () => {
    const row = { metadata: { source: 'report.pdf', blob: 'bytes' } }
    expect(withoutField(row, 'metadata.blob')).toEqual({ metadata: { source: 'report.pdf' } })
    expect(row.metadata.blob).toBe('bytes')
  })
})
