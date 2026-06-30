/**
 * @aihub/app-sdk
 *
 * SDK for AIHub-generated applications.
 * Provides hooks and utilities for config, auth, AI Toggle, data fetching, and bug reporting.
 */

export { useAppConfig } from './useAppConfig'
export { useAIChat } from './useAIChat'
export { useAIDataSource } from './useAIDataSource'
export { useAIAction } from './useAIAction'
export { AIToggleProvider } from './AIToggleProvider'
export { getUser, fetchUser, type AppUser } from './auth'
export { fetchData } from './api'
export {
  useDataset,
  executeDataset,
  type DatasetColumn,
  type DatasetResult,
  type UseDatasetState,
} from './useDataset'
export {
  useDatasetMutation,
  type DatasetMutationResult,
} from './useDatasetMutation'
export {
  useAppQuery,
  useAppMutation,
  useAppSchema,
  type AppQueryResult,
  type AppMutationResult,
} from './useAppDB'
export { reportBug, type ReportBugOptions, type ReportBugResult } from './reportBug'
export { BugReportButton } from './BugReportButton'
export {
  AppErrorBoundary,
  type AppErrorBoundaryProps,
} from './ErrorBoundary'
