import { useEffect, useState } from 'react'
import { FileText, File as FileIcon, X, Loader2, AlertCircle, ExternalLink } from 'lucide-react'
import { apiClient } from '@/api/client'
import type { ChatAttachment } from '@/types'
import { cn } from '@/lib/utils'

export function formatBytes(n: number): string {
  if (!n || n < 1024) return `${n || 0} B`
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

/**
 * A viewable URL for an attachment. Files attached in this session carry a local
 * object URL; anything reloaded from history is fetched with the bearer token
 * (an <img src> can't send it) and held as an object URL for the component's life.
 */
export function useAttachmentUrl(att: ChatAttachment, enabled = true): string | null {
  const [url, setUrl] = useState<string | null>(att.localUrl ?? null)
  useEffect(() => {
    if (att.localUrl) {
      setUrl(att.localUrl)
      return
    }
    if (!enabled) return
    let cancelled = false
    let objectUrl: string | null = null
    apiClient
      .getBlob(`/ai/attachments/${att.id}`)
      .then(({ blob }) => {
        if (cancelled) return
        objectUrl = URL.createObjectURL(new Blob([blob], { type: att.mime || blob.type }))
        setUrl(objectUrl)
      })
      .catch(() => {
        /* thumbnail is a nicety — the chip still shows name/size */
      })
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [att.id, att.localUrl, att.mime, enabled])
  return url
}

/** Open the full attachment in a new tab (popup-blocker safe: the tab opens synchronously). */
export function openAttachment(att: ChatAttachment): void {
  if (att.localUrl) {
    window.open(att.localUrl, '_blank', 'noopener')
    return
  }
  const win = window.open('', '_blank')
  apiClient
    .getBlob(`/ai/attachments/${att.id}`)
    .then(({ blob }) => {
      const url = URL.createObjectURL(new Blob([blob], { type: att.mime || blob.type }))
      if (win) win.location.href = url
      else window.open(url, '_blank', 'noopener')
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
    })
    .catch(() => win?.close())
}

export type AttachmentStatus = 'uploading' | 'ready' | 'error'

interface AttachmentThumbProps {
  att: ChatAttachment
  status?: AttachmentStatus
  error?: string
  note?: string
  onRemove?: () => void
}

/**
 * One attachment: an image thumbnail, or a file chip for PDFs / text files.
 * Used both in the composer (pending uploads, with a remove button) and inside
 * sent message bubbles (click opens the full file).
 */
export function AttachmentThumb({ att, status = 'ready', error, note, onRemove }: AttachmentThumbProps) {
  const url = useAttachmentUrl(att, att.kind === 'image')
  const title = [att.name, formatBytes(att.size), note, error].filter(Boolean).join(' · ')
  const isImage = att.kind === 'image'

  return (
    <div
      className={cn(
        'group relative shrink-0 overflow-hidden rounded-lg border text-left',
        status === 'error' ? 'border-destructive' : 'border-border/60',
        isImage ? 'h-20 w-20 bg-background/40' : 'flex max-w-[220px] items-center gap-2 bg-background/40 px-2.5 py-1.5'
      )}
      title={title}
    >
      {isImage ? (
        <button
          type="button"
          onClick={() => openAttachment(att)}
          className="block h-full w-full"
          aria-label={`Open ${att.name}`}
        >
          {url ? (
            <img src={url} alt={att.name} className="h-full w-full object-cover" />
          ) : (
            <div className="flex h-full w-full items-center justify-center">
              <Loader2 size={14} className="animate-spin opacity-60" />
            </div>
          )}
        </button>
      ) : (
        <button
          type="button"
          onClick={() => openAttachment(att)}
          className="flex min-w-0 items-center gap-2"
          aria-label={`Open ${att.name}`}
        >
          {att.kind === 'pdf' ? <FileText size={16} className="shrink-0" /> : <FileIcon size={16} className="shrink-0" />}
          <span className="flex min-w-0 flex-col">
            <span className="truncate text-xs font-medium">{att.name}</span>
            <span className="text-[10px] opacity-70">
              {att.kind === 'pdf' ? 'PDF' : 'text'} · {formatBytes(att.size)}
            </span>
          </span>
          <ExternalLink size={11} className="shrink-0 opacity-0 transition-opacity group-hover:opacity-70" />
        </button>
      )}

      {status === 'uploading' && (
        <div className="absolute inset-0 flex items-center justify-center bg-background/70">
          <Loader2 size={16} className="animate-spin" />
        </div>
      )}
      {status === 'error' && (
        <div className="absolute inset-0 flex items-center justify-center bg-destructive/20 text-destructive">
          <AlertCircle size={16} />
        </div>
      )}
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${att.name}`}
          className="absolute right-0.5 top-0.5 rounded-full bg-background/90 p-0.5 text-foreground shadow opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100"
        >
          <X size={12} />
        </button>
      )}
    </div>
  )
}

/** Thumbnails/chips for the attachments on a sent message. */
export function AttachmentList({ attachments }: { attachments: ChatAttachment[] }) {
  if (!attachments.length) return null
  return (
    <div className="mb-2 flex flex-wrap gap-2">
      {attachments.map((a) => (
        <AttachmentThumb key={a.id} att={a} />
      ))}
    </div>
  )
}
