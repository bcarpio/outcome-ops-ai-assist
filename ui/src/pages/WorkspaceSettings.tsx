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
} from '../lib/api'

type Tab = 'general' | 'members' | 'integrations' | 'sharing' | 'documents'

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
      const [confluenceData, jiraData, outlookData] = await Promise.all([
        listConfluenceConnections(workspaceId),
        listJiraConnections(workspaceId),
        listOutlookConnections(workspaceId).catch(() => []),
      ])
      setConfluenceConnections(confluenceData)
      setJiraConnections(jiraData)
      setOutlookConnections(outlookData)
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
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add member')
    } finally {
      setAddingMember(false)
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
                {members.map(member => (
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
                        >
                          <option value="member">Member</option>
                          <option value="workspace_admin">Admin</option>
                        </select>
                        <button
                          onClick={() => handleRemoveMember(member.user_email)}
                          className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                          title="Remove member"
                        >
                          <UserMinus size={18} />
                        </button>
                      </div>
                    )}
                  </li>
                ))}
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
      </main>

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
                <div>
                  <label htmlFor="member-email" className="block text-sm font-medium text-gray-700 mb-1">
                    Email
                  </label>
                  <input
                    id="member-email"
                    type="email"
                    value={newMemberEmail}
                    onChange={(e) => setNewMemberEmail(e.target.value)}
                    placeholder="user@example.com"
                    className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                    autoFocus
                  />
                </div>
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
