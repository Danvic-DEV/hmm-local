import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, AlertTriangle, CheckCircle2, RefreshCw, Waves } from 'lucide-react'

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  integrationsAPI,
  type HmmLocalStratumCandidateIncidentRow,
  poolsAPI,
  type HmmLocalStratumOperationalPool,
  type PoolRecoveryStatusPool,
  type PoolTileSet,
  type PoolTilesResponse,
} from '@/lib/api'

const STRATUM_POOL_TYPE = 'hmm_local_stratum'

function formatPercent(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return 'N/A'
  }
  return `${value.toFixed(2)}%`
}

function formatNumber(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return 'N/A'
  }
  return new Intl.NumberFormat().format(value)
}

function formatStorageMB(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return 'N/A'
  }
  if (value >= 1024) {
    return `${(value / 1024).toFixed(2)} GB`
  }
  return `${value.toFixed(1)} MB`
}

function getFreshness(lastUpdated: string | null): {
  label: 'Fresh' | 'Aging' | 'Stale' | 'Unknown'
  className: string
} {
  if (!lastUpdated) {
    return {
      label: 'Unknown',
      className: 'border-slate-700/50 bg-slate-900/30 text-slate-300',
    }
  }

  const timestamp = new Date(lastUpdated).getTime()
  if (Number.isNaN(timestamp)) {
    return {
      label: 'Unknown',
      className: 'border-slate-700/50 bg-slate-900/30 text-slate-300',
    }
  }

  const ageSeconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000))
  if (ageSeconds <= 60) {
    return {
      label: 'Fresh',
      className: 'border-emerald-700/50 bg-emerald-900/30 text-emerald-300',
    }
  }
  if (ageSeconds <= 180) {
    return {
      label: 'Aging',
      className: 'border-amber-700/60 bg-amber-900/30 text-amber-300',
    }
  }
  return {
    label: 'Stale',
    className: 'border-red-700/60 bg-red-900/30 text-red-300',
  }
}

function getRejectRisk(rejectRate: number | null | undefined): {
  label: 'Normal' | 'Elevated' | 'High' | 'Unknown'
  className: string
} {
  if (typeof rejectRate !== 'number' || !Number.isFinite(rejectRate)) {
    return {
      label: 'Unknown',
      className: 'border-slate-700/50 bg-slate-900/30 text-slate-300',
    }
  }
  if (rejectRate >= 5) {
    return {
      label: 'High',
      className: 'border-red-700/60 bg-red-900/30 text-red-300',
    }
  }
  if (rejectRate >= 3) {
    return {
      label: 'Elevated',
      className: 'border-amber-700/60 bg-amber-900/30 text-amber-300',
    }
  }
  return {
    label: 'Normal',
    className: 'border-emerald-700/50 bg-emerald-900/30 text-emerald-300',
  }
}

function formatTimeAgo(iso: string | null): string {
  if (!iso) {
    return 'Unknown'
  }

  const timestamp = new Date(iso).getTime()
  if (Number.isNaN(timestamp)) {
    return iso
  }

  const deltaSeconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000))
  if (deltaSeconds < 60) {
    return `${deltaSeconds}s ago`
  }

  const deltaMinutes = Math.floor(deltaSeconds / 60)
  if (deltaMinutes < 60) {
    return `${deltaMinutes}m ago`
  }

  const deltaHours = Math.floor(deltaMinutes / 60)
  if (deltaHours < 24) {
    return `${deltaHours}h ago`
  }

  const deltaDays = Math.floor(deltaHours / 24)
  return `${deltaDays}d ago`
}

function formatUtcDateTime(iso: string | null): string {
  if (!iso) {
    return 'Unknown'
  }
  const timestamp = new Date(iso)
  if (Number.isNaN(timestamp.getTime())) {
    return iso
  }
  return timestamp.toISOString().replace('T', ' ').replace('.000Z', 'Z')
}

function shortHash(value: string | null | undefined, chars: number = 8): string {
  if (!value) {
    return 'N/A'
  }
  if (value.length <= chars * 2 + 3) {
    return value
  }
  return `${value.slice(0, chars)}...${value.slice(-chars)}`
}

function getIncidentTone(incident: HmmLocalStratumCandidateIncidentRow): {
  label: string
  className: string
} {
  if (incident.accepted_by_node) {
    return {
      label: 'Accepted',
      className: 'border-emerald-700/50 bg-emerald-900/30 text-emerald-300',
    }
  }

  const category = String(incident.reject_category || '').toLowerCase()
  if (category.includes('invalid') || category.includes('bad') || category.includes('rpc_error')) {
    return {
      label: 'Rejected',
      className: 'border-red-700/60 bg-red-900/30 text-red-300',
    }
  }

  return {
    label: 'Inconclusive',
    className: 'border-amber-700/60 bg-amber-900/30 text-amber-300',
  }
}

export default function HmmLocalStratum() {
  const queryClient = useQueryClient()
  const [dashboardsEnabled, setDashboardsEnabled] = useState(false)
  const [banner, setBanner] = useState<{ tone: 'success' | 'error' | 'info'; message: string } | null>(null)

  const settingsQuery = useQuery({
    queryKey: ['integrations', 'hmm-local-stratum', 'settings'],
    queryFn: () => integrationsAPI.getHmmLocalStratumSettings(),
    staleTime: 30000,
  })

  useEffect(() => {
    if (typeof settingsQuery.data?.enabled === 'boolean') {
      setDashboardsEnabled(settingsQuery.data.enabled)
    }
  }, [settingsQuery.data?.enabled])

  const saveSettingsMutation = useMutation({
    mutationFn: () => integrationsAPI.saveHmmLocalStratumSettings(dashboardsEnabled),
    onSuccess: (response) => {
      setBanner({ tone: 'success', message: response.message || 'Settings saved' })
      queryClient.invalidateQueries({ queryKey: ['integrations', 'hmm-local-stratum', 'settings'] })
      queryClient.invalidateQueries({ queryKey: ['layout', 'stratum-dashboards-enabled'] })
      setTimeout(() => setBanner(null), 3500)
    },
    onError: (error: Error) => {
      setBanner({ tone: 'error', message: error.message || 'Failed to save settings' })
      setTimeout(() => setBanner(null), 3500)
    },
  })

  const tilesQuery = useQuery<PoolTilesResponse>({
    queryKey: ['integrations', 'hmm-local-stratum', 'tiles'],
    queryFn: () => poolsAPI.getPoolTiles(),
    refetchInterval: 30000,
    refetchOnWindowFocus: false,
    staleTime: 25000,
  })

  const recoveryQuery = useQuery({
    queryKey: ['integrations', 'hmm-local-stratum', 'recovery-status'],
    queryFn: () => poolsAPI.getRecoveryStatus(24),
    refetchInterval: 30000,
    refetchOnWindowFocus: false,
    staleTime: 25000,
  })

  const operationalQuery = useQuery({
    queryKey: ['integrations', 'hmm-local-stratum', 'operational'],
    queryFn: () => integrationsAPI.getHmmLocalStratumOperational(),
    refetchInterval: 30000,
    refetchOnWindowFocus: false,
    staleTime: 25000,
  })

  const incidentsQuery = useQuery({
    queryKey: ['integrations', 'hmm-local-stratum', 'candidate-incidents'],
    queryFn: () => integrationsAPI.getHmmLocalStratumCandidateIncidents(24, 50),
    refetchInterval: 30000,
    refetchOnWindowFocus: false,
    staleTime: 25000,
  })

  const stratumPools = useMemo(() => {
    return Object.values(tilesQuery.data || {}).filter(
      (tile): tile is PoolTileSet => tile.pool_type === STRATUM_POOL_TYPE
    )
  }, [tilesQuery.data])

  const recoveryByPoolId = useMemo(() => {
    const map = new Map<string, PoolRecoveryStatusPool>()
    for (const pool of recoveryQuery.data?.pools || []) {
      map.set(String(pool.pool_id), pool)
    }
    return map
  }, [recoveryQuery.data?.pools])

  const recoveryByPoolName = useMemo(() => {
    const map = new Map<string, PoolRecoveryStatusPool>()
    for (const pool of recoveryQuery.data?.pools || []) {
      const key = String(pool.pool_name || '').trim().toLowerCase()
      if (key) {
        map.set(key, pool)
      }
    }
    return map
  }, [recoveryQuery.data?.pools])

  const operationalByPoolId = useMemo(() => {
    const map = new Map<string, HmmLocalStratumOperationalPool>()
    for (const item of operationalQuery.data?.pools || []) {
      if (item.pool?.id !== null && item.pool?.id !== undefined) {
        map.set(String(item.pool.id), item)
      }
    }
    return map
  }, [operationalQuery.data?.pools])

  const operationalByPoolName = useMemo(() => {
    const map = new Map<string, HmmLocalStratumOperationalPool>()
    for (const item of operationalQuery.data?.pools || []) {
      const key = String(item.pool?.name || '').trim().toLowerCase()
      if (key) {
        map.set(key, item)
      }
    }
    return map
  }, [operationalQuery.data?.pools])

  const summary = useMemo(() => {
    const unhealthyPools = stratumPools.filter((pool) => pool.tile_1_health.health_status === false).length

    const recoveryTotals = stratumPools.reduce(
      (acc, pool) => {
        const recovery = recoveryByPoolId.get(String(pool.pool_id))
        acc.recovered += recovery?.recovered_count || 0
        acc.unresolved += recovery?.unresolved_count || 0
        return acc
      },
      { recovered: 0, unresolved: 0 }
    )

    const staleFeeds = stratumPools.filter((pool) => getFreshness(pool.last_updated).label === 'Stale').length
    const driverWarnings = stratumPools.reduce((acc, pool) => acc + (pool.warnings?.length || 0), 0)

    return {
      unhealthyPools,
      staleFeeds,
      driverWarnings,
      recovered: recoveryTotals.recovered,
      unresolved: recoveryTotals.unresolved,
    }
  }, [recoveryByPoolId, stratumPools])

  if (tilesQuery.isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Activity className="h-8 w-8 animate-spin text-blue-500" />
      </div>
    )
  }

  if (tilesQuery.isError) {
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-3 text-3xl font-semibold text-foreground">
          <Waves className="h-8 w-8 text-blue-400" />
          <span>HMM-Local Stratum</span>
        </div>
        <div className="rounded-lg border border-red-700 bg-red-900/30 p-4 text-red-200">
          Failed to load Stratum operational data.
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-3 text-3xl font-semibold text-foreground">
          <Waves className="h-8 w-8 text-blue-400" />
          <span>HMM-Local Stratum</span>
        </div>
        <p className="text-base text-muted-foreground">
          Dedicated operational status view for local Stratum integration pools.
        </p>
      </div>

      {banner && (
        <div
          className={`rounded-lg border px-4 py-3 text-sm ${
            banner.tone === 'success'
              ? 'border-emerald-700/60 bg-emerald-900/30 text-emerald-200'
              : banner.tone === 'error'
              ? 'border-red-700/60 bg-red-900/30 text-red-200'
              : 'border-blue-700/60 bg-blue-900/30 text-blue-200'
          }`}
        >
          {banner.message}
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>HMM-Local Stratum dashboards</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <label className="flex items-start gap-3 rounded-lg border border-slate-700/50 bg-slate-900/30 p-3">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4"
              checked={dashboardsEnabled}
              onChange={(event) => setDashboardsEnabled(event.target.checked)}
              disabled={settingsQuery.isLoading || saveSettingsMutation.isPending}
            />
            <div>
              <div className="text-sm font-medium text-slate-100">Enable dedicated HMM-Local Stratum dashboards</div>
              <div className="text-xs text-slate-400">
                When enabled, a new top-level navigation section appears with BTC, BCH and DGB pages focused on mining pool data.
              </div>
            </div>
          </label>

          <button
            type="button"
            className="inline-flex items-center rounded-md border border-blue-700/60 bg-blue-900/30 px-3 py-2 text-sm text-blue-200 hover:bg-blue-900/40 disabled:opacity-50"
            onClick={() => saveSettingsMutation.mutate()}
            disabled={saveSettingsMutation.isPending || settingsQuery.isLoading}
          >
            {saveSettingsMutation.isPending ? 'Saving…' : 'Save setting'}
          </button>
        </CardContent>
      </Card>

      {stratumPools.length === 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>No HMM-Local Stratum pool configured</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm text-muted-foreground">
            <p>
              Add or update a pool with driver <span className="font-semibold text-foreground">hmm_local_stratum</span> to populate this page.
            </p>
            <div>
              <Link className="text-blue-400 hover:text-blue-300 underline" to="/pools">
                Go to Pools
              </Link>
            </div>
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm text-muted-foreground">Configured pools</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="text-3xl font-semibold">{stratumPools.length}</div>
                <p className="mt-1 text-xs text-muted-foreground">{summary.unhealthyPools} degraded</p>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm text-muted-foreground">Data freshness risk</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="text-3xl font-semibold">{summary.staleFeeds}</div>
                <p className="mt-1 text-xs text-muted-foreground">Stale feeds (&gt; 3m old)</p>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm text-muted-foreground">Driver warnings</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="text-3xl font-semibold">{summary.driverWarnings}</div>
                <p className="mt-1 text-xs text-muted-foreground">Unresolved / not-loaded flags</p>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm text-muted-foreground">Driver recoveries (24h)</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="text-3xl font-semibold">{summary.recovered}</div>
                <p className="mt-1 text-xs text-muted-foreground">{summary.unresolved} unresolved</p>
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Candidate incidents (24h)</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {incidentsQuery.isLoading ? (
                <div className="text-sm text-slate-300">Loading candidate incidents…</div>
              ) : incidentsQuery.isError ? (
                <div className="rounded-lg border border-amber-700/60 bg-amber-900/30 p-3 text-sm text-amber-300">
                  Failed to load candidate incidents: {(incidentsQuery.error as Error)?.message || 'Unknown error'}
                </div>
              ) : (
                <>
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="inline-flex rounded-full border border-slate-700/60 bg-slate-900/40 px-2 py-1 text-slate-300">
                      Accepted: {incidentsQuery.data?.summary.accepted ?? 0}
                    </span>
                    <span className="inline-flex rounded-full border border-slate-700/60 bg-slate-900/40 px-2 py-1 text-slate-300">
                      Rejected: {incidentsQuery.data?.summary.rejected ?? 0}
                    </span>
                    <span className="inline-flex rounded-full border border-slate-700/60 bg-slate-900/40 px-2 py-1 text-slate-300">
                      Rows: {incidentsQuery.data?.count ?? 0}
                    </span>
                    <span className="inline-flex rounded-full border border-slate-700/60 bg-slate-900/40 px-2 py-1 text-slate-400">
                      Updated {formatTimeAgo(incidentsQuery.data?.fetched_at || null)}
                    </span>
                  </div>

                  {(incidentsQuery.data?.fetch_errors?.length || 0) > 0 && (
                    <div className="rounded-lg border border-amber-700/60 bg-amber-900/30 p-3 text-xs text-amber-300">
                      Some Stratum endpoints were unavailable. Showing partial results.
                    </div>
                  )}

                  {(incidentsQuery.data?.rows?.length || 0) === 0 ? (
                    <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-4 text-sm text-slate-300">
                      No candidate incidents in the last 24 hours.
                    </div>
                  ) : (
                    <div className="overflow-x-auto rounded-lg border border-slate-700/60">
                      <table className="min-w-full text-left text-xs">
                        <thead className="bg-slate-900/60 text-slate-300">
                          <tr>
                            <th className="px-3 py-2 font-medium">Time (UTC)</th>
                            <th className="px-3 py-2 font-medium">Coin</th>
                            <th className="px-3 py-2 font-medium">Worker</th>
                            <th className="px-3 py-2 font-medium">Job</th>
                            <th className="px-3 py-2 font-medium">Template</th>
                            <th className="px-3 py-2 font-medium">Block</th>
                            <th className="px-3 py-2 font-medium">Result</th>
                            <th className="px-3 py-2 font-medium">Category</th>
                            <th className="px-3 py-2 font-medium">Latency</th>
                            <th className="px-3 py-2 font-medium">Variant</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(incidentsQuery.data?.rows || []).map((incident) => {
                            const tone = getIncidentTone(incident)
                            return (
                              <tr key={`${incident.ts}-${incident.coin}-${incident.job_id || 'no-job'}`} className="border-t border-slate-800/70 text-slate-200">
                                <td className="px-3 py-2 whitespace-nowrap">{formatUtcDateTime(incident.ts)}</td>
                                <td className="px-3 py-2 whitespace-nowrap">{incident.coin || 'N/A'}</td>
                                <td className="px-3 py-2" title={incident.worker || ''}>{shortHash(incident.worker, 10)}</td>
                                <td className="px-3 py-2 whitespace-nowrap">{incident.job_id || 'N/A'}</td>
                                <td className="px-3 py-2 whitespace-nowrap">{incident.template_height ?? 'N/A'}</td>
                                <td className="px-3 py-2" title={incident.block_hash || ''}>{shortHash(incident.block_hash, 10)}</td>
                                <td className="px-3 py-2 whitespace-nowrap">
                                  <span className={`inline-flex rounded-full border px-2 py-1 ${tone.className}`}>{tone.label}</span>
                                </td>
                                <td className="px-3 py-2 whitespace-nowrap">{incident.reject_category || 'accepted'}</td>
                                <td className="px-3 py-2 whitespace-nowrap">
                                  {typeof incident.latency_ms === 'number' ? `${incident.latency_ms.toFixed(2)} ms` : 'N/A'}
                                </td>
                                <td className="px-3 py-2" title={incident.matched_variant || ''}>{incident.matched_variant || 'N/A'}</td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}
                </>
              )}
            </CardContent>
          </Card>

          <div className="space-y-4">
            {stratumPools.map((pool) => {
              const poolId = String(pool.pool_id || '').trim()
              const poolNameKey = String(pool.display_name || '').trim().toLowerCase()
              const recovery =
                (poolId ? recoveryByPoolId.get(poolId) : undefined) ||
                recoveryByPoolName.get(poolNameKey) ||
                (stratumPools.length === 1 && (recoveryQuery.data?.pools?.length || 0) === 1
                  ? recoveryQuery.data?.pools?.[0]
                  : undefined)
              const operational =
                (poolId ? operationalByPoolId.get(poolId) : undefined) ||
                operationalByPoolName.get(poolNameKey) ||
                (stratumPools.length === 1 && (operationalQuery.data?.pools?.length || 0) === 1
                  ? operationalQuery.data?.pools?.[0]
                  : undefined)
              const datastore = operational?.stats?.datastore
              const dbHealth = operational?.database
              const dbPool = dbHealth?.pool
              const dbPg = dbHealth?.postgresql
              const dbHwm24 = dbHealth?.high_water_marks?.last_24h
              const dbHwmBoot = dbHealth?.high_water_marks?.since_boot
              const runtimeCoin = String(pool.tile_4_blocks.currency || '').toUpperCase()
              const runtime = runtimeCoin ? operational?.stats?.coins?.[runtimeCoin] : undefined

              const batchesOk = Number(datastore?.total_write_batches_ok || 0)
              const batchesFailed = Number(datastore?.total_write_batches_failed || 0)
              const batchTotal = batchesOk + batchesFailed
              const batchSuccessPct = batchTotal > 0 ? (batchesOk / batchTotal) * 100 : null

              const sharesAccepted = Number(runtime?.shares_accepted || 0)
              const sharesRejected = Number(runtime?.shares_rejected || 0)
              const sharesTotal = sharesAccepted + sharesRejected
              const shareAcceptPct = sharesTotal > 0 ? (sharesAccepted / sharesTotal) * 100 : null

              const blocksCandidates = Number(runtime?.block_candidates || 0)
              const blocksRejected = Number(runtime?.blocks_rejected || 0)

              const freshness = getFreshness(pool.last_updated)
              const rejectRisk = getRejectRisk(pool.tile_3_shares.reject_rate)

              return (
                <Card key={pool.pool_id}>
                  <CardHeader className="space-y-2">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <CardTitle className="text-lg">{pool.display_name}</CardTitle>
                      <div className="flex items-center gap-2 text-xs">
                        <span className="inline-flex items-center gap-1 rounded-full border border-slate-700 bg-slate-900/60 px-2 py-1 text-slate-300">
                          <RefreshCw className="h-3 w-3" /> Updated {formatTimeAgo(pool.last_updated)}
                        </span>
                        <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-1 ${freshness.className}`}>
                          Feed {freshness.label}
                        </span>
                        {pool.tile_1_health.health_status ? (
                          <span className="inline-flex items-center gap-1 rounded-full border border-emerald-700/50 bg-emerald-900/30 px-2 py-1 text-emerald-300">
                            <CheckCircle2 className="h-3 w-3" /> Healthy
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 rounded-full border border-amber-700/60 bg-amber-900/30 px-2 py-1 text-amber-300">
                            <AlertTriangle className="h-3 w-3" /> Degraded
                          </span>
                        )}
                      </div>
                    </div>
                    {(pool.warnings || []).length > 0 && (
                      <div className="flex flex-wrap gap-2">
                        {(pool.warnings || []).map((warning) => (
                          <span
                            key={`${pool.pool_id}-${warning}`}
                            className="inline-flex rounded-full border border-amber-700/60 bg-amber-900/30 px-2 py-1 text-xs text-amber-300"
                          >
                            {warning}
                          </span>
                        ))}
                      </div>
                    )}
                  </CardHeader>

                  <CardContent className="space-y-4">
                    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                      <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                        <div className="text-xs uppercase tracking-wide text-slate-400">Service health</div>
                        <div className="mt-1 text-sm text-slate-100">{pool.tile_1_health.health_message || 'No message'}</div>
                        <div className="mt-1 text-xs text-slate-400">Latency: {pool.tile_1_health.latency_ms ?? 'N/A'} ms</div>
                      </div>

                      <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                        <div className="text-xs uppercase tracking-wide text-slate-400">Operational info (/stats)</div>
                        {operationalQuery.isLoading ? (
                          <div className="mt-1 text-sm text-slate-300">Loading operational stats…</div>
                        ) : operationalQuery.isError ? (
                          <>
                            <div className="mt-1 text-sm text-amber-300">Unavailable</div>
                            <div className="mt-1 text-xs text-slate-400">{(operationalQuery.error as Error)?.message || 'Operational query failed'}</div>
                          </>
                        ) : operational?.status !== 'ok' ? (
                          <>
                            <div className="mt-1 text-sm text-amber-300">Unavailable</div>
                            <div className="mt-1 text-xs text-slate-400">{operational?.error || 'No response from Stratum API'}</div>
                          </>
                        ) : (
                          <>
                            <div className="mt-1 text-sm text-slate-100">
                              Queue: {formatNumber(datastore?.queue_depth)} / max seen {formatNumber(datastore?.max_queue_depth_seen)}
                            </div>
                            <div className="mt-1 text-xs text-slate-400">
                              Rows written: {formatNumber(datastore?.total_rows_written)} · Dropped: {formatNumber(datastore?.total_dropped)}
                            </div>
                            <div className="text-xs text-slate-400">
                              Batches ok/fail: {formatNumber(datastore?.total_write_batches_ok)} / {formatNumber(datastore?.total_write_batches_failed)}
                            </div>
                            <div className="text-xs text-slate-400">
                              Enqueued: {formatNumber(datastore?.total_enqueued)} · Spooled/Replayed: {formatNumber(datastore?.total_spooled_rows)} / {formatNumber(datastore?.total_replayed_rows)}
                            </div>
                            <div className="text-xs text-slate-400">
                              Retries: {formatNumber(datastore?.total_retries)} · Consecutive failures: {formatNumber(datastore?.consecutive_write_failures)}
                            </div>
                            <div className="text-xs text-slate-400">
                              Last write: {formatTimeAgo(datastore?.last_write_ok_at || null)} · Latency: {datastore?.last_write_latency_ms?.toFixed(2) ?? 'N/A'} ms
                            </div>
                            <div className="text-xs text-slate-400">
                              DB: {operational.stats?.db_enabled ? 'enabled' : 'disabled'} · Datastore: {datastore?.enabled ? 'enabled' : 'disabled'}
                            </div>
                            <div className="text-xs text-slate-400">
                              Retention (d): H {formatNumber(datastore?.hashrate_retention_days)} · N {formatNumber(datastore?.network_retention_days)} · K {formatNumber(datastore?.kpi_retention_days)}
                            </div>
                            {datastore?.spool_path && (
                              <div className="truncate text-xs text-slate-500" title={datastore.spool_path}>
                                Spool path: {datastore.spool_path}
                              </div>
                            )}
                          </>
                        )}
                      </div>

                      <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                        <div className="text-xs uppercase tracking-wide text-slate-400">Quality / reliability</div>
                        <div className="mt-1 flex items-center gap-2">
                          <span className={`inline-flex rounded-full border px-2 py-1 text-xs ${rejectRisk.className}`}>
                            Reject risk: {rejectRisk.label}
                          </span>
                        </div>
                        <div className="mt-1 text-xs text-slate-400">
                          Reject rate: {formatPercent(pool.tile_3_shares.reject_rate)}
                        </div>
                        <div className="text-xs text-slate-400">
                          Valid {pool.tile_3_shares.shares_valid ?? 'N/A'} · Invalid {pool.tile_3_shares.shares_invalid ?? 'N/A'}
                        </div>
                      </div>

                      <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                        <div className="text-xs uppercase tracking-wide text-slate-400">Driver recovery (24h)</div>
                        <div className="mt-1 text-sm text-slate-100">Recovered: {recovery?.recovered_count || 0}</div>
                        <div className="mt-1 text-xs text-slate-400">Unresolved: {recovery?.unresolved_count || 0}</div>
                        <div className="text-xs text-slate-400">
                          Last event: {recovery?.last_event_at ? formatTimeAgo(recovery.last_event_at) : 'None'}
                        </div>
                      </div>
                    </div>

                    {operational?.status === 'ok' && (
                      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
                        <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                          <div className="text-xs text-slate-400">Queue pressure</div>
                          <div className="mt-1 text-xl font-semibold text-slate-100">{formatNumber(datastore?.queue_depth)}</div>
                          <div className="text-xs text-slate-400">
                            max seen {formatNumber(datastore?.max_queue_depth_seen)}
                          </div>
                          <div className="mt-1 text-xs text-slate-400">
                            dropped {formatNumber(datastore?.total_dropped)} · retries {formatNumber(datastore?.total_retries)}
                          </div>
                        </div>

                        <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                          <div className="text-xs text-slate-400">Write reliability</div>
                          <div className="mt-1 text-xl font-semibold text-slate-100">
                            {batchSuccessPct !== null ? `${batchSuccessPct.toFixed(1)}%` : 'N/A'}
                          </div>
                          <div className="text-xs text-slate-400">
                            ok/fail {formatNumber(datastore?.total_write_batches_ok)} / {formatNumber(datastore?.total_write_batches_failed)}
                          </div>
                          <div className="mt-1 text-xs text-slate-400">
                            last write {formatTimeAgo(datastore?.last_write_ok_at || null)} · {datastore?.last_write_latency_ms?.toFixed(1) ?? 'N/A'} ms
                          </div>
                        </div>

                        <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                          <div className="text-xs text-slate-400">Share pipeline{runtimeCoin ? ` (${runtimeCoin})` : ''}</div>
                          <div className="mt-1 text-xl font-semibold text-slate-100">{shareAcceptPct !== null ? `${shareAcceptPct.toFixed(1)}%` : 'N/A'}</div>
                          <div className="text-xs text-slate-400">accept rate</div>
                          <div className="mt-1 text-xs text-slate-400">
                            accepted {formatNumber(runtime?.shares_accepted)} · rejected {formatNumber(runtime?.shares_rejected)}
                          </div>
                          <div className="text-xs text-slate-400">
                            workers {formatNumber(runtime?.connected_workers)} · last share {formatTimeAgo(runtime?.last_share_at || null)}
                          </div>
                        </div>

                        <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                          <div className="text-xs text-slate-400">Block pipeline{runtimeCoin ? ` (${runtimeCoin})` : ''}</div>
                          <div className="mt-1 text-xl font-semibold text-slate-100">{formatNumber(runtime?.blocks_accepted)}</div>
                          <div className="text-xs text-slate-400">accepted by node</div>
                          <div className="mt-1 text-xs text-slate-400">
                            candidates {formatNumber(blocksCandidates)} · rejected {formatNumber(blocksRejected)}
                          </div>
                          <div className="text-xs text-slate-400 truncate" title={runtime?.last_block_submit_result || ''}>
                            last result {runtime?.last_block_submit_result || 'N/A'}
                          </div>
                        </div>
                      </div>
                    )}

                    <div className="border-t border-slate-700/60 pt-4">
                      <div className="mb-3 text-xs uppercase tracking-wide text-slate-400">Stratum DB health</div>
                      {operational?.database_status !== 'ok' ? (
                        <div className="rounded-lg border border-amber-700/60 bg-amber-900/30 p-3 text-xs text-amber-300">
                          Unavailable: {operational?.database_error || 'No DB health response'}
                        </div>
                      ) : (
                        <>
                          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
                            <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                              <div className="text-xs text-slate-400">Connection Pool</div>
                              <div className="mt-1 flex items-baseline gap-2">
                                <span className="text-xl font-semibold text-slate-100">{dbPool?.utilization_percent?.toFixed(1) ?? 'N/A'}%</span>
                                <span className="text-xs text-slate-400">utilized</span>
                              </div>
                              <div className="mt-1 text-xs text-slate-400">
                                {formatNumber(dbPool?.checked_out)}/{formatNumber(dbPool?.total_capacity)} ({formatNumber(dbPool?.max_capacity_configured ?? dbPool?.total_capacity)} max)
                              </div>
                            </div>

                            <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                              <div className="text-xs text-slate-400">Active Queries</div>
                              <div className="mt-1 text-xl font-semibold text-slate-100">{formatNumber(dbPg?.active_connections)}</div>
                              <div className="mt-1 text-xs text-slate-400">Slow (&gt;1m): {formatNumber(dbPg?.long_running_queries)}</div>
                            </div>

                            <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                              <div className="text-xs text-slate-400">Database Size</div>
                              <div className="mt-1 text-xl font-semibold text-slate-100">{formatStorageMB(dbPg?.database_size_mb)}</div>
                              <div className="mt-1 text-xs text-slate-400">total storage</div>
                            </div>

                            <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                              <div className="text-xs text-slate-400">Health Status</div>
                              <div className="mt-1">
                                <span className={`inline-flex items-center rounded-full border px-2 py-1 text-xs ${
                                  dbHealth?.status === 'healthy'
                                    ? 'border-emerald-700/50 bg-emerald-900/30 text-emerald-300'
                                    : dbHealth?.status === 'warning'
                                    ? 'border-amber-700/60 bg-amber-900/30 text-amber-300'
                                    : 'border-red-700/60 bg-red-900/30 text-red-300'
                                }`}>
                                  {(dbHealth?.status || 'unknown').toUpperCase()}
                                </span>
                              </div>
                            </div>
                          </div>

                          <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-4">
                            <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                              <div className="text-xs text-slate-400">High-water marks (last 24h)</div>
                              <div className="mt-1 text-xs text-slate-500">Since {dbHealth?.high_water_marks?.last_24h_date || 'N/A'}</div>
                              <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-slate-300">
                                <div>Pool peak: {formatNumber(dbHwm24?.db_pool_in_use_peak)}</div>
                                <div>Active peak: {formatNumber(dbHwm24?.active_queries_peak)}</div>
                                <div>Wait count: {formatNumber(dbHwm24?.db_pool_wait_count)}</div>
                                <div>Wait sec: {dbHwm24?.db_pool_wait_seconds_sum?.toFixed(1) ?? 'N/A'}s</div>
                                <div>Slow queries: {formatNumber(dbHwm24?.slow_queries_peak)}</div>
                              </div>
                            </div>

                            <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                              <div className="text-xs text-slate-400">High-water marks (since boot)</div>
                              <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-slate-300">
                                <div>Pool peak: {formatNumber(dbHwmBoot?.db_pool_in_use_peak)}</div>
                                <div>Active peak: {formatNumber(dbHwmBoot?.active_queries_peak)}</div>
                                <div>Wait count: {formatNumber(dbHwmBoot?.db_pool_wait_count)}</div>
                                <div>Wait sec: {dbHwmBoot?.db_pool_wait_seconds_sum?.toFixed(1) ?? 'N/A'}s</div>
                                <div>Slow queries: {formatNumber(dbHwmBoot?.slow_queries_peak)}</div>
                              </div>
                            </div>
                          </div>
                        </>
                      )}
                    </div>
                  </CardContent>
                </Card>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}
