import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App'
import { BugReportButton, AppErrorBoundary, AIToggleProvider } from '@aihub/app-sdk'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* Render-error firewall: a crash in App (or anything it renders) shows a
        recoverable panel instead of a blank white screen. The bug-report
        button stays OUTSIDE the boundary so it survives an App crash and lets
        the user file a report about it. */}
    <AppErrorBoundary>
      {/* Floating AI assistant. Self-hides unless the platform admin has
          enabled the AI toggle for this app. Safe to leave mounted. */}
      <AIToggleProvider>
        <App />
      </AIToggleProvider>
    </AppErrorBoundary>
    {/* Floating bug-report button. Self-hides unless the platform admin
        has enabled the widget for this app. Safe to leave mounted. */}
    <BugReportButton />
  </StrictMode>,
)
