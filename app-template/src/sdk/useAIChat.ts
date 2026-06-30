/**
 * useAIChat — Programmatic access to the AI Toggle chat.
 *
 * Provides the ability to send messages to the AI Toggle assistant
 * and receive responses programmatically (beyond the floating chat UI).
 *
 * Usage:
 *   import { useAIChat } from '@aihub/app-sdk'
 *
 *   function SmartInput() {
 *     const { sendMessage, lastResponse, isLoading } = useAIChat()
 *     const handleAsk = () => sendMessage('Summarize the current data')
 *   }
 */

import { useState, useCallback } from 'react'

interface AIChatState {
  sendMessage: (message: string) => void
  lastResponse: string | null
  isLoading: boolean
}

export function useAIChat(): AIChatState {
  const [lastResponse, setLastResponse] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(false)

  const sendMessage = useCallback((message: string) => {
    // Dispatch to the AIToggleProvider's chat handler
    const event = new CustomEvent('aihub:ai-chat-message', {
      detail: { message },
    })
    window.dispatchEvent(event)
    setIsLoading(true)

    const handleResponse = (e: Event) => {
      const detail = (e as CustomEvent).detail
      setLastResponse(detail.response)
      setIsLoading(false)
      window.removeEventListener('aihub:ai-chat-response', handleResponse)
    }
    window.addEventListener('aihub:ai-chat-response', handleResponse)
  }, [])

  return { sendMessage, lastResponse, isLoading }
}
