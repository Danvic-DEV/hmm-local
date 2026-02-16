import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, AlertTriangle, CheckCircle2, RefreshCw, Waves } from 'lucide-react'

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { integrationsAPI, poolsAPI, type PoolRecoveryStatusPool, type PoolTileSet, type PoolTilesResponse } from '@/lib/api'

const STRATUM_POOL_TYPE = 'hmm_local_stratum'

function formatPercent(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return 'N/A'
  }
  return `${value.toFixed(2)}%`
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

  const stratumPools = useMemo(() => {
    return Object.values(tilesQuery.data || {}).filter(
      (tile): tile is PoolTileSet => tile.pool_type === STRATUM_POOL_TYPE
    )
  }, [tilesQuery.data])

  const recoveryByPoolId = useMemo(() => {
    const map = new Map<number, PoolRecoveryStatusPool>()
    for (const pool of recoveryQuery.data?.pools || []) {
      map.set(pool.pool_id, pool)
    }
    return map
  }, [recoveryQuery.data?.pools])

  const summary = useMemo(() => {
    const unhealthyPools = stratumPools.filter((pool) => pool.tile_1_health.health_status === false).length

    const recoveryTotals = stratumPools.reduce(
      (acc, pool) => {
        const poolId = Number.parseInt(pool.pool_id, 10)
        const recovery = Number.isNaN(poolId) ? undefined : recoveryByPoolId.get(poolId)
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

          <div className="space-y-4">
            {stratumPools.map((pool) => {
              const poolId = Number.parseInt(pool.pool_id, 10)
              const recovery = Number.isNaN(poolId) ? undefined : recoveryByPoolId.get(poolId)
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

                  <CardContent className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                    <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                      <div className="text-xs uppercase tracking-wide text-slate-400">Service health</div>
                      <div className="mt-1 text-sm text-slate-100">{pool.tile_1_health.health_message || 'No message'}</div>
                      <div className="mt-1 text-xs text-slate-400">Latency: {pool.tile_1_health.latency_ms ?? 'N/A'} ms</div>
                    </div>

                    <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">
                      <div className="text-xs uppercase tracking-wide text-slate-400">Pipeline status</div>
                      <div className="mt-1 text-sm text-slate-100">Last update: {formatTimeAgo(pool.last_updated)}</div>
                      <div className="mt-1 text-xs text-slate-400">Workers online: {pool.tile_2_network.active_workers ?? 'N/A'}</div>
                      <div className="text-xs text-slate-400">Feed state: {freshness.label}</div>
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
