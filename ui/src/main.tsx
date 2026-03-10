import ReactDOM from 'react-dom/client'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Chat from './pages/Chat'
import Workspaces from './pages/Workspaces'
import WorkspaceSettings from './pages/WorkspaceSettings'
import OrgSettings from './pages/OrgSettings'
import McpServers from './pages/McpServers'
import ApiKeys from './pages/ApiKeys'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  // StrictMode disabled - causes double API calls in dev mode
  // Re-enable for debugging lifecycle issues if needed
  <BrowserRouter>
    <Routes>
      <Route path="/" element={<Chat />} />
      <Route path="/chat" element={<Chat />} />
      <Route path="/chat/:conversationId" element={<Chat />} />
      <Route path="/workspaces" element={<Workspaces />} />
      <Route path="/workspaces/:workspaceId" element={<WorkspaceSettings />} />
      <Route path="/workspaces/:workspaceId/settings" element={<WorkspaceSettings />} />
      <Route path="/org/settings" element={<OrgSettings />} />
      <Route path="/org/mcp-servers" element={<McpServers />} />
      <Route path="/settings/api-keys" element={<ApiKeys />} />
    </Routes>
  </BrowserRouter>,
)
