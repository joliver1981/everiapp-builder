/**
 * useAIAction — Register an action the AI Toggle assistant can invoke.
 *
 * When the AI assistant determines the user wants to perform an action,
 * it can call registered actions with the appropriate parameters.
 *
 * Usage:
 *   import { useAIAction } from '@aihub/app-sdk'
 *
 *   function Dashboard() {
 *     useAIAction('filter_by_date', (params) => {
 *       setDateRange(params.start, params.end)
 *     })
 *     useAIAction('create_chart', (params) => {
 *       addChart(params.type, params.data_key)
 *     })
 *   }
 */

import { useEffect, useRef } from 'react'

// Global action registry
const actions = new Map<string, (params: any) => void>()

export function getActions(): string[] {
  return Array.from(actions.keys())
}

export function executeAction(name: string, params: any): boolean {
  const handler = actions.get(name)
  if (handler) {
    handler(params)
    return true
  }
  return false
}

export function useAIAction(name: string, handler: (params: any) => void): void {
  const handlerRef = useRef(handler)
  handlerRef.current = handler

  useEffect(() => {
    actions.set(name, (params: any) => handlerRef.current(params))
    return () => {
      actions.delete(name)
    }
  }, [name])
}
