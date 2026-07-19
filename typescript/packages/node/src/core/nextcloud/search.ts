import { XMLParser } from 'fast-xml-parser'
import { SyntaxValidator } from 'fast-xml-validator'
import type { PathSpec, PredNode } from '@struktoai/mirage-core'
import type { NextcloudAccessor } from '../../accessor/nextcloud.ts'
import { rawPathOf } from './util.ts'

const Namespace = {
  DAV: 'DAV:',
  OWNCLOUD: 'http://owncloud.org/ns',
  SEARCHDAV: 'https://github.com/icewind1991/SearchDAV/ns',
} as const

const Comparison = {
  EQUAL: 'eq',
  GREATER_THAN_OR_EQUAL: 'gte',
  LESS_THAN_OR_EQUAL: 'lte',
  LIKE: 'like',
} as const

const BooleanOperator = {
  AND: 'and',
  OR: 'or',
} as const

interface Property {
  namespace: (typeof Namespace)[keyof typeof Namespace]
  prefix: 'd' | 'oc'
  name: string
}

export interface FilesSearchQuery {
  tree: PredNode
  minSize?: number | null
  maxSize?: number | null
  mtimeMin?: number | null
  mtimeMax?: number | null
}

export interface SearchEntry {
  key: string
  name: string
  kind: 'f' | 'd'
  size: number | null
  modified: number | null
}

export interface SearchTarget {
  endpoint: string
  resourceScope: string
}

interface CompiledPredicate {
  condition: string | null
}

type ComparisonOperator = (typeof Comparison)[keyof typeof Comparison]
type BooleanOperation = (typeof BooleanOperator)[keyof typeof BooleanOperator]
type XmlRecord = Record<string, unknown>

const DISPLAY_NAME: Property = { namespace: Namespace.DAV, prefix: 'd', name: 'displayname' }
const RESOURCE_TYPE: Property = { namespace: Namespace.DAV, prefix: 'd', name: 'resourcetype' }
const CONTENT_LENGTH: Property = {
  namespace: Namespace.DAV,
  prefix: 'd',
  name: 'getcontentlength',
}
const LAST_MODIFIED: Property = {
  namespace: Namespace.DAV,
  prefix: 'd',
  name: 'getlastmodified',
}
const SIZE: Property = { namespace: Namespace.OWNCLOUD, prefix: 'oc', name: 'size' }
const SELECT_PROPERTIES = [DISPLAY_NAME, RESOURCE_TYPE, CONTENT_LENGTH, LAST_MODIFIED, SIZE]
const ORDER_PROPERTIES = [DISPLAY_NAME, LAST_MODIFIED, SIZE]
const SEARCH_ENDPOINT_PATH = '/remote.php/dav/'
const SEARCH_PAGE_SIZE = 100
const UNAVAILABLE_STATUS_CODES = new Set([404, 405, 501])

const parser = new XMLParser({
  ignoreAttributes: false,
  removeNSPrefix: true,
  parseTagValue: false,
  trimValues: false,
})

function escapeXml(value: string | number): string {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&apos;')
}

function propertyTag(field: Property): string {
  return `<${field.prefix}:${field.name}/>`
}

function property(field: Property): string {
  return `<d:prop>${propertyTag(field)}</d:prop>`
}

function comparison(
  operation: ComparisonOperator,
  field: Property,
  value: string | number,
): string {
  return `<d:${operation}>${property(field)}<d:literal>${escapeXml(value)}</d:literal></d:${operation}>`
}

function isCollection(): string {
  return '<d:is-collection/>'
}

function negate(condition: string): string {
  return `<d:not>${condition}</d:not>`
}

function combine(operation: BooleanOperation, conditions: string[]): string {
  const [only] = conditions
  if (only !== undefined && conditions.length === 1) return only
  return `<d:${operation}>${conditions.join('')}</d:${operation}>`
}

export function globToLike(pattern: string): string {
  let translated = ''
  for (const character of pattern) {
    if (character === '*') translated += '%'
    else if (character === '?') translated += '_'
    else if (character === '\\') translated += '%'
    else translated += character
  }
  return translated
}

function nameCondition(node: Extract<PredNode, { op: 'name' }>): string {
  const wildcard = node.pattern.includes('*') || node.pattern.includes('?')
  const operation = wildcard || node.icase ? Comparison.LIKE : Comparison.EQUAL
  const value = operation === Comparison.LIKE ? globToLike(node.pattern) : node.pattern
  return comparison(operation, DISPLAY_NAME, value)
}

function compilePredicate(node: PredNode): CompiledPredicate | null {
  switch (node.op) {
    case 'true':
      return { condition: null }
    case 'name':
      return node.pattern.includes('[') ? null : { condition: nameCondition(node) }
    case 'type':
      if (node.kind === 'd') return { condition: isCollection() }
      if (node.kind === 'f') return { condition: negate(isCollection()) }
      return null
    case 'not': {
      const compiled = compilePredicate(node.kid)
      return compiled?.condition != null ? { condition: negate(compiled.condition) } : null
    }
    case 'and':
    case 'or': {
      const conditions: string[] = []
      for (const kid of node.kids) {
        const compiled = compilePredicate(kid)
        if (compiled === null) return null
        if (compiled.condition === null) {
          if (node.op === 'or') return null
          continue
        }
        conditions.push(compiled.condition)
      }
      if (conditions.length === 0) return node.op === 'and' ? { condition: null } : null
      const operation = node.op === 'and' ? BooleanOperator.AND : BooleanOperator.OR
      return { condition: combine(operation, conditions) }
    }
    case 'path':
    case 'empty':
      return null
  }
}

function sizeCondition(query: FilesSearchQuery): string | null {
  const bounds: string[] = []
  if (query.minSize != null && query.minSize === query.maxSize) {
    bounds.push(comparison(Comparison.EQUAL, SIZE, query.minSize))
  } else {
    if (query.minSize != null) {
      bounds.push(comparison(Comparison.GREATER_THAN_OR_EQUAL, SIZE, query.minSize))
    }
    if (query.maxSize != null) {
      bounds.push(comparison(Comparison.LESS_THAN_OR_EQUAL, SIZE, query.maxSize))
    }
  }
  if (bounds.length === 0) return null
  const fileBounds = combine(BooleanOperator.AND, [negate(isCollection()), ...bounds])
  const includesZero =
    (query.minSize == null || query.minSize <= 0) && (query.maxSize == null || query.maxSize >= 0)
  return includesZero ? combine(BooleanOperator.OR, [isCollection(), fileBounds]) : fileBounds
}

function whereCondition(query: FilesSearchQuery): string | null {
  const compiled = compilePredicate(query.tree)
  if (compiled === null) return null
  const conditions: string[] = []
  if (compiled.condition !== null) conditions.push(compiled.condition)
  const size = sizeCondition(query)
  if (size !== null) conditions.push(size)
  if (query.mtimeMin != null) {
    conditions.push(
      comparison(Comparison.GREATER_THAN_OR_EQUAL, LAST_MODIFIED, Math.floor(query.mtimeMin)),
    )
  }
  if (query.mtimeMax != null) {
    conditions.push(
      comparison(Comparison.LESS_THAN_OR_EQUAL, LAST_MODIFIED, Math.ceil(query.mtimeMax)),
    )
  }
  return conditions.length > 0 ? combine(BooleanOperator.AND, conditions) : null
}

export function supportsQuery(query: FilesSearchQuery): boolean {
  return whereCondition(query) !== null
}

function decodePath(path: string): string {
  try {
    return decodeURIComponent(path)
  } catch (error) {
    throw new Error(`invalid percent-encoding in Nextcloud path: ${path}`, { cause: error })
  }
}

export function searchTarget(url: string): SearchTarget | null {
  const parsed = new URL(url)
  const marker = parsed.pathname.indexOf(SEARCH_ENDPOINT_PATH)
  if (marker < 0) return null
  const davEnd = marker + SEARCH_ENDPOINT_PATH.length
  const relative = parsed.pathname.slice(davEnd).replace(/^\/+|\/+$/g, '')
  const parts = relative === '' ? [] : relative.split('/')
  if (parts.length < 2 || parts[0] !== 'files') return null
  const endpoint = new URL(parsed.toString())
  endpoint.pathname = parsed.pathname.slice(0, davEnd)
  endpoint.search = ''
  endpoint.hash = ''
  return {
    endpoint: endpoint.toString(),
    resourceScope: decodePath(`/${parts.join('/')}`),
  }
}

function scopePath(target: SearchTarget, path: PathSpec): string {
  const relative = rawPathOf(path).replace(/^\/+|\/+$/g, '')
  return relative === ''
    ? target.resourceScope
    : `${target.resourceScope.replace(/\/+$/, '')}/${relative}`
}

function order(field: Property): string {
  return `<d:order>${property(field)}<d:ascending/></d:order>`
}

export function requestBody(
  target: SearchTarget,
  path: PathSpec,
  query: FilesSearchQuery,
  offset: number,
): string {
  const condition = whereCondition(query)
  if (condition === null) throw new Error('Nextcloud Files Search requires a supported query')
  const selected = SELECT_PROPERTIES.map(propertyTag).join('')
  const ordered = ORDER_PROPERTIES.map(order).join('')
  return `<?xml version="1.0" encoding="UTF-8"?><d:searchrequest xmlns:d="${Namespace.DAV}" xmlns:oc="${Namespace.OWNCLOUD}" xmlns:sd="${Namespace.SEARCHDAV}"><d:basicsearch><d:select><d:prop>${selected}</d:prop></d:select><d:from><d:scope><d:href>${escapeXml(scopePath(target, path))}</d:href><d:depth>infinity</d:depth></d:scope></d:from><d:where>${condition}</d:where><d:orderby>${ordered}</d:orderby><d:limit><d:nresults>${String(SEARCH_PAGE_SIZE)}</d:nresults><sd:firstresult>${String(offset)}</sd:firstresult></d:limit></d:basicsearch></d:searchrequest>`
}

function records(value: unknown): XmlRecord[] {
  if (Array.isArray(value)) return value.filter(isRecord)
  return isRecord(value) ? [value] : []
}

function isRecord(value: unknown): value is XmlRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function stringValue(value: unknown): string | null {
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  return null
}

function successfulProperties(response: XmlRecord): XmlRecord[] {
  const properties: XmlRecord[] = []
  for (const propstat of records(response.propstat)) {
    const status = stringValue(propstat.status) ?? ''
    const fields = status.split(/\s+/)
    if (fields[1] === '200' && isRecord(propstat.prop)) properties.push(propstat.prop)
  }
  if (properties.length === 0) {
    throw new Error('Nextcloud Files Search result has no successful properties')
  }
  return properties
}

function findText(properties: XmlRecord[], field: Property): string | null {
  for (const propertySet of properties) {
    const value = stringValue(propertySet[field.name])
    if (value !== null) return value
  }
  return null
}

function hasCollection(properties: XmlRecord[]): boolean {
  return properties.some((propertySet) => {
    const resourceType = propertySet[RESOURCE_TYPE.name]
    return isRecord(resourceType) && 'collection' in resourceType
  })
}

function stripScope(path: string, scope: string): string | null {
  if (path === scope) return ''
  const prefix = `${scope.replace(/\/+$/, '')}/`
  return path.startsWith(prefix) ? path.slice(prefix.length) : null
}

export function relativePath(href: string, target: SearchTarget): string {
  const hrefPath = decodePath(new URL(href, target.endpoint).pathname).replace(/\/+$/, '')
  const resourceScope = target.resourceScope.replace(/\/+$/, '')
  let relative = stripScope(hrefPath, resourceScope)
  if (relative === null) {
    const davRoot = decodePath(new URL(target.endpoint).pathname).replace(/\/+$/, '')
    relative = stripScope(hrefPath, `${davRoot}${resourceScope}`)
  }
  if (relative === null) {
    throw new Error(`Nextcloud Files Search returned an out-of-scope href: ${href}`)
  }
  return relative !== '' ? `/${relative}` : '/'
}

function modifiedTimestamp(value: string | null): number | null {
  if (value === null) return null
  const milliseconds = Date.parse(value)
  if (Number.isNaN(milliseconds)) {
    throw new Error(`invalid Nextcloud Files Search timestamp: ${value}`)
  }
  return milliseconds / 1000
}

function entrySize(properties: XmlRecord[]): number | null {
  const value = findText(properties, SIZE) ?? findText(properties, CONTENT_LENGTH)
  if (value === null) return null
  const size = Number(value)
  if (!Number.isInteger(size)) throw new Error(`invalid Nextcloud Files Search size: ${value}`)
  return size
}

function parseResponse(response: XmlRecord, target: SearchTarget): SearchEntry {
  const href = stringValue(response.href)
  if (href === null) throw new Error('Nextcloud Files Search result is missing href')
  const properties = successfulProperties(response)
  const key = relativePath(href, target)
  return {
    key,
    name: findText(properties, DISPLAY_NAME) ?? key.replace(/\/+$/, '').split('/').pop() ?? '',
    kind: hasCollection(properties) ? 'd' : 'f',
    size: entrySize(properties),
    modified: modifiedTimestamp(findText(properties, LAST_MODIFIED)),
  }
}

function validateXml(content: string): void {
  try {
    const validation = SyntaxValidator.validate(content)
    if (validation !== true) throw new Error(validation.err.msg)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    throw new Error(`invalid Nextcloud Files Search XML: ${message}`, { cause: error })
  }
}

function parsePage(content: string, target: SearchTarget): SearchEntry[] {
  validateXml(content)
  const parsed = parser.parse(content) as unknown
  if (!isRecord(parsed) || !('multistatus' in parsed)) {
    throw new Error('invalid Nextcloud Files Search response')
  }
  const multistatus = parsed.multistatus
  if (multistatus === '') return []
  if (!isRecord(multistatus)) throw new Error('invalid Nextcloud Files Search response')
  return records(multistatus.response).map((response) => parseResponse(response, target))
}

function authorization(accessor: NextcloudAccessor): string | null {
  const username = accessor.config.username
  if (username === undefined || username === '') return null
  return `Basic ${Buffer.from(`${username}:${accessor.config.password ?? ''}`).toString('base64')}`
}

export async function searchFiles(
  accessor: NextcloudAccessor,
  path: PathSpec,
  query: FilesSearchQuery,
): Promise<SearchEntry[] | null> {
  if (!supportsQuery(query)) return null
  const target = searchTarget(accessor.config.url)
  if (target === null) return null
  const headers: Record<string, string> = {
    Accept: 'application/xml',
    'Content-Type': 'text/xml; charset=utf-8',
  }
  const auth = authorization(accessor)
  if (auth !== null) headers.Authorization = auth
  const entries = new Map<string, SearchEntry>()
  let offset = 0
  for (;;) {
    const response = await fetch(target.endpoint, {
      method: 'SEARCH',
      headers,
      body: requestBody(target, path, query, offset),
      signal: AbortSignal.timeout((accessor.config.timeout ?? 30) * 1000),
    })
    if (UNAVAILABLE_STATUS_CODES.has(response.status)) return null
    if (!response.ok) {
      throw new Error(`Nextcloud Files Search returned HTTP ${String(response.status)}`)
    }
    if (response.status !== 207) {
      throw new Error(
        `Nextcloud Files Search returned HTTP ${String(response.status)}, expected 207`,
      )
    }
    const page = parsePage(await response.text(), target)
    const previousSize = entries.size
    for (const entry of page) if (!entries.has(entry.key)) entries.set(entry.key, entry)
    if (page.length > 0 && entries.size === previousSize) return null
    if (page.length < SEARCH_PAGE_SIZE) break
    offset += page.length
  }
  return [...entries.values()]
}

export { SEARCH_PAGE_SIZE }
