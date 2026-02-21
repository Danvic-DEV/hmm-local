const API_BASE = '/api'

export class APIError extends Error {
  constructor(
    message: string,
    public status: number,
    public data?: unknown
  ) {
    super(message)
    this.name = 'APIError'
  }
}

async function fetchAPI<T>(
  endpoint: string,
  options?: RequestInit
): Promise<T> {
  const url = `${API_BASE}${endpoint}`
  
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  })

  if (!response.ok) {
    const errorData = (await response.json().catch(() => ({}))) as unknown
    let errorMessage = `HTTP ${response.status}`
    if (
      typeof errorData === 'object' &&
      errorData !== null &&
      'message' in errorData &&
      typeof (errorData as { message?: unknown }).message === 'string'
    ) {
      errorMessage = (errorData as { message: string }).message
    }

    throw new APIError(errorMessage, response.status, errorData)
  }

  return (await response.json()) as T
}

// Miners
export interface Miner {
  id: string
  name: string
  type: string
  ip_address: string
  status: string
  hashrate: number
  temperature: number
  power: number
  uptime: number
  pool_address: string
}

export type MinerStatsResponse = Record<string, unknown>

export interface CreateMinerPayload {
  name: string
  miner_type: string
  ip_address: string
  port: number | null
  config?: Record<string, unknown>
}

export const minersAPI = {
  getAll: () => fetchAPI<Miner[]>('/miners'),
  getById: (id: string) => fetchAPI<Miner>(`/miners/${id}`),
  getStats: (id: string) => fetchAPI<MinerStatsResponse>(`/miners/${id}/stats`),
  create: (payload: CreateMinerPayload) =>
    fetchAPI<Miner>('/miners', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
}

// Dashboard
export interface SystemEvent {
  id: number
  timestamp: string
  event_type: string
  source: string
  message: string
  data?: Record<string, unknown> | null
}

export interface RecentEventsResponse {
  events: SystemEvent[]
}

export interface DashboardData {
  stats: {
    online_miners: number
    total_hashrate_ghs: number
    total_pool_hashrate_ghs: number | { display: string; value: number; unit: string }
    pool_efficiency_percent: number
    total_power_watts: number
    avg_efficiency_wth: number
    total_cost_24h_pounds: number
    avg_price_per_kwh_pence: number
    current_energy_price_pence: number
    best_share_24h: {
      difficulty: number
      coin: string
      network_difficulty: number
      percentage: number
      timestamp: string
      time_ago_seconds: number
    }
  }
  miners: Record<string, unknown>[]
  events: SystemEvent[]
  pools: Record<string, unknown>[]
}

export const dashboardAPI = {
  getData: () => fetchAPI<DashboardData>('/dashboard'),
  getAll: (dashboardType: string = 'asic') => 
    fetchAPI<DashboardData>(`/dashboard/all?dashboard_type=${dashboardType}`),
  getEvents: (limit: number = 1000) =>
    fetchAPI<RecentEventsResponse>(`/dashboard/events/recent?limit=${limit}`),
  clearEvents: () =>
    fetchAPI<{ message: string }>(`/dashboard/events`, {
      method: 'DELETE',
    }),
}

// Analytics
export type AnalyticsEfficiencyResponse = Record<string, unknown>
export type AnalyticsPerformanceResponse = Record<string, unknown>

export const analyticsAPI = {
  getEfficiency: (timeRange: string) => 
    fetchAPI<AnalyticsEfficiencyResponse>(`/analytics/efficiency?range=${timeRange}`),
  getPerformance: (minerId: string, timeRange: string) =>
    fetchAPI<AnalyticsPerformanceResponse>(`/analytics/miner/${minerId}?range=${timeRange}`),
}

export const poolsAPI = {
  // New plugin-based endpoints
  getPlatformTiles: () => fetchAPI<PlatformTilesResponse>('/dashboard/pools/platform-tiles'),
  getPoolTiles: (poolId?: string) => {
    const url = poolId ? `/dashboard/pools?pool_id=${poolId}` : '/dashboard/pools'
    return fetchAPI<PoolTilesResponse>(url)
  },
  reorderPools: (items: { pool_id: number; sort_order: number }[]) =>
    fetchAPI<{ success: boolean; updated_count: number; message: string }>('/pools/reorder', {
      method: 'PATCH',
      body: JSON.stringify(items),
    }),
  getRecoveryStatus: (windowHours: number = 24) =>
    fetchAPI<PoolRecoveryStatusResponse>(`/pools/recovery-status?window_hours=${windowHours}`),
}

// New Plugin-Based Pool Tiles
export interface PlatformTile1Health {
  total_pools: number
  healthy_pools: number
  unhealthy_pools: number
  avg_latency_ms: number
  status: "healthy" | "degraded" | "unhealthy" | "no_pools"
}

export interface PlatformTile2Network {
  total_pool_hashrate: number
  total_network_difficulty: number
  avg_pool_percentage: number
  estimated_time_to_block: string | null
}

export interface PlatformTile3Shares {
  total_valid: number
  total_invalid: number
  total_stale: number
  avg_reject_rate: number
}

export interface PlatformTile4Blocks {
  total_blocks_24h: number
  total_earnings_24h: number | null
  currencies: string[]
}

export interface PlatformTilesResponse {
  tile_1_health: PlatformTile1Health
  tile_2_network: PlatformTile2Network
  tile_3_shares: PlatformTile3Shares
  tile_4_blocks: PlatformTile4Blocks
}

export interface PoolTile1Health {
  health_status: boolean
  health_message: string | null
  latency_ms: number | null
}

export interface PoolTile2Network {
  network_difficulty: number | null
  pool_hashrate: number | null
  estimated_time_to_block: string | null
  pool_percentage: number | null
}

export interface PoolTile3Shares {
  shares_valid: number | null
  shares_invalid: number | null
  shares_stale: number | null
  reject_rate: number | null
}

export interface PoolTile4Blocks {
  blocks_found_24h: number | null
  estimated_earnings_24h: number | null
  currency: string | null
  confirmed_balance: number | null
  pending_balance: number | null
  last_block_found: string | null
}

export interface PoolTileSet {
  pool_id: string
  pool_type: string
  display_name: string
  sort_order?: number
  warnings?: string[]
  supports_coins: string[]
  tile_1_health: PoolTile1Health
  tile_2_network: PoolTile2Network
  tile_3_shares: PoolTile3Shares
  tile_4_blocks: PoolTile4Blocks
  last_updated: string | null
  supports_earnings: boolean
  supports_balance: boolean
}

export interface PoolTilesResponse {
  [pool_id: string]: PoolTileSet
}

export interface PoolRecoveryStatusPool {
  pool_id: number
  pool_name: string
  recovered_count: number
  unresolved_count: number
  last_event_at: string | null
  last_message: string | null
}

export interface PoolRecoveryStatusResponse {
  window_hours: number
  totals: {
    recovered: number
    unresolved: number
  }
  pools: PoolRecoveryStatusPool[]
}


export const integrationsAPI = {}
// Health
export interface MinerHealth {
  miner_id: string
  miner_name: string
  score: number
  status: string
  reasons: string[]
  uptime_hours: number
  avg_temp: number
  reject_rate: number
}

export const healthAPI = {
  getFleetHealth: () => fetchAPI<MinerHealth[]>('/health/fleet'),
  getMinerHealth: (id: string) => fetchAPI<Record<string, unknown>>(`/health/miner/${id}`),
}

// Cloud settings
export interface CloudConfigResponse {
  enabled: boolean
  api_key: string | null
  endpoint: string
  installation_name: string
  installation_location?: string | null
  push_interval_minutes: number
}

export interface UpdateCloudConfigPayload {
  enabled: boolean
  api_key: string | null
  endpoint: string
  installation_name: string
  installation_location?: string | null
  push_interval_minutes: number
}

export interface CloudTestResponse {
  success: boolean
  message?: string
  endpoint?: string
}

export interface CloudActionResponse {
  status: string
  message?: string
}

export const cloudAPI = {
  getConfig: () => fetchAPI<CloudConfigResponse>('/cloud/config'),
  updateConfig: (payload: UpdateCloudConfigPayload) =>
    fetchAPI<CloudActionResponse>('/cloud/config', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  testConnection: () =>
    fetchAPI<CloudTestResponse>('/cloud/test', {
      method: 'POST',
    }),
  manualPush: () =>
    fetchAPI<CloudActionResponse>('/cloud/push/manual', {
      method: 'POST',
    }),
}

// Network discovery
export interface NetworkRange {
  cidr: string
  name?: string | null
}

export interface DiscoveryConfigResponse {
  enabled: boolean
  auto_add: boolean
  networks: NetworkRange[]
  scan_interval_hours: number
}

export interface UpdateDiscoveryConfigPayload extends DiscoveryConfigResponse {}

export interface DiscoveredMiner {
  ip: string
  port: number
  type: string
  name: string
  details: Record<string, unknown>
  already_added: boolean
}

export interface DiscoveryScanResponse {
  total_found: number
  new_miners: number
  existing_miners: number
  miners: DiscoveredMiner[]
}

export interface AutoScanResponse {
  total_found: number
  total_added: number
  auto_add_enabled: boolean
  networks_scanned: number
}

export interface NetworkInfoResponse {
  network_cidr: string
  description: string
}

export interface ManualScanPayload {
  network_cidr?: string | null
  timeout?: number
}

export const discoveryAPI = {
  getConfig: () => fetchAPI<DiscoveryConfigResponse>('/discovery/config'),
  updateConfig: (payload: UpdateDiscoveryConfigPayload) =>
    fetchAPI<{ message: string }>('/discovery/config', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  getNetworkInfo: () => fetchAPI<NetworkInfoResponse>('/discovery/network-info'),
  scanNetwork: (payload: ManualScanPayload) =>
    fetchAPI<DiscoveryScanResponse>('/discovery/scan', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  triggerAutoScan: () =>
    fetchAPI<AutoScanResponse>('/discovery/auto-scan', {
      method: 'POST',
    }),
}

// Tuning profiles
export interface TuningProfile {
  id: number
  name: string
  miner_type: string
  description: string | null
  settings: Record<string, string | number>
  is_system: boolean
  created_at: string
}

export interface CreateTuningProfilePayload {
  name: string
  miner_type: string
  description?: string | null
  settings: Record<string, string | number>
}

export interface ApplyProfileResponse {
  message: string
}

export const tuningAPI = {
  getProfiles: (minerType?: string) =>
    fetchAPI<TuningProfile[]>(minerType ? `/tuning/profiles?miner_type=${minerType}` : '/tuning/profiles'),
  createProfile: (payload: CreateTuningProfilePayload) =>
    fetchAPI<TuningProfile>('/tuning/profiles', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  deleteProfile: (profileId: number) =>
    fetchAPI<{ message: string }>(`/tuning/profiles/${profileId}`, {
      method: 'DELETE',
    }),
  applyProfile: (profileId: number, minerId: number) =>
    fetchAPI<ApplyProfileResponse>(`/tuning/profiles/${profileId}/apply/${minerId}`, {
      method: 'POST',
    }),
}

// Notifications
export type NotificationChannelType = 'telegram' | 'discord'

export interface NotificationChannel {
  id: number
  channel_type: NotificationChannelType
  enabled: boolean
  config: Record<string, unknown>
}

export interface NotificationChannelCreatePayload {
  channel_type: NotificationChannelType
  enabled: boolean
  config: Record<string, unknown>
}

export interface NotificationChannelUpdatePayload {
  enabled?: boolean
  config?: Record<string, unknown>
}

export interface AlertConfigItem {
  id: number
  alert_type: string
  enabled: boolean
  config?: Record<string, unknown> | null
}

export interface AlertConfigCreatePayload {
  alert_type: string
  enabled: boolean
  config?: Record<string, unknown> | null
}

// AI settings
export type AIProvider = 'openai' | 'ollama'

export interface AIConfigResponse {
  enabled: boolean
  provider: AIProvider
  model: string
  max_tokens: number
  base_url?: string | null
  api_key?: string | null
}

export interface AIStatusResponse {
  enabled: boolean
  configured: boolean
  provider: AIProvider
  config: {
    enabled: boolean
    provider: AIProvider
    model: string
    max_tokens: number
    base_url?: string | null
  }
}

export interface SaveAIConfigPayload {
  enabled: boolean
  provider: AIProvider
  model: string
  max_tokens: number
  base_url?: string | null
  api_key?: string | null
}

export interface AITestPayload {
  provider: AIProvider
  model: string
  api_key?: string | null
  base_url?: string | null
}

export interface AITestResponse {
  success: boolean
  message?: string
  error?: string
  model?: string
}

export interface SaveAIConfigResponse {
  success: boolean
  error?: string
}

export const aiAPI = {
  getConfig: () => fetchAPI<AIConfigResponse>('/ai/config'),
  getStatus: () => fetchAPI<AIStatusResponse>('/ai/status'),
  saveConfig: (payload: SaveAIConfigPayload) =>
    fetchAPI<SaveAIConfigResponse>('/ai/config', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  testConnection: (payload: AITestPayload) =>
    fetchAPI<AITestResponse>('/ai/test', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
}

// Maintenance
export interface RestartResponse {
  message: string
}

export const maintenanceAPI = {
  restartContainer: () =>
    fetchAPI<RestartResponse>('/settings/restart', {
      method: 'POST',
    }),
}

export interface AlertConfigUpdatePayload extends NotificationChannelUpdatePayload {}

export interface NotificationLogEntry {
  id: number
  timestamp: string
  channel_type: NotificationChannelType
  alert_type: string
  message: string
  success: boolean
  error?: string | null
}

export interface NotificationTestResponse {
  status: string
  message?: string
}

// Audit logs
export interface AuditLogEntry {
  id: number
  timestamp: string
  user: string
  action: string
  resource_type: string
  resource_id?: number | null
  resource_name?: string | null
  changes?: Record<string, unknown> | null
  ip_address?: string | null
  status: string
  error_message?: string | null
}

export interface AuditStatsResponse {
  total_events: number
  days: number
  by_action: Record<string, number>
  by_resource_type: Record<string, number>
  by_status: Record<string, number>
  by_user: Record<string, number>
  success_rate: number
}

export interface AuditLogFilters {
  resourceType?: string
  action?: string
  days?: number
  limit?: number
}

export const auditAPI = {
  getLogs: ({ resourceType, action, days = 7, limit = 250 }: AuditLogFilters = {}) => {
    const params = new URLSearchParams({ days: String(days), limit: String(limit) })
    if (resourceType) params.append('resource_type', resourceType)
    if (action) params.append('action', action)
    return fetchAPI<AuditLogEntry[]>(`/audit/logs?${params.toString()}`)
  },
  getStats: (days: number = 7) => fetchAPI<AuditStatsResponse>(`/audit/stats?days=${days}`),
}

export const notificationsAPI = {
  getChannels: () => fetchAPI<NotificationChannel[]>('/notifications/channels'),
  getChannel: (channelType: NotificationChannelType) =>
    fetchAPI<NotificationChannel>(`/notifications/channels/${channelType}`),
  upsertChannel: (payload: NotificationChannelCreatePayload) =>
    fetchAPI<NotificationChannel>('/notifications/channels', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateChannel: (
    channelType: NotificationChannelType,
    payload: NotificationChannelUpdatePayload
  ) =>
    fetchAPI<NotificationChannel>(`/notifications/channels/${channelType}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  deleteChannel: (channelType: NotificationChannelType) =>
    fetchAPI<{ status: string }>(`/notifications/channels/${channelType}`, {
      method: 'DELETE',
    }),
  testChannel: (channelType: NotificationChannelType) =>
    fetchAPI<NotificationTestResponse>(`/notifications/test/${channelType}`, {
      method: 'POST',
    }),
  getAlerts: () => fetchAPI<AlertConfigItem[]>('/notifications/alerts'),
  upsertAlert: (payload: AlertConfigCreatePayload) =>
    fetchAPI<AlertConfigItem>('/notifications/alerts', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateAlert: (alertType: string, payload: AlertConfigUpdatePayload) =>
    fetchAPI<AlertConfigItem>(`/notifications/alerts/${alertType}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  getLogs: (limit: number = 100) =>
    fetchAPI<NotificationLogEntry[]>(`/notifications/logs?limit=${limit}`),
}

// Stratum (local CKPool stacks)
export interface StratumEvent {
  timestamp: string | null
  severity: string | null
  event_type: string | null
  source: string | null
  message: string | null
}

export interface StratumStatusResponse {
  timestamp: string
  coin: string
  pool: {
    id: number
    name: string
    pool_type: string
    stratum: {
      host: string
      port: number
    }
    manager: {
      base_url: string
    }
  }
  ready: any
  node: {
    mining: any
  }
  ckpool: {
    metrics: {
      summary: any
      shares: any
      blocks: any
    }
    events: StratumEvent[]
  }
  computed: {
    workers_online: number | null
    hashrate: any
    shares_total: number | null
    shares_24h: number | null
    shares_15m: number | null
    workers_down_for_s: number | null
    workers_min_15m: number | null
    workers_max_15m: number | null
    last_block_event: StratumEvent | null
  }
}

export const stratumAPI = {
  getStatus: (coin: 'DGB' | 'BCH' | 'BTC') => fetchAPI<StratumStatusResponse>(`/stratum/${coin}`),
}
