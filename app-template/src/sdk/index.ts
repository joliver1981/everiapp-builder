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
export {
  callConnection,
  useConnectionCall,
  listConnections,
  useConnections,
  type ConnectionCallRequest,
  type ConnectionCallResult,
  type UseConnectionCallState,
  type AppConnection,
  type UseConnectionsState,
} from './callConnection'
export {
  aiChat,
  type ChatMessage,
  type AiChatRequest,
  type AiChatResult,
} from './aiChat'
export {
  callFunction,
  useFunction,
  listFunctions,
  useFunctions,
  type AppFunction,
  type UseFunctionState,
  type UseFunctionsState,
} from './callFunction'
export { reportBug, type ReportBugOptions, type ReportBugResult } from './reportBug'
// Session-expiry contract: platform calls that hit 401 throw
// SESSION_EXPIRED_MESSAGE and dispatch 'aihub:token-expired' on window —
// apps/hosts can listen to prompt a reload (or re-mint a token).
export { SESSION_EXPIRED_MESSAGE, notifySessionExpired } from './session'
// Importing installs the fetch patch that stamps X-AIHub-Trace-Id on
// platform-bound requests (one trace id per app session) and emits client
// spans (dataset/app-DB calls, UI errors, interactions) to the platform.
export { getTraceId, getRecentSpans, emitSpan, installTracing, type ClientSpan } from './tracing'
export { BugReportButton } from './BugReportButton'
export {
  AppErrorBoundary,
  type AppErrorBoundaryProps,
} from './ErrorBoundary'
