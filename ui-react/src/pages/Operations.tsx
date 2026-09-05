import { useQuery } from '@tanstack/react-query'
import { Activity, AlertCircle, Gauge, Layers, ServerCog, ShieldAlert, Sparkles, HardDrive, TrendingUp, Zap } from 'lucide-react'
import { poolsAPI, type PoolRecoveryStatusResponse, type PoolTilesResponse } from '@/lib/api'

interface DatabaseHealth {
  status: string
  pool: {
    size: number
    checked_out: number
    overflow: number
    total_capacity: number
    max_size_configured?: number
    max_overflow_configured?: number
    max_capacity_configured?: number
    utilization_percent: number
  }
  database_type: string
  postgresql?: {
    active_connections: number
    database_size_mb: number
    long_running_queries: number
  }
  high_water_marks?: {
    last_24h_date: string
    last_24h: {
      db_pool_in_use_peak: number
      db_pool_wait_count: number
      db_pool_wait_seconds_sum: number
      active_queries_peak: number
      slow_query_count: number
    }
    since_boot: {
      db_pool_in_use_peak: number
      db_pool_wait_count: number
      db_pool_wait_seconds_sum: number
      active_queries_peak: number
      slow_query_count: number
    }
  }
}

interface OperationsStatus {
  automation_rules: Array<{
    id: number
    name: string
    trigger_type: string
    trigger_config: Record<string, unknown>
    action_type: string
    action_config: Record<string, unknown>
    priority: number
  }>
  strategy: {
    enabled: boolean
    current_price_band: string | null
    current_band_sort_order: number | null
    champion_mode_enabled: boolean
    current_champion_miner_name: string | null
    last_action_time: string | null
    last_price_checked: number | null
    enrolled_miners: Array<{ id: number; name: string; type: string }>
  }
  ha: {
    unstable: boolean
    detail: {
      enabled: boolean
      last_success?: string | null
      downtime_start?: string | null
      alerts_sent?: number
    }
  }
  telemetry: {
    backlog_current: number
    metrics: {
      last_24h_date: string
      last_24h: {
        peak_concurrency: number
        max_backlog: number
      }
      since_boot: {
        peak_concurrency: number
        max_backlog: number
      }
    }
  }
  db_pool: {
    checked_out: number
    total_capacity: number
    utilization_percent: number
    high_water: {
      last_24h_date: string
      last_24h: {
        db_pool_in_use_peak: number
        db_pool_wait_count: number
        db_pool_wait_seconds_sum: number
        active_queries_peak: number
        slow_query_count: number
      }
      since_boot: {
        db_pool_in_use_peak: number
        db_pool_wait_count: number
        db_pool_wait_seconds_sum: number
        active_queries_peak: number
        slow_query_count: number
      }
    }
  }
  modes: {
    ramp_up: boolean
    throttling_writes: boolean
    ha_unstable: boolean
  }
}

export default function Operations() {
  const tileClass = 'bg-gray-900/80 rounded-xl border border-gray-800'

  const { data: dbHealth } = useQuery<DatabaseHealth>({
    queryKey: ['database-health'],
    queryFn: async () => {
      const response = await fetch('/api/health/database')
      if (!response.ok) {
        throw new Error('Failed to fetch database health')
      }
      return response.json()
    },
    refetchInterval: 30000,
    refetchOnWindowFocus: false,
    staleTime: 25000,
  })

  const { data, isLoading, isError } = useQuery<OperationsStatus>({
    queryKey: ['operations-status'],
    queryFn: async () => {
      const response = await fetch('/api/operations/status')
      const payload = await response.json()
      if (!response.ok || payload.error) {
        throw new Error(payload.error || 'Failed to load operations status')
      }
      return payload
    },
    refetchInterval: 5000
  })

  const { data: poolRecoveryStatus } = useQuery<PoolRecoveryStatusResponse>({
    queryKey: ['pools', 'recovery-status', 'operations'],
    queryFn: () => poolsAPI.getRecoveryStatus(24),
    refetchInterval: 30000,
    refetchOnWindowFocus: false,
    staleTime: 25000,
  })

  const { data: poolTiles } = useQuery<PoolTilesResponse>({
    queryKey: ['pools', 'tiles', 'operations'],
    queryFn: () => poolsAPI.getPoolTiles(),
    refetchInterval: 30000,
    refetchOnWindowFocus: false,
    staleTime: 25000,
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Activity className="h-8 w-8 animate-spin text-blue-500" />
      </div>
    )
  }

  if (isError || !data) {
    return (
      <div className="rounded-lg border border-red-700 bg-red-900/30 p-4 text-red-200">
        Failed to load operations status.
      </div>
    )
  }

  const { automation_rules, strategy, ha, telemetry, db_pool } = data
  const modes = data.modes ?? {
    ramp_up: false,
    throttling_writes: false,
    ha_unstable: false,
  }

  const rejectRates = Object.values(poolTiles || {})
    .map((pool) => pool.tile_3_shares?.reject_rate)
    .filter((rate): rate is number => typeof rate === 'number' && Number.isFinite(rate))
  const maxRejectRate = rejectRates.length > 0 ? Math.max(...rejectRates) : null
  const poolsOverAmber = rejectRates.filter((rate) => rate >= 3).length
  const poolsOverRed = rejectRates.filter((rate) => rate >= 5).length

  const rejectRiskLabel =
    maxRejectRate === null ? 'No data' :
    maxRejectRate >= 5 ? 'High' :
    maxRejectRate >= 3 ? 'Elevated' : 'Normal'
  const rejectRiskClass =
    maxRejectRate === null ? 'bg-slate-800/60 text-slate-300 border border-slate-700/40' :
    maxRejectRate >= 5 ? 'bg-red-900/40 text-red-300 border border-red-700/60' :
    maxRejectRate >= 3 ? 'bg-amber-900/40 text-amber-300 border border-amber-700/60' :
    'bg-emerald-900/30 text-emerald-300 border border-emerald-700/40'

  const MiniSparkBars = ({
    values,
    labels,
    color = 'bg-blue-500'
  }: {
    values: number[]
    labels: string[]
    color?: string
  }) => {
    const max = Math.max(...values, 1)

    return (
      <div className="flex items-end gap-2">
        {values.map((value, index) => (
          <div key={`${labels[index]}-${index}`} className="flex flex-col items-center gap-1">
            <div className={`w-3 rounded-full ${color}`} style={{ height: `${(value / max) * 36 + 6}px` }} />
            <span className="text-[10px] text-gray-400">{labels[index]}</span>
          </div>
        ))}
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="rounded-2xl border border-gray-800 bg-gradient-to-r from-blue-950/60 via-slate-900/80 to-gray-900/60 p-6">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <ServerCog className="h-7 w-7 text-blue-300" />
            <div>
              <h1 className="text-2xl font-bold text-white">Operations</h1>
              <p className="text-sm text-gray-400">Real-time system posture & decision trace</p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <span className={`rounded-full px-3 py-1 text-xs font-semibold ${modes.ramp_up ? 'bg-yellow-900/40 text-yellow-300 border border-yellow-700/60' : 'bg-emerald-900/30 text-emerald-300 border border-emerald-700/40'}`}>
              {modes.ramp_up ? 'Ramp-up' : 'Stable'}
            </span>
            <span className={`rounded-full px-3 py-1 text-xs font-semibold ${modes.throttling_writes ? 'bg-red-900/40 text-red-300 border border-red-700/60' : 'bg-slate-800/60 text-slate-300 border border-slate-700/40'}`}>
              {modes.throttling_writes ? 'Write Throttling' : 'Writes OK'}
            </span>
            <span className={`rounded-full px-3 py-1 text-xs font-semibold ${modes.ha_unstable ? 'bg-orange-900/40 text-orange-300 border border-orange-700/60' : 'bg-slate-800/60 text-slate-300 border border-slate-700/40'}`}>
              {modes.ha_unstable ? 'HA Degraded' : 'HA Stable'}
            </span>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-3">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-400">Live state</h2>
        </div>
        <div className="bg-gray-900/80 rounded-xl p-5 border border-gray-800 shadow-lg shadow-blue-500/5">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-sm font-medium text-gray-400">Active automation rules</h3>
            <Layers className="h-4 w-4 text-blue-400" />
          </div>
          {automation_rules.length === 0 ? (
            <div className="text-sm text-gray-500">No active rules.</div>
          ) : (
            <ul className="space-y-2 text-sm">
              {automation_rules.map((rule) => (
                <li key={rule.id} className="rounded-md border border-gray-700 bg-gray-900/50 p-2">
                  <div className="text-white font-semibold">{rule.name}</div>
                  <div className="text-xs text-gray-400">
                    Trigger: {rule.trigger_type} • Action: {rule.action_type}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="bg-gray-900/80 rounded-xl p-5 border border-gray-800 shadow-lg shadow-purple-500/5">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-sm font-medium text-gray-400">Current strategy per miner</h3>
            <Gauge className="h-4 w-4 text-purple-400" />
          </div>
          {strategy.enrolled_miners.length === 0 ? (
            <div className="text-sm text-gray-500">No miners enrolled.</div>
          ) : (
            <ul className="space-y-2 text-sm">
              {strategy.enrolled_miners.map((miner) => (
                <li key={miner.id} className="flex items-center justify-between rounded-md border border-gray-700 bg-gray-900/50 p-2">
                  <span className="text-white">{miner.name}</span>
                  <span className="text-xs text-gray-400">Price Band Strategy</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="bg-gray-900/80 rounded-xl p-5 border border-gray-800 shadow-lg shadow-emerald-500/5">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-sm font-medium text-gray-400">Last decision</h3>
            <Activity className="h-4 w-4 text-green-400" />
          </div>
          <div className="space-y-2 text-sm">
            <div className="text-white">
              Band: {strategy.current_price_band ?? 'N/A'}
              {strategy.current_band_sort_order !== null && (
                <span className="text-gray-500 ml-1">(Band {strategy.current_band_sort_order})</span>
              )}
            </div>
            {strategy.champion_mode_enabled && strategy.current_band_sort_order === 5 && strategy.current_champion_miner_name && (
              <div className="text-purple-300">
                Champion: {strategy.current_champion_miner_name}
              </div>
            )}
            <div className="text-gray-400">
              Last action: {strategy.last_action_time ?? 'N/A'}
            </div>
            <div className="text-gray-400">
              Last price: {strategy.last_price_checked ?? 'N/A'}
            </div>
          </div>
        </div>
      </div>

      <div className="space-y-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-400">Recovery & alerts</h2>

        {poolRecoveryStatus && (
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
            <div className={`${tileClass} p-5`}>
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-medium text-gray-300">Pool driver recovery (24h)</h3>
                <ShieldAlert className="h-4 w-4 text-amber-400" />
              </div>
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="rounded bg-green-500/15 px-2 py-1 text-green-300">
                  Recovered: {poolRecoveryStatus.totals.recovered}
                </span>
                <span className="rounded bg-amber-500/15 px-2 py-1 text-amber-300">
                  Unresolved: {poolRecoveryStatus.totals.unresolved}
                </span>
              </div>
              <p className="mt-3 text-xs text-gray-500">
                Window: last {poolRecoveryStatus.window_hours}h
              </p>
            </div>

            <div className={`${tileClass} p-5`}>
              <h3 className="text-sm font-medium text-gray-300 mb-3">Recent pool recovery activity</h3>
              {poolRecoveryStatus.pools.length === 0 ? (
                <p className="text-sm text-gray-500">No recovery events recorded in this window.</p>
              ) : (
                <div className="space-y-2">
                  {poolRecoveryStatus.pools.slice(0, 8).map((item) => (
                    <div key={item.pool_id} className="rounded border border-gray-700 bg-gray-900/50 p-2">
                      <div className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="font-medium text-white">{item.pool_name}</span>
                        <span className="rounded bg-green-500/10 px-1.5 py-0.5 text-xs text-green-300">recovered {item.recovered_count}</span>
                        <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-xs text-amber-300">unresolved {item.unresolved_count}</span>
                      </div>
                      {item.last_message && (
                        <div className="mt-1 text-xs text-gray-400 truncate" title={item.last_message}>{item.last_message}</div>
                      )}
                      {item.last_event_at && (
                        <div className="mt-1 text-[11px] text-gray-500">Last event: {item.last_event_at}</div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className={`${tileClass} p-5`}>
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-medium text-gray-300">Pool reject-rate risk</h3>
                <AlertCircle className="h-4 w-4 text-amber-400" />
              </div>
              <div className="space-y-2 text-sm">
                <span className={`inline-flex rounded-full px-3 py-1 text-xs font-semibold ${rejectRiskClass}`}>
                  {rejectRiskLabel}
                </span>
                <div className="text-gray-300">
                  Max reject rate: {maxRejectRate !== null ? `${maxRejectRate.toFixed(2)}%` : 'N/A'}
                </div>
                <div className="text-xs text-gray-500">
                  Pools ≥3%: {poolsOverAmber} • Pools ≥5%: {poolsOverRed}
                </div>
                <div className="text-xs text-gray-500">
                  Thresholds: amber ≥3% • red ≥5%
                </div>
              </div>
            </div>
          </div>
        )}

      </div>

      {dbHealth && (
        <>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-400">Capacity & database</h2>
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
            <div className={`${tileClass} p-4`}>
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-medium text-gray-400">Connection Pool</h3>
                <Activity className={`h-4 w-4 ${
                  dbHealth.status === 'critical' ? 'text-red-400' :
                  dbHealth.status === 'warning' ? 'text-yellow-400' :
                  'text-green-400'
                }`} />
              </div>
              <div className="space-y-2">
                <div className="flex items-baseline space-x-2">
                  <span className="text-2xl font-bold text-white">
                    {dbHealth.pool.utilization_percent}%
                  </span>
                  <span className="text-sm text-gray-400">utilized</span>
                </div>
                <div className="w-full bg-gray-700 rounded-full h-2">
                  <div
                    className={`h-2 rounded-full transition-all ${
                      dbHealth.pool.utilization_percent > 90 ? 'bg-red-500' :
                      dbHealth.pool.utilization_percent > 80 ? 'bg-yellow-500' :
                      'bg-green-500'
                    }`}
                    style={{ width: `${dbHealth.pool.utilization_percent}%` }}
                  />
                </div>
                <p className="text-xs text-gray-500">
                  {dbHealth.pool.checked_out}/{dbHealth.pool.total_capacity} connections ({
                    dbHealth.pool.max_capacity_configured ?? dbHealth.pool.total_capacity
                  } max)
                </p>
              </div>
            </div>

            <div className={`${tileClass} p-4`}>
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-medium text-gray-400">Active Queries</h3>
                <Zap className="h-4 w-4 text-blue-400" />
              </div>
              <div className="space-y-1">
                <div className="text-2xl font-bold text-white">
                  {dbHealth.postgresql?.active_connections || 0}
                </div>
                <p className="text-xs text-gray-500">running queries</p>
                {dbHealth.postgresql && dbHealth.postgresql.long_running_queries > 0 && (
                  <p className="text-xs text-yellow-400 flex items-center space-x-1">
                    <AlertCircle className="h-3 w-3" />
                    <span>{dbHealth.postgresql.long_running_queries} slow ({'>'}1min)</span>
                  </p>
                )}
              </div>
            </div>

            <div className={`${tileClass} p-4`}>
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-medium text-gray-400">Database Size</h3>
                <HardDrive className="h-4 w-4 text-purple-400" />
              </div>
              <div className="space-y-1">
                <div className="text-2xl font-bold text-white">
                  {dbHealth.postgresql?.database_size_mb.toFixed(1) || '0'} MB
                </div>
                <p className="text-xs text-gray-500">total storage</p>
              </div>
            </div>

            <div className={`${tileClass} p-4`}>
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-medium text-gray-400">Health Status</h3>
                <TrendingUp className={`h-4 w-4 ${
                  dbHealth.status === 'healthy' ? 'text-green-400' :
                  dbHealth.status === 'warning' ? 'text-yellow-400' :
                  'text-red-400'
                }`} />
              </div>
              <div className="space-y-1">
                <div className={`inline-flex items-center px-3 py-1 rounded-full text-sm font-medium ${
                  dbHealth.status === 'healthy' ? 'bg-green-900/30 text-green-400 border border-green-700' :
                  dbHealth.status === 'warning' ? 'bg-yellow-900/30 text-yellow-400 border border-yellow-700' :
                  'bg-red-900/30 text-red-400 border border-red-700'
                }`}>
                  {dbHealth.status.toUpperCase()}
                </div>
                <p className="text-xs text-gray-500 mt-2">
                  {dbHealth.status === 'healthy' && 'All systems operational'}
                  {dbHealth.status === 'warning' && 'Pool usage high'}
                  {dbHealth.status === 'critical' && 'Pool nearly exhausted'}
                </p>
              </div>
            </div>
          </div>

          {dbHealth.high_water_marks && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className={`${tileClass} p-4`}>
                <h3 className="text-sm font-medium text-gray-400 mb-2">High-water marks (last 24h)</h3>
                <div className="text-xs text-gray-500 mb-3">Since {dbHealth.high_water_marks.last_24h_date}</div>
                <div className="grid grid-cols-2 gap-3 text-sm">
                  <div>
                    <div className="text-gray-400">Pool in-use peak</div>
                    <div className="text-white font-semibold">{dbHealth.high_water_marks.last_24h.db_pool_in_use_peak}</div>
                  </div>
                  <div>
                    <div className="text-gray-400">Active queries peak</div>
                    <div className="text-white font-semibold">{dbHealth.high_water_marks.last_24h.active_queries_peak}</div>
                  </div>
                  <div>
                    <div className="text-gray-400">Wait count</div>
                    <div className="text-white font-semibold">{dbHealth.high_water_marks.last_24h.db_pool_wait_count}</div>
                  </div>
                  <div>
                    <div className="text-gray-400">Wait seconds</div>
                    <div className="text-white font-semibold">{dbHealth.high_water_marks.last_24h.db_pool_wait_seconds_sum.toFixed(1)}s</div>
                  </div>
                  <div>
                    <div className="text-gray-400">Slow queries</div>
                    <div className="text-white font-semibold">{dbHealth.high_water_marks.last_24h.slow_query_count}</div>
                  </div>
                </div>
              </div>

              <div className={`${tileClass} p-4`}>
                <h3 className="text-sm font-medium text-gray-400 mb-2">High-water marks (since boot)</h3>
                <div className="grid grid-cols-2 gap-3 text-sm">
                  <div>
                    <div className="text-gray-400">Pool in-use peak</div>
                    <div className="text-white font-semibold">{dbHealth.high_water_marks.since_boot.db_pool_in_use_peak}</div>
                  </div>
                  <div>
                    <div className="text-gray-400">Active queries peak</div>
                    <div className="text-white font-semibold">{dbHealth.high_water_marks.since_boot.active_queries_peak}</div>
                  </div>
                  <div>
                    <div className="text-gray-400">Wait count</div>
                    <div className="text-white font-semibold">{dbHealth.high_water_marks.since_boot.db_pool_wait_count}</div>
                  </div>
                  <div>
                    <div className="text-gray-400">Wait seconds</div>
                    <div className="text-white font-semibold">{dbHealth.high_water_marks.since_boot.db_pool_wait_seconds_sum.toFixed(1)}s</div>
                  </div>
                  <div>
                    <div className="text-gray-400">Slow queries</div>
                    <div className="text-white font-semibold">{dbHealth.high_water_marks.since_boot.slow_query_count}</div>
                  </div>
                </div>
              </div>
            </div>
          )}

        </>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className={`rounded-xl border p-4 ${modes.ramp_up ? 'border-yellow-700 bg-yellow-900/20' : 'border-gray-700 bg-gray-900/70'}`}>
          <div className="flex items-center gap-2">
            <AlertCircle className={`h-4 w-4 ${modes.ramp_up ? 'text-yellow-400' : 'text-gray-500'}`} />
            <span className="text-sm font-medium text-gray-200">System ramp-up</span>
          </div>
          <p className="text-xs text-gray-400 mt-2">
            {modes.ramp_up ? 'Telemetry backlog detected; system may be catching up.' : 'Normal telemetry cadence.'}
          </p>
        </div>

        <div className={`rounded-xl border p-4 ${modes.throttling_writes ? 'border-red-700 bg-red-900/20' : 'border-gray-700 bg-gray-900/70'}`}>
          <div className="flex items-center gap-2">
            <ShieldAlert className={`h-4 w-4 ${modes.throttling_writes ? 'text-red-400' : 'text-gray-500'}`} />
            <span className="text-sm font-medium text-gray-200">Write throttling</span>
          </div>
          <p className="text-xs text-gray-400 mt-2">
            {modes.throttling_writes ? 'DB pool usage is high; writes may be constrained.' : 'DB pool healthy.'}
          </p>
        </div>

        <div className={`rounded-xl border p-4 ${modes.ha_unstable ? 'border-orange-700 bg-orange-900/20' : 'border-gray-700 bg-gray-900/70'}`}>
          <div className="flex items-center gap-2">
            <AlertCircle className={`h-4 w-4 ${modes.ha_unstable ? 'text-orange-400' : 'text-gray-500'}`} />
            <span className="text-sm font-medium text-gray-200">HA unstable</span>
          </div>
          <p className="text-xs text-gray-400 mt-2">
            {modes.ha_unstable ? 'Home Assistant keepalive is unstable.' : 'Home Assistant stable.'}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-gray-900/80 rounded-xl p-5 border border-gray-800">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium text-gray-400">DB connections</h3>
            <Sparkles className="h-4 w-4 text-blue-400" />
          </div>
          <div className="mt-3 flex items-center justify-between">
            <div>
              <div className="text-2xl font-bold text-white">{db_pool.high_water.last_24h.db_pool_in_use_peak}</div>
              <p className="text-xs text-gray-500">24h peak • since {db_pool.high_water.last_24h_date}</p>
            </div>
            <MiniSparkBars
              values={[db_pool.checked_out, db_pool.high_water.last_24h.db_pool_in_use_peak, db_pool.high_water.since_boot.db_pool_in_use_peak]}
              labels={["Now", "24h", "Boot"]}
              color="bg-blue-500"
            />
          </div>
        </div>
        <div className="bg-gray-900/80 rounded-xl p-5 border border-gray-800">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium text-gray-400">Miner concurrency</h3>
            <Sparkles className="h-4 w-4 text-purple-400" />
          </div>
          <div className="mt-3 flex items-center justify-between">
            <div>
              <div className="text-2xl font-bold text-white">{telemetry.metrics.last_24h.peak_concurrency}</div>
              <p className="text-xs text-gray-500">24h peak • since {telemetry.metrics.last_24h_date}</p>
            </div>
            <MiniSparkBars
              values={[telemetry.metrics.last_24h.peak_concurrency, telemetry.metrics.since_boot.peak_concurrency]}
              labels={["24h", "Boot"]}
              color="bg-purple-500"
            />
          </div>
        </div>
        <div className="bg-gray-900/80 rounded-xl p-5 border border-gray-800">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium text-gray-400">Telemetry backlog</h3>
            <Sparkles className="h-4 w-4 text-emerald-400" />
          </div>
          <div className="mt-3 flex items-center justify-between">
            <div>
              <div className="text-2xl font-bold text-white">{telemetry.metrics.last_24h.max_backlog}</div>
              <p className="text-xs text-gray-500">24h peak • current {telemetry.backlog_current}</p>
            </div>
            <MiniSparkBars
              values={[telemetry.backlog_current, telemetry.metrics.last_24h.max_backlog, telemetry.metrics.since_boot.max_backlog]}
              labels={["Now", "24h", "Boot"]}
              color="bg-emerald-500"
            />
          </div>
        </div>
      </div>

      {ha.detail?.enabled && (
        <div>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-400 mb-2">Integration health</h2>
          <div className="bg-gray-800 rounded-lg p-4 border border-gray-700 text-xs text-gray-500">
          HA last success: {ha.detail.last_success ?? 'N/A'} · Downtime start: {ha.detail.downtime_start ?? 'N/A'} · Alerts: {ha.detail.alerts_sent ?? 0}
          </div>
        </div>
      )}
    </div>
  )
}
