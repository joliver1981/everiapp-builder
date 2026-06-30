import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Sparkles, CheckCircle2, Circle, ArrowRight, Loader2 } from 'lucide-react'
import { apiClient } from '@/api/client'
import { cn } from '@/lib/utils'

interface SetupState {
  needs_setup: boolean
  setup_completed: boolean
  has_identity_provider: boolean
  smtp_configured: boolean
  has_custom_prompt: boolean
  budgets_set: boolean
}

export function SetupWizardPage() {
  const [state, setState] = useState<SetupState | null>(null)
  const [hasAiProvider, setHasAiProvider] = useState(false)
  const [finishing, setFinishing] = useState(false)
  const navigate = useNavigate()

  const load = () => {
    apiClient.get<SetupState>('/setup/state').then(setState).catch(() => {})
    // The builder can't generate anything without an LLM provider, so that's the
    // one step that actually matters on first run.
    apiClient.get<any[]>('/admin/ai-providers')
      .then((p) => setHasAiProvider(Array.isArray(p) && p.length > 0))
      .catch(() => {})
  }
  useEffect(() => { load() }, [])

  const finish = async () => {
    setFinishing(true)
    try {
      await apiClient.post('/setup/complete')
      navigate('/')
    } finally {
      setFinishing(false)
    }
  }

  return (
    <div className="min-h-screen overflow-auto bg-background px-4 py-10">
      <div className="mx-auto max-w-2xl space-y-6">
        <div className="text-center">
          <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-primary">
            <Sparkles size={28} className="text-primary-foreground" />
          </div>
          <h1 className="mt-5 text-2xl font-semibold tracking-tight">Welcome to EveriApp</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            A few steps to get your platform ready. You can change any of this later in Platform settings.
          </p>
        </div>

        {/* AI provider — the one genuinely-required step */}
        <Step done={hasAiProvider} title="Add an AI provider">
          <p className="mb-2 text-xs text-muted-foreground">
            EveriApp builds apps with an LLM, so add at least one provider (Anthropic, OpenAI, …)
            and its API key. Without this, the builder can't generate anything.
          </p>
          <LinkButton onClick={() => navigate('/admin/ai-providers')}>Open AI Providers</LinkButton>
        </Step>

        {/* Identity provider */}
        <Step done={state?.has_identity_provider} title="Connect an identity provider"
              optional>
          <p className="mb-2 text-xs text-muted-foreground">
            Add LDAP / Active Directory, SAML, or OpenID Connect so your team signs in with their org account.
            Skip to use built-in local accounts.
          </p>
          <LinkButton onClick={() => navigate('/admin/platform')}>Open Auth Providers</LinkButton>
        </Step>

        {/* SMTP */}
        <Step done={state?.smtp_configured} title="Set up email notifications" optional>
          <p className="mb-2 text-xs text-muted-foreground">
            Configure SMTP to notify admins about publish requests, deploy failures, and budget breaches.
          </p>
          <LinkButton onClick={() => navigate('/admin/platform')}>Open Settings → Notifications</LinkButton>
        </Step>

        {/* Org defaults */}
        <Step done={state?.has_custom_prompt || state?.budgets_set} title="Set org defaults" optional>
          <p className="mb-2 text-xs text-muted-foreground">
            Add a house-style system prompt and LLM budgets so every generated app matches your standards.
          </p>
          <LinkButton onClick={() => navigate('/admin/platform')}>Open Settings</LinkButton>
        </Step>

        <div className="flex items-center justify-between rounded-lg border border-border bg-card p-4">
          <div>
            <p className="text-sm font-medium">All set?</p>
            <p className="text-xs text-muted-foreground">You can revisit Platform settings anytime.</p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => navigate('/')} className="rounded-lg border border-border px-3 py-2 text-sm hover:bg-accent">
              Skip for now
            </button>
            <button onClick={finish} disabled={finishing}
                    className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
              {finishing ? <Loader2 size={15} className="animate-spin" /> : <ArrowRight size={15} />}
              Finish setup
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function Step({ done, title, optional, children }: {
  done?: boolean; title: string; optional?: boolean; children: React.ReactNode
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="mb-2 flex items-center gap-2">
        {done ? <CheckCircle2 size={18} className="text-green-400" /> : <Circle size={18} className="text-muted-foreground" />}
        <h3 className="text-sm font-semibold">{title}</h3>
        {optional && <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] uppercase text-muted-foreground">optional</span>}
      </div>
      <div className="pl-6">{children}</div>
    </div>
  )
}

function LinkButton({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return (
    <button onClick={onClick}
            className={cn('flex items-center gap-1.5 rounded-lg border border-input bg-secondary px-3 py-1.5 text-xs font-medium hover:bg-accent')}>
      {children} <ArrowRight size={12} />
    </button>
  )
}
