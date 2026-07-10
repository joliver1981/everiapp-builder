/**
 * aiChat — one-call LLM chat through an attached AI-provider Connection.
 *
 * An AI Provider Connection (Admin → Connections → AI Provider) knows its
 * provider's base URL, auth, wire format, and which models the admin exposed.
 * aiChat uses that so your app sends ONE request shape regardless of provider
 * — the platform injects the credential server-side and this helper speaks
 * OpenAI's or Anthropic's format for you:
 *
 *   const res = await aiChat('my-anthropic', {
 *     messages: [{ role: 'user', content: 'Summarize this…' }],
 *   })
 *   // res.text (assistant reply), res.status, res.raw (full provider response)
 *
 * The model defaults to the connection's default model; pass `model` to pick
 * another (show conn.models from useConnections() for a model picker).
 *
 * Like callConnection, aiChat RESOLVES even when the provider errors or the
 * call times out — check `res.error` / `res.status >= 400`. It throws only
 * for platform-side failures (connection not attached, no model configured,
 * session expired). For provider-specific request shapes or endpoints beyond
 * chat (embeddings, images), use callConnection. Note: some reasoning models
 * (e.g. OpenAI's gpt-5 family) reject `temperature` — omit it if the
 * provider errors.
 */
import { callConnection, listConnections, type AppConnection } from './callConnection'

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant'
  content: string
}

export interface AiChatRequest {
  /** Conversation so far; a plain string is shorthand for one user message. */
  messages: ChatMessage[] | string
  /** Model id — defaults to the connection's default model, then its first model. */
  model?: string
  /** System prompt (merged with any 'system'-role messages). */
  system?: string
  /** Output-token cap. Default 4096 where the provider requires one (Anthropic). */
  maxTokens?: number
  /** Sampling temperature. Some reasoning models reject this — omit on errors. */
  temperature?: number
  /** Extra provider-specific body fields, merged last (may override the above). */
  extra?: Record<string, unknown>
}

export interface AiChatResult {
  /** The provider's HTTP status — check `>= 400` before using `text`. */
  status: number
  /** The assistant's reply text ('' when the provider errored). */
  text: string
  /** The model that was requested. */
  model: string
  /** The full, untranslated provider response body. */
  raw: unknown
  /** Human-readable provider error when status >= 400, else null. */
  error: string | null
}

// Connection metadata cache — attached connections rarely change mid-session.
// A lookup miss refetches (newly attached connections are found without a
// reload), a short TTL picks up metadata edits (models/default model), and a
// failed call invalidates it (a deleted-and-recreated connection heals on the
// next attempt instead of serving a dead id forever).
const CONN_CACHE_TTL_MS = 60_000
let connCache: AppConnection[] | null = null
let connCacheAt = 0

async function resolveAiConnection(idOrName: string): Promise<AppConnection> {
  const find = (list: AppConnection[]) =>
    list.find((c) => c.id === idOrName || c.name === idOrName)
  const fresh = connCache && Date.now() - connCacheAt < CONN_CACHE_TTL_MS
  let hit = fresh && connCache ? find(connCache) : undefined
  if (!hit) {
    connCache = await listConnections()
    connCacheAt = Date.now()
    hit = find(connCache)
  }
  if (!hit) {
    throw new Error(
      `aiChat: no attached connection with id or name '${idOrName}' — ` +
        "attach it from this app's Data & APIs panel in the builder",
    )
  }
  if (hit.kind !== 'ai') {
    throw new Error(
      `aiChat: connection '${hit.name}' is a ${hit.kind.toUpperCase()} connection, ` +
        'not an AI provider — use callConnection for generic HTTP calls',
    )
  }
  return hit
}

function extractText(apiFormat: string | null, body: unknown): string {
  const b = body as Record<string, any> | null | undefined
  if (apiFormat === 'anthropic') {
    const blocks = Array.isArray(b?.content) ? b!.content : []
    return blocks
      .filter((blk: any) => blk && blk.type === 'text')
      .map((blk: any) => String(blk.text ?? ''))
      .join('')
  }
  const content = b?.choices?.[0]?.message?.content
  return typeof content === 'string' ? content : ''
}

function extractError(status: number, body: unknown): string | null {
  if (status < 400) return null
  const b = body as Record<string, any> | null | undefined
  const msg = b?.error?.message ?? b?.error ?? b?.message
  if (typeof msg === 'string' && msg) return msg
  if (typeof body === 'string' && body) return body.slice(0, 300)
  try {
    return JSON.stringify(body).slice(0, 300)
  } catch {
    return `HTTP ${status}`
  }
}

/** Send one chat completion through an attached AI-provider Connection. */
export async function aiChat(
  connectionIdOrName: string,
  request: AiChatRequest,
): Promise<AiChatResult> {
  const conn = await resolveAiConnection(connectionIdOrName)

  const model = request.model || conn.default_model || conn.models[0]
  if (!model) {
    throw new Error(
      `aiChat: no model to use — pass request.model, or set a default model on ` +
        `connection '${conn.name}' in Admin → Connections`,
    )
  }

  const msgs: ChatMessage[] =
    typeof request.messages === 'string'
      ? [{ role: 'user', content: request.messages }]
      : request.messages
  const systemParts = [
    ...(request.system ? [request.system] : []),
    ...msgs.filter((m) => m.role === 'system').map((m) => m.content),
  ]
  const chat = msgs.filter((m) => m.role !== 'system')

  let body: Record<string, unknown>
  if (conn.api_format === 'anthropic') {
    body = {
      model,
      // Anthropic requires max_tokens; a cap, not a target. 4096 is valid for
      // every model generation (older models reject higher caps).
      max_tokens: request.maxTokens ?? 4096,
      messages: chat,
      ...(systemParts.length ? { system: systemParts.join('\n\n') } : {}),
      ...(request.temperature !== undefined ? { temperature: request.temperature } : {}),
      ...request.extra,
    }
  } else {
    // OpenAI-compatible. OpenAI (and Azure OpenAI) deprecated max_tokens in
    // favor of max_completion_tokens; gateways like OpenRouter still take
    // max_tokens.
    const tokensKey =
      conn.provider === 'openai' || conn.provider === 'azure_openai'
        ? 'max_completion_tokens'
        : 'max_tokens'
    body = {
      model,
      messages: [
        ...systemParts.map((content) => ({ role: 'system' as const, content })),
        ...chat,
      ],
      ...(request.maxTokens !== undefined ? { [tokensKey]: request.maxTokens } : {}),
      ...(request.temperature !== undefined ? { temperature: request.temperature } : {}),
      ...request.extra,
    }
  }

  const path =
    conn.chat_path || (conn.api_format === 'anthropic' ? '/messages' : '/chat/completions')
  let res
  try {
    res = await callConnection(conn.id, { method: 'POST', path, body })
  } catch (e) {
    // The connection metadata we called with may be stale (deleted/recreated,
    // callable flag flipped) — drop the cache so the next attempt re-resolves.
    connCache = null
    // A gateway timeout / unreachable upstream is an UPSTREAM failure — keep
    // the resolve-don't-throw contract for it (long generations can exceed the
    // connection's timeout). Platform-side failures still throw.
    const gateway = e instanceof Error && /HTTP (502|504)\b/.exec(e.message)
    if (gateway) {
      const status = Number(gateway[1])
      return { status, text: '', model, raw: null, error: e.message }
    }
    throw e
  }

  return {
    status: res.status,
    text: res.status < 400 ? extractText(conn.api_format, res.body) : '',
    model,
    raw: res.body,
    error: extractError(res.status, res.body),
  }
}
