/** Runtime Skills and MCP catalog APIs. */

import type { components } from './generated.js'
import { decodeLocalResponse, localHeaders, normalizeBaseURL } from './http.js'
import type { Fetcher, RuntimeClientConfig } from './http.js'

type Schemas = components['schemas']

export type McpServerInfo = Schemas['McpServerInfo']
export type McpServerCatalog = Schemas['McpServerCatalog']
export type McpServerWriteRequest = Schemas['McpServerWriteRequest']
export type McpServerWriteResponse = Schemas['McpServerWriteResponse']
export type McpServerDeleteResponse = Schemas['McpServerDeleteResponse']
export type SkillFile = Schemas['SkillFile']
export type SkillWriteRequest = Schemas['SkillWriteRequest']
export type SkillWriteResponse = Schemas['SkillWriteResponse']
export type SkillDeleteResponse = Schemas['SkillDeleteResponse']

export interface InstalledSkill {
  name: string
  description: string
  /** Absolute path to the skill's SKILL.md on the user's disk. */
  path: string
  /** Friendly label for the root this skill was discovered in:
   *  "shejane" for `~/.shejane/skills/`, "claude" for `~/.claude/skills/`,
   *  or the last segment for a custom `SHEJANE_RUNTIME_SKILLS_PATH`. */
  source?: string
  /** Absolute path of the root directory itself, e.g.
   *  "/Users/x/.shejane/skills". Used to open the folder in Finder. */
  root_path?: string
}

/** One known skill root the runtime scans — surfaced even when empty so
 *  the UI can render a section header + "drop a SKILL.md here" hint. */
export interface SkillRoot {
  source: string
  path: string
}

export interface SkillCatalog {
  skills: InstalledSkill[]
  roots: SkillRoot[]
}

export async function listInstalledSkills(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<SkillCatalog> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/skills`, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  const body = await decodeLocalResponse<Partial<SkillCatalog>>(response)
  return {
    skills: body.skills ?? [],
    roots: body.roots ?? [],
  }
}

/** List MCP Servers explicitly configured for this Runtime.
 *  Secret values are never returned; only `env_keys` is exposed. */
export async function listMcpServers(
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<McpServerCatalog> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/mcp-servers`, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  const body = await decodeLocalResponse<Partial<McpServerCatalog>>(response)
  return {
    servers: body.servers ?? [],
    sources_scanned: body.sources_scanned ?? [],
  }
}

export async function createMcpServer(
  input: McpServerWriteRequest,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<McpServerWriteResponse> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/mcp-servers`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify(input),
  })
  return decodeLocalResponse<McpServerWriteResponse>(response)
}

export async function updateMcpServer(
  name: string,
  input: McpServerWriteRequest,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<McpServerWriteResponse> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/mcp-servers/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: localHeaders(config, true),
    body: JSON.stringify(input),
  })
  return decodeLocalResponse<McpServerWriteResponse>(response)
}

export async function deleteMcpServer(
  name: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<McpServerDeleteResponse> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/mcp-servers/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    headers: localHeaders(config, false),
  })
  return decodeLocalResponse<McpServerDeleteResponse>(response)
}

export async function createLocalSkill(
  input: SkillWriteRequest,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<SkillWriteResponse> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/skills`, {
    method: 'POST',
    headers: localHeaders(config, true),
    body: JSON.stringify(input),
  })
  return decodeLocalResponse<SkillWriteResponse>(response)
}

export async function getLocalSkillFile(
  name: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<SkillFile> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/skills/${encodeURIComponent(name)}`, {
    method: 'GET',
    headers: localHeaders(config, false),
  })
  return decodeLocalResponse<SkillFile>(response)
}

export async function updateLocalSkill(
  name: string,
  input: SkillWriteRequest,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<SkillWriteResponse> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/skills/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: localHeaders(config, true),
    body: JSON.stringify(input),
  })
  return decodeLocalResponse<SkillWriteResponse>(response)
}

export async function deleteLocalSkill(
  name: string,
  config: RuntimeClientConfig,
  fetcher: Fetcher = fetch,
): Promise<SkillDeleteResponse> {
  const response = await fetcher(`${normalizeBaseURL(config.baseURL)}/v1/skills/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    headers: localHeaders(config, false),
  })
  return decodeLocalResponse<SkillDeleteResponse>(response)
}

