import ReactDOM from 'react-dom/client'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Chat from './pages/Chat'
import Workspaces from './pages/Workspaces'
import WorkspaceSettings from './pages/WorkspaceSettings'
import OrgSettings from './pages/OrgSettings'
import McpServers from './pages/McpServers'
import Analytics from './pages/Analytics'
import WorkspaceAnalytics from './pages/WorkspaceAnalytics'
import Settings from './pages/Settings'
import AiDisclosureModal from './components/AiDisclosureModal'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  // StrictMode disabled - causes double API calls in dev mode
  // Re-enable for debugging lifecycle issues if needed
  <BrowserRouter>
    <AiDisclosureModal />
    <Routes>
      <Route path="/" element={<Chat />} />
<Route path="/chat" element={<Chat />} />
      <Route path="/chat/:conversationId" element={<Chat />} />
      <Route path="/workspaces" element={<Workspaces />} />
      <Route path="/workspaces/:workspaceId" element={<WorkspaceSettings />} />
      <Route path="/workspaces/:workspaceId/settings" element={<WorkspaceSettings />} />
      <Route path="/org/settings" element={<OrgSettings />} />
      <Route path="/org/mcp-servers" element={<McpServers />} />
      <Route path="/analytics" element={<Analytics />} />
      <Route path="/analytics/workspace/:workspaceId" element={<WorkspaceAnalytics />} />
      <Route path="/settings" element={<Settings />} />
      <Route path="/settings/api-keys" element={<Settings />} />
    </Routes>
  </BrowserRouter>,
)
