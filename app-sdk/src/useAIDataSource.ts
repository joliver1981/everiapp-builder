/**
 * useAIDataSource — Register a data source with the AI Toggle assistant.
 *
 * When the AI Toggle is enabled, the assistant can see registered data sources
 * and use them to answer user questions about the app's live data.
 *
 * Usage:
 *   import { useAIDataSource } from '@aihub/app-sdk'
 *
 *   function SalesTable({ data }) {
 *     useAIDataSource('sales', {
 *       data,
 *       columns: ['date', 'product', 'revenue', 'quantity'],
 *       description: 'Monthly sales data',
 *     })
 *     return <Table data={data} />
 *   }
 */

import { useEffect, useRef } from 'react'

interface DataSourceConfig {
  data: any[]
  columns: string[]
  description?: string
}

// Global registry
const dataSources = new Map<string, DataSourceConfig>()

export function getDataSources(): Record<string, DataSourceConfig> {
  return Object.fromEntries(dataSources)
}

export function useAIDataSource(name: string, config: DataSourceConfig): void {
  const configRef = useRef(config)
  configRef.current = config

  useEffect(() => {
    dataSources.set(name, configRef.current)
    return () => {
      dataSources.delete(name)
    }
  }, [name])

  // Update data when it changes
  useEffect(() => {
    dataSources.set(name, config)
  }, [name, config.data, config.columns.join(',')])
}
