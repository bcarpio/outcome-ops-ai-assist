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
  GitBranch,
  Search,
  Lock,
  Globe,
  AlertTriangle,
  RotateCcw,
  ShieldAlert,
} from 'lucide-react'
import {
  getWorkspace,
  getWorkspacePublic,
  type WorkspacePublicView,
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
  listDatabaseConnections,
  createDatabaseConnection,
  validateDatabaseConnection,
  listDatabaseSchemas,
  updateDatabaseScope,
  triggerDatabaseSync,
  deleteDatabaseConnection,
  getGitHubInstallUrl,
  listGitHubInstallations,
  listGitHubInstallationRepos,
  listGitHubRepos,
  addGitHubRepos,
  updateGitHubRepo,
  listGitHubRepoBranches,
  removeGitHubRepo,
  triggerGitHubSync,
  getAzureDevOpsAuthUrl,
  listAzureDevOpsConnections,
  listAzureDevOpsOrgs,
  listAzureDevOpsProjects,
  listAzureDevOpsProjectRepos,
  listAzureDevOpsRepos,
  addAzureDevOpsRepos,
  triggerAzureDevOpsSync,
  disconnectAzureDevOps,
  removeAzureDevOpsRepo,
  updateAzureDevOpsRepo,
  listAzureDevOpsRepoBranches,
  type RepoBranchesResponse,
  type AzureDevOpsConnection,
  type AzureDevOpsOrg,
  type AzureDevOpsProject,
  type AzureDevOpsAvailableRepo,
  type AzureDevOpsRepo,
  type AzureDevOpsRepoType,
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
  type DatabaseConnection,
  type DatabaseEngine,
  type DatabaseSchemaInfo,
  type DatabaseScopeConfig,
  type DatabaseCredentialPayload,
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
import MutationButton from '../components/MutationButton'

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
  // Non-member view of a Global workspace. Set when getWorkspace()
  // fails (403/404) and the fallback getWorkspacePublic() succeeds.
  // Renders a friendly "This is a Global workspace, contact these
  // admins" panel instead of a bare "Workspace not found."
  const [globalPublicView, setGlobalPublicView] = useState<WorkspacePublicView | null>(null)

  // Auto-clear success banners after 3 seconds. Owned by a useEffect
  // with a cleanup so when the component unmounts (or success changes
  // again) the pending timer is cancelled. Replaces the previous
  // inline `setTimeout(() => setSuccess(null), 3000)` pattern, which
  // leaked stray timers past unmount — Vitest would catch the
  // post-teardown setSuccess(null) call as an unhandled `window is
  // not defined` error and fail CI even when every test passed.
  useEffect(() => {
    if (!success) return
    const timer = setTimeout(() => setSuccess(null), 3000)
    return () => clearTimeout(timer)
  }, [success])

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

  // Database integration state
  const [databaseConnections, setDatabaseConnections] = useState<DatabaseConnection[]>([])
  const [syncingDatabase, setSyncingDatabase] = useState<string | null>(null)
  // Connect-new modal state
  const [showDatabaseConnectModal, setShowDatabaseConnectModal] = useState(false)
  const [dbConnectEngine, setDbConnectEngine] = useState<DatabaseEngine>('postgres')
  const [dbConnectHost, setDbConnectHost] = useState('')
  const [dbConnectPort, setDbConnectPort] = useState<number>(5432)
  const [dbConnectDatabaseName, setDbConnectDatabaseName] = useState('')
  const [dbConnectDisplayLabel, setDbConnectDisplayLabel] = useState('')
  const [dbConnectUsername, setDbConnectUsername] = useState('')
  const [dbConnectPassword, setDbConnectPassword] = useState('')
  const [dbConnectSSLMode, setDbConnectSSLMode] = useState<string>('require')
  const [dbConnectMSSQLEncrypt, setDbConnectMSSQLEncrypt] = useState<boolean>(true)
  const [dbValidating, setDbValidating] = useState(false)
  const [dbValidationResult, setDbValidationResult] = useState<{ valid: boolean; error?: string | null; server_version?: string | null } | null>(null)
  const [dbCreating, setDbCreating] = useState(false)
  // Scope picker modal state
  const [configuringDatabaseConnection, setConfiguringDatabaseConnection] = useState<DatabaseConnection | null>(null)
  const [availableDatabaseSchemas, setAvailableDatabaseSchemas] = useState<DatabaseSchemaInfo[]>([])
  const [loadingDatabaseSchemas, setLoadingDatabaseSchemas] = useState(false)
  // Per-schema introspection + sampling toggles (chunk 06). Keys are
  // "catalog.schema". Tables inside selected schemas are auto-
  // discovered on every sync; the admin doesn't pick individual tables.
  const [databaseIntrospectSchemas, setDatabaseIntrospectSchemas] = useState<Set<string>>(new Set())
  const [databaseSamplingSchemas, setDatabaseSamplingSchemas] = useState<Set<string>>(new Set())
  const [savingDatabaseScope, setSavingDatabaseScope] = useState(false)

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
  // Per-row branch override for the GitHub add-repo picker. Empty
  // entry (or missing key) means "use the repo's detected default
  // branch"; a value means the customer picked a non-default (e.g.
  // ``develop``) in the confirmable input. Keyed by full_name so
  // multiple rows can hold their own edits before the user Adds.
  const [githubBranchOverrides, setGithubBranchOverrides] = useState<Record<string, string>>({})
  // Edit-branch modal state (per-row gear icon). One modal at a time
  // for either provider -- the modal keys off the provider so a
  // single set of state carries both. When ``editBranchProvider ==
  // "github"``, ``editBranchTarget`` is the repo full_name; when
  // ``"azure_devops"``, it's the org/project/repo slug.
  const [editBranchProvider, setEditBranchProvider] = useState<'github' | 'azure_devops' | null>(null)
  const [editBranchTarget, setEditBranchTarget] = useState<string | null>(null)
  const [editBranchLoading, setEditBranchLoading] = useState(false)
  const [editBranchData, setEditBranchData] = useState<RepoBranchesResponse | null>(null)
  const [editBranchError, setEditBranchError] = useState<string | null>(null)
  const [editBranchSelected, setEditBranchSelected] = useState<string>('')
  const [editBranchSaving, setEditBranchSaving] = useState(false)

  // Azure DevOps integration state (chunk 08)
  const [azureDevOpsConnections, setAzureDevOpsConnections] = useState<AzureDevOpsConnection[]>([])
  const [azureDevOpsRepos, setAzureDevOpsRepos] = useState<AzureDevOpsRepo[]>([])
  const [loadingAzureDevOps, setLoadingAzureDevOps] = useState(false)
  const [connectingAzureDevOps, setConnectingAzureDevOps] = useState(false)
  const [disconnectingAzureDevOps, setDisconnectingAzureDevOps] = useState<string | null>(null)
  const [syncingAzureDevOps, setSyncingAzureDevOps] = useState<string | null>(null)
  const [removingAzureDevOpsRepo, setRemovingAzureDevOpsRepo] = useState<string | null>(null)
  // Repo picker modal
  const [showAddAzureDevOpsRepo, setShowAddAzureDevOpsRepo] = useState(false)
  const [pickerConnectionId, setPickerConnectionId] = useState<string | null>(null)
  const [azureDevOpsOrgs, setAzureDevOpsOrgs] = useState<AzureDevOpsOrg[]>([])
  const [pickerSelectedOrg, setPickerSelectedOrg] = useState<string>('')
  const [azureDevOpsProjects, setAzureDevOpsProjects] = useState<AzureDevOpsProject[]>([])
  const [pickerSelectedProject, setPickerSelectedProject] = useState<string>('')
  const [azureDevOpsAvailableRepos, setAzureDevOpsAvailableRepos] = useState<AzureDevOpsAvailableRepo[]>([])
  const [loadingAzureDevOpsOrgs, setLoadingAzureDevOpsOrgs] = useState(false)
  const [loadingAzureDevOpsProjects, setLoadingAzureDevOpsProjects] = useState(false)
  const [loadingAzureDevOpsPickerRepos, setLoadingAzureDevOpsPickerRepos] = useState(false)
  const [addingAzureDevOpsRepo, setAddingAzureDevOpsRepo] = useState<string | null>(null)
  const [selectedAzureDevOpsRepoType, setSelectedAzureDevOpsRepoType] = useState<AzureDevOpsRepoType>('application')
  // Per-row branch override for the ADO add-repo picker. Mirror of
  // the GitHub side; keyed by repo name (unique within the project
  // dropdown selection).
  const [azureDevOpsBranchOverrides, setAzureDevOpsBranchOverrides] = useState<Record<string, string>>({})

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
      loadAzureDevOpsData()
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
    setGlobalPublicView(null)
    try {
      const data = await getWorkspace(workspaceId)
      setWorkspace(data)
      setName(data.name)
      setDescription(data.description || '')
    } catch {
      // Membership-required fetch failed (403 or 404). Fall back to
      // the public view -- returns 200 only if this is a Global
      // workspace, so it's a scoped "why can't I access this" answer
      // without leaking private-workspace existence.
      try {
        const pub = await getWorkspacePublic(workspaceId)
        setGlobalPublicView(pub)
      } catch {
        // Both fetches failed -- workspace really doesn't exist or
        // isn't global. Fall through to the "Workspace not found"
        // render.
      }
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
      const [confluenceData, jiraData, outlookData, teamsData, sharePointData, oneDriveData, oneNoteData, databaseData] = await Promise.all([
        listConfluenceConnections(workspaceId),
        listJiraConnections(workspaceId),
        listOutlookConnections(workspaceId).catch(() => []),
        listTeamsConnections(workspaceId).catch(() => []),
        listSharePointConnections(workspaceId).catch(() => []),
        listOneDriveConnections(workspaceId).catch(() => []),
        listOneNoteConnections(workspaceId).catch(() => []),
        listDatabaseConnections(workspaceId).catch(() => []),
      ])
      setConfluenceConnections(confluenceData)
      setJiraConnections(jiraData)
      setOutlookConnections(outlookData)
      setTeamsConnections(teamsData)
      setSharePointConnections(sharePointData)
      setOneDriveConnections(oneDriveData)
      setOneNoteConnections(oneNoteData)
      setDatabaseConnections(databaseData)
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

  // Azure DevOps integration loader (chunk 08). 404 from a deploy
  // that hasn't enabled the integration is silently swallowed so the
  // tab doesn't display an error -- the section just doesn't appear.
  async function loadAzureDevOpsData() {
    if (!workspaceId) return
    setLoadingAzureDevOps(true)
    try {
      const [connections, repos] = await Promise.all([
        listAzureDevOpsConnections(workspaceId).catch(() => []),
        listAzureDevOpsRepos(workspaceId).catch(() => []),
      ])
      setAzureDevOpsConnections(connections)
      setAzureDevOpsRepos(repos)
    } catch (err) {
      console.error('Failed to load Azure DevOps data:', err)
    } finally {
      setLoadingAzureDevOps(false)
    }
  }

  async function handleConnectAzureDevOps() {
    if (!workspaceId) return
    setConnectingAzureDevOps(true)
    try {
      const authUrl = await getAzureDevOpsAuthUrl(workspaceId)
      // Redirect the browser to Microsoft's consent screen. The
      // callback writes the connection row + tokens then redirects
      // back to /workspaces/{id}/settings?tab=integrations
      // &azure_devops_connected={connection_id}.
      window.location.href = authUrl
    } catch (err) {
      console.error('Failed to start Azure DevOps connect:', err)
      setConnectingAzureDevOps(false)
    }
  }

  async function handleDisconnectAzureDevOps(connectionId: string) {
    if (!workspaceId) return
    if (!window.confirm(
      'Disconnect Azure DevOps? Every repo added through this connection will be ' +
      'removed from the workspace and its indexed content will be deleted from the ' +
      'knowledge base. This cannot be undone.'
    )) return
    setDisconnectingAzureDevOps(connectionId)
    try {
      await disconnectAzureDevOps(workspaceId, connectionId)
      await loadAzureDevOpsData()
    } catch (err) {
      console.error('Failed to disconnect Azure DevOps:', err)
    } finally {
      setDisconnectingAzureDevOps(null)
    }
  }

  async function handleSyncAzureDevOps(slug?: string) {
    if (!workspaceId) return
    setSyncingAzureDevOps(slug || 'all')
    try {
      await triggerAzureDevOpsSync(workspaceId, slug)
      await loadAzureDevOpsData()
    } catch (err) {
      console.error('Failed to trigger Azure DevOps sync:', err)
    } finally {
      setSyncingAzureDevOps(null)
    }
  }

  async function handleRemoveAzureDevOpsRepo(slug: string) {
    if (!workspaceId) return
    if (!window.confirm(`Remove ${slug} from this workspace? Indexed content will be deleted.`)) return
    setRemovingAzureDevOpsRepo(slug)
    try {
      await removeAzureDevOpsRepo(workspaceId, slug)
      await loadAzureDevOpsData()
    } catch (err) {
      console.error('Failed to remove Azure DevOps repo:', err)
    } finally {
      setRemovingAzureDevOpsRepo(null)
    }
  }

  // Repo picker (chunk 08 follow-up): cascade org -> project -> repo.
  async function openAzureDevOpsRepoPicker(connectionId: string) {
    if (!workspaceId) return
    setPickerConnectionId(connectionId)
    setShowAddAzureDevOpsRepo(true)
    setPickerSelectedOrg('')
    setPickerSelectedProject('')
    setAzureDevOpsProjects([])
    setAzureDevOpsAvailableRepos([])
    setLoadingAzureDevOpsOrgs(true)
    try {
      const orgs = await listAzureDevOpsOrgs(workspaceId, connectionId)
      setAzureDevOpsOrgs(orgs)
    } catch (err) {
      console.error('Failed to load Azure DevOps orgs:', err)
      setError(err instanceof Error ? err.message : 'Failed to load orgs')
    } finally {
      setLoadingAzureDevOpsOrgs(false)
    }
  }

  async function onSelectAzureDevOpsOrg(org: string) {
    if (!workspaceId || !pickerConnectionId) return
    setPickerSelectedOrg(org)
    setPickerSelectedProject('')
    setAzureDevOpsAvailableRepos([])
    if (!org) {
      setAzureDevOpsProjects([])
      return
    }
    setLoadingAzureDevOpsProjects(true)
    try {
      const projects = await listAzureDevOpsProjects(workspaceId, pickerConnectionId, org)
      setAzureDevOpsProjects(projects)
    } catch (err) {
      console.error('Failed to load Azure DevOps projects:', err)
      setError(err instanceof Error ? err.message : 'Failed to load projects')
    } finally {
      setLoadingAzureDevOpsProjects(false)
    }
  }

  async function onSelectAzureDevOpsProject(project: string) {
    if (!workspaceId || !pickerConnectionId || !pickerSelectedOrg) return
    setPickerSelectedProject(project)
    if (!project) {
      setAzureDevOpsAvailableRepos([])
      return
    }
    setLoadingAzureDevOpsPickerRepos(true)
    try {
      const repos = await listAzureDevOpsProjectRepos(
        workspaceId, pickerConnectionId, pickerSelectedOrg, project,
      )
      setAzureDevOpsAvailableRepos(repos)
    } catch (err) {
      console.error('Failed to load Azure DevOps repos:', err)
      setError(err instanceof Error ? err.message : 'Failed to load repos')
    } finally {
      setLoadingAzureDevOpsPickerRepos(false)
    }
  }

  async function handleAddAzureDevOpsRepo(repoName: string) {
    if (!workspaceId || !pickerConnectionId || !pickerSelectedOrg || !pickerSelectedProject) return
    setAddingAzureDevOpsRepo(repoName)
    setError(null)
    // Pull the customer's override, if any (mirror of GitHub side).
    const overrideRaw = azureDevOpsBranchOverrides[repoName]
    const branchOverride = overrideRaw && overrideRaw.trim() ? overrideRaw.trim() : undefined
    try {
      await addAzureDevOpsRepos(workspaceId, pickerConnectionId, [{
        org: pickerSelectedOrg,
        project: pickerSelectedProject,
        repo: repoName,
        include_code_maps: true,
        repo_type: selectedAzureDevOpsRepoType,
        default_branch: branchOverride,
      }])
      // Optimistically flip the already_added flag so the button disables
      // without a round-trip.
      setAzureDevOpsAvailableRepos(prev =>
        prev.map(r => r.name === repoName ? { ...r, already_added: true } : r),
      )
      // Clear the per-row override so a re-open of the picker starts
      // fresh next time (mirror of GitHub side).
      setAzureDevOpsBranchOverrides(prev => {
        const next = { ...prev }
        delete next[repoName]
        return next
      })
      await loadAzureDevOpsData()
      setSuccess(`Added ${pickerSelectedOrg}/${pickerSelectedProject}/${repoName}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add repo')
    } finally {
      setAddingAzureDevOpsRepo(null)
    }
  }

  function closeAzureDevOpsRepoPicker() {
    setShowAddAzureDevOpsRepo(false)
    setPickerConnectionId(null)
    setPickerSelectedOrg('')
    setPickerSelectedProject('')
    setAzureDevOpsOrgs([])
    setAzureDevOpsProjects([])
    setAzureDevOpsAvailableRepos([])
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
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete connection')
    }
  }

  // ============================================================================
  // Database integration handlers
  // ============================================================================

  function _defaultDbPort(engine: DatabaseEngine): number {
    if (engine === 'mysql') return 3306
    if (engine === 'mssql') return 1433
    return 5432 // postgres
  }

  function openDatabaseConnectModal() {
    setDbConnectEngine('postgres')
    setDbConnectHost('')
    setDbConnectPort(_defaultDbPort('postgres'))
    setDbConnectDatabaseName('')
    setDbConnectDisplayLabel('')
    setDbConnectUsername('')
    setDbConnectPassword('')
    setDbConnectSSLMode('require')
    setDbConnectMSSQLEncrypt(true)
    setDbValidationResult(null)
    setShowDatabaseConnectModal(true)
  }

  function _buildDbCredential(): DatabaseCredentialPayload {
    if (dbConnectEngine === 'mssql') {
      return {
        engine: 'mssql',
        username: dbConnectUsername,
        password: dbConnectPassword,
        encrypt: dbConnectMSSQLEncrypt,
      }
    }
    return {
      engine: dbConnectEngine,
      username: dbConnectUsername,
      password: dbConnectPassword,
      ssl_mode: dbConnectSSLMode || undefined,
    }
  }

  async function handleValidateDatabase() {
    setDbValidating(true)
    setDbValidationResult(null)
    setError(null)
    try {
      const result = await validateDatabaseConnection({
        engine: dbConnectEngine,
        host: dbConnectHost,
        port: dbConnectPort,
        database_name: dbConnectDatabaseName,
        credential: _buildDbCredential(),
      })
      setDbValidationResult(result)
    } catch (err) {
      setDbValidationResult({
        valid: false,
        error: err instanceof Error ? err.message : 'Validation failed',
      })
    } finally {
      setDbValidating(false)
    }
  }

  async function handleCreateDatabaseConnection() {
    if (!workspaceId) return
    setDbCreating(true)
    setError(null)
    try {
      const conn = await createDatabaseConnection({
        workspace_id: workspaceId,
        engine: dbConnectEngine,
        host: dbConnectHost,
        port: dbConnectPort,
        database_name: dbConnectDatabaseName,
        display_label: dbConnectDisplayLabel || undefined,
        credential: _buildDbCredential(),
      })
      setDatabaseConnections(prev => [...prev, conn])
      setShowDatabaseConnectModal(false)
      setSuccess('Database connection created. Configure scope to start syncing.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create database connection')
    } finally {
      setDbCreating(false)
    }
  }


  async function openDatabaseConfiguration(connection: DatabaseConnection) {
    if (!workspaceId) return
    setConfiguringDatabaseConnection(connection)
    setLoadingDatabaseSchemas(true)
    try {
      const schemas = await listDatabaseSchemas(workspaceId, connection.connection_id)
      setAvailableDatabaseSchemas(schemas)
      // Seed picker state from existing scope_config (chunk 06: keys
      // are "catalog.schema").
      const includeList = (connection.scope_config?.introspection?.include) || []
      const samplingIncludeList = (connection.scope_config?.sampling?.include) || []
      const seedIntrospect = new Set<string>(includeList)
      const seedSampling = new Set<string>(samplingIncludeList)

      // First time configuring? Default to "introspect every visible
      // schema, sample nothing" - the regulated-buyer-friendly
      // default that still gives admins a working starting point.
      if (seedIntrospect.size === 0 && includeList.length === 0) {
        new Set(schemas.map(s => `${s.catalog}.${s.schema}`)).forEach(k => seedIntrospect.add(k))
      }
      setDatabaseIntrospectSchemas(seedIntrospect)
      setDatabaseSamplingSchemas(seedSampling)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load database schemas')
      setConfiguringDatabaseConnection(null)
    } finally {
      setLoadingDatabaseSchemas(false)
    }
  }

  function toggleSchemaIntrospect(schemaKey: string) {
    setDatabaseIntrospectSchemas(prev => {
      const next = new Set(prev)
      if (next.has(schemaKey)) {
        next.delete(schemaKey)
        // Drop sampling on the schema if we drop introspection too;
        // can't sample a schema we aren't pulling.
        setDatabaseSamplingSchemas(s => {
          const ns = new Set(s)
          ns.delete(schemaKey)
          return ns
        })
      } else {
        next.add(schemaKey)
      }
      return next
    })
  }

  function toggleSchemaSampling(schemaKey: string) {
    setDatabaseSamplingSchemas(prev => {
      const next = new Set(prev)
      if (next.has(schemaKey)) {
        next.delete(schemaKey)
      } else {
        // Sampling requires introspection - opt-in to introspect too.
        setDatabaseIntrospectSchemas(introspect => {
          if (introspect.has(schemaKey)) return introspect
          const ni = new Set(introspect)
          ni.add(schemaKey)
          return ni
        })
        next.add(schemaKey)
      }
      return next
    })
  }

  async function handleSaveDatabaseScope() {
    if (!workspaceId || !configuringDatabaseConnection) return
    setSavingDatabaseScope(true)
    setError(null)
    try {
      // Materialize per-schema selection into the wire format —
      // chunk 06 uses 2-segment "catalog.schema" keys for both
      // introspection.include and sampling.include.
      const introspectInclude = Array.from(databaseIntrospectSchemas).sort()
      const samplingInclude = Array.from(databaseSamplingSchemas).sort()

      const scope: DatabaseScopeConfig = {
        introspection: { include: introspectInclude, exclude: [] },
        sampling:      { include: samplingInclude, exclude: [] },
        profiling:     { enabled: true },
      }

      await updateDatabaseScope(
        workspaceId,
        configuringDatabaseConnection.connection_id,
        scope,
      )
      setConfiguringDatabaseConnection(null)
      setSuccess('Database scope updated and sync queued')
      loadConnections()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save database scope')
    } finally {
      setSavingDatabaseScope(false)
    }
  }

  async function handleTriggerDatabaseSync(connectionId: string) {
    if (!workspaceId) return
    setSyncingDatabase(connectionId)
    setError(null)
    try {
      await triggerDatabaseSync(workspaceId, connectionId)
      setSuccess('Database sync queued')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to trigger sync')
    } finally {
      setSyncingDatabase(null)
    }
  }

  async function handleDeleteDatabaseConnection(connectionId: string) {
    if (!workspaceId) return
    setError(null)
    try {
      await deleteDatabaseConnection(workspaceId, connectionId)
      setDatabaseConnections(prev => prev.filter(c => c.connection_id !== connectionId))
      setSuccess('Database connection deleted')
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
    // Pull the customer's override, if any. Empty-string is treated
    // as "no override" -- the picker input starts pre-populated with
    // the detected default branch, so an explicit clear is unusual
    // but should still fall through to server-side detection.
    const overrideRaw = githubBranchOverrides[repoName]
    const branchOverride = overrideRaw && overrideRaw.trim() ? overrideRaw.trim() : undefined
    try {
      const repos = await addGitHubRepos(workspaceId, [{
        repo_name: repoName,
        installation_id: selectedInstallation.installation_id,
        include_code_maps: true,
        repo_type: selectedRepoType,
        default_branch: branchOverride,
      }])
      setGithubRepos(prev => [...prev, ...repos])
      setInstallationRepos(prev => prev.filter(r => r.full_name !== repoName))
      // Clear the per-row override so a re-open of the picker starts
      // fresh next time.
      setGithubBranchOverrides(prev => {
        const next = { ...prev }
        delete next[repoName]
        return next
      })
      setSuccess(`Added ${repoName} as ${selectedRepoType}`)
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

  // ==========================================================================
  // Edit-branch modal (gear icon per row, both providers)
  // ==========================================================================

  function openEditBranchModal(
    provider: 'github' | 'azure_devops',
    target: string,
    storedDefault: string | null | undefined,
  ) {
    setEditBranchProvider(provider)
    setEditBranchTarget(target)
    setEditBranchSelected(storedDefault || '')
    setEditBranchData(null)
    setEditBranchError(null)
    // Kick off the branch-list fetch; the modal renders "loading"
    // until it resolves. If it fails, the modal falls back to a
    // plain text input pre-populated with the stored value.
    setEditBranchLoading(true)
    ;(async () => {
      if (!workspaceId) return
      try {
        const data = provider === 'github'
          ? await listGitHubRepoBranches(workspaceId, target)
          : await listAzureDevOpsRepoBranches(workspaceId, target)
        setEditBranchData(data)
        // If the row has no stored branch (legacy pre-chunk-01), seed
        // the input with the provider's current default so the customer
        // has a sensible starting point.
        if (!storedDefault && data.current_default) {
          setEditBranchSelected(data.current_default)
        }
      } catch (err) {
        setEditBranchError(err instanceof Error ? err.message : 'Failed to load branches')
      } finally {
        setEditBranchLoading(false)
      }
    })()
  }

  function closeEditBranchModal() {
    setEditBranchProvider(null)
    setEditBranchTarget(null)
    setEditBranchLoading(false)
    setEditBranchData(null)
    setEditBranchError(null)
    setEditBranchSelected('')
    setEditBranchSaving(false)
  }

  async function handleSaveBranch() {
    if (!workspaceId || !editBranchProvider || !editBranchTarget) return
    const chosen = editBranchSelected.trim()
    if (!chosen) {
      setError('Branch name cannot be empty')
      return
    }
    setEditBranchSaving(true)
    setError(null)
    try {
      if (editBranchProvider === 'github') {
        const updated = await updateGitHubRepo(workspaceId, editBranchTarget, {
          default_branch: chosen,
        })
        setGithubRepos(prev => prev.map(r => r.repo_name === editBranchTarget ? updated : r))
      } else {
        const updated = await updateAzureDevOpsRepo(workspaceId, editBranchTarget, {
          default_branch: chosen,
        })
        setAzureDevOpsRepos(prev => prev.map(r => r.slug === editBranchTarget ? updated : r))
      }
      setSuccess(`Sync branch updated to "${chosen}" for ${editBranchTarget}`)
      closeEditBranchModal()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update branch')
      setEditBranchSaving(false)
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

  // Non-member landed on a Global workspace: show a friendly info
  // panel with the workspace's name / description / admin contact
  // list instead of a bare "Workspace not found." Backend enforces
  // ``is_global`` == true so this can't reveal private workspaces.
  if (!workspace && globalPublicView) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center px-4">
        <div className="max-w-lg w-full bg-white rounded-lg border shadow-sm p-6">
          <div className="flex items-center gap-2 mb-3">
            <Globe className="text-blue-600" size={20} />
            <h1 className="text-lg font-semibold text-gray-900">
              {globalPublicView.name}
            </h1>
            <span
              className="text-xs px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 inline-flex items-center gap-1"
              title="Global workspace"
            >
              Global
            </span>
          </div>
          {globalPublicView.description && (
            <p className="text-sm text-gray-600 mb-4">{globalPublicView.description}</p>
          )}
          <p className="text-sm text-gray-700 mb-3">
            This is a <strong>Global workspace</strong>. Its content is available
            to you in chat and PR checks across the deployment, but only
            workspace admins can manage its members, repos, and integrations.
          </p>
          {globalPublicView.admin_emails.length > 0 ? (
            <>
              <p className="text-sm text-gray-700 mb-2">
                To request changes (add a repo, update standards, add a member),
                contact a workspace admin:
              </p>
              <ul className="space-y-1 mb-4">
                {globalPublicView.admin_emails.map(email => (
                  <li key={email} className="text-sm">
                    <a
                      href={`mailto:${email}`}
                      className="text-blue-600 hover:text-blue-700 hover:underline"
                    >
                      {email}
                    </a>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="text-sm text-gray-500 mb-4">
              No workspace admins listed. Contact your org admin to request
              changes.
            </p>
          )}
          <button
            onClick={() => navigate('/workspaces')}
            className="text-sm text-blue-600 hover:text-blue-700"
          >
            ← Back to workspaces
          </button>
        </div>
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
                  <MutationButton
                    onClick={handleSaveGeneral}
                    disabled={saving}
                    className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                  >
                    {saving && <Loader2 size={16} className="animate-spin" />}
                    Save Changes
                  </MutationButton>
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
                        <MutationButton
                          onClick={() => handleRemoveMember(member.user_email)}
                          className={`p-1.5 rounded transition-colors ${isLastAdmin ? 'text-gray-300 cursor-not-allowed' : 'text-red-500 hover:bg-red-50'}`}
                          title={isLastAdmin ? 'Cannot remove the last admin' : 'Remove member'}
                          disabled={isLastAdmin}
                        >
                          <UserMinus size={18} />
                        </MutationButton>
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
                  <MutationButton
                    onClick={handleConnectGitHub}
                    disabled={connectingGitHub}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm bg-gray-900 text-white rounded-lg hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    {connectingGitHub ? (
                      <Loader2 size={16} className="animate-spin" />
                    ) : (
                      <Plus size={16} />
                    )}
                    Connect GitHub
                  </MutationButton>
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
                          <MutationButton
                            onClick={() => openGitHubRepoModal(inst)}
                            className="flex items-center gap-2 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            <Plus size={16} />
                            Add Repos
                          </MutationButton>
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
                              <div className="flex items-center gap-2 flex-wrap">
                                <p className="font-medium text-gray-900 truncate">{repo.repo_name}</p>
                                <span className="text-xs px-1.5 py-0.5 rounded bg-gray-100 text-gray-600">
                                  {repo.repo_type || 'application'}
                                </span>
                                {repo.default_branch && (
                                  <span
                                    className="text-xs px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 font-mono inline-flex items-center gap-1"
                                    title={`Syncing from branch ${repo.default_branch}`}
                                  >
                                    <GitBranch size={11} />
                                    {repo.default_branch}
                                  </span>
                                )}
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
                                  type="button"
                                  onClick={() => openEditBranchModal('github', repo.repo_name, repo.default_branch)}
                                  className="p-1.5 text-gray-500 hover:bg-gray-100 rounded transition-colors"
                                  title="Change sync branch"
                                >
                                  <Settings size={16} />
                                </button>
                                <MutationButton
                                  onClick={() => handleTriggerGitHubSync(repo.repo_name)}
                                  disabled={syncingGitHub === repo.repo_name || (repo.repo_type !== 'standards' && !repo.include_code_maps)}
                                  className="p-1.5 text-gray-500 hover:bg-gray-100 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                  title="Sync now"
                                >
                                  {syncingGitHub === repo.repo_name ? (
                                    <Loader2 size={16} className="animate-spin" />
                                  ) : (
                                    <RefreshCw size={16} />
                                  )}
                                </MutationButton>
                                <MutationButton
                                  onClick={() => handleRemoveGitHubRepo(repo.repo_name)}
                                  disabled={removingGitHubRepo === repo.repo_name}
                                  className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                  title="Remove repo"
                                >
                                  {removingGitHubRepo === repo.repo_name ? (
                                    <Loader2 size={16} className="animate-spin" />
                                  ) : (
                                    <Trash2 size={16} />
                                  )}
                                </MutationButton>
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

            {/* Azure DevOps Integration Section (chunk 08) */}
            <div className="bg-white rounded-lg border shadow-sm">
              <div className="p-4 border-b flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <GitBranch className="text-blue-700" size={24} />
                  <div>
                    <h3 className="font-medium">Azure DevOps</h3>
                    <p className="text-sm text-gray-500">Connect Azure DevOps to sync repositories for chat search</p>
                  </div>
                </div>
                {isAdmin && azureDevOpsConnections.length === 0 && (
                  <MutationButton
                    onClick={handleConnectAzureDevOps}
                    disabled={connectingAzureDevOps}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm bg-gray-900 text-white rounded-lg hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    {connectingAzureDevOps ? (
                      <Loader2 size={16} className="animate-spin" />
                    ) : (
                      <Plus size={16} />
                    )}
                    Connect Azure DevOps
                  </MutationButton>
                )}
              </div>
              {loadingAzureDevOps ? (
                <div className="p-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : azureDevOpsConnections.length === 0 ? (
                <div className="p-8 text-center text-gray-500">
                  <GitBranch className="mx-auto text-gray-300 mb-2" size={48} />
                  <p>No Azure DevOps connection</p>
                  <p className="text-sm mt-1">Connect your Azure DevOps tenant to sync repositories</p>
                </div>
              ) : (
                <div>
                  {azureDevOpsConnections.map(conn => (
                    <div key={conn.connection_id} className="border-b last:border-b-0">
                      <div className="p-4 bg-gray-50 flex items-center justify-between">
                        <div className="flex items-center gap-3">
                          <GitBranch className="text-gray-600" size={20} />
                          <div>
                            <p className="font-medium text-gray-900">{conn.display_name}</p>
                            <p className="text-sm text-gray-500">{conn.user_email_external}</p>
                          </div>
                        </div>
                        {isAdmin && (
                          <div className="flex items-center gap-2">
                            <MutationButton
                              onClick={() => openAzureDevOpsRepoPicker(conn.connection_id)}
                              className="flex items-center gap-2 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
                            >
                              <Plus size={16} />
                              Add Repos
                            </MutationButton>
                            <MutationButton
                              onClick={() => handleDisconnectAzureDevOps(conn.connection_id)}
                              disabled={disconnectingAzureDevOps === conn.connection_id}
                              className="flex items-center gap-2 px-3 py-1.5 text-sm text-red-600 hover:bg-red-50 rounded-lg transition-colors disabled:opacity-50"
                            >
                              {disconnectingAzureDevOps === conn.connection_id ? (
                                <Loader2 size={16} className="animate-spin" />
                              ) : (
                                <Trash2 size={16} />
                              )}
                              Disconnect
                            </MutationButton>
                          </div>
                        )}
                      </div>
                    </div>
                  ))}

                  {azureDevOpsRepos.length === 0 ? (
                    <div className="p-8 text-center text-gray-500">
                      <p>No repositories added yet</p>
                      <p className="text-sm mt-1">Click <strong>Add Repos</strong> above to pick from your Azure DevOps orgs and projects.</p>
                    </div>
                  ) : (
                    <ul className="divide-y">
                      {azureDevOpsRepos.map(repo => (
                        <li key={repo.slug} className="p-4 flex items-center justify-between hover:bg-gray-50">
                          <div className="flex items-center gap-3 flex-1 min-w-0">
                            <GitBranch className="text-gray-400 flex-shrink-0" size={20} />
                            <div className="min-w-0">
                              <p className="font-medium text-gray-900 truncate">{repo.slug}</p>
                              <p className="text-sm text-gray-500">
                                {repo.default_branch || '(branch unknown)'} ·{' '}
                                <span className={
                                  repo.last_sync_status === 'success'
                                    ? 'text-green-600'
                                    : repo.last_sync_status === 'failed'
                                      ? 'text-red-600'
                                      : 'text-gray-500'
                                }>
                                  {repo.last_sync_status}
                                </span>
                                {repo.last_sync_error && (
                                  <span className="text-red-500"> · {repo.last_sync_error}</span>
                                )}
                              </p>
                            </div>
                          </div>
                          {isAdmin && (
                            <div className="flex items-center gap-2 flex-shrink-0">
                              <button
                                type="button"
                                onClick={() => openEditBranchModal('azure_devops', repo.slug, repo.default_branch)}
                                className="p-1.5 text-gray-500 hover:bg-gray-100 rounded transition-colors"
                                title="Change sync branch"
                              >
                                <Settings size={14} />
                              </button>
                              <MutationButton
                                onClick={() => handleSyncAzureDevOps(repo.slug)}
                                disabled={syncingAzureDevOps === repo.slug}
                                className="flex items-center gap-1 px-2 py-1 text-sm text-blue-600 hover:bg-blue-50 rounded transition-colors disabled:opacity-50"
                              >
                                {syncingAzureDevOps === repo.slug ? (
                                  <Loader2 size={14} className="animate-spin" />
                                ) : (
                                  <RefreshCw size={14} />
                                )}
                                Sync
                              </MutationButton>
                              <MutationButton
                                onClick={() => handleRemoveAzureDevOpsRepo(repo.slug)}
                                disabled={removingAzureDevOpsRepo === repo.slug}
                                className="flex items-center gap-1 px-2 py-1 text-sm text-red-600 hover:bg-red-50 rounded transition-colors disabled:opacity-50"
                              >
                                {removingAzureDevOpsRepo === repo.slug ? (
                                  <Loader2 size={14} className="animate-spin" />
                                ) : (
                                  <Trash2 size={14} />
                                )}
                              </MutationButton>
                            </div>
                          )}
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
                  <MutationButton
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
                  </MutationButton>
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
                            <MutationButton
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
                            </MutationButton>
                            <MutationButton
                              onClick={() => handleDeleteConnection(conn.connection_id)}
                              className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                              title="Disconnect"
                            >
                              <Trash2 size={16} />
                            </MutationButton>
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
                  <MutationButton
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
                  </MutationButton>
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
                            <MutationButton
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
                            </MutationButton>
                            <MutationButton
                              onClick={() => handleDeleteJiraConnection(conn.connection_id)}
                              className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                              title="Disconnect"
                            >
                              <Trash2 size={16} />
                            </MutationButton>
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
                  <MutationButton
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
                  </MutationButton>
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
                            <MutationButton
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
                            </MutationButton>
                            <MutationButton
                              onClick={() => handleDeleteOutlookConnection(conn.connection_id)}
                              className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                              title="Disconnect"
                            >
                              <Trash2 size={16} />
                            </MutationButton>
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
                  <MutationButton
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
                  </MutationButton>
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
                            <MutationButton
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
                            </MutationButton>
                            <MutationButton
                              onClick={() => handleDeleteTeamsConnection(conn.connection_id)}
                              className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                              title="Disconnect"
                            >
                              <Trash2 size={16} />
                            </MutationButton>
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
                  <MutationButton
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
                  </MutationButton>
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
                            <MutationButton
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
                            </MutationButton>
                            <MutationButton
                              onClick={() => handleDeleteSharePointConnection(conn.connection_id)}
                              className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                              title="Disconnect"
                            >
                              <Trash2 size={16} />
                            </MutationButton>
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
                  <MutationButton
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
                  </MutationButton>
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
                            <MutationButton
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
                            </MutationButton>
                            <MutationButton
                              onClick={() => handleDeleteOneDriveConnection(conn.connection_id)}
                              className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                              title="Disconnect"
                            >
                              <Trash2 size={16} />
                            </MutationButton>
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
                  <MutationButton
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
                  </MutationButton>
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
                            <MutationButton
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
                            </MutationButton>
                            <MutationButton
                              onClick={() => handleDeleteOneNoteConnection(conn.connection_id)}
                              className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                              title="Disconnect"
                            >
                              <Trash2 size={16} />
                            </MutationButton>
                          </div>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* Database Section */}
            <div className="bg-white rounded-lg border shadow-sm">
              <div className="p-4 border-b flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <svg className="w-6 h-6 text-blue-600" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M12 3C7.58 3 4 4.79 4 7v10c0 2.21 3.59 4 8 4s8-1.79 8-4V7c0-2.21-3.58-4-8-4zm0 2c3.87 0 6 1.5 6 2s-2.13 2-6 2-6-1.5-6-2 2.13-2 6-2zm0 14c-3.87 0-6-1.5-6-2v-2.23c1.61.79 3.79 1.23 6 1.23s4.39-.44 6-1.23V17c0 .5-2.13 2-6 2zm0-5c-3.87 0-6-1.5-6-2V9.77c1.61.79 3.79 1.23 6 1.23s4.39-.44 6-1.23V12c0 .5-2.13 2-6 2z" />
                  </svg>
                  <div>
                    <h3 className="font-medium">Database</h3>
                    <p className="text-sm text-gray-500">Production-truth context from your databases (Postgres, MySQL, MS SQL Server)</p>
                  </div>
                </div>
                {isAdmin && (
                  <MutationButton
                    onClick={openDatabaseConnectModal}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
                  >
                    <Plus size={16} />
                    Connect
                  </MutationButton>
                )}
              </div>
              {loadingConnections ? (
                <div className="p-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : databaseConnections.length === 0 ? (
                <div className="p-8 text-center text-gray-500">
                  No database connections yet
                </div>
              ) : (
                <ul className="divide-y">
                  {databaseConnections.map(conn => (
                    <li key={conn.connection_id} className="p-4">
                      <div className="flex items-start justify-between">
                        <div className="flex-1">
                          <div className="flex items-center gap-2 flex-wrap">
                            <p className="font-medium text-gray-900">{conn.display_label}</p>
                            <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-blue-100 text-blue-700 uppercase">
                              {conn.engine}
                            </span>
                            {/* chunk 07: surface DBA-created schemas the
                                admin hasn't included yet. N = available
                                schemas minus already-included. Schemas
                                always require admin confirmation (no
                                auto-add), so this nudges them into the
                                picker. */}
                            {(() => {
                              const available = conn.available_schemas || []
                              const included = conn.scope_config?.introspection?.include || []
                              const includedSet = new Set(included)
                              const newCount = available.filter(s => !includedSet.has(s)).length
                              return available.length > 0 && newCount > 0 ? (
                                <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-amber-100 text-amber-700">
                                  {newCount} new schema{newCount !== 1 ? 's' : ''} available - click Configure
                                </span>
                              ) : null
                            })()}
                          </div>
                          <p className="text-sm text-gray-500 mt-0.5">{conn.host}:{conn.port} / {conn.database_name}</p>
                          <p className="text-sm text-gray-500 mt-1">
                            {(conn.scope_config?.introspection?.include || []).length === 0 ? (
                              <span className="text-amber-600">No schemas selected - click Configure</span>
                            ) : (
                              <>
                                {conn.scope_config.introspection.include.length} schema{conn.scope_config.introspection.include.length !== 1 ? 's' : ''} selected
                                {' | '}
                                {conn.tables_count || 0} tables (auto-discovered)
                                {' | '}
                                {(conn.scope_config?.sampling?.include || []).length > 0
                                  ? `sampling: ${conn.scope_config.sampling.include.length} schema${conn.scope_config.sampling.include.length !== 1 ? 's' : ''}`
                                  : 'sampling: off'}
                              </>
                            )}
                          </p>
                          <p className="text-xs text-gray-400 mt-1">
                            Status: <span className={
                              conn.last_sync_status === 'completed' ? 'text-green-600' :
                              conn.last_sync_status === 'error' || conn.last_sync_status === 'auth_error' ? 'text-red-600' :
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
                              onClick={() => openDatabaseConfiguration(conn)}
                              className="px-3 py-1.5 text-sm text-blue-600 hover:bg-blue-50 rounded transition-colors"
                            >
                              Configure
                            </button>
                            <MutationButton
                              onClick={() => handleTriggerDatabaseSync(conn.connection_id)}
                              disabled={syncingDatabase === conn.connection_id || (conn.scope_config?.introspection?.include || []).length === 0}
                              className="p-1.5 text-gray-500 hover:bg-gray-100 rounded transition-colors disabled:opacity-50"
                              title="Sync now"
                            >
                              {syncingDatabase === conn.connection_id ? (
                                <Loader2 size={16} className="animate-spin" />
                              ) : (
                                <RefreshCw size={16} />
                              )}
                            </MutationButton>
                            <MutationButton
                              onClick={() => handleDeleteDatabaseConnection(conn.connection_id)}
                              className="p-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                              title="Disconnect"
                            >
                              <Trash2 size={16} />
                            </MutationButton>
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
                  <MutationButton
                    onClick={handleSaveSharing}
                    disabled={savingSharing}
                    className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                  >
                    {savingSharing && <Loader2 size={16} className="animate-spin" />}
                    Save Changes
                  </MutationButton>
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
                        <MutationButton
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
                        </MutationButton>
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
                        <MutationButton
                          onClick={handleSaveSystemPrompt}
                          disabled={savingSystemPrompt || systemPrompt === savedSystemPrompt}
                          className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                        >
                          {savingSystemPrompt && <Loader2 size={16} className="animate-spin" />}
                          Save Changes
                        </MutationButton>
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
              <MutationButton
                onClick={handleDelete}
                disabled={deleting}
                className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 transition-colors flex items-center gap-2"
              >
                {deleting && <Loader2 size={16} className="animate-spin" />}
                Delete
              </MutationButton>
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
              <MutationButton
                onClick={handleAddMember}
                disabled={!newMemberEmail.trim() || addingMember}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
              >
                {addingMember && <Loader2 size={16} className="animate-spin" />}
                Add
              </MutationButton>
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
                <MutationButton
                  onClick={handleSaveSpaces}
                  disabled={savingSpaces || selectedSpaces.size === 0}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {savingSpaces && <Loader2 size={16} className="animate-spin" />}
                  Save & Sync
                </MutationButton>
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
                <MutationButton
                  onClick={handleSaveProjects}
                  disabled={savingProjects || selectedProjects.size === 0}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {savingProjects && <Loader2 size={16} className="animate-spin" />}
                  Save & Sync
                </MutationButton>
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
                <MutationButton
                  onClick={handleSaveFolders}
                  disabled={savingFolders || selectedFolders.size === 0}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {savingFolders && <Loader2 size={16} className="animate-spin" />}
                  Save & Sync
                </MutationButton>
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
                <MutationButton
                  onClick={handleSaveTeamsConfig}
                  disabled={savingTeamsConfig || selectedChannels.length === 0}
                  className="px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {savingTeamsConfig && <Loader2 size={16} className="animate-spin" />}
                  Save & Sync
                </MutationButton>
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
                <MutationButton
                  onClick={handleSaveSPSites}
                  disabled={savingSPSites || selectedSPSites.size === 0}
                  className="px-4 py-2 bg-teal-600 text-white rounded-lg hover:bg-teal-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {savingSPSites && <Loader2 size={16} className="animate-spin" />}
                  Save & Sync
                </MutationButton>
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
                <MutationButton
                  onClick={handleSaveODFolders}
                  disabled={savingODFolders || selectedODFolders.size === 0}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {savingODFolders && <Loader2 size={16} className="animate-spin" />}
                  Save & Sync
                </MutationButton>
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
                <MutationButton
                  onClick={handleSaveONNotebooks}
                  disabled={savingONNotebooks || selectedONNotebooks.size === 0}
                  className="px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {savingONNotebooks && <Loader2 size={16} className="animate-spin" />}
                  Save & Sync
                </MutationButton>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Database Connect Modal */}
      {showDatabaseConnectModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-xl mx-4 max-h-[90vh] flex flex-col">
            <div className="p-6 border-b">
              <h2 className="text-lg font-semibold">Connect Database</h2>
              <p className="text-sm text-gray-500 mt-1">
                Postgres (incl. Aurora / RDS / Redshift), MySQL (incl. Aurora / RDS / MariaDB), or MS SQL Server.
              </p>
            </div>
            <div className="flex-1 overflow-y-auto p-6 space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Engine</label>
                <select
                  value={dbConnectEngine}
                  onChange={(e) => {
                    const engine = e.target.value as DatabaseEngine
                    setDbConnectEngine(engine)
                    setDbConnectPort(_defaultDbPort(engine))
                    setDbValidationResult(null)
                  }}
                  className="w-full border rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  <option value="postgres">PostgreSQL (incl. Aurora / RDS / Redshift)</option>
                  <option value="mysql">MySQL (incl. Aurora / RDS / MariaDB)</option>
                  <option value="mssql">MS SQL Server</option>
                </select>
              </div>
              <div className="grid grid-cols-3 gap-3">
                <div className="col-span-2">
                  <label className="block text-sm font-medium text-gray-700 mb-1">Host</label>
                  <input
                    type="text"
                    value={dbConnectHost}
                    onChange={(e) => setDbConnectHost(e.target.value)}
                    placeholder="db.internal.example.com"
                    className="w-full border rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Port</label>
                  <input
                    type="number"
                    value={dbConnectPort}
                    onChange={(e) => setDbConnectPort(Number(e.target.value))}
                    className="w-full border rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Database name</label>
                <input
                  type="text"
                  value={dbConnectDatabaseName}
                  onChange={(e) => setDbConnectDatabaseName(e.target.value)}
                  placeholder="analytics"
                  className="w-full border rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Display label (optional)</label>
                <input
                  type="text"
                  value={dbConnectDisplayLabel}
                  onChange={(e) => setDbConnectDisplayLabel(e.target.value)}
                  placeholder="Analytics Warehouse"
                  className="w-full border rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Username</label>
                  <input
                    type="text"
                    value={dbConnectUsername}
                    onChange={(e) => setDbConnectUsername(e.target.value)}
                    autoComplete="off"
                    className="w-full border rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Password</label>
                  <input
                    type="password"
                    value={dbConnectPassword}
                    onChange={(e) => setDbConnectPassword(e.target.value)}
                    autoComplete="new-password"
                    className="w-full border rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                </div>
              </div>
              {dbConnectEngine !== 'mssql' && (
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">SSL mode</label>
                  <select
                    value={dbConnectSSLMode}
                    onChange={(e) => setDbConnectSSLMode(e.target.value)}
                    className="w-full border rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
                  >
                    <option value="require">require</option>
                    <option value="verify-ca">verify-ca</option>
                    <option value="verify-full">verify-full</option>
                    <option value="disable">disable (NOT recommended)</option>
                  </select>
                </div>
              )}
              {dbConnectEngine === 'mssql' && (
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={dbConnectMSSQLEncrypt}
                    onChange={(e) => setDbConnectMSSQLEncrypt(e.target.checked)}
                    className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
                  />
                  Encrypt connection (Encrypt=yes)
                </label>
              )}

              {dbValidationResult && (
                <div className={`p-3 rounded text-sm ${dbValidationResult.valid ? 'bg-green-50 text-green-800' : 'bg-red-50 text-red-700'}`}>
                  {dbValidationResult.valid ? (
                    <>
                      Connection successful{dbValidationResult.server_version && <> — server version: {dbValidationResult.server_version}</>}
                    </>
                  ) : (
                    <>Connection failed: {dbValidationResult.error}</>
                  )}
                </div>
              )}
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex items-center justify-between border-t">
              <MutationButton
                onClick={handleValidateDatabase}
                disabled={dbValidating || !dbConnectHost || !dbConnectDatabaseName || !dbConnectUsername}
                className="px-3 py-1.5 text-sm border border-blue-600 text-blue-600 rounded-lg hover:bg-blue-50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-2"
              >
                {dbValidating && <Loader2 size={14} className="animate-spin" />}
                Test connection
              </MutationButton>
              <div className="flex gap-3">
                <button
                  onClick={() => setShowDatabaseConnectModal(false)}
                  className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors"
                >
                  Cancel
                </button>
                <MutationButton
                  onClick={handleCreateDatabaseConnection}
                  disabled={dbCreating || !dbConnectHost || !dbConnectDatabaseName || !dbConnectUsername || !dbConnectPassword}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {dbCreating && <Loader2 size={16} className="animate-spin" />}
                  Save connection
                </MutationButton>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Database Scope Picker Modal */}
      {configuringDatabaseConnection && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-3xl mx-4 max-h-[85vh] flex flex-col">
            <div className="p-6 border-b">
              <h2 className="text-lg font-semibold">Configure Database Scope</h2>
              <p className="text-sm text-gray-500 mt-1">
                Choose which schemas to introspect from {configuringDatabaseConnection.display_label}.
              </p>
              <p className="text-xs text-gray-500 mt-2">
                Introspect = pull schema + safe aggregate statistics for every table in the selected schema. New tables added to a selected schema are auto-discovered on the next hourly sync.
                Sample = also pull example rows from every table in the selected schema (Comprehend PII redaction runs at ingest if enabled).
              </p>
            </div>
            <div className="flex-1 overflow-y-auto p-6">
              {loadingDatabaseSchemas ? (
                <div className="py-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : availableDatabaseSchemas.length === 0 ? (
                <p className="text-center text-gray-500 py-4">No schemas visible. The connection user may lack USAGE on any schema, or the database has no user schemas yet. Check Lambda logs for the actual introspection error.</p>
              ) : (() => {
                // Group the (catalog, schema, table) flat list returned
                // by the picker API into per-schema buckets so we can
                // render schemas as the selection unit and show their
                // current tables read-only beneath.
                const schemaBuckets = new Map<string, { catalog: string; schema: string; tables: string[] }>()
                availableDatabaseSchemas.forEach(s => {
                  const key = `${s.catalog}.${s.schema}`
                  if (!schemaBuckets.has(key)) {
                    schemaBuckets.set(key, { catalog: s.catalog, schema: s.schema, tables: [] })
                  }
                  schemaBuckets.get(key)!.tables.push(s.table)
                })
                const ordered = Array.from(schemaBuckets.entries()).sort(([a], [b]) => a.localeCompare(b))
                return (
                  <table className="w-full text-sm">
                    <thead className="text-xs uppercase text-gray-500">
                      <tr>
                        <th className="text-left p-2">Catalog</th>
                        <th className="text-left p-2">Schema</th>
                        <th className="text-left p-2">Tables (auto-discovered)</th>
                        <th className="text-center p-2">Introspect</th>
                        <th className="text-center p-2">Sample rows</th>
                      </tr>
                    </thead>
                    <tbody>
                      {ordered.map(([schemaKey, bucket]) => {
                        // chunk 07: badge a schema "NEW" when it
                        // appeared in the last sync's available_schemas
                        // snapshot but is not yet in the picker's
                        // selected-set. Clears once the admin saves with
                        // it included (or explicitly leaves it out).
                        const availableSet = new Set(
                          configuringDatabaseConnection.available_schemas || []
                        )
                        const isNew = availableSet.has(schemaKey) && !databaseIntrospectSchemas.has(schemaKey)
                        return (
                        <tr key={schemaKey} className="border-t hover:bg-gray-50 align-top">
                          <td className="p-2 text-gray-600">{bucket.catalog || '-'}</td>
                          <td className="p-2 font-medium text-gray-900">
                            {bucket.schema}
                            {isNew && (
                              <span className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-100 text-amber-700 uppercase tracking-wide">
                                NEW
                              </span>
                            )}
                          </td>
                          <td className="p-2 text-gray-600 text-xs">
                            <span className="font-mono">{bucket.tables.length} table{bucket.tables.length !== 1 ? 's' : ''}</span>
                            <span className="text-gray-400 ml-2">{bucket.tables.slice(0, 6).join(', ')}{bucket.tables.length > 6 ? `, +${bucket.tables.length - 6} more` : ''}</span>
                          </td>
                          <td className="p-2 text-center">
                            <input
                              type="checkbox"
                              checked={databaseIntrospectSchemas.has(schemaKey)}
                              onChange={() => toggleSchemaIntrospect(schemaKey)}
                              className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
                            />
                          </td>
                          <td className="p-2 text-center">
                            <input
                              type="checkbox"
                              checked={databaseSamplingSchemas.has(schemaKey)}
                              onChange={() => toggleSchemaSampling(schemaKey)}
                              className="w-4 h-4 text-amber-600 rounded focus:ring-amber-500"
                            />
                          </td>
                        </tr>
                        )
                      })}
                    </tbody>
                  </table>
                )
              })()}
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex items-center justify-between border-t">
              <p className="text-sm text-gray-500">
                {databaseIntrospectSchemas.size} schema{databaseIntrospectSchemas.size !== 1 ? 's' : ''} to introspect, {databaseSamplingSchemas.size} with sampling
              </p>
              <div className="flex gap-3">
                <button
                  onClick={() => setConfiguringDatabaseConnection(null)}
                  className="px-4 py-2 text-gray-700 hover:bg-gray-200 rounded-lg transition-colors"
                >
                  Cancel
                </button>
                <MutationButton
                  onClick={handleSaveDatabaseScope}
                  disabled={savingDatabaseScope || databaseIntrospectSchemas.size === 0}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
                >
                  {savingDatabaseScope && <Loader2 size={16} className="animate-spin" />}
                  Save & Sync
                </MutationButton>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Edit Sync Branch Modal (both providers). Opens from the
          gear icon on each added-repo row. Shows a branch dropdown
          populated from chunk 06's /branches endpoint, falls back to
          a text input if the endpoint fails (permission, rate-limit),
          and surfaces a drift badge when the stored branch differs
          from the provider's current default. */}
      {editBranchProvider && editBranchTarget && (
        <div
          className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
          role="dialog"
          aria-modal="true"
          aria-label="Change sync branch"
        >
          <div className="bg-white rounded-lg shadow-xl w-full max-w-md mx-4 flex flex-col">
            <div className="p-6 border-b">
              <h2 className="text-lg font-semibold">Change sync branch</h2>
              <p className="text-sm text-gray-500 mt-1 font-mono truncate">{editBranchTarget}</p>
            </div>
            <div className="p-6 space-y-4">
              {editBranchLoading ? (
                <div className="py-6 flex items-center justify-center gap-2 text-gray-500">
                  <Loader2 className="animate-spin" size={18} />
                  <span>Loading branches...</span>
                </div>
              ) : editBranchError ? (
                <>
                  <div className="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded p-3">
                    Could not load branch list ({editBranchError}). Enter the branch name manually.
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Sync from branch</label>
                    <input
                      type="text"
                      value={editBranchSelected}
                      onChange={(e) => setEditBranchSelected(e.target.value)}
                      placeholder="e.g. main, master, develop"
                      className="w-full px-3 py-2 text-sm font-mono border rounded focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                  </div>
                </>
              ) : editBranchData ? (
                <>
                  {/* Drift badge: stored branch differs from
                      provider's current default. Only render when we
                      have both values and they disagree. */}
                  {editBranchData.stored_default
                    && editBranchData.stored_default !== editBranchData.current_default && (
                    <div className="text-sm bg-amber-50 border border-amber-200 rounded p-3">
                      <p className="text-amber-900 font-medium">Branch drift detected</p>
                      <p className="text-amber-800 mt-1">
                        Currently syncing from{' '}
                        <code className="font-mono bg-white px-1 rounded">
                          {editBranchData.stored_default}
                        </code>{' '}
                        but the provider now reports{' '}
                        <code className="font-mono bg-white px-1 rounded">
                          {editBranchData.current_default}
                        </code>{' '}
                        as the default.
                      </p>
                      <button
                        type="button"
                        onClick={() => setEditBranchSelected(editBranchData.current_default)}
                        className="mt-2 text-sm text-blue-700 hover:text-blue-900 underline"
                      >
                        Use {editBranchData.current_default} (current provider default)
                      </button>
                    </div>
                  )}
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Sync from branch</label>
                    <select
                      value={editBranchSelected}
                      onChange={(e) => setEditBranchSelected(e.target.value)}
                      className="w-full px-3 py-2 text-sm font-mono border rounded focus:outline-none focus:ring-2 focus:ring-blue-500"
                    >
                      {/* If the current stored branch isn't in the
                          returned list (deleted, or paginated past the
                          500 cap), include it as an explicit option so
                          we never lose the customer's current
                          selection to a dropdown that can't render it. */}
                      {editBranchSelected
                        && !editBranchData.branches.some(b => b.name === editBranchSelected) && (
                        <option value={editBranchSelected}>{editBranchSelected} (not in list)</option>
                      )}
                      {editBranchData.branches.map(b => (
                        <option key={b.name} value={b.name}>
                          {b.name}
                          {b.name === editBranchData.current_default ? ' (provider default)' : ''}
                        </option>
                      ))}
                    </select>
                  </div>
                </>
              ) : null}
            </div>
            <div className="p-6 border-t flex justify-end gap-2">
              <button
                type="button"
                onClick={closeEditBranchModal}
                disabled={editBranchSaving}
                className="px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 rounded transition-colors disabled:opacity-50"
              >
                Cancel
              </button>
              <MutationButton
                onClick={handleSaveBranch}
                disabled={
                  editBranchSaving
                  || editBranchLoading
                  || !editBranchSelected.trim()
                  || editBranchSelected.trim() === (editBranchData?.stored_default || '')
                }
                className="px-4 py-2 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-1"
              >
                {editBranchSaving && <Loader2 size={14} className="animate-spin" />}
                Save
              </MutationButton>
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
                          <label className="text-xs text-gray-500 mt-1 flex items-center gap-1">
                            <span>Sync from branch:</span>
                            <input
                              type="text"
                              value={
                                githubBranchOverrides[repo.full_name] ?? repo.default_branch
                              }
                              onChange={(e) =>
                                setGithubBranchOverrides(prev => ({
                                  ...prev,
                                  [repo.full_name]: e.target.value,
                                }))
                              }
                              placeholder={repo.default_branch}
                              className="ml-1 px-1.5 py-0.5 text-xs font-mono border border-gray-200 rounded focus:outline-none focus:ring-1 focus:ring-blue-400 focus:border-blue-400 w-32"
                              aria-label={`Sync branch for ${repo.full_name}`}
                            />
                          </label>
                        </div>
                      </div>
                      <MutationButton
                        onClick={() => handleAddGitHubRepo(repo.full_name)}
                        disabled={addingGitHubRepo === repo.full_name}
                        className="ml-4 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-1"
                      >
                        {addingGitHubRepo === repo.full_name ? (
                          <Loader2 size={14} className="animate-spin" />
                        ) : (
                          <Plus size={14} />
                        )}
                        Add
                      </MutationButton>
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

      {/* Add Azure DevOps Repo Modal */}
      {showAddAzureDevOpsRepo && pickerConnectionId && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-lg mx-4 max-h-[80vh] flex flex-col">
            <div className="p-6 border-b">
              <h2 className="text-lg font-semibold">Add Azure DevOps Repositories</h2>
              <p className="text-sm text-gray-500 mt-1">
                Pick an organization, a project, and a repository to add.
              </p>
            </div>
            <div className="p-4 border-b space-y-3">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Organization</label>
                <select
                  value={pickerSelectedOrg}
                  onChange={(e) => onSelectAzureDevOpsOrg(e.target.value)}
                  disabled={loadingAzureDevOpsOrgs}
                  className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  <option value="">
                    {loadingAzureDevOpsOrgs ? 'Loading orgs...' : 'Select an organization'}
                  </option>
                  {azureDevOpsOrgs.map(org => (
                    <option key={org.accountId} value={org.accountName}>{org.accountName}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Project</label>
                <select
                  value={pickerSelectedProject}
                  onChange={(e) => onSelectAzureDevOpsProject(e.target.value)}
                  disabled={!pickerSelectedOrg || loadingAzureDevOpsProjects}
                  className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-50"
                >
                  <option value="">
                    {!pickerSelectedOrg
                      ? 'Pick an organization first'
                      : loadingAzureDevOpsProjects
                        ? 'Loading projects...'
                        : 'Select a project'}
                  </option>
                  {azureDevOpsProjects.map(p => (
                    <option key={p.id} value={p.name}>{p.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Repository Type</label>
                <select
                  value={selectedAzureDevOpsRepoType}
                  onChange={(e) => setSelectedAzureDevOpsRepoType(e.target.value as AzureDevOpsRepoType)}
                  className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  <option value="application">Application (code, Terraform, serverless)</option>
                  <option value="standards">Standards (ADRs, docs, YAML, JSON, XML)</option>
                </select>
              </div>
            </div>
            <div className="flex-1 overflow-y-auto">
              {!pickerSelectedProject ? (
                <div className="py-8 text-center text-gray-500">
                  Pick an organization and project to see repositories
                </div>
              ) : loadingAzureDevOpsPickerRepos ? (
                <div className="py-8 flex items-center justify-center">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              ) : azureDevOpsAvailableRepos.length === 0 ? (
                <div className="py-8 text-center text-gray-500">
                  No repositories in this project
                </div>
              ) : (
                <ul className="divide-y">
                  {azureDevOpsAvailableRepos.map(repo => (
                    <li key={repo.id} className="p-4 flex items-center justify-between hover:bg-gray-50">
                      <div className="flex items-center gap-3 min-w-0 flex-1">
                        <GitBranch className="text-gray-400 flex-shrink-0" size={20} />
                        <div className="min-w-0">
                          <p className="font-medium text-gray-900 truncate">{repo.name}</p>
                          {repo.default_branch ? (
                            <label className="text-xs text-gray-500 mt-1 flex items-center gap-1">
                              <span>Sync from branch:</span>
                              <input
                                type="text"
                                value={
                                  azureDevOpsBranchOverrides[repo.name] ?? repo.default_branch
                                }
                                onChange={(e) =>
                                  setAzureDevOpsBranchOverrides(prev => ({
                                    ...prev,
                                    [repo.name]: e.target.value,
                                  }))
                                }
                                placeholder={repo.default_branch}
                                className="ml-1 px-1.5 py-0.5 text-xs font-mono border border-gray-200 rounded focus:outline-none focus:ring-1 focus:ring-blue-400 focus:border-blue-400 w-32"
                                aria-label={`Sync branch for ${repo.name}`}
                              />
                            </label>
                          ) : (
                            <p className="text-xs text-gray-400 mt-0.5">
                              (empty repo — no default branch)
                            </p>
                          )}
                        </div>
                      </div>
                      <MutationButton
                        onClick={() => handleAddAzureDevOpsRepo(repo.name)}
                        disabled={addingAzureDevOpsRepo === repo.name || repo.already_added || !repo.default_branch}
                        className="ml-4 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-1"
                      >
                        {addingAzureDevOpsRepo === repo.name ? (
                          <Loader2 size={14} className="animate-spin" />
                        ) : repo.already_added ? (
                          <Check size={14} />
                        ) : (
                          <Plus size={14} />
                        )}
                        {repo.already_added ? 'Added' : 'Add'}
                      </MutationButton>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="px-6 py-4 bg-gray-50 rounded-b-lg flex justify-end border-t">
              <button
                onClick={closeAzureDevOpsRepoPicker}
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
