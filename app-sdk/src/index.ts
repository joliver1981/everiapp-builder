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
export {
  aiDecide,
  useDecision,
  type DecisionResult,
  type AiDecideOptions,
  type UseDecisionState,
} from './aiDecide'
export { reportBug, type ReportBugOptions, type ReportBugResult } from './reportBug'
// Importing installs the fetch patch that stamps X-AIHub-Trace-Id on
// platform-bound requests (one trace id per app session) and emits client
// spans (dataset/app-DB calls, UI errors, interactions) to the platform.
export { getTraceId, getRecentSpans, emitSpan, installTracing, type ClientSpan } from './tracing'
export { BugReportButton } from './BugReportButton'
export {
  AppErrorBoundary,
  type AppErrorBoundaryProps,
} from './ErrorBoundary'
