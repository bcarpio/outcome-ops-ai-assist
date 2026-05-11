import { useState, useEffect } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import {
  ArrowLeft,
  Settings,
  Users,
  Plug,
  Trash2,
  Plus,
  Loader2,
  UserMinus,
  Crown,
  User,
  X,
  Check,
  RefreshCw,
  ExternalLink,
  FileText,
  Share2,
  Upload,
  File,
  FolderOpen,
  Github,
  Search,
  Lock,
  Globe,
  AlertTriangle,
  RotateCcw,
  ShieldAlert,
} from 'lucide-react'
import {
  getWorkspace,
  updateWorkspace,
  deleteWorkspace,
  listMembers,
  addMember,
  updateMember,
  removeMember,
  listConfluenceConnections,
  getConfluenceAuthUrl,
  listConfluenceSpaces,
  updateConfluenceSpaces,
  triggerConfluenceSync,
  deleteConfluenceConnection,
  listJiraConnections,
  getJiraAuthUrl,
  listJiraProjects,
  updateJiraProjects,
  triggerJiraSync,
  deleteJiraConnection,
  getSearchAccess,
  updateSearchAccess,
  listSharingOptions,
  listDocuments,
  getDocumentUploadUrl,
  uploadDocumentToS3,
  deleteDocument,
  listOutlookConnections,
  getOutlookAuthUrl,
  listOutlookFolders,
  updateOutlookFolders,
  triggerOutlookSync,
  deleteOutlookConnection,
  listTeamsConnections,
  getTeamsAuthUrl,
  listTeamsTeams,
  listTeamsChannels,
  saveTeamsConfig,
  triggerTeamsSync,
  deleteTeamsConnection,
  listSharePointConnections,
  getSharePointAuthUrl,
  listSharePointSites,
  updateSharePointSites,
  triggerSharePointSync,
  deleteSharePointConnection,
  listOneDriveConnections,
  getOneDriveAuthUrl,
  listOneDriveFolders,
  updateOneDriveFolders,
  triggerOneDriveSync,
  deleteOneDriveConnection,
  listOneNoteConnections,
  getOneNoteAuthUrl,
  listOneNoteNotebooks,
  updateOneNoteNotebooks,
  triggerOneNoteSync,
  deleteOneNoteConnection,
  getGitHubInstallUrl,
  listGitHubInstallations,
  listGitHubInstallationRepos,
  listGitHubRepos,
  addGitHubRepos,
  updateGitHubRepo,
  removeGitHubRepo,
  triggerGitHubSync,
  type Workspace,
  type WorkspaceMember,
  type ConfluenceConnection,
  type ConfluenceSpace,
  type JiraConnection,
  type JiraProject,
  type WorkspaceSearchAccess,
  type WorkspaceDocument,
  type GitHubInstallation,
  type GitHubRepo,
  type GitHubAvailableRepo,
  type GitHubRepoType,
  type OutlookConnection,
  type OutlookFolder,
  type TeamsConnection,
  type TeamsTeam,
  type TeamsChannel,
  type TeamsChannelConfig,
  type SharePointConnection,
  type SharePointSite,
  type SharePointSiteConfig,
  type OneDriveConnection,
  type OneDriveFolder,
  type OneDriveFolderConfig,
  type OneNoteConnection,
  type OneNoteNotebook,
  type OneNoteNotebookConfig,
  searchUsers,
  type SearchUser,
  getWorkspaceSystemPrompt,
  updateWorkspaceSystemPrompt,
  resetWorkspaceSystemPrompt,
  type WorkspaceSystemPrompt,
  getWorkspacePrivacy,
  updateWorkspacePrivacy,
  type WorkspacePrivacy,
  type PrivacyMode,
} from '../lib/api'
import AdminNav from '../components/AdminNav'

type Tab = 'general' | 'members' | 'integrations' | 'sharing' | 'documents' | 'advanced'

export default function WorkspaceSettings() {
  const { workspaceId } = useParams()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const initialTab = (searchParams.get('tab') as Tab) || 'general'
  const configureConnectionId = searchParams.get('configure')
  const [activeTab, setActiveTab] = useState<Tab>(initialTab)
  const [workspace, setWorkspace] = useState<Workspace | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  // General tab state
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleting, setDeleting] = useState(false)

  // Members tab state
  const [members, setMembers] = useState<WorkspaceMember[]>([])
  const [loadingMembers, setLoadingMembers] = useState(false)
  const [showAddMember, setShowAddMember] = useState(false)
  const [newMemberEmail, setNewMemberEmail] = useState('')
  const [newMemberRole, setNewMemberRole] = useState<'member' | 'workspace_admin'>('member')
  const [addingMember, setAddingMember] = useState(false)
  const [userSearchQuery, setUserSearchQuery] = useState('')
  const [userSearchResults, setUserSearchResults] = useState<SearchUser[]>([])
  const [searchingUsers, setSearchingUsers] = useState(false)

  // Integrations tab state
  const [confluenceConnections, setConfluenceConnections] = useState<ConfluenceConnection[]>([])
  const [loadingConnections, setLoadingConnections] = useState(false)
  const [connectingConfluence, setConnectingConfluence] = useState(false)

  // Space configuration modal state
  const [configuringConnection, setConfiguringConnection] = useState<ConfluenceConnection | null>(null)
  const [availableSpaces, setAvailableSpaces] = useState<ConfluenceSpace[]>([])
  const [loadingSpaces, setLoadingSpaces] = useState(false)
  const [selectedSpaces, setSelectedSpaces] = useState<Set<string>>(new Set())
  const [includeAttachments, setIncludeAttachments] = useState(false)
  const [savingSpaces, setSavingSpaces] = useState(false)
  const [syncing, setSyncing] = useState<string | null>(null)

  // Jira integration state
  const [jiraConnections, setJiraConnections] = useState<JiraConnection[]>([])
  const [connectingJira, setConnectingJira] = useState(false)

  // Jira project configuration modal state
  const [configuringJiraConnection, setConfiguringJiraConnection] = useState<JiraConnection | null>(null)
  const [availableProjects, setAvailableProjects] = useState<JiraProject[]>([])
  const [loadingProjects, setLoadingProjects] = useState(false)
  const [selectedProjects, setSelectedProjects] = useState<Set<string>>(new Set())
  const [jiraIncludeAttachments, setJiraIncludeAttachments] = useState(true)
  const [jiraIncludeComments, setJiraIncludeComments] = useState(true)
  const [savingProjects, setSavingProjects] = useState(false)
  const [syncingJira, setSyncingJira] = useState<string | null>(null)

  // Outlook integration state
  const [outlookConnections, setOutlookConnections] = useState<OutlookConnection[]>([])
  const [connectingOutlook, setConnectingOutlook] = useState(false)

  // Outlook folder configuration modal state
  const [configuringOutlookConnection, setConfiguringOutlookConnection] = useState<OutlookConnection | null>(null)
  const [availableFolders, setAvailableFolders] = useState<OutlookFolder[]>([])
  const [loadingFolders, setLoadingFolders] = useState(false)
  const [selectedFolders, setSelectedFolders] = useState<Set<string>>(new Set())
  const [savingFolders, setSavingFolders] = useState(false)
  const [syncingOutlook, setSyncingOutlook] = useState<string | null>(null)

  // Teams integration state
  const [teamsConnections, setTeamsConnections] = useState<TeamsConnection[]>([])
  const [connectingTeams, setConnectingTeams] = useState(false)
  const [syncingTeams, setSyncingTeams] = useState<string | null>(null)
  const [configuringTeamsConnection, setConfiguringTeamsConnection] = useState<TeamsConnection | null>(null)
  const [availableTeams, setAvailableTeams] = useState<TeamsTeam[]>([])
  const [loadingTeams, setLoadingTeams] = useState(false)
  const [selectedTeamId, setSelectedTeamId] = useState<string>('')
  const [availableChannels, setAvailableChannels] = useState<TeamsChannel[]>([])
  const [loadingChannels, setLoadingChannels] = useState(false)
  const [selectedChannels, setSelectedChannels] = useState<TeamsChannelConfig[]>([])
  const [teamsDaysBack, setTeamsDaysBack] = useState(30)
  const [teamsSyncFiles, setTeamsSyncFiles] = useState(true)
  const [savingTeamsConfig, setSavingTeamsConfig] = useState(false)

  // SharePoint integration state
  const [sharePointConnections, setSharePointConnections] = useState<SharePointConnection[]>([])
  const [connectingSharePoint, setConnectingSharePoint] = useState(false)
  const [syncingSharePoint, setSyncingSharePoint] = useState<string | null>(null)
  const [configuringSharePointConnection, setConfiguringSharePointConnection] = useState<SharePointConnection | null>(null)
  const [availableSPSites, setAvailableSPSites] = useState<SharePointSite[]>([])
  const [loadingSPSites, setLoadingSPSites] = useState(false)
  const [selectedSPSites, setSelectedSPSites] = useState<Set<string>>(new Set())
  const [spIncludePages, setSpIncludePages] = useState(true)
  const [savingSPSites, setSavingSPSites] = useState(false)

  // OneDrive integration state
  const [oneDriveConnections, setOneDriveConnections] = useState<OneDriveConnection[]>([])
  const [connectingOneDrive, setConnectingOneDrive] = useState(false)
  const [syncingOneDrive, setSyncingOneDrive] = useState<string | null>(null)
  const [configuringOneDriveConnection, setConfiguringOneDriveConnection] = useState<OneDriveConnection | null>(null)
  const [availableODFolders, setAvailableODFolders] = useState<OneDriveFolder[]>([])
  const [loadingODFolders, setLoadingODFolders] = useState(false)
  const [selectedODFolders, setSelectedODFolders] = useState<Set<string>>(new Set())
  const [savingODFolders, setSavingODFolders] = useState(false)

  // OneNote integration state
  const [oneNoteConnections, setOneNoteConnections] = useState<OneNoteConnection[]>([])
  const [connectingOneNote, setConnectingOneNote] = useState(false)
  const [syncingOneNote, setSyncingOneNote] = useState<string | null>(null)
  const [configuringOneNoteConnection, setConfiguringOneNoteConnection] = useState<OneNoteConnection | null>(null)
  const [availableONNotebooks, setAvailableONNotebooks] = useState<OneNoteNotebook[]>([])
  const [loadingONNotebooks, setLoadingONNotebooks] = useState(false)
  const [selectedONNotebooks, setSelectedONNotebooks] = useState<Set<string>>(new Set())
  const [savingONNotebooks, setSavingONNotebooks] = useState(false)

  // Legacy repo state removed - now using GitHub App integration

  // GitHub App integration state
  const [githubInstallations, setGithubInstallations] = useState<GitHubInstallation[]>([])
  const [githubRepos, setGithubRepos] = useState<GitHubRepo[]>([])
  const [loadingGitHub, setLoadingGitHub] = useState(false)
  const [connectingGitHub, setConnectingGitHub] = useState(false)
  const [showAddGitHubRepo, setShowAddGitHubRepo] = useState(false)
  const [selectedInstallation, setSelectedInstallation] = useState<GitHubInstallation | null>(null)
  const [installationRepos, setInstallationRepos] = useState<GitHubAvailableRepo[]>([])
  const [loadingInstallationRepos, setLoadingInstallationRepos] = useState(false)
  const [addingGitHubRepo, setAddingGitHubRepo] = useState<string | null>(null)
  const [removingGitHubRepo, setRemovingGitHubRepo] = useState<string | null>(null)
  const [syncingGitHub, setSyncingGitHub] = useState<string | null>(null)
  const [githubRepoSearchQuery, setGithubRepoSearchQuery] = useState('')
  const [selectedRepoType, setSelectedRepoType] = useState<GitHubRepoType>('application')

  // Sharing tab state
  const [, setSharingAccess] = useState<WorkspaceSearchAccess[]>([])
  const [sharingOptions, setSharingOptions] = useState<WorkspaceSearchAccess[]>([])
  const [loadingSharing, setLoadingSharing] = useState(false)
  const [savingSharing, setSavingSharing] = useState(false)
  const [selectedSharingIds, setSelectedSharingIds] = useState<Set<string>>(new Set())

  // Documents tab state
  const [documents, setDocuments] = useState<WorkspaceDocument[]>([])
  const [loadingDocuments, setLoadingDocuments] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [uploadProgress, setUploadProgress] = useState(0)
  const [deletingDoc, setDeletingDoc] = useState<string | null>(null)
  const [dragOver, setDragOver] = useState(false)

  // Advanced tab state (system prompt)
  const [systemPromptData, setSystemPromptData] = useState<WorkspaceSystemPrompt | null>(null)
  const [systemPrompt, setSystemPrompt] = useState('')
  const [savedSystemPrompt, setSavedSystemPrompt] = useState('')
  const [loadingSystemPrompt, setLoadingSystemPrompt] = useState(false)
  const [savingSystemPrompt, setSavingSystemPrompt] = useState(false)
  const [resettingSystemPrompt, setResettingSystemPrompt] = useState(false)
  const [showResetPromptConfirm, setShowResetPromptConfirm] = useState(false)

  // Privacy state (PII redaction posture)
  const [privacy, setPrivacy] = useState<WorkspacePrivacy | null>(null)
  const [savingPrivacy, setSavingPrivacy] = useState(false)
  const [privacyError, setPrivacyError] = useState<string | null>(null)


  useEffect(() => {
    if (workspaceId) {
      loadWorkspace()
    }
  }, [workspaceId])

  useEffect(() => {
    if (workspaceId && activeTab === 'members') {
      loadMembers()
    } else if (workspaceId && activeTab === 'integrations') {
      loadConnections()
      loadGitHubData()
    } else if (workspaceId && activeTab === 'sharing') {
      loadSharing()
    } else if (workspaceId && activeTab === 'documents') {
      loadDocumentsList()
    } else if (workspaceId && activeTab === 'advanced') {
      loadSystemPrompt()
    }
  }, [workspaceId, activeTab])

  // Handle configure parameter from OAuth callback
  useEffect(() => {
    if (configureConnectionId && confluenceConnections.length > 0) {
      const conn = confluenceConnections.find(c => c.connection_id === configureConnectionId)
      if (conn) {
        openSpaceConfiguration(conn)
        // Clear the configure parameter from URL to prevent re-opening on tab switch
        const newParams = new URLSearchParams(searchParams)
        newParams.delete('configure')
        setSearchParams(newParams, { replace: true })
      }
    }
  }, [configureConnectionId, confluenceConnections])

  async function loadWorkspace() {
    if (!workspaceId) return
    setLoading(true)
    try {
      const data = await getWorkspace(workspaceId)
      setWorkspace(data)
      setName(data.name)
      setDescription(data.description || '')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load workspace')
    } finally {
      setLoading(false)
    }
    // Load privacy mode in parallel; fail silently to not block the page.
    try {
      const p = await getWorkspacePrivacy(workspaceId)
      setPrivacy(p)
    } catch {
      /* keep null; section just won't render */
    }
  }

  async function handlePrivacyChange(mode: PrivacyMode) {
    if (!workspaceId || !privacy || privacy.privacy_mode === mode) return
    setSavingPrivacy(true)
    setPrivacyError(null)
    try {
      const updated = await updateWorkspacePrivacy(workspaceId, mode)
      setPrivacy(updated)
    } catch (e) {
      setPrivacyError(
        e instanceof Error ? e.message : 'Failed to update privacy mode',
      )
    } finally {
      setSavingPrivacy(false)
    }
  }

  async function loadMembers() {
    if (!workspaceId) return
    setLoadingMembers(true)
    try {
      const data = await listMembers(workspaceId)
      setMembers(data)
    } catch (err) {
      console.error('Failed to load members:', err)
    } finally {
      setLoadingMembers(false)
    }
  }

  async function loadConnections() {
    if (!workspaceId) return
    setLoadingConnections(true)
    try {
      const [confluenceData, jiraData, outlookData, teamsData, sharePointData, oneDriveData, oneNoteData] = await Promise.all([
        listConfluenceConnections(workspaceId),
        listJiraConnections(workspaceId),
        listOutlookConnections(workspaceId).catch(() => []),
        listTeamsConnections(workspaceId).catch(() => []),
        listSharePointConnections(workspaceId).catch(() => []),
        listOneDriveConnections(workspaceId).catch(() => []),
        listOneNoteConnections(workspaceId).catch(() => []),
      ])
      setConfluenceConnections(confluenceData)
      setJiraConnections(jiraData)
      setOutlookConnections(outlookData)
      setTeamsConnections(teamsData)
      setSharePointConnections(sharePointData)
      setOneDriveConnections(oneDriveData)
      setOneNoteConnections(oneNoteData)
    } catch (err) {
      console.error('Failed to load connections:', err)
    } finally {
      setLoadingConnections(false)
    }
  }

  async function loadGitHubData() {
    if (!workspaceId) return
    setLoadingGitHub(true)
    try {
      const [installations, repos] = await Promise.all([
        listGitHubInstallations(workspaceId),
        listGitHubRepos(workspaceId),
      ])
      setGithubInstallations(installations)
      setGithubRepos(repos)
    } catch (err) {
      console.error('Failed to load GitHub data:', err)
    } finally {
      setLoadingGitHub(false)
    }
  }

  // Legacy loadRepos function removed - now using GitHub App integration

  // Legacy repo functions removed - now using GitHub App integration

  async function loadSharing() {
    if (!workspaceId) return
    setLoadingSharing(true)
    try {
      const [access, options] = await Promise.all([
        getSearchAccess(workspaceId),
        listSharingOptions(workspaceId),
      ])
      setSharingAccess(access)
      setSharingOptions(options)
      setSelectedSharingIds(new Set(access.map(a => a.workspace_id)))
    } catch (err) {
      console.error('Failed to load sharing:', err)
    } finally {
      setLoadingSharing(false)
    }
  }

  async function loadDocumentsList() {
    if (!workspaceId) return
    setLoadingDocuments(true)
    try {
      const docs = await listDocuments(workspaceId)
      setDocuments(docs)
    } catch (err) {
      console.error('Failed to load documents:', err)
    } finally {
      setLoadingDocuments(false)
    }
  }

  async function handleFileUpload(files: FileList | null) {
    if (!workspaceId || !files || files.length === 0) return

    const allowedExtensions = ['.md', '.txt', '.pdf', '.docx', '.xlsx', '.pptx', '.csv', '.html']
    const maxSize = 200 * 1024 * 1024 // 200MB

    for (const file of Array.from(files)) {
      const ext = '.' + file.name.split('.').pop()?.toLowerCase()
      if (!allowedExtensions.includes(ext)) {
        setError(`File type ${ext} not supported. Allowed: ${allowedExtensions.join(', ')}`)
        continue
      }
      if (file.size > maxSize) {
        setError(`File ${file.name} exceeds 200MB limit`)
        continue
      }

      setUploading(true)
      setUploadProgress(0)
      setError(null)

      try {
        // Get presigned URL
        const { upload_url } = await getDocumentUploadUrl(
          workspaceId,
          file.name,
          file.type || 'application/octet-stream'
        )

        // Upload to S3 with progress tracking
        await uploadDocumentToS3(upload_url, file, (progress) => {
          setUploadProgress(progress)
        })

        setSuccess(`Uploaded ${file.name}`)
        setTimeout(() => setSuccess(null), 3000)

        // Refresh document list
        loadDocumentsList()
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to upload file')
      } finally {
        setUploading(false)
        setUploadProgress(0)
      }
    }
  }

  async function handleDeleteDocument(docPath: string) {
    if (!workspaceId) return
    setDeletingDoc(docPath)
    setError(null)
    try {
      await deleteDocument(workspaceId, docPath)
      setDocuments(prev => prev.filter(d => d.path !== docPath))
      setSuccess('Document deleted')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete document')
    } finally {
      setDeletingDoc(null)
    }
  }

  function formatFileSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  function handleDragOver(e: React.DragEvent) {
    e.preventDefault()
    setDragOver(true)
  }

  function handleDragLeave(e: React.DragEvent) {
    e.preventDefault()
    setDragOver(false)
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault()
    setDragOver(false)
    handleFileUpload(e.dataTransfer.files)
  }

  async function handleSaveSharing() {
    if (!workspaceId) return
    setSavingSharing(true)
    setError(null)
    try {
      const updated = await updateSearchAccess(workspaceId, Array.from(selectedSharingIds))
      setSharingAccess(updated)
      setSuccess('Sharing settings updated')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update sharing')
    } finally {
      setSavingSharing(false)
    }
  }

  function toggleSharingWorkspace(wsId: string) {
    setSelectedSharingIds(prev => {
      const next = new Set(prev)
      if (next.has(wsId)) {
        next.delete(wsId)
      } else {
        next.add(wsId)
      }
      return next
    })
  }

  async function handleConnectConfluence() {
    if (!workspaceId) return
    setConnectingConfluence(true)
    setError(null)
    try {
      const authUrl = await getConfluenceAuthUrl(workspaceId)
      window.location.href = authUrl
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start Confluence connection')
      setConnectingConfluence(false)
    }
  }

  async function openSpaceConfiguration(connection: ConfluenceConnection) {
    if (!workspaceId) return
    setConfiguringConnection(connection)
    setLoadingSpaces(true)
    setError(null)
    try {
      const { spaces } = await listConfluenceSpaces(workspaceId, connection.connection_id)
      setAvailableSpaces(spaces)
      const selected = new Set((connection.selected_spaces || []).map(s => s.id))
      setSelectedSpaces(selected)
      setIncludeAttachments(connection.include_attachments)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load spaces')
      setConfiguringConnection(null)
    } finally {
      setLoadingSpaces(false)
    }
  }

  async function handleSaveSpaces() {
    if (!workspaceId || !configuringConnection) return
    setSavingSpaces(true)
    setError(null)
    try {
      const spacesToSave = availableSpaces.filter(s => selectedSpaces.has(s.id))
      await updateConfluenceSpaces(
        workspaceId,
        configuringConnection.connection_id,
        spacesToSave,
        includeAttachments
      )
      setConfiguringConnection(null)
      setSuccess('Spaces updated and sync queued')
      loadConnections()
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save spaces')
    } finally {
      setSavingSpaces(false)
    }
  }

  async function handleTriggerSync(connectionId: string) {
    if (!workspaceId) return
    setSyncing(connectionId)
    setError(null)
    try {
      await triggerConfluenceSync(workspaceId, connectionId)
      setSuccess('Sync queued')
      loadConnections()
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to trigger sync')
    } finally {
      setSyncing(null)
    }
  }

  async function handleDeleteConnection(connectionId: string) {
    if (!workspaceId) return
    setError(null)
    try {
      await deleteConfluenceConnection(workspaceId, connectionId)
      setConfluenceConnections(prev => prev.filter(c => c.connection_id !== connectionId))
      setSuccess('Connection deleted')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete connection')
    }
  }

  function toggleSpace(spaceId: string) {
    setSelectedSpaces(prev => {
      const next = new Set(prev)
      if (next.has(spaceId)) {
        next.delete(spaceId)
      } else {
        next.add(spaceId)
      }
      return next
    })
  }

  // Jira handlers
  async function handleConnectJira() {
    if (!workspaceId) return
    setConnectingJira(true)
    setError(null)
    try {
      const authUrl = await getJiraAuthUrl(workspaceId)
      window.location.href = authUrl
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start Jira connection')
      setConnectingJira(false)
    }
  }

  async function openProjectConfiguration(connection: JiraConnection) {
    if (!workspaceId) return
    setConfiguringJiraConnection(connection)
    setLoadingProjects(true)
    setError(null)
    try {
      const { projects } = await listJiraProjects(workspaceId, connection.connection_id)
      setAvailableProjects(projects)
      const selected = new Set((connection.selected_projects || []).map(p => p.key))
      setSelectedProjects(selected)
      setJiraIncludeAttachments(connection.include_attachments)
      setJiraIncludeComments(connection.include_comments)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load projects')
      setConfiguringJiraConnection(null)
    } finally {
      setLoadingProjects(false)
    }
  }

  async function handleSaveProjects() {
    if (!workspaceId || !configuringJiraConnection) return
    setSavingProjects(true)
    setError(null)
    try {
      await updateJiraProjects(
        workspaceId,
        configuringJiraConnection.connection_id,
        Array.from(selectedProjects),
        jiraIncludeAttachments,
        jiraIncludeComments
      )
      setConfiguringJiraConnection(null)
      setSuccess('Projects updated and sync queued')
      loadConnections()
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save projects')
    } finally {
      setSavingProjects(false)
    }
  }

  async function handleTriggerJiraSync(connectionId: string) {
    if (!workspaceId) return
    setSyncingJira(connectionId)
    setError(null)
    try {
      await triggerJiraSync(workspaceId, connectionId)
      setSuccess('Jira sync queued')
      loadConnections()
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to trigger sync')
    } finally {
      setSyncingJira(null)
    }
  }

  async function handleDeleteJiraConnection(connectionId: string) {
    if (!workspaceId) return
    setError(null)
    try {
      await deleteJiraConnection(workspaceId, connectionId)
      setJiraConnections(prev => prev.filter(c => c.connection_id !== connectionId))
      setSuccess('Jira connection deleted')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete connection')
    }
  }

  function toggleProject(projectKey: string) {
    setSelectedProjects(prev => {
      const next = new Set(prev)
      if (next.has(projectKey)) {
        next.delete(projectKey)
      } else {
        next.add(projectKey)
      }
      return next
    })
  }

  // Outlook handlers
  async function handleConnectOutlook() {
    if (!workspaceId) return
    setConnectingOutlook(true)
    setError(null)
    try {
      const authUrl = await getOutlookAuthUrl(workspaceId)
      window.location.href = authUrl
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start Outlook connection')
      setConnectingOutlook(false)
    }
  }

  async function openFolderConfiguration(connection: OutlookConnection) {
    if (!workspaceId) return
    setConfiguringOutlookConnection(connection)
    setLoadingFolders(true)
    setError(null)
    try {
      const { folders } = await listOutlookFolders(workspaceId, connection.connection_id)
      setAvailableFolders(folders)
      const selected = new Set((connection.selected_folders || []).map(f => f.id))
      setSelectedFolders(selected)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load folders')
      setConfiguringOutlookConnection(null)
    } finally {
      setLoadingFolders(false)
    }
  }

  async function handleSaveFolders() {
    if (!workspaceId || !configuringOutlookConnection) return
    setSavingFolders(true)
    setError(null)
    try {
      const foldersToSave = availableFolders.filter(f => selectedFolders.has(f.id))
      await updateOutlookFolders(
        workspaceId,
        configuringOutlookConnection.connection_id,
        foldersToSave
      )
      setConfiguringOutlookConnection(null)
      setSuccess('Folders updated and sync queued')
      loadConnections()
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save folders')
    } finally {
      setSavingFolders(false)
    }
  }

  async function handleTriggerOutlookSync(connectionId: string) {
    if (!workspaceId) return
    setSyncingOutlook(connectionId)
    setError(null)
    try {
      await triggerOutlookSync(workspaceId, connectionId)
      setSuccess('Outlook sync queued')
      loadConnections()
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to trigger sync')
    } finally {
      setSyncingOutlook(null)
    }
  }

  async function handleDeleteOutlookConnection(connectionId: string) {
    if (!workspaceId) return
    setError(null)
    try {
      await deleteOutlookConnection(workspaceId, connectionId)
      setOutlookConnections(prev => prev.filter(c => c.connection_id !== connectionId))
      setSuccess('Outlook connection deleted')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete connection')
    }
  }

  function toggleFolder(folderId: string) {
    setSelectedFolders(prev => {
      const next = new Set(prev)
      if (next.has(folderId)) {
        next.delete(folderId)
      } else {
        next.add(folderId)
      }
      return next
    })
  }

  // Teams handlers
  async function handleConnectTeams() {
    if (!workspaceId) return
    setConnectingTeams(true)
    setError(null)
    try {
      const authUrl = await getTeamsAuthUrl(workspaceId)
      window.location.href = authUrl
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start Teams connection')
      setConnectingTeams(false)
    }
  }

  async function openTeamsConfiguration(connection: TeamsConnection) {
    if (!workspaceId) return
    setConfiguringTeamsConnection(connection)
    setLoadingTeams(true)
    setError(null)
    try {
      const teams = await listTeamsTeams(workspaceId, connection.connection_id)
      setAvailableTeams(teams)
      // Restore existing config
      const existingChannels = connection.config?.include_channels || []
      setSelectedChannels(existingChannels)
      setTeamsDaysBack(connection.config?.days_back || 30)
      setTeamsSyncFiles(connection.config?.sync_files !== false)
      setSelectedTeamId('')
      setAvailableChannels([])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load teams')
      setConfiguringTeamsConnection(null)
    } finally {
      setLoadingTeams(false)
    }
  }

  async function handleTeamSelect(teamId: string) {
    if (!workspaceId || !configuringTeamsConnection) return
    setSelectedTeamId(teamId)
    if (!teamId) {
      setAvailableChannels([])
      return
    }
    setLoadingChannels(true)
    try {
      const channels = await listTeamsChannels(
        workspaceId,
        configuringTeamsConnection.connection_id,
        teamId
      )
      setAvailableChannels(channels)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load channels')
    } finally {
      setLoadingChannels(false)
    }
  }

  function toggleTeamsChannel(teamId: string, teamName: string, channelId: string, channelName: string) {
    setSelectedChannels(prev => {
      const exists = prev.find(c => c.team_id === teamId && c.channel_id === channelId)
      if (exists) {
        return prev.filter(c => !(c.team_id === teamId && c.channel_id === channelId))
      }
      return [...prev, { team_id: teamId, team_name: teamName, channel_id: channelId, channel_name: channelName }]
    })
  }

  async function handleSaveTeamsConfig() {
    if (!workspaceId || !configuringTeamsConnection) return
    setSavingTeamsConfig(true)
    setError(null)
    try {
      await saveTeamsConfig(
        workspaceId,
        configuringTeamsConnection.connection_id,
        {
          days_back: teamsDaysBack,
          include_channels: selectedChannels,
          sync_files: teamsSyncFiles,
        }
      )
      setConfiguringTeamsConnection(null)
      setSuccess('Teams configuration saved and sync queued')
      loadConnections()
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save configuration')
    } finally {
      setSavingTeamsConfig(false)
    }
  }

  async function handleTriggerTeamsSync(connectionId: string) {
    if (!workspaceId) return
    setSyncingTeams(connectionId)
    setError(null)
    try {
      await triggerTeamsSync(workspaceId, connectionId)
      setSuccess('Teams sync queued')
      loadConnections()
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to trigger sync')
    } finally {
      setSyncingTeams(null)
    }
  }

  async function handleDeleteTeamsConnection(connectionId: string) {
    if (!workspaceId) return
    setError(null)
    try {
      await deleteTeamsConnection(workspaceId, connectionId)
      setTeamsConnections(prev => prev.filter(c => c.connection_id !== connectionId))
      setSuccess('Teams connection deleted')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete connection')
    }
  }

  // SharePoint handlers
  async function handleConnectSharePoint() {
    if (!workspaceId) return
    setConnectingSharePoint(true)
    setError(null)
    try {
      const authUrl = await getSharePointAuthUrl(workspaceId)
      window.location.href = authUrl
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start SharePoint connection')
      setConnectingSharePoint(false)
    }
  }

  async function openSharePointConfiguration(connection: SharePointConnection) {
    if (!workspaceId) return
    setConfiguringSharePointConnection(connection)
    setLoadingSPSites(true)
    setError(null)
    try {
      const sites = await listSharePointSites(workspaceId, connection.connection_id)
      setAvailableSPSites(sites)
      const selected = new Set((connection.selected_sites || []).map(s => s.site_id))
      setSelectedSPSites(selected)
      setSpIncludePages(connection.include_pages !== false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load sites')
      setConfiguringSharePointConnection(null)
    } finally {
      setLoadingSPSites(false)
    }
  }

  async function handleSaveSPSites() {
    if (!workspaceId || !configuringSharePointConnection) return
    setSavingSPSites(true)
    setError(null)
    try {
      const sitesToSave: SharePointSiteConfig[] = availableSPSites
        .filter(s => selectedSPSites.has(s.id))
        .map(s => ({ site_id: s.id, site_name: s.friendly_name || s.displayName, drives: s.drives || [] }))
      await updateSharePointSites(
        workspaceId,
        configuringSharePointConnection.connection_id,
        sitesToSave,
        spIncludePages
      )
      setConfiguringSharePointConnection(null)
      setSuccess('Sites updated and sync queued')
      loadConnections()
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save sites')
    } finally {
      setSavingSPSites(false)
    }
  }

  async function handleTriggerSharePointSync(connectionId: string) {
    if (!workspaceId) return
    setSyncingSharePoint(connectionId)
    setError(null)
    try {
      await triggerSharePointSync(workspaceId, connectionId)
      setSuccess('SharePoint sync queued')
      loadConnections()
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to trigger sync')
    } finally {
      setSyncingSharePoint(null)
    }
  }

  async function handleDeleteSharePointConnection(connectionId: string) {
    if (!workspaceId) return
    setError(null)
    try {
      await deleteSharePointConnection(workspaceId, connectionId)
      setSharePointConnections(prev => prev.filter(c => c.connection_id !== connectionId))
      setSuccess('SharePoint connection deleted')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete connection')
    }
  }

  // OneDrive handlers
  async function handleConnectOneDrive() {
    if (!workspaceId) return
    setConnectingOneDrive(true)
    setError(null)
    try {
      const authUrl = await getOneDriveAuthUrl(workspaceId)
      window.location.href = authUrl
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start OneDrive connection')
      setConnectingOneDrive(false)
    }
  }

  async function openOneDriveConfiguration(connection: OneDriveConnection) {
    if (!workspaceId) return
    setConfiguringOneDriveConnection(connection)
    setLoadingODFolders(true)
    setError(null)
    try {
      const folders = await listOneDriveFolders(workspaceId, connection.connection_id)
      setAvailableODFolders(folders)
      const selected = new Set((connection.selected_folders || []).map(f => f.folder_id))
      setSelectedODFolders(selected)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load folders')
      setConfiguringOneDriveConnection(null)
    } finally {
      setLoadingODFolders(false)
    }
  }

  async function handleSaveODFolders() {
    if (!workspaceId || !configuringOneDriveConnection) return
    setSavingODFolders(true)
    setError(null)
    try {
      const foldersToSave: OneDriveFolderConfig[] = availableODFolders
        .filter(f => selectedODFolders.has(f.folder_id))
        .map(f => ({
          folder_id: f.folder_id,
          folder_name: f.folder_name,
          folder_path: f.folder_path,
        }))
      await updateOneDriveFolders(
        workspaceId,
        configuringOneDriveConnection.connection_id,
        foldersToSave
      )
      setConfiguringOneDriveConnection(null)
      setSuccess('Folders updated and sync queued')
      loadConnections()
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save folders')
    } finally {
      setSavingODFolders(false)
    }
  }

  async function handleTriggerOneDriveSync(connectionId: string) {
    if (!workspaceId) return
    setSyncingOneDrive(connectionId)
    setError(null)
    try {
      await triggerOneDriveSync(workspaceId, connectionId)
      setSuccess('OneDrive sync queued')
      loadConnections()
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to trigger sync')
    } finally {
      setSyncingOneDrive(null)
    }
  }

  async function handleDeleteOneDriveConnection(connectionId: string) {
    if (!workspaceId) return
    setError(null)
    try {
      await deleteOneDriveConnection(workspaceId, connectionId)
      setOneDriveConnections(prev => prev.filter(c => c.connection_id !== connectionId))
      setSuccess('OneDrive connection deleted')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete connection')
    }
  }

  // OneNote handlers
  async function handleConnectOneNote() {
    if (!workspaceId) return
    setConnectingOneNote(true)
    setError(null)
    try {
      const authUrl = await getOneNoteAuthUrl(workspaceId)
      window.location.href = authUrl
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start OneNote connection')
      setConnectingOneNote(false)
    }
  }

  async function openOneNoteConfiguration(connection: OneNoteConnection) {
    if (!workspaceId) return
    setConfiguringOneNoteConnection(connection)
    setLoadingONNotebooks(true)
    setError(null)
    try {
      const nbs = await listOneNoteNotebooks(workspaceId, connection.connection_id)
      setAvailableONNotebooks(nbs)
      const selected = new Set((connection.selected_notebooks || []).map(n => n.notebook_id))
      setSelectedONNotebooks(selected)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load notebooks')
      setConfiguringOneNoteConnection(null)
    } finally {
      setLoadingONNotebooks(false)
    }
  }

  async function handleSaveONNotebooks() {
    if (!workspaceId || !configuringOneNoteConnection) return
    setSavingONNotebooks(true)
    setError(null)
    try {
      const notebooksToSave: OneNoteNotebookConfig[] = availableONNotebooks
        .filter(n => selectedONNotebooks.has(n.notebook_id))
        .map(n => ({
          notebook_id: n.notebook_id,
          notebook_name: n.notebook_name,
        }))
      await updateOneNoteNotebooks(
        workspaceId,
        configuringOneNoteConnection.connection_id,
        notebooksToSave
      )
      setConfiguringOneNoteConnection(null)
      setSuccess('Notebooks updated and sync queued')
      loadConnections()
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save notebooks')
    } finally {
      setSavingONNotebooks(false)
    }
  }

  async function handleTriggerOneNoteSync(connectionId: string) {
    if (!workspaceId) return
    setSyncingOneNote(connectionId)
    setError(null)
    try {
      await triggerOneNoteSync(workspaceId, connectionId)
      setSuccess('OneNote sync queued')
      loadConnections()
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to trigger sync')
    } finally {
      setSyncingOneNote(null)
    }
  }

  async function handleDeleteOneNoteConnection(connectionId: string) {
    if (!workspaceId) return
    setError(null)
    try {
      await deleteOneNoteConnection(workspaceId, connectionId)
      setOneNoteConnections(prev => prev.filter(c => c.connection_id !== connectionId))
      setSuccess('OneNote connection deleted')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete connection')
    }
  }

  function toggleSPSite(siteId: string) {
    setSelectedSPSites(prev => {
      const next = new Set(prev)
      if (next.has(siteId)) {
        next.delete(siteId)
      } else {
        next.add(siteId)
      }
      return next
    })
  }

  function toggleODFolder(folderId: string) {
    setSelectedODFolders(prev => {
      const next = new Set(prev)
      if (next.has(folderId)) {
        next.delete(folderId)
      } else {
        next.add(folderId)
      }
      return next
    })
  }

  function toggleONNotebook(notebookId: string) {
    setSelectedONNotebooks(prev => {
      const next = new Set(prev)
      if (next.has(notebookId)) {
        next.delete(notebookId)
      } else {
        next.add(notebookId)
      }
      return next
    })
  }

  // GitHub App handlers
  async function handleConnectGitHub() {
    if (!workspaceId) return
    setConnectingGitHub(true)
    setError(null)
    try {
      const installUrl = await getGitHubInstallUrl(workspaceId)
      window.location.href = installUrl
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start GitHub connection')
      setConnectingGitHub(false)
    }
  }

  async function openGitHubRepoModal(installation: GitHubInstallation) {
    if (!workspaceId) return
    setSelectedInstallation(installation)
    setShowAddGitHubRepo(true)
    setLoadingInstallationRepos(true)
    setGithubRepoSearchQuery('')
    try {
      const repos = await listGitHubInstallationRepos(workspaceId, installation.installation_id)
      // Filter out already-added repos
      const addedNames = new Set(githubRepos.map(r => r.repo_name))
      setInstallationRepos(repos.filter(r => !addedNames.has(r.full_name)))
    } catch (err) {
      console.error('Failed to load installation repos:', err)
      setError(err instanceof Error ? err.message : 'Failed to load available repos')
    } finally {
      setLoadingInstallationRepos(false)
    }
  }

  async function handleAddGitHubRepo(repoName: string) {
    if (!workspaceId || !selectedInstallation) return
    setAddingGitHubRepo(repoName)
    setError(null)
    try {
      const repos = await addGitHubRepos(workspaceId, [{
        repo_name: repoName,
        installation_id: selectedInstallation.installation_id,
        include_code_maps: true,
        repo_type: selectedRepoType,
      }])
      setGithubRepos(prev => [...prev, ...repos])
      setInstallationRepos(prev => prev.filter(r => r.full_name !== repoName))
      setSuccess(`Added ${repoName} as ${selectedRepoType}`)
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add repo')
    } finally {
      setAddingGitHubRepo(null)
    }
  }

  async function handleRemoveGitHubRepo(repoName: string) {
    if (!workspaceId) return
    setRemovingGitHubRepo(repoName)
    setError(null)
    try {
      await removeGitHubRepo(workspaceId, repoName)
      setGithubRepos(prev => prev.filter(r => r.repo_name !== repoName))
      setSuccess(`Removed ${repoName}`)
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to remove repo')
    } finally {
      setRemovingGitHubRepo(null)
    }
  }

  async function handleToggleCodeMaps(repo: GitHubRepo) {
    if (!workspaceId) return
    try {
      const updated = await updateGitHubRepo(workspaceId, repo.repo_name, {
        include_code_maps: !repo.include_code_maps,
      })
      setGithubRepos(prev => prev.map(r => r.repo_name === repo.repo_name ? updated : r))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update repo')
    }
  }

  async function handleTriggerGitHubSync(repoName?: string) {
    if (!workspaceId) return
    setSyncingGitHub(repoName || 'all')
    setError(null)
    try {
      await triggerGitHubSync(workspaceId, repoName)
      setSuccess(repoName ? `Sync queued for ${repoName}` : 'Sync queued for all repos')
      loadGitHubData()
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to trigger sync')
    } finally {
      setSyncingGitHub(null)
    }
  }

  const filteredInstallationRepos = installationRepos.filter(r =>
    r.full_name.toLowerCase().includes(githubRepoSearchQuery.toLowerCase()) ||
    (r.description || '').toLowerCase().includes(githubRepoSearchQuery.toLowerCase())
  )

  async function handleSaveGeneral() {
    if (!workspaceId) return
    setSaving(true)
    setError(null)
    setSuccess(null)
    try {
      const updated = await updateWorkspace(workspaceId, { name, description })
      setWorkspace(updated)
      setSuccess('Workspace updated successfully')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update workspace')
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete() {
    if (!workspaceId) return
    setDeleting(true)
    try {
      await deleteWorkspace(workspaceId)
      navigate('/workspaces')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete workspace')
      setDeleting(false)
    }
  }

  async function handleAddMember() {
    if (!workspaceId || !newMemberEmail.trim()) return
    setAddingMember(true)
    setError(null)
    try {
      const member = await addMember(workspaceId, newMemberEmail.trim(), newMemberRole)
      setMembers(prev => [...prev, member])
      setShowAddMember(false)
      setNewMemberEmail('')
      setNewMemberRole('member')
      setUserSearchQuery('')
      setUserSearchResults([])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add member')
    } finally {
      setAddingMember(false)
    }
  }

  async function handleUserSearch(query: string) {
    setUserSearchQuery(query)
    if (query.length < 2) {
      setUserSearchResults([])
      return
    }
    setSearchingUsers(true)
    try {
      const results = await searchUsers(query)
      const memberEmails = new Set(members.map(m => m.user_email))
      setUserSearchResults(results.filter(u => !memberEmails.has(u.user_email)))
    } catch {
      setUserSearchResults([])
    } finally {
      setSearchingUsers(false)
    }
  }

  async function handleUpdateMemberRole(email: string, role: 'member' | 'workspace_admin') {
    if (!workspaceId) return
    try {
      await updateMember(workspaceId, email, role)
      setMembers(prev => prev.map(m => m.user_email === email ? { ...m, role } : m))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update member role')
    }
  }

  async function handleRemoveMember(email: string) {
    if (!workspaceId) return
    try {
      await removeMember(workspaceId, email)
      setMembers(prev => prev.filter(m => m.user_email !== email))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to remove member')
    }
  }

  async function loadSystemPrompt() {
    if (!workspaceId) return
    setLoadingSystemPrompt(true)
    try {
      const data = await getWorkspaceSystemPrompt(workspaceId)
      setSystemPromptData(data)
      const prompt = data.custom_system_prompt || ''
      setSystemPrompt(prompt)
      setSavedSystemPrompt(prompt)
    } catch (err) {
      console.error('Failed to load system prompt:', err)
    } finally {
      setLoadingSystemPrompt(false)
    }
  }

  async function handleSaveSystemPrompt() {
    if (!workspaceId) return
    setSavingSystemPrompt(true)
    setError(null)
    try {
      const data = await updateWorkspaceSystemPrompt(workspaceId, systemPrompt)
      setSystemPromptData(data)
      setSavedSystemPrompt(systemPrompt)
      setSuccess('System prompt updated')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update system prompt')
    } finally {
      setSavingSystemPrompt(false)
    }
  }

  async function handleResetSystemPrompt() {
    if (!workspaceId) return
    setResettingSystemPrompt(true)
    setError(null)
    try {
      const data = await resetWorkspaceSystemPrompt(workspaceId)
      setSystemPromptData(data)
      setSystemPrompt('')
      setSavedSystemPrompt('')
      setShowResetPromptConfirm(false)
      setSuccess('System prompt reset to default')
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reset system prompt')
    } finally {
      setResettingSystemPrompt(false)
    }
  }

  const isAdmin = workspace?.role === 'workspace_admin' || workspace?.role === 'org_admin'

  if (loading) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <Loader2 className="animate-spin text-blue-600" size={32} />
      </div>
    )
  }

  if (!workspace) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center">
          <p className="text-gray-500">Workspace not found</p>
          <button
            onClick={() => navigate('/workspaces')}
            className="mt-4 text-blue-600 hover:text-blue-700"
          >
            Back to workspaces
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b">
        <div className="max-w-4xl mx-auto px-4 py-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <button
                onClick={() => navigate('/workspaces')}
                className="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
              >
                <ArrowLeft size={20} />
              </button>
              <div>
                <h1 className="text-xl font-semibold">{workspace.name}</h1>
                <p className="text-sm text-gray-500">Workspace Settings</p>
              </div>
            </div>
            <AdminNav />
          </div>
        </div>
      </header>

      {/* Tabs */}
      <div className="bg-white border-b">
        <div className="max-w-4xl mx-auto px-4">
          <nav className="flex gap-6">
            {[
              { id: 'general' as Tab, label: 'General', icon: Settings },
              { id: 'members' as Tab, label: 'Members', icon: Users },
              { id: 'documents' as Tab, label: 'Documents', icon: FolderOpen },
              { id: 'integrations' as Tab, label: 'Integrations', icon: Plug },
              { id: 'sharing' as Tab, label: 'Sharing', icon: Share2 },
              ...(isAdmin ? [{ id: 'advanced' as Tab, label: 'Advanced', icon: AlertTriangle }] : []),
            ].map(tab => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-2 py-4 border-b-2 transition-colors ${
                  activeTab === tab.id
                    ? 'border-blue-600 text-blue-600'
                    : 'border-transparent text-gray-500 hover:text-gray-700'
                }`}
              >
                <tab.icon size={18} />
                {tab.label}
              </button>
            ))}
          </nav>
        </div>
      </div>

      {/* Content */}
      <main className="max-w-4xl mx-auto px-4 py-8">
        {error && (
          <div className="mb-6 p-4 bg-red-50 border border-red-200 rounded-lg text-red-600 flex items-center justify-between">
            {error}
            <button onClick={() => setError(null)} className="text-red-400 hover:text-red-600">
              <X size={18} />
            </button>
          </div>
        )}

        {success && (
          <div className="mb-6 p-4 bg-green-50 border border-green-200 rounded-lg text-green-600 flex items-center gap-2">
            <Check size={18} />
            {success}
          </div>
        )}

        {/* General Tab */}
        {activeTab === 'general' && (
          <div className="bg-white rounded-lg border shadow-sm">
            <div className="p-6 space-y-6">
              <div>
                <label htmlFor="ws-name" className="block text-sm font-medium text-gray-700 mb-1">
                  Name
                </label>
                <input
                  id="ws-name"
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  disabled={!isAdmin}
                  className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100"
                />
              </div>
              <div>
                <label htmlFor="ws-desc" className="block text-sm font-medium text-gray-700 mb-1">
                  Description
                </label>
                <textarea
                  id="ws-desc"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  disabled={!isAdmin}
                  rows={3}
                  className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none disabled:bg-gray-100"
                />
              </div>
              {isAdmin && (
                <div className="flex justify-end">
                  <button
                    onClick={handleSaveGeneral}
                    disabled={saving}
                    className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                  >
                    {saving && <Loader2 size={16} className="animate-spin" />}
                    Save Changes
                  </button>
                </div>
              )}
            </div>

            {/* Privacy section (PII redaction). Hidden if the org-level
                kill switch is off - per spec, an inert toggle is worse
                than no toggle (admins click and nothing happens). */}
            {privacy && (
              <div className="border-t p-6">
                <div className="flex items-center justify-between mb-2">
                  <h3 className="text-lg font-medium text-gray-900">Privacy</h3>
                  <ShieldAlert size={20} className="text-gray-500" />
                </div>

                {!privacy.redaction_enabled_at_org_level ? (
                  <div className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-sm text-gray-700">
                    <div className="font-medium mb-1">
                      PII redaction is disabled organization-wide.
                    </div>
                    An organization administrator has set
                    <code className="font-mono mx-1">pii_redaction_enabled = false</code>
                    in the platform configuration. Per-workspace privacy
                    settings are unavailable until that's enabled.
                  </div>
                ) : (
                  <>
                    <p className="text-sm text-gray-600 mb-3">
                      Controls whether the platform scrubs personally-identifiable
                      information (emails, phone numbers, names, addresses, etc.)
                      from documents before they're embedded into this workspace's
                      knowledge base.
                    </p>

                    <div className="space-y-2">
                      <label className="flex items-start gap-2 cursor-pointer p-2 rounded border hover:bg-gray-50">
                        <input
                          type="radio"
                          name="privacy-mode"
                          value="STRICT"
                          checked={privacy.privacy_mode === 'STRICT'}
                          onChange={() => handlePrivacyChange('STRICT')}
                          disabled={!isAdmin || savingPrivacy}
                          className="mt-0.5"
                        />
                        <span>
                          <span className="font-medium text-gray-900">
                            Redact PII (recommended)
                          </span>
                          <span className="block text-xs text-gray-600">
                            Default. Documents are scanned via Amazon Comprehend
                            and any PII is replaced with type tags (e.g.,
                            {' '}<code className="font-mono">[REDACTED:EMAIL]</code>)
                            before embedding.
                          </span>
                        </span>
                      </label>

                      <label className="flex items-start gap-2 cursor-pointer p-2 rounded border hover:bg-gray-50">
                        <input
                          type="radio"
                          name="privacy-mode"
                          value="NONE"
                          checked={privacy.privacy_mode === 'NONE'}
                          onChange={() => handlePrivacyChange('NONE')}
                          disabled={!isAdmin || savingPrivacy}
                          className="mt-0.5"
                        />
                        <span>
                          <span className="font-medium text-gray-900">No redaction</span>
                          <span className="block text-xs text-gray-600">
                            Use only for workspaces containing purely technical
                            content (e.g., source code) where PII isn't expected.
                            Document the justification in the workspace description
                            for audit.
                          </span>
                        </span>
                      </label>
                    </div>

                    {privacy.privacy_mode === 'NONE' && (
                      <div className="rounded-lg border border-rose-200 bg-rose-50 p-3 mt-3 text-sm text-rose-900">
                        <div className="font-medium mb-1">
                          PII redaction is disabled for this workspace.
                        </div>
                        Raw email addresses, names, and other identifiers will be
                        stored in the knowledge base. Only use this setting for
                        workspaces that contain purely technical content. Consider
                        noting the justification in the workspace description so
                        InfoSec audits have the paper trail.
                      </div>
                    )}

                    {privacyError && (
                      <div className="text-sm text-rose-600 mt-2">{privacyError}</div>
                    )}

                    {privacy.updated_at && (
                      <div className="text-xs text-gray-500 mt-2">
                        Last changed by {privacy.updated_by} at {privacy.updated_at}
                      </div>
                    )}
                  </>
                )}
              </div>
            )}

            {isAdmin && (
              <div className="border-t p-6">
                <h3 className="text-lg font-medium text-red-600 mb-2">Danger Zone</h3>
                <p className="text-sm text-gray-500 mb-4">
                  Deleting this workspace will remove all repos and integrations. This cannot be undone.
                </p>
                <button
                  onClick={() => setShowDeleteConfirm(true)}
                  className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors flex items-center gap-2"
                >
                  <Trash2 size={16} />
                  Delete Workspace
                </button>
              </div>
            )}
          </div>
        )}

        {/* Members Tab */}
        {activeTab === 'members' && (
          <div className="bg-white rounded-lg border shadow-sm">
            <div className="p-4 border-b flex items-center justify-between">
              <h3 className="font-medium">Members ({members.length})</h3>
              {isAdmin && (
                <button
                  onClick={() => setShowAddMember(true)}
                  className="flex items-center gap-2 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
                >
                  <Plus size={16} />
                  Add Member
                </button>
              )}
            </div>
            {loadingMembers ? (
              <div className="p-8 flex items-center justify-center">
                <Loader2 className="animate-spin text-gray-400" size={24} />
              </div>
            ) : (
              <ul className="divide-y">
                {members.map(member => {
                  const adminCount = members.filter(m => m.role === 'workspace_admin').length
                  const isLastAdmin = member.role === 'workspace_admin' && adminCount <= 1
                  return (
                  <li key={member.user_email} className="p-4 flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      {member.role === 'workspace_admin' ? (
                        <Crown className="text-amber-500" size={20} />
                      ) : (
                        <User className="text-gray-400" size={20} />
                      )}
                      <div>
                        <p className="font-medium text-gray-900">{member.user_email}</p>
                        <p className="text-sm text-gray-500 capitalize">{member.role.replace('_', ' ')}</p>
                      </div>
                    </div>
                    {isAdmin && (
                      <div className="flex items-center gap-2">
                        <select
                          value={member.role}
                          onChange={(e) => handleUpdateMemberRole(member.user_email, e.target.value as 'member' | 'workspace_admin')}
                          className="text-sm border rounded px-2 py-1"
                          disabled={isLastAdmin}
                          title={isLastAdmin ? 'Cannot demote the last admin' : undefined}
                        >
                          <option value="member">Member</option>
                          <option value="workspace_admin">Admin</option>
                        </select>
                        <button
                          onClick={() => handleRemoveMember(member.user_email)}
                          className={`p-1.5 rounded transition-colors ${isLastAdmin ? 'text-gray-300 cursor-not-allowed' : 'text-red-500 hover:bg-red-50'}`}
                          title={isLastAdmin ? 'Cannot remove the last admin' : 'Remove member'}
                          disabled={isLastAdmin}
                        >
                          <UserMinus size={18} />
                        </button>
                      </div>
                    )}
                  </li>
                  )
                })}
              </ul>
            )}
          </div>
        )}

        {/* Integrations Tab */}
        {activeTab === 'integrations' && (
          <div className="space-y-6">
            {/* GitHub App Integration Section */}
            <div className="bg-white rounded-lg border shadow-sm">
              <div className="p-4 border-b flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <Github className="text-gray-800" size={24} />
                  <div>
                    <h3 className="font-medium">GitHub</h3>
                    <p className="text-sm text-gray-500">Connect GitHub to sync repositories for code maps</p>
                  </div>
                </div>
                {isAdmin && githubInstallations.length === 0 && (
                  <button
                    onClick={handleConnectGitHub}
                    disabled={connectingGitHub}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm bg-gray-900 text-white rounded-lg hover:bg-gray-800 disabled:opacity-50 transition-colors"
                  >
                    {connectingGitHub ? (
                      <Loader2 size={16} className="animate-spin" />
                    ) : (
                      <Plus size={16} />
                    )}
                    Connect GitHub
                  </button>
                )}
              </div>
              {loadingGitHub ? (
                <div className="p-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : githubInstallations.length === 0 ? (
                <div className="p-8 text-center text-gray-500">
                  <Github className="mx-auto text-gray-300 mb-2" size={48} />
                  <p>No GitHub App installed</p>
                  <p className="text-sm mt-1">Connect your GitHub account to sync repositories</p>
                </div>
              ) : (
                <div>
                  {/* Installations */}
                  {githubInstallations.map(inst => (
                    <div key={inst.installation_id} className="border-b last:border-b-0">
                      <div className="p-4 bg-gray-50 flex items-center justify-between">
                        <div className="flex items-center gap-3">
                          <Github className="text-gray-600" size={20} />
                          <div>
                            <p className="font-medium text-gray-900">{inst.account_login}</p>
                            <p className="text-sm text-gray-500">{inst.account_type}</p>
                          </div>
                        </div>
                        {isAdmin && (
                          <button
                            onClick={() => openGitHubRepoModal(inst)}
                            className="flex items-center gap-2 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
                          >
                            <Plus size={16} />
                            Add Repos
                          </button>
                        )}
                      </div>
                    </div>
                  ))}

                  {/* GitHub Repos List */}
                  {githubRepos.length === 0 ? (
                    <div className="p-8 text-center text-gray-500">
                      <p>No repositories selected</p>
                      <p className="text-sm mt-1">Add repos from your GitHub installations above</p>
                    </div>
                  ) : (
                    <ul className="divide-y">
                      {githubRepos.map(repo => (
                        <li key={repo.repo_name} className="p-4 flex items-center justify-between hover:bg-gray-50">
                          <div className="flex items-center gap-3 flex-1 min-w-0">
                            <Github className="text-gray-400 flex-shrink-0" size={20} />
                            <div className="min-w-0">
                              <div className="flex items-center gap-2">
                                <p className="font-medium text-gray-900 truncate">{repo.repo_name}</p>
                                <span className="text-xs px-1.5 py-0.5 rounded bg-gray-100 text-gray-600">
                                  {repo.repo_type || 'application'}
                                </span>
                              </div>
                              <p className="text-sm text-gray-500">
                                {repo.last_sync_status ? (
                                  <>
                                    <span className={
                                      repo.last_sync_status === 'success' ? 'text-green-600' :
                                      repo.last_sync_status === 'failed' ? 'text-red-600' :
                                      repo.last_sync_status === 'in_progress' || repo.last_sync_status === 'queued' ? 'text-blue-600' :
                                      'text-gray-500'
                                    }>
                                      {repo.last_sync_status}
                                    </span>
                                    {repo.last_sync_at && <> | {new Date(repo.last_sync_at).toLocaleDateString()}</>}
                                  </>
                                ) : (
                                  <span className="text-amber-600">Never synced</span>
                                )}
                              </p>
                            </div>
                          </div>
                          <div className="flex items-center gap-2 ml-4">
                            {repo.repo_type !== 'standards' && (
                              <label className="flex items-center gap-2 text-sm cursor-pointer" title="Include in code maps">
                                <input
                                  type="checkbox"
                                  checked={repo.include_code_maps}
                                  onChange={() => handleToggleCodeMaps(repo)}
                                  className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
                                />
                                <span className="text-gray-600 hidden sm:inline">Code maps</span>
                              </label>
                            )}
                            {isAdmin && (
                              <>
                                <button
                                  onClick={() => handleTriggerGitHubSync(repo.repo_name)}
                                  disabled={syncingGitHub === repo.repo_name || (repo.repo_type !== 'standards' && !repo.include_code_maps)}
                                  className="p-1.5 text-gray-500 hover:bg-gray-100 rounded transition-colors disabled:opacity-50"
                                  title="Sync now"
                                >
                                  {syncingGitHub === repo.repo_name ? (
                                    <Loader2 size={16} className="animate-spin" />
                                  ) : (
                                    <RefreshCw size={16} />
                                  )}
                                </button>
                                <button
                                  onClick={() => handleRemoveGitHubRepo(repo.repo_name)}
                                  disabled={removingGitHubRepo === repo.repo_name}
                                  className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors disabled:opacity-50"
                                  title="Remove repo"
                                >
                                  {removingGitHubRepo === repo.repo_name ? (
                                    <Loader2 size={16} className="animate-spin" />
                                  ) : (
                                    <Trash2 size={16} />
                                  )}
                                </button>
                              </>
                            )}
                          </div>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </div>

            {/* Confluence Section */}
            <div className="bg-white rounded-lg border shadow-sm">
              <div className="p-4 border-b flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <FileText className="text-blue-500" size={24} />
                  <div>
                    <h3 className="font-medium">Confluence</h3>
                    <p className="text-sm text-gray-500">Sync pages from Confluence Cloud</p>
                  </div>
                </div>
                {isAdmin && confluenceConnections.length === 0 && (
                  <button
                    onClick={handleConnectConfluence}
                    disabled={connectingConfluence}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
                  >
                    {connectingConfluence ? (
                      <Loader2 size={16} className="animate-spin" />
                    ) : (
                      <Plus size={16} />
                    )}
                    Connect
                  </button>
                )}
              </div>
              {loadingConnections ? (
                <div className="p-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : confluenceConnections.length === 0 ? (
                <div className="p-8 text-center text-gray-500">
                  No Confluence connections yet
                </div>
              ) : (
                <ul className="divide-y">
                  {confluenceConnections.map(conn => (
                    <li key={conn.connection_id} className="p-4">
                      <div className="flex items-start justify-between">
                        <div className="flex-1">
                          <div className="flex items-center gap-2">
                            <p className="font-medium text-gray-900">{conn.site_name}</p>
                            <a
                              href={conn.site_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-gray-400 hover:text-gray-600"
                            >
                              <ExternalLink size={14} />
                            </a>
                          </div>
                          <p className="text-sm text-gray-500 mt-1">
                            {(conn.selected_spaces || []).length === 0 ? (
                              <span className="text-amber-600">No spaces selected - click Configure</span>
                            ) : (
                              <>
                                {conn.selected_spaces.length} space{conn.selected_spaces.length !== 1 ? 's' : ''} selected
                                {' | '}
                                {conn.page_count} pages synced
                              </>
                            )}
                          </p>
                          <p className="text-xs text-gray-400 mt-1">
                            Status: <span className={
                              conn.last_sync_status === 'completed' ? 'text-green-600' :
                              conn.last_sync_status === 'failed' ? 'text-red-600' :
                              conn.last_sync_status === 'syncing' ? 'text-blue-600' :
                              'text-gray-500'
                            }>
                              {conn.last_sync_status}
                            </span>
                            {conn.last_sync_at && (
                              <> | Last sync: {new Date(conn.last_sync_at).toLocaleDateString()}</>
                            )}
                          </p>
                        </div>
                        {isAdmin && (
                          <div className="flex items-center gap-2 ml-4">
                            <button
                              onClick={() => openSpaceConfiguration(conn)}
                              className="px-3 py-1.5 text-sm text-blue-600 hover:bg-blue-50 rounded transition-colors"
                            >
                              Configure
                            </button>
                            <button
                              onClick={() => handleTriggerSync(conn.connection_id)}
                              disabled={syncing === conn.connection_id || (conn.selected_spaces || []).length === 0}
                              className="p-1.5 text-gray-500 hover:bg-gray-100 rounded transition-colors disabled:opacity-50"
                              title="Sync now"
                            >
                              {syncing === conn.connection_id ? (
                                <Loader2 size={16} className="animate-spin" />
                              ) : (
                                <RefreshCw size={16} />
                              )}
                            </button>
                            <button
                              onClick={() => handleDeleteConnection(conn.connection_id)}
                              className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                              title="Disconnect"
                            >
                              <Trash2 size={16} />
                            </button>
                          </div>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* Jira Section */}
            <div className="bg-white rounded-lg border shadow-sm">
              <div className="p-4 border-b flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <svg className="w-6 h-6 text-blue-600" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M11.53 2c-.55 0-1.08.22-1.47.61L2.61 10.06c-.81.81-.81 2.13 0 2.94l7.45 7.45c.81.81 2.13.81 2.94 0l7.45-7.45c.81-.81.81-2.13 0-2.94l-7.45-7.45c-.39-.39-.92-.61-1.47-.61zm.53 4.04l5.43 5.43-5.43 5.43-5.43-5.43 5.43-5.43z"/>
                  </svg>
                  <div>
                    <h3 className="font-medium">Jira</h3>
                    <p className="text-sm text-gray-500">Sync issues from Jira Cloud</p>
                  </div>
                </div>
                {isAdmin && jiraConnections.length === 0 && (
                  <button
                    onClick={handleConnectJira}
                    disabled={connectingJira}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
                  >
                    {connectingJira ? (
                      <Loader2 size={16} className="animate-spin" />
                    ) : (
                      <Plus size={16} />
                    )}
                    Connect
                  </button>
                )}
              </div>
              {loadingConnections ? (
                <div className="p-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : jiraConnections.length === 0 ? (
                <div className="p-8 text-center text-gray-500">
                  No Jira connections yet
                </div>
              ) : (
                <ul className="divide-y">
                  {jiraConnections.map(conn => (
                    <li key={conn.connection_id} className="p-4">
                      <div className="flex items-start justify-between">
                        <div className="flex-1">
                          <div className="flex items-center gap-2">
                            <p className="font-medium text-gray-900">{conn.site_name}</p>
                            <a
                              href={conn.site_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-gray-400 hover:text-gray-600"
                            >
                              <ExternalLink size={14} />
                            </a>
                          </div>
                          <p className="text-sm text-gray-500 mt-1">
                            {(conn.selected_projects || []).length === 0 ? (
                              <span className="text-amber-600">No projects selected - click Configure</span>
                            ) : (
                              <>
                                {conn.selected_projects.length} project{conn.selected_projects.length !== 1 ? 's' : ''} selected
                                {' | '}
                                {conn.issue_count} issues synced
                              </>
                            )}
                          </p>
                          <p className="text-xs text-gray-400 mt-1">
                            Status: <span className={
                              conn.last_sync_status === 'success' ? 'text-green-600' :
                              conn.last_sync_status === 'failed' || conn.last_sync_status === 'auth_error' ? 'text-red-600' :
                              conn.last_sync_status === 'in_progress' || conn.last_sync_status === 'queued' ? 'text-blue-600' :
                              'text-gray-500'
                            }>
                              {conn.last_sync_status === 'in_progress' ? 'syncing' : conn.last_sync_status}
                            </span>
                            {conn.last_sync_at && (
                              <> | Last sync: {new Date(conn.last_sync_at).toLocaleDateString()}</>
                            )}
                            {conn.last_sync_error && (
                              <span className="text-red-500"> | {conn.last_sync_error}</span>
                            )}
                          </p>
                        </div>
                        {isAdmin && (
                          <div className="flex items-center gap-2 ml-4">
                            <button
                              onClick={() => openProjectConfiguration(conn)}
                              className="px-3 py-1.5 text-sm text-blue-600 hover:bg-blue-50 rounded transition-colors"
                            >
                              Configure
                            </button>
                            <button
                              onClick={() => handleTriggerJiraSync(conn.connection_id)}
                              disabled={syncingJira === conn.connection_id || (conn.selected_projects || []).length === 0}
                              className="p-1.5 text-gray-500 hover:bg-gray-100 rounded transition-colors disabled:opacity-50"
                              title="Sync now"
                            >
                              {syncingJira === conn.connection_id ? (
                                <Loader2 size={16} className="animate-spin" />
                              ) : (
                                <RefreshCw size={16} />
                              )}
                            </button>
                            <button
                              onClick={() => handleDeleteJiraConnection(conn.connection_id)}
                              className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                              title="Disconnect"
                            >
                              <Trash2 size={16} />
                            </button>
                          </div>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* Outlook Section */}
            <div className="bg-white rounded-lg border shadow-sm">
              <div className="p-4 border-b flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <svg className="w-6 h-6 text-blue-500" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/>
                  </svg>
                  <div>
                    <h3 className="font-medium">Outlook 365</h3>
                    <p className="text-sm text-gray-500">Sync emails from Microsoft Outlook</p>
                  </div>
                </div>
                {isAdmin && outlookConnections.length === 0 && (
                  <button
                    onClick={handleConnectOutlook}
                    disabled={connectingOutlook}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
                  >
                    {connectingOutlook ? (
                      <Loader2 size={16} className="animate-spin" />
                    ) : (
                      <Plus size={16} />
                    )}
                    Connect
                  </button>
                )}
              </div>
              {loadingConnections ? (
                <div className="p-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : outlookConnections.length === 0 ? (
                <div className="p-8 text-center text-gray-500">
                  No Outlook connections yet
                </div>
              ) : (
                <ul className="divide-y">
                  {outlookConnections.map(conn => (
                    <li key={conn.connection_id} className="p-4">
                      <div className="flex items-start justify-between">
                        <div className="flex-1">
                          <div className="flex items-center gap-2">
                            <p className="font-medium text-gray-900">{conn.display_name}</p>
                          </div>
                          <p className="text-sm text-gray-500 mt-0.5">{conn.user_email}</p>
                          <p className="text-sm text-gray-500 mt-1">
                            {(conn.selected_folders || []).length === 0 ? (
                              <span className="text-amber-600">No folders selected - click Configure</span>
                            ) : (
                              <>
                                {conn.selected_folders.length} folder{conn.selected_folders.length !== 1 ? 's' : ''} selected
                                {' | '}
                                {conn.email_count} emails synced
                              </>
                            )}
                          </p>
                          <p className="text-xs text-gray-400 mt-1">
                            Status: <span className={
                              conn.last_sync_status === 'completed' ? 'text-green-600' :
                              conn.last_sync_status === 'failed' || conn.last_sync_status === 'auth_error' ? 'text-red-600' :
                              conn.last_sync_status === 'syncing' || conn.last_sync_status === 'queued' ? 'text-blue-600' :
                              'text-gray-500'
                            }>
                              {conn.last_sync_status}
                            </span>
                            {conn.last_sync_at && (
                              <> | Last sync: {new Date(conn.last_sync_at).toLocaleDateString()}</>
                            )}
                            {conn.last_sync_error && (
                              <span className="text-red-500"> | {conn.last_sync_error}</span>
                            )}
                          </p>
                        </div>
                        {isAdmin && (
                          <div className="flex items-center gap-2 ml-4">
                            <button
                              onClick={() => openFolderConfiguration(conn)}
                              className="px-3 py-1.5 text-sm text-blue-600 hover:bg-blue-50 rounded transition-colors"
                            >
                              Configure
                            </button>
                            <button
                              onClick={() => handleTriggerOutlookSync(conn.connection_id)}
                              disabled={syncingOutlook === conn.connection_id || (conn.selected_folders || []).length === 0}
                              className="p-1.5 text-gray-500 hover:bg-gray-100 rounded transition-colors disabled:opacity-50"
                              title="Sync now"
                            >
                              {syncingOutlook === conn.connection_id ? (
                                <Loader2 size={16} className="animate-spin" />
                              ) : (
                                <RefreshCw size={16} />
                              )}
                            </button>
                            <button
                              onClick={() => handleDeleteOutlookConnection(conn.connection_id)}
                              className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                              title="Disconnect"
                            >
                              <Trash2 size={16} />
                            </button>
                          </div>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* Teams Section */}
            <div className="bg-white rounded-lg border shadow-sm">
              <div className="p-4 border-b flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <svg className="w-6 h-6 text-purple-600" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M19.27 7.73a3 3 0 0 0-1.54.43A3.5 3.5 0 1 0 14 10.5v8A1.5 1.5 0 0 0 15.5 20h4a1.5 1.5 0 0 0 1.5-1.5V10a3 3 0 0 0-1.73-2.27zM20 11a1 1 0 1 1-2 0 1 1 0 0 1 2 0zM9 8a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zm-4 4.5A1.5 1.5 0 0 1 6.5 11h5a1.5 1.5 0 0 1 1.5 1.5V20H5v-7.5z"/>
                  </svg>
                  <div>
                    <h3 className="font-medium">Microsoft Teams</h3>
                    <p className="text-sm text-gray-500">Sync channel messages and files from Teams</p>
                  </div>
                </div>
                {isAdmin && teamsConnections.length === 0 && (
                  <button
                    onClick={handleConnectTeams}
                    disabled={connectingTeams}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50 transition-colors"
                  >
                    {connectingTeams ? (
                      <Loader2 size={16} className="animate-spin" />
                    ) : (
                      <Plus size={16} />
                    )}
                    Connect
                  </button>
                )}
              </div>
              {loadingConnections ? (
                <div className="p-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : teamsConnections.length === 0 ? (
                <div className="p-8 text-center text-gray-500">
                  No Teams connections yet
                </div>
              ) : (
                <ul className="divide-y">
                  {teamsConnections.map(conn => (
                    <li key={conn.connection_id} className="p-4">
                      <div className="flex items-start justify-between">
                        <div className="flex-1">
                          <div className="flex items-center gap-2">
                            <p className="font-medium text-gray-900">{conn.display_name}</p>
                          </div>
                          <p className="text-sm text-gray-500 mt-0.5">{conn.user_email}</p>
                          <p className="text-sm text-gray-500 mt-1">
                            {(conn.config?.include_channels || []).length === 0 ? (
                              <span className="text-amber-600">No channels selected - click Configure</span>
                            ) : (
                              <>
                                {conn.config.include_channels.length} channel{conn.config.include_channels.length !== 1 ? 's' : ''} selected
                                {' | '}
                                {conn.message_count || 0} messages synced
                                {conn.config.sync_files && <> | Files enabled</>}
                              </>
                            )}
                          </p>
                          <p className="text-xs text-gray-400 mt-1">
                            Status: <span className={
                              conn.last_sync_status === 'completed' ? 'text-green-600' :
                              conn.last_sync_status === 'failed' || conn.last_sync_status === 'auth_error' ? 'text-red-600' :
                              conn.last_sync_status === 'syncing' || conn.last_sync_status === 'queued' ? 'text-blue-600' :
                              'text-gray-500'
                            }>
                              {conn.last_sync_status || 'pending'}
                            </span>
                            {conn.last_sync_at && (
                              <> | Last sync: {new Date(conn.last_sync_at).toLocaleDateString()}</>
                            )}
                            {conn.last_sync_error && (
                              <span className="text-red-500"> | {conn.last_sync_error}</span>
                            )}
                          </p>
                        </div>
                        {isAdmin && (
                          <div className="flex items-center gap-2 ml-4">
                            <button
                              onClick={() => openTeamsConfiguration(conn)}
                              className="px-3 py-1.5 text-sm text-purple-600 hover:bg-purple-50 rounded transition-colors"
                            >
                              Configure
                            </button>
                            <button
                              onClick={() => handleTriggerTeamsSync(conn.connection_id)}
                              disabled={syncingTeams === conn.connection_id || (conn.config?.include_channels || []).length === 0}
                              className="p-1.5 text-gray-500 hover:bg-gray-100 rounded transition-colors disabled:opacity-50"
                              title="Sync now"
                            >
                              {syncingTeams === conn.connection_id ? (
                                <Loader2 size={16} className="animate-spin" />
                              ) : (
                                <RefreshCw size={16} />
                              )}
                            </button>
                            <button
                              onClick={() => handleDeleteTeamsConnection(conn.connection_id)}
                              className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                              title="Disconnect"
                            >
                              <Trash2 size={16} />
                            </button>
                          </div>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* SharePoint Section */}
            <div className="bg-white rounded-lg border shadow-sm">
              <div className="p-4 border-b flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <svg className="w-6 h-6 text-teal-600" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/>
                  </svg>
                  <div>
                    <h3 className="font-medium">SharePoint</h3>
                    <p className="text-sm text-gray-500">Sync documents and pages from SharePoint sites</p>
                  </div>
                </div>
                {isAdmin && sharePointConnections.length === 0 && (
                  <button
                    onClick={handleConnectSharePoint}
                    disabled={connectingSharePoint}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm bg-teal-600 text-white rounded-lg hover:bg-teal-700 disabled:opacity-50 transition-colors"
                  >
                    {connectingSharePoint ? (
                      <Loader2 size={16} className="animate-spin" />
                    ) : (
                      <Plus size={16} />
                    )}
                    Connect
                  </button>
                )}
              </div>
              {loadingConnections ? (
                <div className="p-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : sharePointConnections.length === 0 ? (
                <div className="p-8 text-center text-gray-500">
                  No SharePoint connections yet
                </div>
              ) : (
                <ul className="divide-y">
                  {sharePointConnections.map(conn => (
                    <li key={conn.connection_id} className="p-4">
                      <div className="flex items-start justify-between">
                        <div className="flex-1">
                          <div className="flex items-center gap-2">
                            <p className="font-medium text-gray-900">{conn.display_name}</p>
                          </div>
                          <p className="text-sm text-gray-500 mt-0.5">{conn.user_email}</p>
                          <p className="text-sm text-gray-500 mt-1">
                            {(conn.selected_sites || []).length === 0 ? (
                              <span className="text-amber-600">No sites selected - click Configure</span>
                            ) : (
                              <>
                                {conn.selected_sites.length} site{conn.selected_sites.length !== 1 ? 's' : ''} selected
                                {' | '}
                                {conn.file_count || 0} files
                                {conn.include_pages && <> | {conn.page_count || 0} pages</>}
                              </>
                            )}
                          </p>
                          <p className="text-xs text-gray-400 mt-1">
                            Status: <span className={
                              conn.last_sync_status === 'completed' ? 'text-green-600' :
                              conn.last_sync_status === 'failed' || conn.last_sync_status === 'auth_error' ? 'text-red-600' :
                              conn.last_sync_status === 'syncing' || conn.last_sync_status === 'queued' ? 'text-blue-600' :
                              'text-gray-500'
                            }>
                              {conn.last_sync_status || 'pending'}
                            </span>
                            {conn.last_sync_at && (
                              <> | Last sync: {new Date(conn.last_sync_at).toLocaleDateString()}</>
                            )}
                            {conn.last_sync_error && (
                              <span className="text-red-500"> | {conn.last_sync_error}</span>
                            )}
                          </p>
                        </div>
                        {isAdmin && (
                          <div className="flex items-center gap-2 ml-4">
                            <button
                              onClick={() => openSharePointConfiguration(conn)}
                              className="px-3 py-1.5 text-sm text-teal-600 hover:bg-teal-50 rounded transition-colors"
                            >
                              Configure
                            </button>
                            <button
                              onClick={() => handleTriggerSharePointSync(conn.connection_id)}
                              disabled={syncingSharePoint === conn.connection_id || (conn.selected_sites || []).length === 0}
                              className="p-1.5 text-gray-500 hover:bg-gray-100 rounded transition-colors disabled:opacity-50"
                              title="Sync now"
                            >
                              {syncingSharePoint === conn.connection_id ? (
                                <Loader2 size={16} className="animate-spin" />
                              ) : (
                                <RefreshCw size={16} />
                              )}
                            </button>
                            <button
                              onClick={() => handleDeleteSharePointConnection(conn.connection_id)}
                              className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                              title="Disconnect"
                            >
                              <Trash2 size={16} />
                            </button>
                          </div>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* OneDrive Section */}
            <div className="bg-white rounded-lg border shadow-sm">
              <div className="p-4 border-b flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <svg className="w-6 h-6 text-blue-600" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M14.5 9C14.5 6.79 12.71 5 10.5 5c-1.83 0-3.36 1.24-3.85 2.91C5.16 8.04 4 9.39 4 11c0 1.66 1.34 3 3 3h7.5c1.38 0 2.5-1.12 2.5-2.5S15.88 9 14.5 9zM19 15h-2v3h-2v-3h-2v-2h2v-3h2v3h2v2z"/>
                  </svg>
                  <div>
                    <h3 className="font-medium">OneDrive</h3>
                    <p className="text-sm text-gray-500">Sync files from your personal OneDrive folders</p>
                  </div>
                </div>
                {isAdmin && oneDriveConnections.length === 0 && (
                  <button
                    onClick={handleConnectOneDrive}
                    disabled={connectingOneDrive}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
                  >
                    {connectingOneDrive ? (
                      <Loader2 size={16} className="animate-spin" />
                    ) : (
                      <Plus size={16} />
                    )}
                    Connect
                  </button>
                )}
              </div>
              {loadingConnections ? (
                <div className="p-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : oneDriveConnections.length === 0 ? (
                <div className="p-8 text-center text-gray-500">
                  No OneDrive connections yet
                </div>
              ) : (
                <ul className="divide-y">
                  {oneDriveConnections.map(conn => (
                    <li key={conn.connection_id} className="p-4">
                      <div className="flex items-start justify-between">
                        <div className="flex-1">
                          <div className="flex items-center gap-2">
                            <p className="font-medium text-gray-900">{conn.display_name}</p>
                          </div>
                          <p className="text-sm text-gray-500 mt-0.5">{conn.user_email}</p>
                          <p className="text-sm text-gray-500 mt-1">
                            {(conn.selected_folders || []).length === 0 ? (
                              <span className="text-amber-600">No folders selected - click Configure</span>
                            ) : (
                              <>
                                {conn.selected_folders.length} folder{conn.selected_folders.length !== 1 ? 's' : ''} selected
                                {' | '}
                                {conn.file_count || 0} files
                              </>
                            )}
                          </p>
                          <p className="text-xs text-gray-400 mt-1">
                            Status: <span className={
                              conn.last_sync_status === 'completed' ? 'text-green-600' :
                              conn.last_sync_status === 'failed' || conn.last_sync_status === 'auth_error' ? 'text-red-600' :
                              conn.last_sync_status === 'syncing' || conn.last_sync_status === 'queued' ? 'text-blue-600' :
                              'text-gray-500'
                            }>
                              {conn.last_sync_status || 'pending'}
                            </span>
                            {conn.last_sync_at && (
                              <> | Last sync: {new Date(conn.last_sync_at).toLocaleDateString()}</>
                            )}
                            {conn.last_sync_error && (
                              <span className="text-red-500"> | {conn.last_sync_error}</span>
                            )}
                          </p>
                        </div>
                        {isAdmin && (
                          <div className="flex items-center gap-2 ml-4">
                            <button
                              onClick={() => openOneDriveConfiguration(conn)}
                              className="px-3 py-1.5 text-sm text-blue-600 hover:bg-blue-50 rounded transition-colors"
                            >
                              Configure
                            </button>
                            <button
                              onClick={() => handleTriggerOneDriveSync(conn.connection_id)}
                              disabled={syncingOneDrive === conn.connection_id || (conn.selected_folders || []).length === 0}
                              className="p-1.5 text-gray-500 hover:bg-gray-100 rounded transition-colors disabled:opacity-50"
                              title="Sync now"
                            >
                              {syncingOneDrive === conn.connection_id ? (
                                <Loader2 size={16} className="animate-spin" />
                              ) : (
                                <RefreshCw size={16} />
                              )}
                            </button>
                            <button
                              onClick={() => handleDeleteOneDriveConnection(conn.connection_id)}
                              className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                              title="Disconnect"
                            >
                              <Trash2 size={16} />
                            </button>
                          </div>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* OneNote Section */}
            <div className="bg-white rounded-lg border shadow-sm">
              <div className="p-4 border-b flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <svg className="w-6 h-6 text-purple-600" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M14 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8l-6-6zm-2 14H8v-2h4v2zm4-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/>
                  </svg>
                  <div>
                    <h3 className="font-medium">OneNote</h3>
                    <p className="text-sm text-gray-500">Sync notebook pages from Microsoft OneNote</p>
                  </div>
                </div>
                {isAdmin && oneNoteConnections.length === 0 && (
                  <button
                    onClick={handleConnectOneNote}
                    disabled={connectingOneNote}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50 transition-colors"
                  >
                    {connectingOneNote ? (
                      <Loader2 size={16} className="animate-spin" />
                    ) : (
                      <Plus size={16} />
                    )}
                    Connect
                  </button>
                )}
              </div>
              {loadingConnections ? (
                <div className="p-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : oneNoteConnections.length === 0 ? (
                <div className="p-8 text-center text-gray-500">
                  No OneNote connections yet
                </div>
              ) : (
                <ul className="divide-y">
                  {oneNoteConnections.map(conn => (
                    <li key={conn.connection_id} className="p-4">
                      <div className="flex items-start justify-between">
                        <div className="flex-1">
                          <div className="flex items-center gap-2">
                            <p className="font-medium text-gray-900">{conn.display_name}</p>
                          </div>
                          <p className="text-sm text-gray-500 mt-0.5">{conn.user_email}</p>
                          <p className="text-sm text-gray-500 mt-1">
                            {(conn.selected_notebooks || []).length === 0 ? (
                              <span className="text-amber-600">No notebooks selected - click Configure</span>
                            ) : (
                              <>
                                {conn.selected_notebooks.length} notebook{conn.selected_notebooks.length !== 1 ? 's' : ''} selected
                                {' | '}
                                {conn.page_count || 0} pages
                              </>
                            )}
                          </p>
                          <p className="text-xs text-gray-400 mt-1">
                            Status: <span className={
                              conn.last_sync_status === 'completed' ? 'text-green-600' :
                              conn.last_sync_status === 'failed' || conn.last_sync_status === 'auth_error' ? 'text-red-600' :
                              conn.last_sync_status === 'syncing' || conn.last_sync_status === 'queued' ? 'text-blue-600' :
                              'text-gray-500'
                            }>
                              {conn.last_sync_status || 'pending'}
                            </span>
                            {conn.last_sync_at && (
                              <> | Last sync: {new Date(conn.last_sync_at).toLocaleDateString()}</>
                            )}
                            {conn.last_sync_error && (
                              <span className="text-red-500"> | {conn.last_sync_error}</span>
                            )}
                          </p>
                        </div>
                        {isAdmin && (
                          <div className="flex items-center gap-2 ml-4">
                            <button
                              onClick={() => openOneNoteConfiguration(conn)}
                              className="px-3 py-1.5 text-sm text-purple-600 hover:bg-purple-50 rounded transition-colors"
                            >
                              Configure
                            </button>
                            <button
                              onClick={() => handleTriggerOneNoteSync(conn.connection_id)}
                              disabled={syncingOneNote === conn.connection_id || (conn.selected_notebooks || []).length === 0}
                              className="p-1.5 text-gray-500 hover:bg-gray-100 rounded transition-colors disabled:opacity-50"
                              title="Sync now"
                            >
                              {syncingOneNote === conn.connection_id ? (
                                <Loader2 size={16} className="animate-spin" />
                              ) : (
                                <RefreshCw size={16} />
                              )}
                            </button>
                            <button
                              onClick={() => handleDeleteOneNoteConnection(conn.connection_id)}
                              className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                              title="Disconnect"
                            >
                              <Trash2 size={16} />
                            </button>
                          </div>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}

        {/* Sharing Tab */}
        {activeTab === 'sharing' && (
          <div className="bg-white rounded-lg border shadow-sm">
            <div className="p-6 border-b">
              <h3 className="font-medium text-lg">Cross-Workspace Search Access</h3>
              <p className="text-sm text-gray-500 mt-1">
                Allow other workspaces to search this workspace's content. This enables cross-team knowledge sharing.
              </p>
            </div>
            {loadingSharing ? (
              <div className="p-8 flex items-center justify-center">
                <Loader2 className="animate-spin text-gray-400" size={24} />
              </div>
            ) : !isAdmin ? (
              <div className="p-8 text-center text-gray-500">
                Only workspace admins can manage sharing settings.
              </div>
            ) : sharingOptions.length === 0 ? (
              <div className="p-8 text-center text-gray-500">
                No other workspaces available to share with.
              </div>
            ) : (
              <>
                <div className="p-6 space-y-3">
                  <p className="text-sm font-medium text-gray-700 mb-4">
                    Select workspaces that can search this workspace:
                  </p>
                  {sharingOptions.map(ws => (
                    <label
                      key={ws.workspace_id}
                      className="flex items-center gap-3 p-3 rounded-lg border cursor-pointer hover:bg-gray-50 transition-colors"
                    >
                      <input
                        type="checkbox"
                        checked={selectedSharingIds.has(ws.workspace_id)}
                        onChange={() => toggleSharingWorkspace(ws.workspace_id)}
                        className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
                      />
                      <div className="flex-1">
                        <p className="font-medium text-gray-900">{ws.name}</p>
                      </div>
                    </label>
                  ))}
                </div>
                <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex items-center justify-between border-t">
                  <p className="text-sm text-gray-500">
                    {selectedSharingIds.size} workspace{selectedSharingIds.size !== 1 ? 's' : ''} can search this workspace
                  </p>
                  <button
                    onClick={handleSaveSharing}
                    disabled={savingSharing}
                    className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                  >
                    {savingSharing && <Loader2 size={16} className="animate-spin" />}
                    Save Changes
                  </button>
                </div>
              </>
            )}
          </div>
        )}

        {/* Documents Tab */}
        {activeTab === 'documents' && (
          <div className="space-y-6">
            {/* Upload Area */}
            <div
              className={`bg-white rounded-lg border-2 border-dashed p-8 text-center transition-colors ${
                dragOver ? 'border-blue-500 bg-blue-50' : 'border-gray-300'
              } ${uploading ? 'opacity-50 pointer-events-none' : ''}`}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={handleDrop}
            >
              <input
                type="file"
                id="file-upload"
                className="hidden"
                multiple
                accept=".md,.txt,.pdf,.docx,.xlsx,.pptx,.csv,.html"
                onChange={(e) => handleFileUpload(e.target.files)}
                disabled={uploading}
              />
              <Upload className="mx-auto text-gray-400 mb-4" size={48} />
              <p className="text-gray-600 mb-2">
                Drag and drop files here, or{' '}
                <label
                  htmlFor="file-upload"
                  className="text-blue-600 hover:text-blue-700 cursor-pointer font-medium"
                >
                  browse
                </label>
              </p>
              <p className="text-sm text-gray-400">
                Supported formats: PDF, DOCX, XLSX, PPTX, MD, TXT, CSV, HTML (max 200MB)
              </p>
              {uploading && (
                <div className="mt-4">
                  <div className="h-2 bg-gray-200 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-blue-600 transition-all duration-300"
                      style={{ width: `${uploadProgress}%` }}
                    />
                  </div>
                  <p className="text-sm text-gray-500 mt-2">Uploading... {uploadProgress}%</p>
                </div>
              )}
            </div>

            {/* Documents List */}
            <div className="bg-white rounded-lg border shadow-sm">
              <div className="p-4 border-b flex items-center justify-between">
                <h3 className="font-medium">Documents ({documents.length})</h3>
                <button
                  onClick={loadDocumentsList}
                  disabled={loadingDocuments}
                  className="p-2 text-gray-500 hover:bg-gray-100 rounded transition-colors"
                  title="Refresh"
                >
                  <RefreshCw size={16} className={loadingDocuments ? 'animate-spin' : ''} />
                </button>
              </div>
              {loadingDocuments ? (
                <div className="p-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : documents.length === 0 ? (
                <div className="p-8 text-center text-gray-500">
                  <File className="mx-auto text-gray-300 mb-2" size={48} />
                  <p>No documents uploaded yet</p>
                  <p className="text-sm mt-1">Upload documents above to make them searchable</p>
                </div>
              ) : (
                <ul className="divide-y">
                  {documents.map(doc => (
                    <li key={doc.path} className="p-4 flex items-center justify-between hover:bg-gray-50">
                      <div className="flex items-center gap-3 min-w-0">
                        <FileText className="text-gray-400 flex-shrink-0" size={20} />
                        <div className="min-w-0">
                          <p className="font-medium text-gray-900 truncate" title={doc.path}>
                            {doc.filename}
                          </p>
                          <p className="text-sm text-gray-500">
                            {formatFileSize(doc.size)} | {new Date(doc.last_modified).toLocaleDateString()}
                          </p>
                        </div>
                      </div>
                      {isAdmin && (
                        <button
                          onClick={() => handleDeleteDocument(doc.path)}
                          disabled={deletingDoc === doc.path}
                          className="p-2 text-red-500 hover:bg-red-50 rounded transition-colors disabled:opacity-50"
                          title="Delete document"
                        >
                          {deletingDoc === doc.path ? (
                            <Loader2 size={16} className="animate-spin" />
                          ) : (
                            <Trash2 size={16} />
                          )}
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}

        {/* Advanced Tab */}
        {activeTab === 'advanced' && isAdmin && (
          <div className="bg-white rounded-lg border shadow-sm">
            <div className="p-6">
              <h3 className="text-lg font-medium mb-4">Advanced Settings</h3>

              {/* Danger Zone */}
              <div className="border-2 border-red-300 rounded-lg">
                <div className="bg-red-50 px-6 py-4 border-b border-red-300 rounded-t-lg">
                  <div className="flex items-center gap-2">
                    <AlertTriangle size={20} className="text-red-600" />
                    <h4 className="text-lg font-medium text-red-600">Danger Zone</h4>
                  </div>
                </div>

                <div className="p-6 space-y-4">
                  <div>
                    <h5 className="font-medium text-gray-900 mb-1">Custom System Prompt</h5>
                    <p className="text-sm text-gray-500">
                      Override the default AI system prompt for this workspace. This changes how the AI reasons about your content. Most teams do not need to change this.
                    </p>
                  </div>

                  {loadingSystemPrompt ? (
                    <div className="py-8 flex items-center justify-center">
                      <Loader2 className="animate-spin text-gray-400" size={24} />
                    </div>
                  ) : (
                    <>
                      <textarea
                        value={systemPrompt}
                        onChange={(e) => {
                          if (e.target.value.length <= 10000) {
                            setSystemPrompt(e.target.value)
                          }
                        }}
                        rows={10}
                        placeholder={`No custom system prompt configured. The default prompt is active.\n\nExample use cases:\n- Security teams: "Code samples in context are detection signatures, not malicious intent."\n- Sales teams: "Help draft outbound emails and LinkedIn posts. Use a professional tone."\n- Platform teams: "Always recommend our standard Terraform modules."`}
                        className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 resize-vertical font-mono text-sm"
                      />
                      <div className="flex items-center justify-between">
                        <p className="text-sm text-gray-400">
                          {systemPrompt.length} / 10,000
                        </p>
                        {systemPromptData?.updated_by && systemPromptData?.updated_at && systemPromptData?.custom_system_prompt && (
                          <p className="text-sm text-gray-400">
                            Last updated by {systemPromptData.updated_by} on{' '}
                            {new Date(systemPromptData.updated_at).toLocaleDateString()}
                          </p>
                        )}
                      </div>

                      <div className="flex items-center gap-3 pt-2">
                        <button
                          onClick={handleSaveSystemPrompt}
                          disabled={savingSystemPrompt || systemPrompt === savedSystemPrompt}
                          className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                        >
                          {savingSystemPrompt && <Loader2 size={16} className="animate-spin" />}
                          Save Changes
                        </button>
                        <button
                          onClick={() => setShowResetPromptConfirm(true)}
                          disabled={resettingSystemPrompt || !savedSystemPrompt}
                          className="px-4 py-2 border border-red-300 text-red-600 rounded-lg hover:bg-red-50 disabled:opacity-50 transition-colors flex items-center gap-2"
                        >
                          {resettingSystemPrompt && <Loader2 size={16} className="animate-spin" />}
                          <RotateCcw size={16} />
                          Reset to Default
                        </button>
                      </div>
                    </>
                  )}
                </div>
              </div>
            </div>
          </div>
        )}
      </main>

      {/* Reset System Prompt Confirmation Modal */}
      {showResetPromptConfirm && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-md mx-4 p-6">
            <h2 className="text-lg font-semibold text-red-600 mb-2">Reset System Prompt?</h2>
            <p className="text-gray-600 mb-6">
              Are you sure? This will remove the custom prompt and revert to the default system prompt.
            </p>
            <div className="flex justify-end gap-3">
              <button
                onClick={() => setShowResetPromptConfirm(false)}
                className="px-4 py-2 text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleResetSystemPrompt}
                disabled={resettingSystemPrompt}
                className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 transition-colors flex items-center gap-2"
              >
                {resettingSystemPrompt && <Loader2 size={16} className="animate-spin" />}
                Reset
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Delete Confirmation Modal */}
      {showDeleteConfirm && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-md mx-4 p-6">
            <h2 className="text-lg font-semibold text-red-600 mb-2">Delete Workspace?</h2>
            <p className="text-gray-600 mb-6">
              This will permanently delete "{workspace.name}" and all associated data. This action cannot be undone.
            </p>
            <div className="flex justify-end gap-3">
              <button
                onClick={() => setShowDeleteConfirm(false)}
                className="px-4 py-2 text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleDelete}
                disabled={deleting}
                className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 transition-colors flex items-center gap-2"
              >
                {deleting && <Loader2 size={16} className="animate-spin" />}
                Delete
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Add Member Modal */}
      {showAddMember && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-md mx-4">
            <div className="p-6">
              <h2 className="text-lg font-semibold mb-4">Add Member</h2>
              <div className="space-y-4">
                <div className="relative">
                  <label htmlFor="member-search" className="block text-sm font-medium text-gray-700 mb-1">
                    Search users
                  </label>
                  <div className="relative">
                    <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
                    <input
                      id="member-search"
                      type="text"
                      value={newMemberEmail || userSearchQuery}
                      onChange={(e) => {
                        if (newMemberEmail) {
                          setNewMemberEmail('')
                        }
                        handleUserSearch(e.target.value)
                      }}
                      placeholder="Search by email..."
                      className="w-full pl-9 pr-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                      autoFocus
                    />
                    {searchingUsers && <Loader2 size={16} className="absolute right-3 top-1/2 -translate-y-1/2 animate-spin text-gray-400" />}
                  </div>
                  {userSearchResults.length > 0 && !newMemberEmail && (
                    <div className="absolute z-10 w-full mt-1 bg-white border rounded-lg shadow-lg max-h-48 overflow-y-auto">
                      {userSearchResults.map(user => (
                        <button
                          key={user.user_email}
                          onClick={() => {
                            setNewMemberEmail(user.user_email)
                            setUserSearchQuery('')
                            setUserSearchResults([])
                          }}
                          className="w-full px-3 py-2 text-left hover:bg-gray-50 flex items-center gap-2 text-sm"
                        >
                          <User size={14} className="text-gray-400 shrink-0" />
                          <div className="min-w-0">
                            <div className="truncate">{user.user_email}</div>
                            {user.name && <div className="text-xs text-gray-500 truncate">{user.name}{user.is_service_account ? ' (service account)' : ''}</div>}
                          </div>
                        </button>
                      ))}
                    </div>
                  )}
                  {userSearchQuery.length >= 2 && userSearchResults.length === 0 && !searchingUsers && (
                    <p className="mt-1 text-xs text-gray-500">No matching users found. You can still type a full email to invite.</p>
                  )}
                </div>
                {!newMemberEmail && userSearchQuery.length >= 2 && (
                  <div>
                    <label htmlFor="member-email-manual" className="block text-sm font-medium text-gray-700 mb-1">
                      Or enter email directly
                    </label>
                    <input
                      id="member-email-manual"
                      type="email"
                      value=""
                      onChange={(e) => {
                        setNewMemberEmail(e.target.value)
                        setUserSearchQuery('')
                        setUserSearchResults([])
                      }}
                      placeholder="user@example.com"
                      className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                  </div>
                )}
                {newMemberEmail && (
                  <div className="flex items-center gap-2 px-3 py-2 bg-blue-50 border border-blue-200 rounded-lg text-sm">
                    <User size={14} className="text-blue-600 shrink-0" />
                    <span className="truncate">{newMemberEmail}</span>
                    <button onClick={() => { setNewMemberEmail(''); setUserSearchQuery('') }} className="ml-auto text-blue-600 hover:text-blue-800">
                      <X size={14} />
                    </button>
                  </div>
                )}
                <div>
                  <label htmlFor="member-role" className="block text-sm font-medium text-gray-700 mb-1">
                    Role
                  </label>
                  <select
                    id="member-role"
                    value={newMemberRole}
                    onChange={(e) => setNewMemberRole(e.target.value as 'member' | 'workspace_admin')}
                    className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                  >
                    <option value="member">Member</option>
                    <option value="workspace_admin">Admin</option>
                  </select>
                </div>
              </div>
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex justify-end gap-3">
              <button
                onClick={() => {
                  setShowAddMember(false)
                  setNewMemberEmail('')
                  setNewMemberRole('member')
                  setUserSearchQuery('')
                  setUserSearchResults([])
                }}
                className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleAddMember}
                disabled={!newMemberEmail.trim() || addingMember}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
              >
                {addingMember && <Loader2 size={16} className="animate-spin" />}
                Add
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Space Configuration Modal */}
      {configuringConnection && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-lg mx-4 max-h-[80vh] flex flex-col">
            <div className="p-6 border-b">
              <h2 className="text-lg font-semibold">Configure Confluence Spaces</h2>
              <p className="text-sm text-gray-500 mt-1">
                Select which spaces to sync from {configuringConnection.site_name}
              </p>
            </div>
            <div className="flex-1 overflow-y-auto p-6">
              {loadingSpaces ? (
                <div className="py-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : (
                <div className="space-y-4">
                  <div className="space-y-2">
                    {availableSpaces.map(space => (
                      <label
                        key={space.id}
                        className="flex items-center gap-3 p-3 rounded-lg border cursor-pointer hover:bg-gray-50 transition-colors"
                      >
                        <input
                          type="checkbox"
                          checked={selectedSpaces.has(space.id)}
                          onChange={() => toggleSpace(space.id)}
                          className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
                        />
                        <div className="flex-1">
                          <p className="font-medium text-gray-900">{space.name}</p>
                          <p className="text-sm text-gray-500">{space.key} - {space.type}</p>
                        </div>
                      </label>
                    ))}
                  </div>
                  {availableSpaces.length === 0 && (
                    <p className="text-center text-gray-500 py-4">No spaces found</p>
                  )}
                  <div className="border-t pt-4 mt-4">
                    <label className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={includeAttachments}
                        onChange={(e) => setIncludeAttachments(e.target.checked)}
                        className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
                      />
                      <div>
                        <p className="font-medium text-gray-900">Include attachments</p>
                        <p className="text-sm text-gray-500">Sync PDFs, images, and other files</p>
                      </div>
                    </label>
                  </div>
                </div>
              )}
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex items-center justify-between border-t">
              <p className="text-sm text-gray-500">
                {selectedSpaces.size} space{selectedSpaces.size !== 1 ? 's' : ''} selected
              </p>
              <div className="flex gap-3">
                <button
                  onClick={() => setConfiguringConnection(null)}
                  className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleSaveSpaces}
                  disabled={savingSpaces || selectedSpaces.size === 0}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {savingSpaces && <Loader2 size={16} className="animate-spin" />}
                  Save & Sync
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Jira Project Configuration Modal */}
      {configuringJiraConnection && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-lg mx-4 max-h-[80vh] flex flex-col">
            <div className="p-6 border-b">
              <h2 className="text-lg font-semibold">Configure Jira Projects</h2>
              <p className="text-sm text-gray-500 mt-1">
                Select which projects to sync from {configuringJiraConnection.site_name}
              </p>
            </div>
            <div className="flex-1 overflow-y-auto p-6">
              {loadingProjects ? (
                <div className="py-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : (
                <div className="space-y-4">
                  <div className="space-y-2">
                    {availableProjects.map(project => (
                      <label
                        key={project.id}
                        className="flex items-center gap-3 p-3 rounded-lg border cursor-pointer hover:bg-gray-50 transition-colors"
                      >
                        <input
                          type="checkbox"
                          checked={selectedProjects.has(project.key)}
                          onChange={() => toggleProject(project.key)}
                          className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
                        />
                        <div className="flex-1">
                          <p className="font-medium text-gray-900">{project.name}</p>
                          <p className="text-sm text-gray-500">{project.key} - {project.projectTypeKey || 'software'}</p>
                        </div>
                      </label>
                    ))}
                  </div>
                  {availableProjects.length === 0 && (
                    <p className="text-center text-gray-500 py-4">No projects found</p>
                  )}
                  <div className="border-t pt-4 mt-4 space-y-3">
                    <label className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={jiraIncludeComments}
                        onChange={(e) => setJiraIncludeComments(e.target.checked)}
                        className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
                      />
                      <div>
                        <p className="font-medium text-gray-900">Include comments</p>
                        <p className="text-sm text-gray-500">Sync issue comments for better context</p>
                      </div>
                    </label>
                    <label className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={jiraIncludeAttachments}
                        onChange={(e) => setJiraIncludeAttachments(e.target.checked)}
                        className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
                      />
                      <div>
                        <p className="font-medium text-gray-900">Include attachments</p>
                        <p className="text-sm text-gray-500">Sync PDFs, images, and other files</p>
                      </div>
                    </label>
                  </div>
                </div>
              )}
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex items-center justify-between border-t">
              <p className="text-sm text-gray-500">
                {selectedProjects.size} project{selectedProjects.size !== 1 ? 's' : ''} selected
              </p>
              <div className="flex gap-3">
                <button
                  onClick={() => setConfiguringJiraConnection(null)}
                  className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleSaveProjects}
                  disabled={savingProjects || selectedProjects.size === 0}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {savingProjects && <Loader2 size={16} className="animate-spin" />}
                  Save & Sync
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Outlook Folder Configuration Modal */}
      {configuringOutlookConnection && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-lg mx-4 max-h-[80vh] flex flex-col">
            <div className="p-6 border-b">
              <h2 className="text-lg font-semibold">Configure Mail Folders</h2>
              <p className="text-sm text-gray-500 mt-1">
                Select which folders to sync from {configuringOutlookConnection.user_email}
              </p>
            </div>
            <div className="flex-1 overflow-y-auto p-6">
              {loadingFolders ? (
                <div className="py-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : (
                <div className="space-y-4">
                  <div className="space-y-2">
                    {availableFolders.map(folder => (
                      <label
                        key={folder.id}
                        className="flex items-center gap-3 p-3 rounded-lg border cursor-pointer hover:bg-gray-50 transition-colors"
                      >
                        <input
                          type="checkbox"
                          checked={selectedFolders.has(folder.id)}
                          onChange={() => toggleFolder(folder.id)}
                          className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
                        />
                        <div className="flex-1">
                          <p className="font-medium text-gray-900">{folder.displayName}</p>
                          <p className="text-sm text-gray-500">
                            {folder.totalItemCount} item{folder.totalItemCount !== 1 ? 's' : ''}
                            {(folder.unreadItemCount ?? 0) > 0 && (
                              <> ({folder.unreadItemCount} unread)</>
                            )}
                          </p>
                        </div>
                      </label>
                    ))}
                  </div>
                  {availableFolders.length === 0 && (
                    <p className="text-center text-gray-500 py-4">No mail folders found</p>
                  )}
                </div>
              )}
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex items-center justify-between border-t">
              <p className="text-sm text-gray-500">
                {selectedFolders.size} folder{selectedFolders.size !== 1 ? 's' : ''} selected
              </p>
              <div className="flex gap-3">
                <button
                  onClick={() => setConfiguringOutlookConnection(null)}
                  className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleSaveFolders}
                  disabled={savingFolders || selectedFolders.size === 0}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {savingFolders && <Loader2 size={16} className="animate-spin" />}
                  Save & Sync
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Teams Channel Configuration Modal */}
      {configuringTeamsConnection && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-lg mx-4 max-h-[80vh] flex flex-col">
            <div className="p-6 border-b">
              <h2 className="text-lg font-semibold">Configure Teams Channels</h2>
              <p className="text-sm text-gray-500 mt-1">
                Select teams and channels to sync from {configuringTeamsConnection.user_email}
              </p>
            </div>
            <div className="flex-1 overflow-y-auto p-6">
              {loadingTeams ? (
                <div className="py-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : (
                <div className="space-y-4">
                  {/* Team selector */}
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Select a Team</label>
                    <select
                      value={selectedTeamId}
                      onChange={(e) => handleTeamSelect(e.target.value)}
                      className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-purple-500"
                    >
                      <option value="">Choose a team...</option>
                      {availableTeams.map(team => (
                        <option key={team.id} value={team.id}>{team.displayName}</option>
                      ))}
                    </select>
                  </div>

                  {/* Channel list for selected team */}
                  {selectedTeamId && (
                    <div>
                      <label className="block text-sm font-medium text-gray-700 mb-2">Channels</label>
                      {loadingChannels ? (
                        <div className="py-4 flex items-center justify-center">
                          <Loader2 className="animate-spin text-gray-400" size={20} />
                        </div>
                      ) : availableChannels.length === 0 ? (
                        <p className="text-sm text-gray-500">No channels found</p>
                      ) : (
                        <div className="space-y-2 max-h-48 overflow-y-auto">
                          {availableChannels.map(channel => {
                            const team = availableTeams.find(t => t.id === selectedTeamId)
                            const isSelected = selectedChannels.some(
                              c => c.team_id === selectedTeamId && c.channel_id === channel.id
                            )
                            return (
                              <label
                                key={channel.id}
                                className="flex items-center gap-3 p-3 rounded-lg border cursor-pointer hover:bg-gray-50 transition-colors"
                              >
                                <input
                                  type="checkbox"
                                  checked={isSelected}
                                  onChange={() => toggleTeamsChannel(
                                    selectedTeamId,
                                    team?.displayName || '',
                                    channel.id,
                                    channel.displayName
                                  )}
                                  className="w-4 h-4 text-purple-600 rounded focus:ring-purple-500"
                                />
                                <div className="flex-1">
                                  <p className="font-medium text-gray-900">{channel.displayName}</p>
                                  {channel.description && (
                                    <p className="text-sm text-gray-500">{channel.description}</p>
                                  )}
                                </div>
                              </label>
                            )
                          })}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Selected channels summary */}
                  {selectedChannels.length > 0 && (
                    <div>
                      <label className="block text-sm font-medium text-gray-700 mb-2">
                        Selected Channels ({selectedChannels.length})
                      </label>
                      <div className="space-y-1">
                        {selectedChannels.map(ch => (
                          <div key={`${ch.team_id}-${ch.channel_id}`} className="flex items-center justify-between p-2 bg-purple-50 rounded text-sm">
                            <span>{ch.team_name} / {ch.channel_name}</span>
                            <button
                              onClick={() => toggleTeamsChannel(ch.team_id, ch.team_name, ch.channel_id, ch.channel_name)}
                              className="text-red-500 hover:text-red-700"
                            >
                              <X size={14} />
                            </button>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Options */}
                  <div className="space-y-3 pt-2 border-t">
                    <div>
                      <label className="block text-sm font-medium text-gray-700 mb-1">
                        Days of history to sync
                      </label>
                      <input
                        type="number"
                        value={teamsDaysBack}
                        onChange={(e) => setTeamsDaysBack(Math.max(1, parseInt(e.target.value) || 30))}
                        min={1}
                        max={365}
                        className="w-24 px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-purple-500"
                      />
                    </div>
                    <label className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={teamsSyncFiles}
                        onChange={(e) => setTeamsSyncFiles(e.target.checked)}
                        className="w-4 h-4 text-purple-600 rounded focus:ring-purple-500"
                      />
                      <span className="text-sm text-gray-700">Sync shared files from channels</span>
                    </label>
                  </div>
                </div>
              )}
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex items-center justify-between border-t">
              <p className="text-sm text-gray-500">
                {selectedChannels.length} channel{selectedChannels.length !== 1 ? 's' : ''} selected
              </p>
              <div className="flex gap-3">
                <button
                  onClick={() => setConfiguringTeamsConnection(null)}
                  className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleSaveTeamsConfig}
                  disabled={savingTeamsConfig || selectedChannels.length === 0}
                  className="px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {savingTeamsConfig && <Loader2 size={16} className="animate-spin" />}
                  Save & Sync
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* SharePoint Site Configuration Modal */}
      {configuringSharePointConnection && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-lg mx-4 max-h-[80vh] flex flex-col">
            <div className="p-6 border-b">
              <h2 className="text-lg font-semibold">Configure SharePoint Sites</h2>
              <p className="text-sm text-gray-500 mt-1">
                Select which sites to sync from {configuringSharePointConnection.user_email}
              </p>
            </div>
            <div className="flex-1 overflow-y-auto p-6">
              {loadingSPSites ? (
                <div className="py-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : (
                <div className="space-y-4">
                  <div className="space-y-2">
                    {availableSPSites.map(site => (
                      <label
                        key={site.id}
                        className="flex items-center gap-3 p-3 rounded-lg border cursor-pointer hover:bg-gray-50 transition-colors"
                      >
                        <input
                          type="checkbox"
                          checked={selectedSPSites.has(site.id)}
                          onChange={() => toggleSPSite(site.id)}
                          className="w-4 h-4 text-teal-600 rounded focus:ring-teal-500"
                        />
                        <div className="flex-1">
                          <p className="font-medium text-gray-900">{site.friendly_name || site.displayName}</p>
                          {site.webUrl && (
                            <p className="text-sm text-gray-500 truncate">{site.webUrl}</p>
                          )}
                        </div>
                      </label>
                    ))}
                  </div>
                  {availableSPSites.length === 0 && (
                    <p className="text-center text-gray-500 py-4">No SharePoint sites found</p>
                  )}
                  <div className="pt-2 border-t">
                    <label className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={spIncludePages}
                        onChange={(e) => setSpIncludePages(e.target.checked)}
                        className="w-4 h-4 text-teal-600 rounded focus:ring-teal-500"
                      />
                      <span className="text-sm text-gray-700">Include SharePoint pages (in addition to document library files)</span>
                    </label>
                  </div>
                </div>
              )}
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex items-center justify-between border-t">
              <p className="text-sm text-gray-500">
                {selectedSPSites.size} site{selectedSPSites.size !== 1 ? 's' : ''} selected
              </p>
              <div className="flex gap-3">
                <button
                  onClick={() => setConfiguringSharePointConnection(null)}
                  className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleSaveSPSites}
                  disabled={savingSPSites || selectedSPSites.size === 0}
                  className="px-4 py-2 bg-teal-600 text-white rounded-lg hover:bg-teal-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {savingSPSites && <Loader2 size={16} className="animate-spin" />}
                  Save & Sync
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* OneDrive Folder Configuration Modal */}
      {configuringOneDriveConnection && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-lg mx-4 max-h-[80vh] flex flex-col">
            <div className="p-6 border-b">
              <h2 className="text-lg font-semibold">Configure OneDrive Folders</h2>
              <p className="text-sm text-gray-500 mt-1">
                Select which folders to sync from {configuringOneDriveConnection.user_email}
              </p>
            </div>
            <div className="flex-1 overflow-y-auto p-6">
              {loadingODFolders ? (
                <div className="py-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : (
                <div className="space-y-2">
                  {availableODFolders.map(folder => (
                    <label
                      key={folder.folder_id}
                      className="flex items-center gap-3 p-3 rounded-lg border cursor-pointer hover:bg-gray-50 transition-colors"
                    >
                      <input
                        type="checkbox"
                        checked={selectedODFolders.has(folder.folder_id)}
                        onChange={() => toggleODFolder(folder.folder_id)}
                        className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
                      />
                      <div className="flex-1">
                        <p className="font-medium text-gray-900">{folder.folder_name}</p>
                        {folder.folder_path && folder.folder_path !== '/' && (
                          <p className="text-sm text-gray-500 truncate">{folder.folder_path}</p>
                        )}
                      </div>
                    </label>
                  ))}
                  {availableODFolders.length === 0 && (
                    <p className="text-center text-gray-500 py-4">No OneDrive folders found</p>
                  )}
                </div>
              )}
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex items-center justify-between border-t">
              <p className="text-sm text-gray-500">
                {selectedODFolders.size} folder{selectedODFolders.size !== 1 ? 's' : ''} selected
              </p>
              <div className="flex gap-3">
                <button
                  onClick={() => setConfiguringOneDriveConnection(null)}
                  className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleSaveODFolders}
                  disabled={savingODFolders || selectedODFolders.size === 0}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {savingODFolders && <Loader2 size={16} className="animate-spin" />}
                  Save & Sync
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* OneNote Notebook Configuration Modal */}
      {configuringOneNoteConnection && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-lg mx-4 max-h-[80vh] flex flex-col">
            <div className="p-6 border-b">
              <h2 className="text-lg font-semibold">Configure OneNote Notebooks</h2>
              <p className="text-sm text-gray-500 mt-1">
                Select which notebooks to sync from {configuringOneNoteConnection.user_email}
              </p>
            </div>
            <div className="flex-1 overflow-y-auto p-6">
              {loadingONNotebooks ? (
                <div className="py-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : (
                <div className="space-y-2">
                  {availableONNotebooks.map(nb => {
                    const friendlyLabel = nb.is_default
                      ? 'Personal Notebook'
                      : nb.is_shared
                      ? 'Shared Notebook'
                      : nb.notebook_name
                    const showRawName = friendlyLabel !== nb.notebook_name
                    return (
                      <label
                        key={nb.notebook_id}
                        className="flex items-center gap-3 p-3 rounded-lg border cursor-pointer hover:bg-gray-50 transition-colors"
                      >
                        <input
                          type="checkbox"
                          checked={selectedONNotebooks.has(nb.notebook_id)}
                          onChange={() => toggleONNotebook(nb.notebook_id)}
                          className="w-4 h-4 text-purple-600 rounded focus:ring-purple-500"
                        />
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <p className="font-medium text-gray-900 truncate">{friendlyLabel}</p>
                            {nb.is_default && (
                              <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-purple-100 text-purple-700">
                                Default
                              </span>
                            )}
                            {nb.is_shared && (
                              <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-blue-100 text-blue-700">
                                Shared
                              </span>
                            )}
                          </div>
                          {showRawName && (
                            <p className="text-xs text-gray-500 mt-0.5 truncate">{nb.notebook_name}</p>
                          )}
                        </div>
                      </label>
                    )
                  })}
                  {availableONNotebooks.length === 0 && (
                    <p className="text-center text-gray-500 py-4">No OneNote notebooks found</p>
                  )}
                </div>
              )}
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex items-center justify-between border-t">
              <p className="text-sm text-gray-500">
                {selectedONNotebooks.size} notebook{selectedONNotebooks.size !== 1 ? 's' : ''} selected
              </p>
              <div className="flex gap-3">
                <button
                  onClick={() => setConfiguringOneNoteConnection(null)}
                  className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleSaveONNotebooks}
                  disabled={savingONNotebooks || selectedONNotebooks.size === 0}
                  className="px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {savingONNotebooks && <Loader2 size={16} className="animate-spin" />}
                  Save & Sync
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Add GitHub Repo Modal */}
      {showAddGitHubRepo && selectedInstallation && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-lg mx-4 max-h-[80vh] flex flex-col">
            <div className="p-6 border-b">
              <h2 className="text-lg font-semibold">Add Repositories from {selectedInstallation.account_login}</h2>
              <p className="text-sm text-gray-500 mt-1">
                Select repositories to sync for code maps
              </p>
            </div>
            <div className="p-4 border-b space-y-3">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Repository Type</label>
                <select
                  value={selectedRepoType}
                  onChange={(e) => setSelectedRepoType(e.target.value as GitHubRepoType)}
                  className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  <option value="application">Application (code, Terraform, serverless)</option>
                  <option value="standards">Standards (ADRs, docs, YAML, JSON, XML)</option>
                </select>
              </div>
              <div className="relative">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" size={18} />
                <input
                  type="text"
                  value={githubRepoSearchQuery}
                  onChange={(e) => setGithubRepoSearchQuery(e.target.value)}
                  placeholder="Search repositories..."
                  className="w-full pl-10 pr-4 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
            </div>
            <div className="flex-1 overflow-y-auto">
              {loadingInstallationRepos ? (
                <div className="py-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : filteredInstallationRepos.length === 0 ? (
                <div className="py-8 text-center text-gray-500">
                  {installationRepos.length === 0
                    ? 'No repositories available or all have been added'
                    : 'No repositories match your search'}
                </div>
              ) : (
                <ul className="divide-y">
                  {filteredInstallationRepos.map(repo => (
                    <li key={repo.full_name} className="p-4 flex items-center justify-between hover:bg-gray-50">
                      <div className="flex items-center gap-3 min-w-0 flex-1">
                        <Github className="text-gray-400 flex-shrink-0" size={20} />
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <p className="font-medium text-gray-900 truncate">{repo.full_name}</p>
                            {repo.private ? (
                              <span title="Private"><Lock className="text-amber-500 flex-shrink-0" size={14} /></span>
                            ) : (
                              <span title="Public"><Globe className="text-green-500 flex-shrink-0" size={14} /></span>
                            )}
                          </div>
                          {repo.description && (
                            <p className="text-sm text-gray-500 truncate">{repo.description}</p>
                          )}
                          <p className="text-xs text-gray-400 mt-0.5">
                            Default branch: {repo.default_branch}
                          </p>
                        </div>
                      </div>
                      <button
                        onClick={() => handleAddGitHubRepo(repo.full_name)}
                        disabled={addingGitHubRepo === repo.full_name}
                        className="ml-4 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-1"
                      >
                        {addingGitHubRepo === repo.full_name ? (
                          <Loader2 size={14} className="animate-spin" />
                        ) : (
                          <Plus size={14} />
                        )}
                        Add
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex justify-end border-t">
              <button
                onClick={() => {
                  setShowAddGitHubRepo(false)
                  setSelectedInstallation(null)
                  setGithubRepoSearchQuery('')
                }}
                className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors"
              >
                Done
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
