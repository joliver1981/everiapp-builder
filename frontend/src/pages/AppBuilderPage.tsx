import { useEffect, useState, useCallback, useRef, type PointerEvent as ReactPointerEvent } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  MessageSquare,
  Code2,
  Eye,
  Plus,
  Save,
  Loader2,
  PanelLeftClose,
  PanelLeftOpen,
  ToggleLeft,
  ToggleRight,
  Upload,
  History,
  Settings,
  X,
  RotateCcw,
  Circle,
  ChevronRight,
  Shield,
  Trash2,
  UserPlus,
  Play,
  Square,
  RefreshCw,
  AlertCircle,
  Wand2,
  Rocket,
  Bug,
  ShieldCheck,
  Database,
  FileDiff,
  BarChart3,
  Radio,
  LocateFixed,
  MessageSquarePlus,
} from 'lucide-react'
import { ChatPanel } from '@/components/chat/ChatPanel'
import { CodeEditor, type EditorSelectionContext } from '@/components/editor/CodeEditor'
import { FileTree } from '@/components/editor/FileTree'
import { LiveCodePanel } from '@/components/builder/LiveCodePanel'
import { CollabOverlay } from '@/components/builder/CollabOverlay'
import { SetupWizardPreview } from '@/components/wizard/SetupWizardPreview'
import { DeploymentsPanel } from '@/components/builder/DeploymentsPanel'
import { AppDataPanel } from '@/components/builder/AppDataPanel'
import { VersionDiffModal } from '@/components/builder/VersionDiffModal'
import { AppAnalyticsPanel } from '@/components/builder/AppAnalyticsPanel'
import { EmbedModal } from '@/components/builder/EmbedModal'
import { DependencyScanModal } from '@/components/builder/DependencyScanModal'
import { RewindModal } from '@/components/builder/RewindModal'
import { TracesModal } from '@/components/builder/TracesModal'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { useAppStore } from '@/stores/appStore'
import { useChatStore } from '@/stores/chatStore'
import { useAuthStore } from '@/stores/authStore'
import { apiClient } from '@/api/client'
import { cn } from '@/lib/utils'

type Panel = 'chat' | 'code' | 'preview'
type RightPanel = 'none' | 'versions' | 'settings' | 'permissions' | 'wizard' | 'deployments' | 'data' | 'analytics' | 'live'

// Shape of /api/apps/{id}/runtime/status responses — includes streaming phase
// fields so the UI can show "Installing dependencies (15s)..." instead of a
// silent spinner.
interface RuntimeStatusResp {
  app_id: string
  status: 'starting' | 'running' | 'stopped' | 'error' | string
  port?: number | null
  source?: string | null
  error?: string | null
  phase?: string | null
  phase_detail?: string | null
  phase_elapsed_seconds?: number | null
}

// Right panel resize: the live-code panel defaults wider than the rest, so we
// track (and persist) a dragged width per group rather than one shared value.
const RIGHT_PANEL_MIN_WIDTH = 280
const RIGHT_PANEL_DEFAULT_WIDTHS = { live: 544, other: 320 } as const
type RightPanelWidths = { live: number; other: number }

function loadRightPanelWidths(): RightPanelWidths {
  try {
    const saved = JSON.parse(localStorage.getItem('aihub.rightPanel.widths') || '{}')
    return {
      live: typeof saved.live === 'number' ? saved.live : RIGHT_PANEL_DEFAULT_WIDTHS.live,
      other: typeof saved.other === 'number' ? saved.other : RIGHT_PANEL_DEFAULT_WIDTHS.other,
    }
  } catch {
    return { ...RIGHT_PANEL_DEFAULT_WIDTHS }
  }
}

const PHASE_LABELS: Record<string, string> = {
  queued: 'Queued',
  installing: 'Installing npm dependencies',
  spawning: 'Starting Vite dev server',
  waiting: 'Waiting for server to come up',
  running: 'Running',
  failed: 'Failed',
}

interface OpenTab {
  path: string
  content: string
  savedContent: string
  language: string
}

interface Version {
  id: string
  version: number
  notes: string
  published_by: string
  created_at: string
}

// ---- semver helpers (public marketplace release version) ------------------
type SemverPart = 'patch' | 'minor' | 'major'
function parseSemver(v: string): [number, number, number] {
  const m = /^(\d+)\.(\d+)\.(\d+)$/.exec((v || '').trim())
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : [0, 0, 0]
}
function bumpSemver(v: string, part: SemverPart): string {
  const [maj, min, pat] = parseSemver(v)
  if (part === 'major') return `${maj + 1}.0.0`
  if (part === 'minor') return `${maj}.${min + 1}.0`
  return `${maj}.${min}.${pat + 1}`
}
function cmpSemver(a: string, b: string): number {
  const pa = parseSemver(a), pb = parseSemver(b)
  for (let i = 0; i < 3; i++) if (pa[i] !== pb[i]) return pa[i] < pb[i] ? -1 : 1
  return 0
}
function isValidSemver(v: string): boolean {
  // Canonical semver only — reject leading zeros (e.g. 1.02.0), which compare
  // equal to 1.2.0 but store as a distinct string (duplicate release rows).
  return /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test((v || '').trim())
}
/** Highest valid semver in the list, or '' when none. */
function maxSemver(list: string[]): string {
  return list.filter(isValidSemver).reduce((a, b) => (a && cmpSemver(a, b) >= 0 ? a : b), '')
}
/** Bump `base` by `part`, then patch-increment past any already-taken versions. */
function nextFreeVersion(base: string, part: SemverPart, taken: string[]): string {
  let v = bumpSemver(base, part)
  let guard = 0
  while (taken.includes(v) && guard++ < 1000) v = bumpSemver(v, 'patch')
  return v
}

export function AppBuilderPage() {
  const { appId } = useParams()
  const navigate = useNavigate()
  const { currentApp, setCurrentApp, createApp } = useAppStore()
  const { connect, clearMessages, disconnect, isStreaming, loadHistory } = useChatStore()
  const codeNav = useChatStore((s) => s.codeNav)
  const liveCodeEnabled = useChatStore((s) => s.liveCodeEnabled)
  const setLiveCodeEnabled = useChatStore((s) => s.setLiveCodeEnabled)
  const autoJumpEnabled = useChatStore((s) => s.autoJumpEnabled)
  const setAutoJumpEnabled = useChatStore((s) => s.setAutoJumpEnabled)
  const setEditorContext = useChatStore((s) => s.setEditorContext)
  const turnFilePaths = useChatStore((s) => s.turnFilePaths)
  const token = useAuthStore(() => apiClient.getToken())
  const user = useAuthStore((s) => s.user)

  const [activePanel, setActivePanel] = useState<Panel>('chat')
  const [rightPanel, setRightPanel] = useState<RightPanel>('none')
  // Drag-to-resize state for the right panel (persisted per panel group).
  const [rightPanelWidths, setRightPanelWidths] = useState<RightPanelWidths>(loadRightPanelWidths)
  const [isResizingPanel, setIsResizingPanel] = useState(false)

  useEffect(() => {
    try { localStorage.setItem('aihub.rightPanel.widths', JSON.stringify(rightPanelWidths)) } catch { /* ignore */ }
  }, [rightPanelWidths])

  const startPanelResize = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    e.preventDefault()
    const group = rightPanel === 'live' ? 'live' : 'other'
    const startX = e.clientX
    const startWidth = rightPanelWidths[group]
    setIsResizingPanel(true)
    const onMove = (ev: PointerEvent) => {
      const maxWidth = Math.round(window.innerWidth * 0.7)
      const width = Math.min(Math.max(startWidth + (startX - ev.clientX), RIGHT_PANEL_MIN_WIDTH), maxWidth)
      setRightPanelWidths((w) => ({ ...w, [group]: width }))
    }
    const onUp = () => {
      setIsResizingPanel(false)
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }, [rightPanel, rightPanelWidths])

  const resetPanelWidth = useCallback(() => {
    const group = rightPanel === 'live' ? 'live' : 'other'
    setRightPanelWidths((w) => ({ ...w, [group]: RIGHT_PANEL_DEFAULT_WIDTHS[group] }))
  }, [rightPanel])
  // Jump-to-code: highlight range + token passed to the Code panel's editor.
  const [navHighlight, setNavHighlight] = useState<{ startLine: number; endLine: number } | null>(null)
  const [navRevealToken, setNavRevealToken] = useState(0)
  // In-code collaboration overlay (persisted open/closed).
  const [showCollab, setShowCollab] = useState(() => {
    try { return localStorage.getItem('aihub.collab.open') === '1' } catch { return false }
  })
  const [showFileTree, setShowFileTree] = useState(true)
  const [files, setFiles] = useState<any[]>([])
  const [openTabs, setOpenTabs] = useState<OpenTab[]>([])
  const [activeTabPath, setActiveTabPath] = useState<string | null>(null)
  const [isCreating, setIsCreating] = useState(false)
  const [isSaving, setIsSaving] = useState(false)
  const [isPublishing, setIsPublishing] = useState(false)
  const [versions, setVersions] = useState<Version[]>([])
  const [publishNotes, setPublishNotes] = useState('')
  const [showPublishDialog, setShowPublishDialog] = useState(false)
  const [publishResult, setPublishResult] = useState<{ version: number } | null>(null)
  const [publishError, setPublishError] = useState<string | null>(null)
  const [appName, setAppName] = useState('')
  const [isEditingName, setIsEditingName] = useState(false)
  const [showMarketplaceDialog, setShowMarketplaceDialog] = useState(false)
  const [isPublishingToMarketplace, setIsPublishingToMarketplace] = useState(false)
  const [marketplaceCategory, setMarketplaceCategory] = useState('general')
  const [marketplaceShortDesc, setMarketplaceShortDesc] = useState('')
  const [marketplaceDescription, setMarketplaceDescription] = useState('')
  const [marketplaceTags, setMarketplaceTags] = useState('')
  const [marketplaceLicense, setMarketplaceLicense] = useState('MIT')
  const [marketplaceNotes, setMarketplaceNotes] = useState('')
  const [marketplaceSetupInstructions, setMarketplaceSetupInstructions] = useState('')
  // null = latest saved snapshot (default)
  const [marketplaceVersion, setMarketplaceVersion] = useState<number | null>(null)
  // Public release semver — bump buttons seed it from last_published_version.
  const [marketplaceSemver, setMarketplaceSemver] = useState('')
  // Semvers already on the listing (grey-out to avoid 409 collisions).
  const [publishedVersions, setPublishedVersions] = useState<string[]>([])
  // The TRUE highest release: local last_published_version OR anything the
  // remote listing already has. Covers pre-existing listings whose local field
  // is still empty (new column) and stale local values after a publish.
  const effectiveLast = maxSemver([
    ...(currentApp?.last_published_version ? [currentApp.last_published_version] : []),
    ...publishedVersions,
  ])
  const [isSuggesting, setIsSuggesting] = useState(false)
  const [suggestError, setSuggestError] = useState<string | null>(null)
  const [marketplaceShots, setMarketplaceShots] = useState(true)
  const [marketplaceResult, setMarketplaceResult] = useState<{ message: string; url: string } | null>(null)
  const [marketplaceConfig, setMarketplaceConfig] = useState<{
    configured: boolean; url_configured: boolean; key_configured: boolean; marketplace_url: string
  } | null>(null)

  // When the Publish-to-Marketplace dialog opens, check whether external publishing
  // is configured so we can warn upfront. Developers can't read admin settings
  // directly, so this goes through a dedicated, non-secret status endpoint.
  useEffect(() => {
    if (!showMarketplaceDialog) return
    apiClient
      .get<{ configured: boolean; url_configured: boolean; key_configured: boolean; marketplace_url: string }>(
        '/marketplace/publish-config',
      )
      .then(setMarketplaceConfig)
      .catch(() => setMarketplaceConfig(null))
    // Fresh dialog: publish the latest snapshot, prefill the saved LISTING
    // metadata (short desc, description, category, tags, license, setup
    // instructions) so it stays consistent across publishes, and default the
    // release version to a minor bump of the last published semver.
    setMarketplaceVersion(null)
    setSuggestError(null)
    setMarketplaceSetupInstructions(currentApp?.setup_instructions || '')
    setMarketplaceDescription(currentApp?.description || '')
    const listing = currentApp?.marketplace_listing || {}
    setMarketplaceShortDesc(listing.short_description || '')
    setMarketplaceCategory(listing.category || 'general')
    setMarketplaceTags((listing.tags || []).join(', '))
    setMarketplaceLicense(listing.license || 'MIT')
    // Release notes are per-version — always start fresh.
    setMarketplaceNotes('')
    // Provisional seed from the local field; the effect below re-seeds off the
    // authoritative remote version list once it loads.
    const last = currentApp?.last_published_version || ''
    setMarketplaceSemver(last ? bumpSemver(last, 'minor') : '1.0.0')
    // Best-effort: which semvers are already on the listing (grey them out).
    setPublishedVersions([])
    if (currentApp) {
      apiClient
        .get<{ versions: string[] }>(`/marketplace/published-versions?app_id=${currentApp.id}`)
        .then((d) => setPublishedVersions(d.versions || []))
        .catch(() => setPublishedVersions([]))
    }
  }, [showMarketplaceDialog])  // eslint-disable-line react-hooks/exhaustive-deps

  // Once the listing's existing versions load, re-seed the release version to a
  // free bump above the TRUE highest — fixes pre-existing listings (empty local
  // field → no bump buttons) and any collision with the provisional default.
  // Only overrides an unusable pick (empty / invalid / already-taken), so a
  // valid user choice is preserved.
  useEffect(() => {
    if (!showMarketplaceDialog || !effectiveLast) return
    setMarketplaceSemver((cur) =>
      !isValidSemver(cur) || publishedVersions.includes(cur)
        ? nextFreeVersion(effectiveLast, 'minor', publishedVersions)
        : cur,
    )
  }, [publishedVersions, showMarketplaceDialog, effectiveLast])  // eslint-disable-line react-hooks/exhaustive-deps


  // Runtime / preview state
  const [runtimeStatus, setRuntimeStatus] = useState<'stopped' | 'starting' | 'running' | 'error'>('stopped')
  const [runtimeError, setRuntimeError] = useState<string | null>(null)
  const [runtimePort, setRuntimePort] = useState<number | null>(null)
  const [previewKey, setPreviewKey] = useState(0) // bump to force iframe reload
  // Live progress for the runtime — populated from /runtime/status polling
  // while status='starting'. Surfaces "Installing dependencies (15s)..." etc.
  const [runtimePhase, setRuntimePhase] = useState<string | null>(null)
  const [runtimePhaseDetail, setRuntimePhaseDetail] = useState<string | null>(null)
  const [runtimePhaseElapsed, setRuntimePhaseElapsed] = useState<number | null>(null)

  // Delete state
  const [showDeleteDialog, setShowDeleteDialog] = useState(false)
  const [isDeleting, setIsDeleting] = useState(false)

  const activeTab = openTabs.find((t) => t.path === activeTabPath) ?? null
  const hasUnsavedChanges = openTabs.some((t) => t.content !== t.savedContent)

  // Load app and connect WebSocket
  useEffect(() => {
    if (appId) {
      apiClient.get(`/apps/${appId}`).then((app: any) => {
        setCurrentApp(app)
        setAppName(app.name)
      })
      // Load conversation history — map the restored code_refs onto each message so the
      // "jump to code" chips re-appear on reload exactly as they were live.
      apiClient.get(`/ai/conversations/${appId}`).then((data: any) => {
        if (data.messages && data.messages.length > 0) {
          const msgs = data.messages.map((m: any) => ({
            id: m.id,
            role: m.role,
            content: m.content,
            codeRefs: m.code_refs?.length ? m.code_refs : undefined,
            timestamp: m.timestamp,
          }))
          loadHistory(msgs, data.conversation_id)
        }
      }).catch(() => {
        // No history or error, just continue
      })
    }
    return () => {
      disconnect()
      clearMessages()
    }
  }, [appId])

  useEffect(() => {
    if (currentApp && token) {
      connect(token).catch(console.error)
    }
  }, [currentApp, token])

  // Auto-refresh file tree when AI finishes generating, AND reload any open tab the AI just
  // changed so the user watches edits land in the editor (in-code collaboration). Only
  // AI-changed tabs are refetched, so unrelated unsaved edits aren't clobbered.
  const wasStreamingRef = useRef(false)
  useEffect(() => {
    if (wasStreamingRef.current && !isStreaming && currentApp) {
      loadFileTree()
      const changed = new Set(turnFilePaths)
      if (changed.size > 0) {
        openTabs.forEach((tab) => {
          if (!changed.has(tab.path)) return
          apiClient
            .get<{ content: string; language: string }>(`/apps/${currentApp.id}/files/${tab.path}`)
            .then((res) => {
              setOpenTabs((prev) => prev.map((t) =>
                t.path === tab.path
                  ? { ...t, content: res.content, savedContent: res.content, language: res.language }
                  : t
              ))
            })
            .catch(() => { /* file may have been deleted this turn; ignore */ })
        })
      }
    }
    wasStreamingRef.current = isStreaming
  }, [isStreaming, currentApp])

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault()
        handleSaveCurrentFile()
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [activeTabPath, openTabs])

  // Load file tree
  const loadFileTree = useCallback(async () => {
    if (!currentApp) return
    try {
      const tree = await apiClient.get<any[]>(`/apps/${currentApp.id}/files`)
      setFiles(tree)
    } catch {
      setFiles([])
    }
  }, [currentApp])

  useEffect(() => {
    if (activePanel === 'code' && currentApp) {
      loadFileTree()
    }
  }, [activePanel, currentApp, loadFileTree])

  // Load versions
  const loadVersions = useCallback(async () => {
    if (!currentApp) return
    try {
      const data = await apiClient.get<Version[]>(`/apps/${currentApp.id}/versions`)
      setVersions(data)
    } catch {
      setVersions([])
    }
  }, [currentApp])

  useEffect(() => {
    if (rightPanel === 'versions' && currentApp) {
      loadVersions()
    }
  }, [rightPanel, currentApp, loadVersions])

  // File operations
  const handleSelectFile = async (path: string) => {
    if (!currentApp) return

    // Check if already open
    const existing = openTabs.find((t) => t.path === path)
    if (existing) {
      setActiveTabPath(path)
      return
    }

    try {
      const result = await apiClient.get<{ content: string; language: string }>(
        `/apps/${currentApp.id}/files/${path}`
      )
      const newTab: OpenTab = {
        path,
        content: result.content,
        savedContent: result.content,
        language: result.language,
      }
      setOpenTabs((prev) => [...prev, newTab])
      setActiveTabPath(path)
    } catch {
      // ignore
    }
  }

  // Jump-to-code: when a nav request lands (a chat ref click or AI auto-jump), switch to
  // the Code panel, open the target file, then highlight its lines once content loads.
  useEffect(() => {
    if (!codeNav || !currentApp) return
    setActivePanel('code')
    let cancelled = false
    ;(async () => {
      await handleSelectFile(codeNav.path)
      if (cancelled) return
      if (codeNav.startLine) {
        setNavHighlight({ startLine: codeNav.startLine, endLine: codeNav.endLine ?? codeNav.startLine })
        setNavRevealToken(codeNav.token)
      } else {
        setNavHighlight(null) // whole-file jump — just open it, no line highlight
      }
    })()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [codeNav?.token])

  // Feed the in-code overlay: tag the editor's selection/viewport with the active file path.
  const handleEditorContextChange = (ctx: EditorSelectionContext) => {
    setEditorContext(activeTabPath ? { path: activeTabPath, ...ctx } : null)
  }

  // Clear shared editor context when there's nothing to look at (left the Code panel / no tab).
  useEffect(() => {
    if (activePanel !== 'code' || !activeTabPath) setEditorContext(null)
  }, [activePanel, activeTabPath, setEditorContext])

  const toggleCollab = (next: boolean) => {
    setShowCollab(next)
    try { localStorage.setItem('aihub.collab.open', next ? '1' : '0') } catch { /* ignore */ }
  }

  const handleCloseTab = (path: string) => {
    setOpenTabs((prev) => prev.filter((t) => t.path !== path))
    if (activeTabPath === path) {
      const remaining = openTabs.filter((t) => t.path !== path)
      setActiveTabPath(remaining.length > 0 ? remaining[remaining.length - 1].path : null)
    }
  }

  const handleTabContentChange = (content: string) => {
    if (!activeTabPath) return
    setOpenTabs((prev) =>
      prev.map((t) => (t.path === activeTabPath ? { ...t, content } : t))
    )
  }

  const handleSaveCurrentFile = async () => {
    if (!currentApp || !activeTab || activeTab.content === activeTab.savedContent) return
    setIsSaving(true)
    try {
      await apiClient.put(`/apps/${currentApp.id}/files/${activeTab.path}`, {
        content: activeTab.content,
      })
      setOpenTabs((prev) =>
        prev.map((t) =>
          t.path === activeTab.path ? { ...t, savedContent: t.content } : t
        )
      )
    } finally {
      setIsSaving(false)
    }
  }

  // App operations
  const handleCreateApp = async () => {
    setIsCreating(true)
    try {
      const app = await createApp('Untitled App', '')
      navigate(`/builder/${app.id}`)
    } finally {
      setIsCreating(false)
    }
  }

  const handlePublish = async () => {
    if (!currentApp) return
    setIsPublishing(true)
    setPublishError(null)
    try {
      await apiClient.post(`/apps/${currentApp.id}/versions`, { notes: publishNotes })
      // Refresh app to get new version number
      const app = await apiClient.get<any>(`/apps/${currentApp.id}`)
      setCurrentApp(app)
      loadVersions()
      setPublishResult({ version: app.current_version })
    } catch (err: any) {
      setPublishError(err?.message || 'Failed to create the version. Please try again.')
    } finally {
      setIsPublishing(false)
    }
  }

  const closePublishDialog = () => {
    setShowPublishDialog(false)
    setPublishNotes('')
    setPublishResult(null)
    setPublishError(null)
  }

  const handlePublishToMarketplace = async () => {
    if (!currentApp) return
    setIsPublishingToMarketplace(true)
    setMarketplaceResult(null)
    try {
      // Credentials come from platform settings (Platform → Settings →
      // EveriApp Marketplace) — admins configure them once.
      const result = await apiClient.post<any>('/marketplace/publish-external', {
        app_id: currentApp.id,
        category: marketplaceCategory,
        tags: marketplaceTags.split(',').map((t) => t.trim()).filter(Boolean).slice(0, 10),
        short_description: marketplaceShortDesc || currentApp.description?.slice(0, 300) || '',
        description: marketplaceDescription,
        license: marketplaceLicense || 'MIT',
        release_notes: marketplaceNotes,
        setup_instructions: marketplaceSetupInstructions,
        version: marketplaceVersion,
        version_semver: marketplaceSemver,
        capture_screenshots: marketplaceShots,
      })
      setMarketplaceResult({
        message: result.message,
        url: result.marketplace_url || '',
      })
      // Reflect the just-shipped release so a repeat publish this session bumps
      // from it (last_published_version was stale until the app is refetched).
      const shipped = result.version_semver || marketplaceSemver
      setPublishedVersions((prev) => (prev.includes(shipped) ? prev : [...prev, shipped]))
      apiClient.get<any>(`/apps/${currentApp.id}`).then(setCurrentApp).catch(() => {})
    } catch (err: any) {
      let detail = err.message || 'Failed to publish'
      try {
        const parsed = JSON.parse(err.message)
        if (typeof parsed.detail === 'string') detail = parsed.detail
      } catch { /* not JSON */ }
      setMarketplaceResult({ message: `Error: ${detail}`, url: '' })
    } finally {
      setIsPublishingToMarketplace(false)
    }
  }

  const handleSuggestMetadata = async () => {
    if (!currentApp) return
    setIsSuggesting(true)
    setSuggestError(null)
    try {
      const s = await apiClient.post<{
        short_description: string; description: string; category: string; tags: string[]
        release_notes: string; setup_instructions: string
        suggested_bump?: 'patch' | 'minor' | 'major'
      }>('/marketplace/suggest-metadata', {
        app_id: currentApp.id,
        version: marketplaceVersion,
      })
      // Fill every field the AI drafted; everything stays editable.
      if (s.short_description) setMarketplaceShortDesc(s.short_description)
      if (s.description) setMarketplaceDescription(s.description)
      if (s.category) setMarketplaceCategory(s.category)
      if (s.tags?.length) setMarketplaceTags(s.tags.join(', '))
      if (s.release_notes) setMarketplaceNotes(s.release_notes)
      if (s.setup_instructions) setMarketplaceSetupInstructions(s.setup_instructions)
      // Pre-select the release version from the AI's bump suggestion (only when
      // there's a prior release to bump from — first publish stays 1.0.0), and
      // skip any version already on the listing so it never fills a taken one.
      if (s.suggested_bump && effectiveLast) {
        setMarketplaceSemver(nextFreeVersion(effectiveLast, s.suggested_bump, publishedVersions))
      }
    } catch (err: any) {
      let detail = err.message || 'Suggestion failed'
      try {
        const parsed = JSON.parse(err.message)
        if (typeof parsed.detail === 'string') detail = parsed.detail
      } catch { /* not JSON */ }
      setSuggestError(detail)
    } finally {
      setIsSuggesting(false)
    }
  }

  const handleRollback = async (version: number) => {
    if (!currentApp) return
    try {
      await apiClient.post(`/apps/${currentApp.id}/versions/${version}/rollback`)
      const app = await apiClient.get<any>(`/apps/${currentApp.id}`)
      setCurrentApp(app)
      loadVersions()
      loadFileTree()
      // Clear open tabs since files changed
      setOpenTabs([])
      setActiveTabPath(null)
    } catch {
      // ignore
    }
  }

  const handleSaveAppName = async () => {
    if (!currentApp || !appName.trim()) return
    await apiClient.put(`/apps/${currentApp.id}`, { name: appName.trim() })
    setCurrentApp({ ...currentApp, name: appName.trim() })
    setIsEditingName(false)
  }

  // Runtime controls
  const applyRuntimeResp = (resp: RuntimeStatusResp) => {
    const prevStatus = runtimeStatus
    setRuntimeStatus(resp.status as any)
    if (resp.port) setRuntimePort(resp.port)
    setRuntimeError(resp.error ?? null)
    setRuntimePhase(resp.phase ?? null)
    setRuntimePhaseDetail(resp.phase_detail ?? null)
    setRuntimePhaseElapsed(resp.phase_elapsed_seconds ?? null)
    // Bump iframe key only when we just transitioned into running, so we
    // don't tear down an already-rendering preview on every poll tick.
    if (resp.status === 'running' && prevStatus !== 'running') {
      setPreviewKey((k) => k + 1)
    }
  }

  const handleStartPreview = async () => {
    if (!currentApp) return
    setRuntimeStatus('starting')
    setRuntimeError(null)
    setRuntimePhase('queued')
    setRuntimePhaseDetail(null)
    setRuntimePhaseElapsed(null)
    try {
      const resp = await apiClient.post<RuntimeStatusResp>(
        `/apps/${currentApp.id}/runtime/start`,
        { source: 'draft' }
      )
      applyRuntimeResp(resp)
    } catch {
      setRuntimeStatus('error')
      setRuntimeError('Failed to start app')
    }
  }

  const handleStopPreview = async () => {
    if (!currentApp) return
    try {
      await apiClient.post(`/apps/${currentApp.id}/runtime/stop`)
    } catch { /* ignore */ }
    setRuntimeStatus('stopped')
    setRuntimePort(null)
    setRuntimeError(null)
  }

  const handleRefreshPreview = () => {
    setPreviewKey((k) => k + 1)
  }

  const handleDeleteApp = async () => {
    if (!currentApp) return
    setIsDeleting(true)
    try {
      // Stop the runtime if running
      try {
        await apiClient.post(`/apps/${currentApp.id}/runtime/stop`)
      } catch { /* ignore */ }
      // Disconnect WebSocket
      disconnect()
      // Delete the app
      await apiClient.delete(`/apps/${currentApp.id}`)
      navigate('/apps')
    } catch {
      setIsDeleting(false)
    }
  }

  // Check runtime status when switching to preview
  useEffect(() => {
    if (activePanel === 'preview' && currentApp) {
      apiClient.get<RuntimeStatusResp>(`/apps/${currentApp.id}/runtime/status`)
        .then(applyRuntimeResp)
        .catch(() => setRuntimeStatus('stopped'))
    }
  }, [activePanel, currentApp])

  // While the runtime is starting, poll /runtime/status every 1.5s so the UI
  // can show real progress instead of a silent "Starting..." for 60 seconds.
  useEffect(() => {
    if (!currentApp || runtimeStatus !== 'starting') return
    const id = setInterval(async () => {
      try {
        const s = await apiClient.get<RuntimeStatusResp>(`/apps/${currentApp.id}/runtime/status`)
        applyRuntimeResp(s)
      } catch {
        /* transient — keep polling */
      }
    }, 1500)
    return () => clearInterval(id)
  }, [currentApp, runtimeStatus])

  // No app selected — show creation screen
  if (!appId || !currentApp) {
    return (
      <div className="flex h-full flex-col items-center justify-center">
        <h2 className="text-xl font-semibold">Create a New App</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          Start by creating an app, then build it with AI
        </p>
        <button
          onClick={handleCreateApp}
          disabled={isCreating}
          className="mt-6 flex items-center gap-2 rounded-xl bg-primary px-6 py-3 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
        >
          {isCreating ? <Loader2 size={18} className="animate-spin" /> : <Plus size={18} />}
          Create App
        </button>
      </div>
    )
  }

  return (
    <div className="flex h-screen flex-col">
      {/* Top bar */}
      <div className="flex items-center justify-between border-b border-border px-4 py-2">
        <div className="flex items-center gap-3">
          {isEditingName ? (
            <input
              value={appName}
              onChange={(e) => setAppName(e.target.value)}
              onBlur={handleSaveAppName}
              onKeyDown={(e) => e.key === 'Enter' && handleSaveAppName()}
              className="rounded border border-input bg-secondary px-2 py-0.5 text-sm font-semibold focus:outline-none focus:ring-2 focus:ring-ring"
              autoFocus
            />
          ) : (
            <h1
              className="cursor-pointer text-sm font-semibold hover:text-primary"
              onClick={() => setIsEditingName(true)}
              title="Click to rename"
            >
              {currentApp.name}
            </h1>
          )}
          <span className={cn(
            'rounded px-2 py-0.5 text-xs',
            currentApp.status === 'published'
              ? 'bg-success/10 text-success'
              : 'bg-muted text-muted-foreground'
          )}>
            {currentApp.status === 'published' ? `v${currentApp.current_version}` : 'Draft'}
          </span>
          {hasUnsavedChanges && (
            <span className="text-xs text-warning">Unsaved changes</span>
          )}
        </div>

        {/* Panel tabs */}
        <div className="flex items-center gap-1 rounded-lg bg-muted p-1">
          {([
            { key: 'chat' as Panel, icon: MessageSquare, label: 'Chat' },
            { key: 'code' as Panel, icon: Code2, label: 'Code' },
            { key: 'preview' as Panel, icon: Eye, label: 'Preview' },
          ]).map((tab) => (
            <button
              key={tab.key}
              onClick={() => setActivePanel(tab.key)}
              className={cn(
                'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
                activePanel === tab.key
                  ? 'bg-background text-foreground shadow-sm'
                  : 'text-muted-foreground hover:text-foreground'
              )}
            >
              <tab.icon size={14} />
              {tab.label}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-1">
          {/* AI Toggle */}
          <button
            onClick={async () => {
              await apiClient.put(`/apps/${currentApp.id}`, {
                ai_toggle_enabled: !currentApp.ai_toggle_enabled,
              })
              setCurrentApp({ ...currentApp, ai_toggle_enabled: !currentApp.ai_toggle_enabled })
            }}
            className={cn(
              'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              currentApp.ai_toggle_enabled
                ? 'bg-success/10 text-success'
                : 'text-muted-foreground hover:text-foreground'
            )}
            title="AI Toggle — embedded AI assistant for end users"
          >
            {currentApp.ai_toggle_enabled ? <ToggleRight size={16} /> : <ToggleLeft size={16} />}
            AI
          </button>

          {/* Bug widget toggle + auto-approve risk threshold */}
          <button
            onClick={async () => {
              const next = !currentApp.bug_widget_enabled
              await apiClient.put(`/apps/${currentApp.id}`, { bug_widget_enabled: next })
              setCurrentApp({ ...currentApp, bug_widget_enabled: next })
            }}
            className={cn(
              'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              currentApp.bug_widget_enabled
                ? 'bg-success/10 text-success'
                : 'text-muted-foreground hover:text-foreground'
            )}
            title="Bug Reporter — floating bug button + AI triage"
          >
            <Bug size={14} />
            Bugs
          </button>
          {currentApp.bug_widget_enabled && (
            <select
              value={currentApp.bug_fix_auto_approve_max_risk || 'none'}
              onChange={async (e) => {
                const v = e.target.value
                await apiClient.put(`/apps/${currentApp.id}`, { bug_fix_auto_approve_max_risk: v })
                setCurrentApp({ ...currentApp, bug_fix_auto_approve_max_risk: v as any })
              }}
              className="rounded-lg border border-input bg-secondary px-2 py-1 text-[11px] text-foreground"
              title="Auto-approve AI fixes at or below this risk level"
            >
              <option value="none" className="bg-popover text-foreground">Auto-fix: off</option>
              <option value="low" className="bg-popover text-foreground">Auto-fix: low risk</option>
              <option value="medium" className="bg-popover text-foreground">Auto-fix: low + medium</option>
            </select>
          )}

          {/* AI self-verify level — runs after every chat turn, fixes errors, loops up to max.
              Note: the <select> needs a solid bg-secondary (not bg-transparent), otherwise
              the OS-native <option> popup renders white-on-white and is unreadable. */}
          <div
            className="flex items-center gap-1"
            title="After each AI chat turn, verify the generated code and ask the AI to fix any errors before declaring done"
          >
            <ShieldCheck size={12} className={cn(
              currentApp.ai_verify_level && currentApp.ai_verify_level !== 'off'
                ? 'text-success'
                : 'text-muted-foreground'
            )} />
            <select
              value={currentApp.ai_verify_level || 'tsc_build_boot_runtime'}
              onChange={async (e) => {
                const v = e.target.value
                await apiClient.put(`/apps/${currentApp.id}`, { ai_verify_level: v })
                setCurrentApp({ ...currentApp, ai_verify_level: v as any })
              }}
              className="rounded-lg border border-input bg-secondary px-2 py-1 text-[11px] text-foreground focus:outline-none"
            >
              <option value="off" className="bg-popover text-foreground">Verify: off</option>
              <option value="tsc" className="bg-popover text-foreground">Verify: tsc</option>
              <option value="tsc_build" className="bg-popover text-foreground">Verify: tsc+build</option>
              <option value="tsc_build_boot" className="bg-popover text-foreground">Verify: +boot</option>
              <option value="tsc_build_boot_runtime" className="bg-popover text-foreground">Verify: +runtime (full)</option>
              <option value="tsc_build_boot_runtime_a11y" className="bg-popover text-foreground">Verify: +runtime +a11y</option>
            </select>
          </div>

          {/* Collaboration: live code view + auto-jump (this feature) */}
          <div className="mx-1 h-5 w-px bg-border" />

          {/* Live code view toggle — opens a side panel that streams files as the AI writes them */}
          <button
            onClick={() => {
              const next = !liveCodeEnabled
              setLiveCodeEnabled(next)
              setRightPanel(next ? 'live' : (rightPanel === 'live' ? 'none' : rightPanel))
            }}
            className={cn(
              'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              liveCodeEnabled
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground hover:text-foreground'
            )}
            title="Watch the AI write code live in a side panel"
          >
            <Radio size={14} />
            Live
          </button>

          {/* Auto-jump toggle — when on, code the AI references opens + highlights automatically */}
          <button
            onClick={() => setAutoJumpEnabled(!autoJumpEnabled)}
            className={cn(
              'flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs font-medium transition-colors',
              autoJumpEnabled
                ? 'bg-success/10 text-success'
                : 'text-muted-foreground hover:text-foreground'
            )}
            title={autoJumpEnabled
              ? 'Auto-jump ON — the AI opens & highlights code it points you to. Click to turn off (refs stay clickable).'
              : 'Auto-jump OFF — code references stay clickable but won’t auto-open. Click to turn on.'}
          >
            <LocateFixed size={14} />
          </button>

          {/* Versions panel toggle */}
          <button
            onClick={() => setRightPanel(rightPanel === 'versions' ? 'none' : 'versions')}
            className={cn(
              'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              rightPanel === 'versions'
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground hover:text-foreground'
            )}
            title="Version history"
          >
            <History size={14} />
          </button>

          {/* Analytics panel toggle */}
          <button
            onClick={() => setRightPanel(rightPanel === 'analytics' ? 'none' : 'analytics')}
            className={cn(
              'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              rightPanel === 'analytics'
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground hover:text-foreground'
            )}
            title="Usage analytics"
          >
            <BarChart3 size={14} />
          </button>

          {/* Settings panel toggle */}
          <button
            onClick={() => setRightPanel(rightPanel === 'settings' ? 'none' : 'settings')}
            className={cn(
              'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              rightPanel === 'settings'
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground hover:text-foreground'
            )}
            title="App settings"
          >
            <Settings size={14} />
          </button>

          {/* Permissions panel toggle */}
          <button
            onClick={() => setRightPanel(rightPanel === 'permissions' ? 'none' : 'permissions')}
            className={cn(
              'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              rightPanel === 'permissions'
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground hover:text-foreground'
            )}
            title="App permissions"
          >
            <Shield size={14} />
          </button>

          {/* Wizard panel toggle */}
          <button
            onClick={() => setRightPanel(rightPanel === 'wizard' ? 'none' : 'wizard')}
            className={cn(
              'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              rightPanel === 'wizard'
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground hover:text-foreground'
            )}
            title="Setup wizard"
          >
            <Wand2 size={14} />
          </button>

          {/* Data panel toggle */}
          <button
            onClick={() => setRightPanel(rightPanel === 'data' ? 'none' : 'data')}
            className={cn(
              'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              rightPanel === 'data'
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground hover:text-foreground'
            )}
            title="Data sources"
          >
            <Database size={14} />
          </button>

          {/* Deployments panel toggle */}
          <button
            onClick={() => setRightPanel(rightPanel === 'deployments' ? 'none' : 'deployments')}
            className={cn(
              'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              rightPanel === 'deployments'
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground hover:text-foreground'
            )}
            title="Deployments"
          >
            <Rocket size={14} />
          </button>

          {/* Delete button */}
          <button
            onClick={() => setShowDeleteDialog(true)}
            className="rounded-lg p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
            title="Delete app"
          >
            <Trash2 size={14} />
          </button>

          {/* Save a new local version snapshot */}
          <button
            onClick={() => setShowPublishDialog(true)}
            title="Save a new immutable version snapshot (stored locally in the builder)"
            className="ml-2 flex items-center gap-1.5 rounded-lg bg-primary px-4 py-1.5 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
          >
            <Save size={14} />
            Save Version
          </button>

          {/* Share publicly on the marketplace (needs a published version first) */}
          {currentApp.status === 'published' && (
            <button
              onClick={() => setShowMarketplaceDialog(true)}
              title="Share this app publicly on the EveriApp Marketplace"
              className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              <Upload size={14} />
              To Marketplace
            </button>
          )}
        </div>
      </div>

      {/* Main content area */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left: Main panel (chat / code / preview) */}
        <div className="flex flex-1 overflow-hidden">
          {/* Chat panel */}
          {activePanel === 'chat' && (
            <div className="flex-1">
              <ChatPanel appId={currentApp.id} />
            </div>
          )}

          {/* Code panel */}
          {activePanel === 'code' && (
            <>
              {showFileTree && (
                <div className="w-56 border-r border-border bg-card">
                  <div className="flex items-center justify-between border-b border-border px-3 py-2">
                    <span className="text-xs font-medium text-muted-foreground">FILES</span>
                    <button onClick={() => setShowFileTree(false)} className="text-muted-foreground hover:text-foreground">
                      <PanelLeftClose size={14} />
                    </button>
                  </div>
                  <FileTree files={files} selectedFile={activeTabPath} onSelectFile={handleSelectFile} />
                </div>
              )}

              <div className="flex flex-1 flex-col">
                {/* File tabs */}
                <div className="flex items-center border-b border-border">
                  {!showFileTree && (
                    <button onClick={() => setShowFileTree(true)} className="px-2 text-muted-foreground hover:text-foreground">
                      <PanelLeftOpen size={14} />
                    </button>
                  )}
                  <div className="flex flex-1 overflow-x-auto">
                    {openTabs.map((tab) => {
                      const isModified = tab.content !== tab.savedContent
                      const isActive = tab.path === activeTabPath
                      const fileName = tab.path.split('/').pop()
                      return (
                        <button
                          key={tab.path}
                          onClick={() => setActiveTabPath(tab.path)}
                          className={cn(
                            'group flex items-center gap-1.5 border-r border-border px-3 py-1.5 text-xs',
                            isActive
                              ? 'bg-background text-foreground'
                              : 'bg-muted/50 text-muted-foreground hover:text-foreground'
                          )}
                        >
                          {isModified && <Circle size={6} className="fill-warning text-warning" />}
                          <span className="max-w-32 truncate">{fileName}</span>
                          <button
                            onClick={(e) => { e.stopPropagation(); handleCloseTab(tab.path) }}
                            className="ml-1 rounded opacity-0 hover:bg-accent group-hover:opacity-100"
                          >
                            <X size={12} />
                          </button>
                        </button>
                      )
                    })}
                  </div>
                  {activeTab && activeTab.content !== activeTab.savedContent && (
                    <button
                      onClick={handleSaveCurrentFile}
                      disabled={isSaving}
                      className="flex items-center gap-1 px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
                      title="Save (Ctrl+S)"
                    >
                      {isSaving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
                    </button>
                  )}
                  {/* Collaboration overlay toggle */}
                  <button
                    onClick={() => toggleCollab(!showCollab)}
                    className={cn(
                      'ml-auto flex items-center gap-1 px-3 py-1.5 text-xs font-medium transition-colors',
                      showCollab ? 'text-primary' : 'text-muted-foreground hover:text-foreground'
                    )}
                    title="Collaborate — a movable chat over the code that knows the file & selection you're viewing"
                  >
                    <MessageSquarePlus size={13} />
                    Collaborate
                  </button>
                </div>

                <div className="relative flex-1">
                  {activeTab ? (
                    <CodeEditor
                      value={activeTab.content}
                      language={activeTab.language}
                      onChange={handleTabContentChange}
                      highlight={activeTab.path === codeNav?.path ? navHighlight : null}
                      revealToken={navRevealToken}
                      onContextChange={handleEditorContextChange}
                    />
                  ) : (
                    <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                      Select a file from the tree to edit
                    </div>
                  )}
                  {/* Floating in-code collaboration chat — drag, minimize, see-through */}
                  {showCollab && (
                    <CollabOverlay appId={currentApp.id} onClose={() => toggleCollab(false)} />
                  )}
                </div>
              </div>
            </>
          )}

          {/* Preview panel */}
          {activePanel === 'preview' && (
            <div className="flex flex-1 flex-col">
              {/* Preview toolbar */}
              <div className="flex items-center gap-2 border-b border-border px-3 py-2">
                {runtimeStatus === 'stopped' || runtimeStatus === 'error' ? (
                  <button
                    onClick={handleStartPreview}
                    className="flex items-center gap-1.5 rounded-lg bg-success px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-success/90"
                  >
                    <Play size={12} />
                    Start Preview
                  </button>
                ) : runtimeStatus === 'starting' ? (
                  <button disabled className="flex items-center gap-1.5 rounded-lg bg-muted px-3 py-1.5 text-xs font-medium text-muted-foreground">
                    <Loader2 size={12} className="animate-spin" />
                    Starting...
                  </button>
                ) : (
                  <>
                    <button
                      onClick={handleStopPreview}
                      className="flex items-center gap-1.5 rounded-lg bg-destructive/10 px-3 py-1.5 text-xs font-medium text-destructive transition-colors hover:bg-destructive/20"
                    >
                      <Square size={12} />
                      Stop
                    </button>
                    <button
                      onClick={handleStartPreview}
                      className="flex items-center gap-1.5 rounded-lg bg-muted px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
                      title="Restart"
                    >
                      <RefreshCw size={12} />
                      Restart
                    </button>
                    <button
                      onClick={handleRefreshPreview}
                      className="rounded-lg p-1.5 text-muted-foreground transition-colors hover:text-foreground"
                      title="Reload iframe"
                    >
                      <RotateCcw size={12} />
                    </button>
                  </>
                )}
                <div className="ml-auto flex items-center gap-2">
                  <span className={cn(
                    'flex items-center gap-1 text-xs',
                    runtimeStatus === 'running' ? 'text-success' :
                    runtimeStatus === 'starting' ? 'text-warning' :
                    runtimeStatus === 'error' ? 'text-destructive' :
                    'text-muted-foreground'
                  )}>
                    <Circle size={6} className={cn(
                      'fill-current',
                      runtimeStatus === 'running' && 'animate-pulse'
                    )} />
                    {runtimeStatus === 'running' ? 'Running' :
                     runtimeStatus === 'starting' ? 'Starting' :
                     runtimeStatus === 'error' ? 'Error' : 'Stopped'}
                  </span>
                </div>
              </div>

              {/* Preview content — served through the platform proxy (/apps/{id}/), NOT the raw
                  Vite port, so the runtime proxy injects the SDK globals (window.__AIHUB_APP_ID__ /
                  __AIHUB_TOKEN__) and the app is same-origin with /api (useDataset etc. work). The
                  dev's token rides in the query param (iframe nav can't send an auth header). */}
              {runtimeStatus === 'running' && runtimePort ? (
                <iframe
                  key={previewKey}
                  src={`${import.meta.env.DEV ? 'http://localhost:8800' : ''}/apps/${currentApp.id}/?__aihub_token=${encodeURIComponent(token || '')}`}
                  className="flex-1 border-0"
                  title="App Preview"
                />
              ) : runtimeStatus === 'error' ? (
                <div className="flex flex-1 flex-col items-center justify-center gap-3">
                  <AlertCircle size={40} className="text-destructive/40" />
                  <p className="max-w-sm text-center text-sm text-muted-foreground">
                    {runtimeError || 'An error occurred while starting the app'}
                  </p>
                  <button
                    onClick={handleStartPreview}
                    className="flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-xs font-medium text-primary-foreground hover:bg-primary/90"
                  >
                    <RefreshCw size={12} />
                    Retry
                  </button>
                </div>
              ) : runtimeStatus === 'starting' ? (
                <div className="flex flex-1 items-center justify-center">
                  <div className="max-w-md text-center">
                    <Loader2 size={32} className="mx-auto animate-spin text-primary" />
                    <p className="mt-3 text-sm font-medium text-foreground">
                      {runtimePhase ? PHASE_LABELS[runtimePhase] || runtimePhase : 'Starting dev server...'}
                      {runtimePhaseElapsed != null && runtimePhaseElapsed > 1 && (
                        <span className="ml-2 text-xs font-normal text-muted-foreground">
                          ({Math.round(runtimePhaseElapsed)}s)
                        </span>
                      )}
                    </p>
                    {runtimePhaseDetail && (
                      <p className="mt-1 text-xs text-muted-foreground">{runtimePhaseDetail}</p>
                    )}
                    {runtimePhase === 'installing' && (
                      <p className="mt-3 text-xs text-muted-foreground/70">
                        This only happens once — subsequent starts are instant.
                      </p>
                    )}
                  </div>
                </div>
              ) : (
                <div className="flex flex-1 items-center justify-center">
                  <div className="text-center">
                    <Eye size={48} className="mx-auto text-muted-foreground/30" />
                    <p className="mt-4 text-sm text-muted-foreground">
                      Click "Start Preview" to run your app
                    </p>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Right panel: Versions or Settings */}
        {rightPanel !== 'none' && (
          <div
            className="relative shrink-0 border-l border-border bg-card"
            style={{ width: rightPanelWidths[rightPanel === 'live' ? 'live' : 'other'] }}
          >
            {/* Drag handle: straddles the left border; double-click resets to default width. */}
            <div
              onPointerDown={startPanelResize}
              onDoubleClick={resetPanelWidth}
              title="Drag to resize"
              className={cn(
                'absolute inset-y-0 -left-1 z-20 w-2 cursor-col-resize transition-colors hover:bg-primary/40',
                isResizingPanel && 'bg-primary/40'
              )}
            />
            {rightPanel === 'live' && <LiveCodePanel />}
            {rightPanel === 'versions' && (
              <VersionsPanel
                appId={currentApp.id}
                versions={versions}
                currentVersion={currentApp.current_version}
                appStatus={currentApp.status}
                onRollback={handleRollback}
                onRefresh={loadVersions}
                onPublishToMarketplace={() => setShowMarketplaceDialog(true)}
              />
            )}
            {rightPanel === 'analytics' && (
              <AppAnalyticsPanel appId={currentApp.id} />
            )}
            {rightPanel === 'settings' && (
              <AppSettingsPanel appId={currentApp.id} />
            )}
            {rightPanel === 'permissions' && (
              <AppPermissionsPanel appId={currentApp.id} />
            )}
            {rightPanel === 'wizard' && (
              <SetupWizardPreview appId={currentApp.id} />
            )}
            {rightPanel === 'deployments' && (
              <DeploymentsPanel
                appId={currentApp.id}
                currentVersion={currentApp.current_version}
              />
            )}
            {rightPanel === 'data' && (
              <AppDataPanel appId={currentApp.id} />
            )}
          </div>
        )}
      </div>

      {/* While dragging the panel edge, a transparent overlay keeps pointer events
          away from the preview iframe (which would otherwise swallow the drag). */}
      {isResizingPanel && (
        <div className="fixed inset-0 z-50 cursor-col-resize select-none" />
      )}

      {/* Save Version dialog (local version snapshot) */}
      {showPublishDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div className="w-full max-w-md rounded-2xl border border-border bg-card p-6">
            <h2 className="text-lg font-semibold">Save Version</h2>

            {publishResult ? (
              <div className="mt-4">
                <div className="rounded-lg bg-green-500/10 p-3 text-sm text-green-700 dark:text-green-300">
                  <p className="font-medium">Version {publishResult.version} created.</p>
                  <p className="mt-1 text-xs">
                    Saved as an immutable snapshot in the builder. To share this app publicly,
                    publish it to the marketplace.
                  </p>
                </div>
                <div className="mt-4 flex justify-end gap-2">
                  <button
                    onClick={closePublishDialog}
                    className="rounded-lg px-4 py-2 text-sm text-muted-foreground hover:text-foreground"
                  >
                    Done
                  </button>
                  <button
                    onClick={() => { closePublishDialog(); setShowMarketplaceDialog(true) }}
                    className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
                  >
                    <Upload size={14} />
                    Publish to Marketplace
                  </button>
                </div>
              </div>
            ) : (
              <>
                <p className="mt-1 text-sm text-muted-foreground">
                  This will create version {currentApp.current_version + 1} as an immutable snapshot,
                  stored locally in the builder. It does not publish to the marketplace.
                </p>
                {publishError && (
                  <div className="mt-3 flex items-start gap-2 rounded-lg bg-red-500/10 p-3 text-sm text-red-700 dark:text-red-300">
                    <AlertCircle size={16} className="mt-0.5 shrink-0" />
                    <span>{publishError}</span>
                  </div>
                )}
                <div className="mt-4">
                  <label className="mb-1 block text-xs font-medium text-muted-foreground">Release Notes</label>
                  <textarea
                    value={publishNotes}
                    onChange={(e) => setPublishNotes(e.target.value)}
                    className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                    placeholder="What changed in this version?"
                    rows={3}
                  />
                </div>
                <div className="mt-4 flex justify-end gap-2">
                  <button
                    onClick={closePublishDialog}
                    className="rounded-lg px-4 py-2 text-sm text-muted-foreground hover:text-foreground"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handlePublish}
                    disabled={isPublishing}
                    className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                  >
                    {isPublishing && <Loader2 size={14} className="animate-spin" />}
                    Save v{currentApp.current_version + 1}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* Publish to Marketplace dialog */}
      {showMarketplaceDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div className="max-h-[88vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-border bg-card p-6">
            <h2 className="text-lg font-semibold flex items-center gap-2">
              <Upload size={18} />
              Publish to Marketplace
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Publish &quot;{currentApp.name}&quot; to the EveriApp Marketplace website.
            </p>

            {marketplaceResult ? (
              <div className="mt-4">
                <div className={cn(
                  'rounded-lg p-3 text-sm',
                  marketplaceResult.url ? 'bg-green-500/10 text-green-700 dark:text-green-300' : 'bg-red-500/10 text-red-700 dark:text-red-300'
                )}>
                  <p>{marketplaceResult.message}</p>
                  {marketplaceResult.url && (
                    <a
                      href={marketplaceResult.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="mt-2 block underline text-xs"
                    >
                      View on Marketplace →
                    </a>
                  )}
                </div>
                <div className="mt-4 flex justify-end">
                  <button
                    onClick={() => { setShowMarketplaceDialog(false); setMarketplaceResult(null) }}
                    className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
                  >
                    Done
                  </button>
                </div>
              </div>
            ) : (
              <>
                {marketplaceConfig && !marketplaceConfig.configured && (
                  <div className="mt-4 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300">
                    <p className="font-medium">Marketplace publishing isn&apos;t set up yet.</p>
                    <p className="mt-1">
                      {!marketplaceConfig.url_configured
                        ? 'The marketplace URL is missing. '
                        : !marketplaceConfig.key_configured
                          ? 'A developer API key is missing. '
                          : ''}
                      You need a free API key from the EveriApp Marketplace, saved under Platform → Settings.
                    </p>
                    <div className="mt-2 flex items-center gap-4">
                      {user?.role === 'admin' ? (
                        <button
                          type="button"
                          onClick={() => navigate('/admin/platform')}
                          className="font-medium underline hover:opacity-80"
                        >
                          Configure →
                        </button>
                      ) : (
                        <span className="opacity-80">Ask an admin to configure it under Platform → Settings.</span>
                      )}
                      <a
                        href={`${marketplaceConfig.marketplace_url || 'https://aihub-marketplace.vercel.app'}/publish`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="font-medium underline hover:opacity-80"
                      >
                        Get a free API key →
                      </a>
                    </div>
                  </div>
                )}
                <div className="mt-4 space-y-5">
                  {/* ── Listing ─────────────────────────────────────── */}
                  <section className="space-y-3">
                    <div className="flex items-center justify-between border-b border-border pb-1.5">
                      <h3 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Listing</h3>
                      <button
                        type="button"
                        onClick={handleSuggestMetadata}
                        disabled={isSuggesting}
                        className="flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50"
                        title="Let the AI draft the description, tags, release notes and setup instructions from the app's code"
                      >
                        {isSuggesting ? <Loader2 size={12} className="animate-spin" /> : <Wand2 size={12} />}
                        {isSuggesting ? 'Drafting…' : 'Suggest with AI'}
                      </button>
                    </div>
                    {suggestError && (
                      <p className="rounded-lg bg-red-500/10 p-2 text-xs text-red-700 dark:text-red-300">{suggestError}</p>
                    )}
                    <div>
                      <label className="mb-1 block text-xs font-medium text-muted-foreground">Short description</label>
                      <input
                        value={marketplaceShortDesc}
                        onChange={(e) => setMarketplaceShortDesc(e.target.value)}
                        maxLength={300}
                        className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                        placeholder="One-line summary shown on gallery cards"
                      />
                    </div>
                    <div>
                      <label className="mb-1 block text-xs font-medium text-muted-foreground">Description (markdown)</label>
                      <textarea
                        value={marketplaceDescription}
                        onChange={(e) => setMarketplaceDescription(e.target.value)}
                        rows={4}
                        className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                        placeholder={'The full listing page — markdown supported.\n\n## Features\n- ...'}
                      />
                    </div>
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <label className="mb-1 block text-xs font-medium text-muted-foreground">Category</label>
                        <select
                          value={marketplaceCategory}
                          onChange={(e) => setMarketplaceCategory(e.target.value)}
                          className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                        >
                          {['general','productivity','finance','communication','analytics','developer-tools','design','marketing','hr','education','entertainment','utilities'].map(cat => (
                            <option key={cat} value={cat}>{cat.charAt(0).toUpperCase() + cat.slice(1).replace('-', ' ')}</option>
                          ))}
                        </select>
                      </div>
                      <div>
                        <label className="mb-1 block text-xs font-medium text-muted-foreground">License</label>
                        <select
                          value={marketplaceLicense}
                          onChange={(e) => setMarketplaceLicense(e.target.value)}
                          className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                        >
                          {['MIT', 'Apache-2.0', 'GPL-3.0', 'BSD-3-Clause', 'proprietary'].map((l) => (
                            <option key={l} value={l}>{l}</option>
                          ))}
                        </select>
                      </div>
                    </div>
                    <div>
                      <label className="mb-1 block text-xs font-medium text-muted-foreground">Tags (comma-separated)</label>
                      <input
                        value={marketplaceTags}
                        onChange={(e) => setMarketplaceTags(e.target.value)}
                        className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                        placeholder="dashboard, sales, reporting"
                      />
                    </div>
                  </section>

                  {/* ── Version & release ───────────────────────────── */}
                  <section className="space-y-3">
                    <h3 className="border-b border-border pb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Version &amp; release</h3>
                  <div>
                    <label className="mb-1 block text-xs font-medium text-muted-foreground">Release version</label>
                    {effectiveLast ? (
                      <>
                        <div className="mb-1 text-[11px] text-muted-foreground">
                          Last published: v{effectiveLast}
                        </div>
                        <div className="flex items-center gap-2">
                          <div className="flex overflow-hidden rounded-lg border border-input text-[11px]">
                            {(['patch', 'minor', 'major'] as const).map((part) => {
                              const next = bumpSemver(effectiveLast, part)
                              const active = marketplaceSemver === next
                              const taken = publishedVersions.includes(next)
                              return (
                                <button
                                  key={part}
                                  type="button"
                                  disabled={taken}
                                  title={taken ? `v${next} is already published` : undefined}
                                  onClick={() => setMarketplaceSemver(next)}
                                  className={cn(
                                    'border-r border-input px-2 py-1.5 capitalize last:border-r-0 disabled:opacity-40 disabled:line-through',
                                    active ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-accent',
                                  )}
                                >
                                  {part} → {next}
                                </button>
                              )
                            })}
                          </div>
                          <input
                            value={marketplaceSemver}
                            onChange={(e) => setMarketplaceSemver(e.target.value.trim())}
                            className="w-20 rounded-lg border border-input bg-secondary px-2 py-1.5 text-center font-mono text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                          />
                        </div>
                      </>
                    ) : (
                      <div className="flex items-center gap-2">
                        <input
                          value={marketplaceSemver}
                          onChange={(e) => setMarketplaceSemver(e.target.value.trim())}
                          className="w-24 rounded-lg border border-input bg-secondary px-2 py-1.5 text-center font-mono text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                        />
                        <span className="text-[11px] text-muted-foreground">First release — semver, e.g. 1.0.0</span>
                      </div>
                    )}
                    {!isValidSemver(marketplaceSemver) && (
                      <p className="mt-1 text-xs text-destructive">Enter a valid version like 1.2.3</p>
                    )}
                    {isValidSemver(marketplaceSemver) && publishedVersions.includes(marketplaceSemver) && (
                      <p className="mt-1 text-xs text-destructive">
                        v{marketplaceSemver} is already published — choose a different version.
                      </p>
                    )}
                    {effectiveLast && isValidSemver(marketplaceSemver) &&
                      !publishedVersions.includes(marketplaceSemver) &&
                      cmpSemver(marketplaceSemver, effectiveLast) < 0 && (
                        <p className="mt-1 rounded-lg bg-amber-500/10 p-2 text-xs text-amber-700 dark:text-amber-300">
                          Lower than your last release v{effectiveLast} — it&apos;ll be published, but v{effectiveLast} stays the default download.
                        </p>
                      )}
                  </div>
                  <div>
                    <label className="mb-1 block text-xs font-medium text-muted-foreground">Code snapshot to ship</label>
                    <select
                      value={marketplaceVersion ?? ''}
                      onChange={(e) => setMarketplaceVersion(e.target.value === '' ? null : Number(e.target.value))}
                      className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                    >
                      <option value="">{`v${currentApp.current_version} — latest`}</option>
                      {versions.filter((v) => v.version !== currentApp.current_version).map((v) => (
                        <option key={v.id} value={v.version}>
                          {`v${v.version}${v.notes ? ` — ${v.notes.slice(0, 60)}` : ''}`}
                        </option>
                      ))}
                    </select>
                    {marketplaceVersion !== null && marketplaceVersion < currentApp.current_version && (
                      <p className="mt-1 rounded-lg bg-amber-500/10 p-2 text-xs text-amber-700 dark:text-amber-300">
                        You&apos;re shipping an older code snapshot — auto-screenshots are skipped
                        (they&apos;d show the current draft). Set the release version above accordingly.
                      </p>
                    )}
                  </div>
                  <div>
                    <label className="mb-1 block text-xs font-medium text-muted-foreground">Release notes (markdown)</label>
                    <textarea
                      value={marketplaceNotes}
                      onChange={(e) => setMarketplaceNotes(e.target.value)}
                      rows={2}
                      className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                      placeholder="What's new in this version? Markdown supported."
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs font-medium text-muted-foreground">Setup instructions (markdown)</label>
                    <textarea
                      value={marketplaceSetupInstructions}
                      onChange={(e) => setMarketplaceSetupInstructions(e.target.value)}
                      rows={3}
                      className="w-full rounded-lg border border-input bg-secondary px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                      placeholder={'What must users do after installing? e.g.\n1. Ask IT for a read-only ERP account\n2. Paste the API key in the setup wizard'}
                    />
                    <p className="mt-0.5 text-[10px] text-muted-foreground">
                      Shown on the marketplace listing so users know what the app needs to run.
                    </p>
                  </div>
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={marketplaceShots}
                        onChange={(e) => setMarketplaceShots(e.target.checked)}
                        className="rounded border-input"
                      />
                      Capture screenshots automatically
                      <span className="text-[10px] text-muted-foreground">(boots the app headlessly — adds ~30s)</span>
                    </label>
                  </section>
                </div>
                <div className="mt-5 flex items-center justify-between gap-3 border-t border-border pt-4">
                  <p className="text-[10px] text-muted-foreground">
                    Credentials from Platform → Settings → EveriApp Marketplace.
                  </p>
                  <div className="flex gap-2">
                    <button
                      onClick={() => setShowMarketplaceDialog(false)}
                      className="rounded-lg px-4 py-2 text-sm text-muted-foreground hover:text-foreground"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={handlePublishToMarketplace}
                      disabled={isPublishingToMarketplace || !isValidSemver(marketplaceSemver) || publishedVersions.includes(marketplaceSemver)}
                      className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                    >
                      {isPublishingToMarketplace && <Loader2 size={14} className="animate-spin" />}
                      {isPublishingToMarketplace ? 'Publishing…' : 'Publish to Marketplace'}
                    </button>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      <ConfirmDialog
        open={showDeleteDialog}
        onClose={() => setShowDeleteDialog(false)}
        onConfirm={handleDeleteApp}
        title="Delete App"
        description={`Are you sure you want to delete "${currentApp?.name}"? All versions, files, and settings will be permanently removed. This cannot be undone.`}
        confirmLabel="Delete App"
        variant="danger"
        isLoading={isDeleting}
      />
    </div>
  )
}

// ---- Versions Panel ----
function VersionsPanel({
  appId,
  versions,
  currentVersion,
  appStatus,
  onRollback,
  onRefresh,
  onPublishToMarketplace,
}: {
  appId: string
  versions: Version[]
  currentVersion: number
  appStatus: string
  onRollback: (version: number) => void
  onRefresh: () => void
  onPublishToMarketplace: () => void
}) {
  // When set, render the diff modal for these two refs ("draft" or a number).
  const [diffRefs, setDiffRefs] = useState<{ from: string; to: string } | null>(null)
  const [showEmbed, setShowEmbed] = useState(false)
  const [showDeps, setShowDeps] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const [showTraces, setShowTraces] = useState(false)

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h3 className="text-sm font-semibold">Version History</h3>
        <div className="flex items-center gap-2">
          <button onClick={() => setShowHistory(true)}
                  className="rounded px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground"
                  title="Rewind the draft to a prior AI turn">History</button>
          <button onClick={() => setShowDeps(true)}
                  className="rounded px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground"
                  title="Check dependencies for advisories">Deps</button>
          <button onClick={() => setShowTraces(true)}
                  className="rounded px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground"
                  title="Inspect what the AI did on each build — full traceability">Traces</button>
          {currentVersion > 0 && (
            <button
              onClick={() => setDiffRefs({ from: String(currentVersion), to: 'draft' })}
              className="rounded px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground"
              title="Compare unpublished draft to the current version"
            >
              Draft vs v{currentVersion}
            </button>
          )}
          <button onClick={onRefresh} className="text-muted-foreground hover:text-foreground">
            <RotateCcw size={14} />
          </button>
        </div>
      </div>
      {diffRefs && (
        <VersionDiffModal
          appId={appId}
          fromRef={diffRefs.from}
          toRef={diffRefs.to}
          onClose={() => setDiffRefs(null)}
        />
      )}
      {showDeps && <DependencyScanModal appId={appId} onClose={() => setShowDeps(false)} />}
      {showHistory && <RewindModal appId={appId} onClose={() => setShowHistory(false)} />}
      {showTraces && <TracesModal appId={appId} onClose={() => setShowTraces(false)} />}
      <div className="flex-1 overflow-y-auto p-3">
        {versions.length === 0 ? (
          <div className="py-8 text-center text-xs text-muted-foreground">
            No published versions yet
          </div>
        ) : (
          <div className="space-y-2">
            {versions.map((v) => (
              <div
                key={v.id}
                className={cn(
                  'rounded-lg border border-border p-3',
                  v.version === currentVersion && 'border-primary/30 bg-primary/5'
                )}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">v{v.version}</span>
                    {v.version === currentVersion && (
                      <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                        Current
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-1">
                    {v.version !== currentVersion && currentVersion > 0 && (
                      <button
                        onClick={() => setDiffRefs({ from: String(v.version), to: String(currentVersion) })}
                        className="flex items-center gap-1 rounded px-2 py-1 text-[10px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                        title={`Compare v${v.version} to current v${currentVersion}`}
                      >
                        <FileDiff size={10} />
                        Diff
                      </button>
                    )}
                    {v.version !== currentVersion && (
                      <button
                        onClick={() => onRollback(v.version)}
                        className="flex items-center gap-1 rounded px-2 py-1 text-[10px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                      >
                        <RotateCcw size={10} />
                        Rollback
                      </button>
                    )}
                  </div>
                </div>
                {v.notes && (
                  <p className="mt-1 text-xs text-muted-foreground">{v.notes}</p>
                )}
                <p className="mt-1 text-[10px] text-muted-foreground/60">
                  {new Date(v.created_at).toLocaleString()}
                </p>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Distribution actions */}
      {appStatus === 'published' && versions.length > 0 && (
        <div className="space-y-2 border-t border-border p-3">
          <button
            onClick={onPublishToMarketplace}
            className="flex w-full items-center justify-center gap-2 rounded-lg border border-border px-3 py-2 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <Upload size={12} />
            Publish to Marketplace
          </button>
          <button
            onClick={() => setShowEmbed(true)}
            className="flex w-full items-center justify-center gap-2 rounded-lg border border-border px-3 py-2 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <Code2 size={12} />
            Embed in a page
          </button>
        </div>
      )}
      {showEmbed && <EmbedModal appId={appId} onClose={() => setShowEmbed(false)} />}
    </div>
  )
}

// ---- App Settings Panel ----
function AppSettingsPanel({ appId }: { appId: string }) {
  const [settings, setSettings] = useState<any[]>([])
  const [globalSecrets, setGlobalSecrets] = useState<any[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [showAddForm, setShowAddForm] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')
  const [editSecretRef, setEditSecretRef] = useState('')
  const [newSetting, setNewSetting] = useState({
    key: '', label: '', type: 'string', description: '', required: false, default_value: '',
  })
  const user = useAuthStore((s) => s.user)

  const fetchSettings = async () => {
    setIsLoading(true)
    try {
      const data = await apiClient.get<any[]>(`/apps/${appId}/settings`)
      setSettings(data)
    } catch {
      setSettings([])
    } finally {
      setIsLoading(false)
    }
  }

  const fetchGlobalSecrets = async () => {
    if (user?.role !== 'admin') return
    try {
      const data = await apiClient.get<any[]>('/secrets')
      setGlobalSecrets(data)
    } catch {
      setGlobalSecrets([])
    }
  }

  useEffect(() => { fetchSettings(); fetchGlobalSecrets() }, [appId])

  const handleAddSetting = async () => {
    try {
      await apiClient.post(`/apps/${appId}/settings`, newSetting)
      setShowAddForm(false)
      setNewSetting({ key: '', label: '', type: 'string', description: '', required: false, default_value: '' })
      fetchSettings()
    } catch {
      // ignore
    }
  }

  const handleDeleteSetting = async (settingId: string) => {
    try {
      await apiClient.delete(`/apps/${appId}/settings/${settingId}`)
      fetchSettings()
    } catch {
      // ignore
    }
  }

  const handleSaveValue = async (settingId: string) => {
    try {
      const update: any = {}
      if (editSecretRef) {
        update.global_secret_ref = editSecretRef
        update.value = ''
      } else {
        update.value = editValue
        update.global_secret_ref = ''
      }
      await apiClient.put(`/apps/${appId}/settings/${settingId}`, update)
      setEditingId(null)
      setEditValue('')
      setEditSecretRef('')
      fetchSettings()
    } catch {
      // ignore
    }
  }

  const startEditing = (s: any) => {
    setEditingId(s.id)
    setEditValue(s.type === 'secret' ? '' : (s.value || s.default_value || ''))
    setEditSecretRef(s.global_secret_ref || '')
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h3 className="text-sm font-semibold">App Settings</h3>
        <button
          onClick={() => setShowAddForm(!showAddForm)}
          className="text-muted-foreground hover:text-foreground"
        >
          <Plus size={14} />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-3">
        {showAddForm && (
          <div className="mb-3 rounded-lg border border-primary/20 bg-primary/5 p-3">
            <p className="mb-2 text-[10px] font-medium text-primary">New Setting</p>
            <div className="space-y-2">
              <input
                value={newSetting.key}
                onChange={(e) => setNewSetting({ ...newSetting, key: e.target.value.replace(/[^a-z0-9_]/gi, '_').toLowerCase() })}
                className="w-full rounded border border-input bg-secondary px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                placeholder="Setting key (e.g., api_url)"
              />
              <input
                value={newSetting.label}
                onChange={(e) => setNewSetting({ ...newSetting, label: e.target.value })}
                className="w-full rounded border border-input bg-secondary px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                placeholder="Display label"
              />
              <input
                value={newSetting.description}
                onChange={(e) => setNewSetting({ ...newSetting, description: e.target.value })}
                className="w-full rounded border border-input bg-secondary px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                placeholder="Description (optional)"
              />
              <div className="flex gap-2">
                <select
                  value={newSetting.type}
                  onChange={(e) => setNewSetting({ ...newSetting, type: e.target.value })}
                  className="flex-1 rounded border border-input bg-secondary px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                >
                  <option value="string">String</option>
                  <option value="secret">Secret</option>
                  <option value="number">Number</option>
                  <option value="boolean">Boolean</option>
                  <option value="url">URL</option>
                </select>
                <label className="flex items-center gap-1 text-[10px] text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={newSetting.required}
                    onChange={(e) => setNewSetting({ ...newSetting, required: e.target.checked })}
                    className="rounded"
                  />
                  Required
                </label>
              </div>
              <input
                value={newSetting.default_value}
                onChange={(e) => setNewSetting({ ...newSetting, default_value: e.target.value })}
                className="w-full rounded border border-input bg-secondary px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                placeholder="Default value (optional)"
              />
              <div className="flex gap-2">
                <button onClick={() => setShowAddForm(false)} className="flex-1 rounded px-2 py-1 text-xs text-muted-foreground hover:bg-accent">
                  Cancel
                </button>
                <button
                  onClick={handleAddSetting}
                  disabled={!newSetting.key || !newSetting.label}
                  className="flex-1 rounded bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                >
                  Add Setting
                </button>
              </div>
            </div>
          </div>
        )}

        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 size={16} className="animate-spin text-muted-foreground" />
          </div>
        ) : settings.length === 0 ? (
          <div className="py-8 text-center text-xs text-muted-foreground">
            <Settings size={24} className="mx-auto mb-2 text-muted-foreground/30" />
            <p>No settings defined</p>
            <p className="mt-1">Settings let your app receive configuration from the platform</p>
          </div>
        ) : (
          <div className="space-y-2">
            {settings.map((s: any) => (
              <div key={s.id} className="group rounded-lg border border-border p-3 transition-colors hover:border-border/80">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-1.5">
                    <span className="text-xs font-medium">{s.label}</span>
                    {s.required && (
                      <span className="text-[9px] text-destructive">*</span>
                    )}
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                      {s.type}
                    </span>
                    <button
                      onClick={() => handleDeleteSetting(s.id)}
                      className="rounded p-0.5 text-muted-foreground/0 transition-colors hover:bg-destructive/10 hover:text-destructive group-hover:text-muted-foreground"
                      title="Delete setting"
                    >
                      <X size={12} />
                    </button>
                  </div>
                </div>
                <p className="mt-0.5 font-mono text-[10px] text-muted-foreground">{s.key}</p>
                {s.description && (
                  <p className="mt-0.5 text-[10px] text-muted-foreground/70">{s.description}</p>
                )}

                {/* Value display / edit */}
                {editingId === s.id ? (
                  <div className="mt-2 space-y-1.5">
                    {/* Option: use global secret ref */}
                    {globalSecrets.length > 0 && (s.type === 'secret' || s.type === 'string') && (
                      <div>
                        <label className="mb-0.5 block text-[10px] text-muted-foreground">
                          Use global secret
                        </label>
                        <select
                          value={editSecretRef}
                          onChange={(e) => { setEditSecretRef(e.target.value); if (e.target.value) setEditValue('') }}
                          className="w-full rounded border border-input bg-secondary px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                        >
                          <option value="">— Enter value directly —</option>
                          {globalSecrets.map((gs: any) => (
                            <option key={gs.id} value={gs.id}>
                              {gs.name} ({gs.category})
                            </option>
                          ))}
                        </select>
                      </div>
                    )}
                    {/* Direct value input */}
                    {!editSecretRef && (
                      <input
                        value={editValue}
                        onChange={(e) => setEditValue(e.target.value)}
                        type={s.type === 'secret' ? 'password' : s.type === 'number' ? 'number' : 'text'}
                        className="w-full rounded border border-input bg-secondary px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                        placeholder={s.type === 'secret' ? 'Enter new secret value' : `Enter ${s.type} value`}
                      />
                    )}
                    <div className="flex gap-1.5">
                      <button
                        onClick={() => { setEditingId(null); setEditValue(''); setEditSecretRef('') }}
                        className="flex-1 rounded px-2 py-1 text-[10px] text-muted-foreground hover:bg-accent"
                      >
                        Cancel
                      </button>
                      <button
                        onClick={() => handleSaveValue(s.id)}
                        disabled={!editValue && !editSecretRef}
                        className="flex-1 rounded bg-primary px-2 py-1 text-[10px] text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                      >
                        Save
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="mt-1.5">
                    {s.global_secret_ref ? (
                      <button
                        onClick={() => startEditing(s)}
                        className="flex w-full items-center gap-1 rounded border border-primary/20 bg-primary/5 px-2 py-1 text-left text-[10px] text-primary hover:bg-primary/10"
                      >
                        <ChevronRight size={10} />
                        Linked to global secret
                      </button>
                    ) : s.is_set ? (
                      <button
                        onClick={() => startEditing(s)}
                        className="w-full rounded border border-border px-2 py-1 text-left font-mono text-[10px] text-muted-foreground hover:border-primary/30 hover:bg-muted/50"
                      >
                        {s.type === 'secret' ? '••••••••' : s.value}
                      </button>
                    ) : (
                      <button
                        onClick={() => startEditing(s)}
                        className="w-full rounded border border-dashed border-border px-2 py-1 text-left text-[10px] text-muted-foreground/50 hover:border-primary/30 hover:text-muted-foreground"
                      >
                        {s.default_value ? `Default: ${s.default_value}` : 'Click to set value'}
                      </button>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ---- App Permissions Panel ----
function AppPermissionsPanel({ appId }: { appId: string }) {
  const [permissions, setPermissions] = useState<any[]>([])
  const [allUsers, setAllUsers] = useState<any[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [showAddForm, setShowAddForm] = useState(false)
  const [addType, setAddType] = useState<'user' | 'group'>('user')
  const [selectedUserId, setSelectedUserId] = useState('')
  const [groupName, setGroupName] = useState('')
  const [permLevel, setPermLevel] = useState('access')
  const user = useAuthStore((s) => s.user)

  const fetchPermissions = async () => {
    setIsLoading(true)
    try {
      const data = await apiClient.get<any[]>(`/apps/${appId}/permissions`)
      setPermissions(data)
    } catch {
      setPermissions([])
    } finally {
      setIsLoading(false)
    }
  }

  const fetchUsers = async () => {
    if (user?.role !== 'admin') return
    try {
      const data = await apiClient.get<any[]>('/admin/users')
      setAllUsers(data)
    } catch {
      setAllUsers([])
    }
  }

  useEffect(() => { fetchPermissions(); fetchUsers() }, [appId])

  const handleAdd = async () => {
    try {
      const body: any = { permission: permLevel }
      if (addType === 'user') {
        body.user_id = selectedUserId
      } else {
        body.group_name = groupName
      }
      await apiClient.post(`/apps/${appId}/permissions`, body)
      setShowAddForm(false)
      setSelectedUserId('')
      setGroupName('')
      fetchPermissions()
    } catch {
      // ignore
    }
  }

  const handleRemove = async (permId: string) => {
    try {
      await apiClient.delete(`/apps/${appId}/permissions/${permId}`)
      fetchPermissions()
    } catch {
      // ignore
    }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h3 className="text-sm font-semibold">Permissions</h3>
        <button
          onClick={() => setShowAddForm(!showAddForm)}
          className="text-muted-foreground hover:text-foreground"
        >
          <UserPlus size={14} />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-3">
        {showAddForm && (
          <div className="mb-3 rounded-lg border border-primary/20 bg-primary/5 p-3">
            <p className="mb-2 text-[10px] font-medium text-primary">Add Permission</p>
            <div className="space-y-2">
              {/* Type selector */}
              <div className="flex gap-1 rounded-lg bg-muted p-0.5">
                <button
                  onClick={() => setAddType('user')}
                  className={cn(
                    'flex-1 rounded-md px-2 py-1 text-[10px] font-medium transition-colors',
                    addType === 'user' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground'
                  )}
                >
                  User
                </button>
                <button
                  onClick={() => setAddType('group')}
                  className={cn(
                    'flex-1 rounded-md px-2 py-1 text-[10px] font-medium transition-colors',
                    addType === 'group' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground'
                  )}
                >
                  AD Group
                </button>
              </div>

              {addType === 'user' ? (
                <select
                  value={selectedUserId}
                  onChange={(e) => setSelectedUserId(e.target.value)}
                  className="w-full rounded border border-input bg-secondary px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                >
                  <option value="">Select user...</option>
                  {allUsers.map((u: any) => (
                    <option key={u.id} value={u.id}>
                      {u.display_name} ({u.username})
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  value={groupName}
                  onChange={(e) => setGroupName(e.target.value)}
                  className="w-full rounded border border-input bg-secondary px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
                  placeholder="AD group name (e.g., App-Users)"
                />
              )}

              <select
                value={permLevel}
                onChange={(e) => setPermLevel(e.target.value)}
                className="w-full rounded border border-input bg-secondary px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
              >
                <option value="access">View access</option>
                <option value="edit">Edit access</option>
              </select>

              <div className="flex gap-2">
                <button onClick={() => setShowAddForm(false)} className="flex-1 rounded px-2 py-1 text-xs text-muted-foreground hover:bg-accent">
                  Cancel
                </button>
                <button
                  onClick={handleAdd}
                  disabled={addType === 'user' ? !selectedUserId : !groupName}
                  className="flex-1 rounded bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                >
                  Add
                </button>
              </div>
            </div>
          </div>
        )}

        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 size={16} className="animate-spin text-muted-foreground" />
          </div>
        ) : permissions.length === 0 ? (
          <div className="py-8 text-center text-xs text-muted-foreground">
            <Shield size={24} className="mx-auto mb-2 text-muted-foreground/30" />
            <p>No permissions set</p>
            <p className="mt-1">By default only the app creator and admins can access this app</p>
          </div>
        ) : (
          <div className="space-y-2">
            {permissions.map((p: any) => (
              <div key={p.id} className="group flex items-center justify-between rounded-lg border border-border p-3">
                <div>
                  <div className="flex items-center gap-1.5">
                    <span className="text-xs font-medium">
                      {p.user_display_name || p.group_name || 'Unknown'}
                    </span>
                    {p.group_name && !p.user_id && (
                      <span className="rounded bg-muted px-1 py-0.5 text-[9px] text-muted-foreground">Group</span>
                    )}
                  </div>
                  <p className="mt-0.5 text-[10px] text-muted-foreground">
                    {p.permission === 'edit' ? 'Can edit' : 'Can view'}
                  </p>
                </div>
                <button
                  onClick={() => handleRemove(p.id)}
                  className="rounded p-1 text-muted-foreground/0 transition-colors hover:bg-destructive/10 hover:text-destructive group-hover:text-muted-foreground"
                  title="Remove permission"
                >
                  <Trash2 size={12} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
