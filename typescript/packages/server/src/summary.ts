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

import { normMountPrefix, type Resource, type Workspace } from '@struktoai/mirage-core'
import type { WorkspaceEntry } from './registry.ts'
import type {
  MountSummary,
  SessionSummary,
  WorkspaceBrief,
  WorkspaceDetail,
  WorkspaceInternals,
} from './schemas.ts'

const AUTO_PREFIXES = new Set(['/dev/'])
const DESCRIPTION_MAX = 120

function isAutoPrefix(prefix: string, observerPrefix: string): boolean {
  if (AUTO_PREFIXES.has(prefix)) return true
  if (prefix === normMountPrefix(observerPrefix)) return true
  return false
}

function userMounts(ws: Workspace) {
  const observerPrefix = ws.observer.prefix
  return ws.mounts().filter((m) => !isAutoPrefix(m.prefix, observerPrefix))
}

function describeResource(resource: Resource): string {
  const raw = resource.prompt ?? ''
  if (raw.length <= DESCRIPTION_MAX) return raw
  return raw.slice(0, DESCRIPTION_MAX - 1).trimEnd() + '\u2026'
}

function buildInternals(ws: Workspace): WorkspaceInternals {
  const cache = ws.cache as typeof ws.cache & { snapshotEntries?: () => unknown[] }
  return {
    cacheBytes: cache.cacheSize,
    cacheEntries: cache.snapshotEntries?.().length ?? 0,
    historyLength: ws.history.entries().length,
    inFlightJobs: ws.jobTable.listJobs().length,
  }
}

export function makeBrief(entry: WorkspaceEntry): WorkspaceBrief {
  const ws = entry.runner.ws
  const mounts = userMounts(ws)
  return {
    id: entry.id,
    mode: mounts[0]?.mode ?? 'read',
    mountCount: mounts.length,
    sessionCount: ws.listSessions().length,
    createdAt: entry.createdAt,
  }
}

export function makeDetail(entry: WorkspaceEntry, verbose = false): WorkspaceDetail {
  const ws = entry.runner.ws
  const mounts = userMounts(ws)
  const mountSummaries: MountSummary[] = mounts.map((m) => ({
    prefix: m.prefix,
    resource: m.resource.kind,
    mode: m.mode,
    description: describeResource(m.resource),
  }))
  const sessions: SessionSummary[] = ws.listSessions().map((s) => ({
    sessionId: s.sessionId,
    cwd: s.cwd,
  }))
  return {
    id: entry.id,
    mode: mounts[0]?.mode ?? 'read',
    createdAt: entry.createdAt,
    mounts: mountSummaries,
    sessions,
    internals: verbose ? buildInternals(ws) : null,
  }
}
