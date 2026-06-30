import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { PageHeader } from '@/components/layout/PageHeader'
import { apiClient, ApiError } from '@/api/client'
import {
  Search, Download, Tag, Loader2,
  Package, Star, X, Globe, Server,
} from 'lucide-react'
import { SetupWizardRenderer, type WizardSchema } from '@/components/wizard/SetupWizardRenderer'
import { cn } from '@/lib/utils'

interface Listing {
  id: string
  app_id: string
  name: string
  description: string
  icon: string
  category: string
  tags: string[]
  version: number
  publisher_name: string
  install_count: number
  setup_wizard: WizardSchema | null
  created_at: string
}

interface RemoteApp {
  id: string
  slug: string
  name: string
  shortDescription: string
  iconUrl: string | null
  category: string
  tags: string[]
  currentVersion: string | null
  developerName: string
  avgRating: string
  reviewCount: number
  installCount: number
  setupWizard?: WizardSchema | null
}

interface RemoteBrowseResponse {
  apps: RemoteApp[]
  pagination: { page: number; totalPages: number; total: number; hasMore: boolean }
  marketplace_url: string
}

const LOCAL_CATEGORIES = ['All', 'general', 'analytics', 'productivity', 'finance', 'hr', 'sales', 'devtools']
const REMOTE_CATEGORIES = ['All', 'general', 'productivity', 'finance', 'communication', 'analytics', 'developer-tools', 'design', 'marketing', 'hr', 'education', 'entertainment', 'utilities']

function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    try {
      const parsed = JSON.parse(err.message)
      if (typeof parsed.detail === 'string') return parsed.detail
    } catch { /* not JSON */ }
  }
  return err instanceof Error ? err.message : 'Something went wrong'
}

// The in-platform "This Server" gallery is being reframed as a Templates library and
// kept DORMANT for now. Flip this to `true` to bring it back: it then reappears as a
// "Templates" tab next to the public EveriApp Marketplace. While `false`, only the
// public marketplace is shown. The backend /api/marketplace endpoints are untouched,
// so any existing listings are preserved and resurface the moment this is re-enabled.
const TEMPLATES_GALLERY_ENABLED = false

export function MarketplacePage() {
  const [tab, setTab] = useState<'local' | 'remote'>(TEMPLATES_GALLERY_ENABLED ? 'local' : 'remote')

  return (
    <div>
      <PageHeader
        title="Marketplace"
        description={
          TEMPLATES_GALLERY_ENABLED
            ? 'Browse and install apps — templates on this server or the public EveriApp Marketplace'
            : 'Browse and install apps from the EveriApp Marketplace'
        }
      />

      <div className="p-6">
        {/* Source tabs — only shown when the Templates gallery is enabled */}
        {TEMPLATES_GALLERY_ENABLED && (
          <div className="mb-6 flex w-fit rounded-lg border border-border p-0.5">
            <button
              onClick={() => setTab('local')}
              className={cn(
                'flex items-center gap-1.5 rounded-md px-4 py-1.5 text-xs font-medium transition-colors',
                tab === 'local' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'
              )}
            >
              <Server size={12} />
              Templates
            </button>
            <button
              onClick={() => setTab('remote')}
              className={cn(
                'flex items-center gap-1.5 rounded-md px-4 py-1.5 text-xs font-medium transition-colors',
                tab === 'remote' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'
              )}
            >
              <Globe size={12} />
              EveriApp Marketplace
            </button>
          </div>
        )}

        {TEMPLATES_GALLERY_ENABLED && tab === 'local' ? <LocalGallery /> : <RemoteGallery />}
      </div>
    </div>
  )
}

// ---- Local (this server) gallery — original behavior ----
function LocalGallery() {
  const navigate = useNavigate()
  const [listings, setListings] = useState<Listing[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState('All')
  const [installing, setInstalling] = useState<string | null>(null)
  const [wizardListing, setWizardListing] = useState<Listing | null>(null)

  useEffect(() => {
    loadListings()
  }, [])

  const loadListings = async () => {
    setIsLoading(true)
    try {
      const data = await apiClient.get<Listing[]>('/marketplace')
      setListings(data)
    } catch {
      setListings([])
    } finally {
      setIsLoading(false)
    }
  }

  const handleInstall = async (listing: Listing, wizardValues: Record<string, string | number | boolean> = {}) => {
    setInstalling(listing.id)
    try {
      const result = await apiClient.post<{ app_id: string }>('/marketplace/install', {
        listing_id: listing.id,
        wizard_values: wizardValues,
      })
      setWizardListing(null)
      navigate(`/builder/${result.app_id}`)
    } catch {
      // ignore
    } finally {
      setInstalling(null)
    }
  }

  const filtered = listings.filter((l) => {
    const matchesSearch = !search ||
      l.name.toLowerCase().includes(search.toLowerCase()) ||
      l.description.toLowerCase().includes(search.toLowerCase())
    const matchesCategory = category === 'All' || l.category === category
    return matchesSearch && matchesCategory
  })

  return (
    <>
      <Filters
        search={search} onSearch={setSearch}
        category={category} onCategory={setCategory}
        categories={LOCAL_CATEGORIES}
      />

      {isLoading ? (
        <Spinner />
      ) : filtered.length === 0 ? (
        <Empty message={listings.length === 0 ? 'No apps published to this server yet' : 'No apps match your filters'} />
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {filtered.map((listing) => (
            <div
              key={listing.id}
              className="flex flex-col rounded-xl border border-border bg-card p-5 transition-colors hover:border-primary/30"
            >
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
                    <Package size={18} />
                  </div>
                  <div>
                    <h3 className="text-sm font-semibold">{listing.name}</h3>
                    <p className="text-xs text-muted-foreground">
                      by {listing.publisher_name}
                    </p>
                  </div>
                </div>
                <span className="rounded bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
                  v{listing.version}
                </span>
              </div>

              <p className="mt-3 flex-1 text-xs text-muted-foreground line-clamp-2">
                {listing.description || 'No description'}
              </p>

              <div className="mt-3 flex flex-wrap gap-1">
                {listing.tags.slice(0, 3).map((tag) => (
                  <span key={tag} className="flex items-center gap-1 rounded bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
                    <Tag size={8} />
                    {tag}
                  </span>
                ))}
              </div>

              <div className="mt-4 flex items-center justify-between border-t border-border pt-3">
                <div className="flex items-center gap-3 text-xs text-muted-foreground">
                  <span className="flex items-center gap-1">
                    <Download size={10} />
                    {listing.install_count}
                  </span>
                  <span className="flex items-center gap-1">
                    <Star size={10} />
                    {listing.category}
                  </span>
                </div>
                <button
                  onClick={() => {
                    if (listing.setup_wizard?.steps?.length) {
                      setWizardListing(listing)
                    } else {
                      handleInstall(listing)
                    }
                  }}
                  disabled={installing === listing.id}
                  className="flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                >
                  {installing === listing.id ? (
                    <Loader2 size={12} className="animate-spin" />
                  ) : (
                    <Download size={12} />
                  )}
                  Install
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {wizardListing && wizardListing.setup_wizard && (
        <WizardModal
          schema={wizardListing.setup_wizard}
          onComplete={(values) => handleInstall(wizardListing, values)}
          onClose={() => setWizardListing(null)}
        />
      )}
    </>
  )
}

// ---- Remote (public EveriApp Marketplace) gallery ----
function RemoteGallery() {
  const navigate = useNavigate()
  const [apps, setApps] = useState<RemoteApp[]>([])
  const [marketplaceUrl, setMarketplaceUrl] = useState('')
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState('All')
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(1)
  const [installing, setInstalling] = useState<string | null>(null)
  const [wizardApp, setWizardApp] = useState<RemoteApp | null>(null)

  // Server-side search with a small debounce
  useEffect(() => {
    const t = setTimeout(load, search ? 350 : 0)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, category, page])

  const load = async () => {
    setIsLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams({ page: String(page) })
      if (search) params.set('q', search)
      if (category !== 'All') params.set('category', category)
      const data = await apiClient.get<RemoteBrowseResponse>(`/marketplace/remote?${params}`)
      setApps(data.apps)
      setTotalPages(data.pagination?.totalPages || 1)
      setMarketplaceUrl(data.marketplace_url)
    } catch (err) {
      setApps([])
      setError(describeError(err))
    } finally {
      setIsLoading(false)
    }
  }

  const handleInstall = async (app: RemoteApp, wizardValues: Record<string, string | number | boolean> = {}) => {
    setInstalling(app.slug)
    setError(null)
    try {
      const result = await apiClient.post<{ app_id: string }>('/marketplace/remote/install', {
        slug: app.slug,
        wizard_values: wizardValues,
      })
      setWizardApp(null)
      navigate(`/builder/${result.app_id}`)
    } catch (err) {
      setError(`Install failed: ${describeError(err)}`)
    } finally {
      setInstalling(null)
    }
  }

  return (
    <>
      <Filters
        search={search} onSearch={(v) => { setPage(1); setSearch(v) }}
        category={category} onCategory={(v) => { setPage(1); setCategory(v) }}
        categories={REMOTE_CATEGORIES}
      />

      {error && (
        <div className="mb-4 rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-2.5 text-sm text-destructive">
          {error}
        </div>
      )}

      {isLoading ? (
        <Spinner />
      ) : apps.length === 0 ? (
        !error && <Empty message={search || category !== 'All' ? 'No apps match your filters' : 'No apps on the marketplace yet'} />
      ) : (
        <>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            {apps.map((app) => (
              <div
                key={app.slug}
                className="flex flex-col rounded-xl border border-border bg-card p-5 transition-colors hover:border-primary/30"
              >
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-3">
                    {app.iconUrl ? (
                      <img
                        src={`${marketplaceUrl}${app.iconUrl.startsWith('/') ? '' : '/'}${app.iconUrl}`}
                        alt=""
                        className="h-10 w-10 rounded-lg object-cover"
                      />
                    ) : (
                      <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
                        <Globe size={18} />
                      </div>
                    )}
                    <div>
                      <h3 className="text-sm font-semibold">{app.name}</h3>
                      <p className="text-xs text-muted-foreground">by {app.developerName}</p>
                    </div>
                  </div>
                  {app.currentVersion && (
                    <span className="rounded bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
                      v{app.currentVersion}
                    </span>
                  )}
                </div>

                <p className="mt-3 flex-1 text-xs text-muted-foreground line-clamp-2">
                  {app.shortDescription || 'No description'}
                </p>

                <div className="mt-3 flex flex-wrap gap-1">
                  {(app.tags || []).slice(0, 3).map((tag) => (
                    <span key={tag} className="flex items-center gap-1 rounded bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
                      <Tag size={8} />
                      {tag}
                    </span>
                  ))}
                </div>

                <div className="mt-4 flex items-center justify-between border-t border-border pt-3">
                  <div className="flex items-center gap-3 text-xs text-muted-foreground">
                    <span className="flex items-center gap-1">
                      <Download size={10} />
                      {app.installCount}
                    </span>
                    <span className="flex items-center gap-1">
                      <Star size={10} />
                      {Number(app.avgRating) > 0 ? `${app.avgRating} (${app.reviewCount})` : app.category}
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {marketplaceUrl && (
                      <a
                        href={`${marketplaceUrl}/apps/${app.slug}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-[10px] text-muted-foreground underline hover:text-foreground"
                      >
                        Details
                      </a>
                    )}
                    <button
                      onClick={() => {
                        if (app.setupWizard?.steps?.length) {
                          setWizardApp(app)
                        } else {
                          handleInstall(app)
                        }
                      }}
                      disabled={installing === app.slug}
                      className="flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                    >
                      {installing === app.slug ? (
                        <Loader2 size={12} className="animate-spin" />
                      ) : (
                        <Download size={12} />
                      )}
                      Install
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>

          {totalPages > 1 && (
            <div className="mt-6 flex items-center justify-center gap-3 text-sm">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page <= 1}
                className="rounded-lg border border-border px-3 py-1.5 text-xs disabled:opacity-40"
              >
                Previous
              </button>
              <span className="text-xs text-muted-foreground">Page {page} of {totalPages}</span>
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page >= totalPages}
                className="rounded-lg border border-border px-3 py-1.5 text-xs disabled:opacity-40"
              >
                Next
              </button>
            </div>
          )}
        </>
      )}

      {wizardApp && wizardApp.setupWizard && (
        <WizardModal
          schema={wizardApp.setupWizard}
          onComplete={(values) => handleInstall(wizardApp, values)}
          onClose={() => setWizardApp(null)}
        />
      )}
    </>
  )
}

// ---- Shared bits ----
function Filters({
  search, onSearch, category, onCategory, categories,
}: {
  search: string
  onSearch: (v: string) => void
  category: string
  onCategory: (v: string) => void
  categories: string[]
}) {
  return (
    <div className="mb-6 flex flex-wrap items-center gap-4">
      <div className="relative flex-1 max-w-md">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
        <input
          type="text"
          placeholder="Search apps..."
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          className="w-full rounded-lg border border-border bg-background py-2 pl-9 pr-3 text-sm outline-none focus:ring-1 focus:ring-primary"
        />
      </div>
      <div className="flex flex-wrap gap-1">
        {categories.map((cat) => (
          <button
            key={cat}
            onClick={() => onCategory(cat)}
            className={cn(
              'rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              category === cat
                ? 'bg-primary text-primary-foreground'
                : 'bg-muted text-muted-foreground hover:text-foreground'
            )}
          >
            {cat === 'All' ? 'All' : cat.charAt(0).toUpperCase() + cat.slice(1).replace('-', ' ')}
          </button>
        ))}
      </div>
    </div>
  )
}

function Spinner() {
  return (
    <div className="flex items-center justify-center py-20">
      <Loader2 size={24} className="animate-spin text-muted-foreground" />
    </div>
  )
}

function Empty({ message }: { message: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-20">
      <Package size={40} className="text-muted-foreground/30" />
      <p className="text-sm text-muted-foreground">{message}</p>
    </div>
  )
}

function WizardModal({
  schema, onComplete, onClose,
}: {
  schema: WizardSchema
  onComplete: (values: Record<string, string | number | boolean>) => void
  onClose: () => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="relative w-full max-w-lg rounded-2xl bg-card p-6 shadow-xl">
        <button
          onClick={onClose}
          className="absolute right-4 top-4 rounded-lg p-1 text-muted-foreground hover:text-foreground"
        >
          <X size={16} />
        </button>
        <SetupWizardRenderer
          schema={schema}
          onComplete={onComplete}
          onCancel={onClose}
        />
      </div>
    </div>
  )
}
