import { useMemo } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Activity, AlertTriangle, BarChart3, CheckCircle2, Cpu, Gauge, Share2, Waves } from 'lucide-react'

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { integrationsAPI, type HmmLocalStratumChartPoint, type HmmLocalStratumCoinDashboardResponse } from '@/lib/api'

const SUPPORTED_COINS = new Set(['BTC', 'BCH', 'DGB'])

function formatHashrateHs(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return 'N/A'
  if (value >= 1e18) return `${(value / 1e18).toFixed(2)} EH/s`
  if (value >= 1e15) return `${(value / 1e15).toFixed(2)} PH/s`
  if (value >= 1e12) return `${(value / 1e12).toFixed(2)} TH/s`
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)} GH/s`
  if (value >= 1e6) return `${(value / 1e6).toFixed(2)} MH/s`
  if (value >= 1e3) return `${(value / 1e3).toFixed(2)} KH/s`
  return `${value.toFixed(2)} H/s`
}

function formatPercent(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'N/A'
  return `${value.toFixed(2)}%`
}

function formatNumber(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'N/A'
  return value.toLocaleString()
}

function formatCompactNumber(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'N/A'
  const abs = Math.abs(value)
  if (abs >= 1e12) return `${(value / 1e12).toFixed(2)}T`
  if (abs >= 1e9) return `${(value / 1e9).toFixed(2)}B`
  if (abs >= 1e6) return `${(value / 1e6).toFixed(2)}M`
  if (abs >= 1e3) return `${(value / 1e3).toFixed(2)}K`
  return value.toFixed(2)
}

function formatVardiff(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'N/A'
  const abs = Math.abs(value)
  if (abs >= 1e3) return `${(value / 1e3).toFixed(2)}K`
  return value.toFixed(2)
}

function formatSeconds(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return 'N/A'

  const total = Math.floor(value)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60

  if (h > 0) return `${h}h ${m}m ${s}s`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

function formatTimeAgo(iso: string | null | undefined): string {
  if (!iso) return 'Unknown'
  const ts = new Date(iso).getTime()
  if (Number.isNaN(ts)) return iso
  const sec = Math.max(0, Math.floor((Date.now() - ts) / 1000))
  if (sec < 60) return `${sec}s ago`
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min}m ago`
  const hrs = Math.floor(min / 60)
  if (hrs < 24) return `${hrs}h ago`
  return `${Math.floor(hrs / 24)}d ago`
}

function formatWorkerName(worker: string): string {
  if (!worker) return worker
  const trimmed = worker.trim()
  if (!trimmed.includes('.')) return trimmed
  const parts = trimmed.split('.')
  const last = parts[parts.length - 1]?.trim()
  return last || trimmed
}

function Sparkline({ points, colorClass }: { points: HmmLocalStratumChartPoint[]; colorClass: string }) {
  const normalized = useMemo(() => {
    const trimmed = points.slice(-60)
    if (trimmed.length < 2) return ''

    const width = 260
    const height = 70
    const padX = 8
    const padY = 8

    const minX = Math.min(...trimmed.map((p) => p.x))
    const maxX = Math.max(...trimmed.map((p) => p.x))
    const minY = Math.min(...trimmed.map((p) => p.y))
    const maxY = Math.max(...trimmed.map((p) => p.y))

    const dx = Math.max(maxX - minX, 1)
    const dy = Math.max(maxY - minY, 1)

    return trimmed
      .map((p) => {
        const x = padX + ((p.x - minX) / dx) * (width - padX * 2)
        const y = height - padY - ((p.y - minY) / dy) * (height - padY * 2)
        return `${x},${y}`
      })
      .join(' ')
  }, [points])

  if (!normalized) {
    return <div className="h-[70px] text-xs text-slate-500 flex items-center">Not enough points</div>
  }

  return (
    <svg viewBox="0 0 260 70" className="h-[70px] w-full">
      <polyline fill="none" strokeWidth="2" className={colorClass} points={normalized} />
    </svg>
  )
}

export default function StratumCoinDashboard() {
  const params = useParams<{ coin: string }>()
  const coin = (params.coin || '').toUpperCase()

  const invalidCoin = !SUPPORTED_COINS.has(coin)

  const { data, isLoading, isError, error } = useQuery<HmmLocalStratumCoinDashboardResponse>({
    queryKey: ['stratum-dashboard', coin],
    queryFn: () => integrationsAPI.getHmmLocalStratumCoinDashboard(coin as 'BTC' | 'BCH' | 'DGB'),
    enabled: !invalidCoin,
    refetchInterval: 30000,
    refetchOnWindowFocus: false,
    staleTime: 25000,
  })

  if (invalidCoin) {
    return (
      <div className="rounded-lg border border-amber-700/60 bg-amber-900/30 p-4 text-amber-200">
        Unsupported coin route. Use BTC, BCH or DGB.
      </div>
    )
  }

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
        Failed to load Stratum dashboard data: {(error as Error)?.message || 'Unknown error'}
      </div>
    )
  }

  const readiness = data.quality?.readiness || 'unknown'
  const stale = Boolean(data.quality?.stale)

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-3 text-3xl font-semibold text-foreground">
          <Waves className="h-8 w-8 text-blue-400" />
          <span>HMM-Local Stratum · {coin}</span>
        </div>
        <p className="text-base text-muted-foreground">
          Pool and miner telemetry for {coin} from local Stratum datastore.
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">Readiness</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold capitalize">{readiness}</div>
            <div className="mt-1 text-xs text-muted-foreground">{stale ? 'Data stale' : 'Data fresh'}</div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">Pool hashrate</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">{formatHashrateHs(data.hashrate?.pool_hashrate_hs)}</div>
            <div className="mt-1 text-xs text-muted-foreground">Workers: {data.workers.count}</div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">Share reject rate</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">{formatPercent(data.kpi?.share_reject_rate_pct ?? null)}</div>
            <div className="mt-1 text-xs text-muted-foreground">
              Accepted {formatNumber(data.kpi?.share_accept_count ?? null)} · Rejected {formatNumber(data.kpi?.share_reject_count ?? null)}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-muted-foreground">Blocks (24h)</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">{formatNumber(data.kpi?.block_accept_count_24h ?? null)}</div>
            <div className="mt-1 text-xs text-muted-foreground">
              Rejected {formatNumber(data.kpi?.block_reject_count_24h ?? null)} · ETA {formatSeconds(data.kpi?.expected_time_to_block_sec ?? null)}
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <BarChart3 className="h-4 w-4" /> Pool hashrate trend
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Sparkline points={data.charts.pool_hashrate_hs || []} colorClass="stroke-blue-400" />
        </CardContent>
      </Card>

      <div className="space-y-4">
        {data.workers.rows.length === 0 ? (
          <Card>
            <CardContent className="py-6 text-sm text-muted-foreground">
              No worker rows available yet for this coin.
            </CardContent>
          </Card>
        ) : (
          data.workers.rows.map((worker) => (
            <Card key={worker.worker}>
              {(() => {
                const hasHashrate = typeof worker.current_hashrate_hs === 'number' && Number.isFinite(worker.current_hashrate_hs) && worker.current_hashrate_hs > 0
                const hasShares =
                  typeof worker.accepted === 'number' &&
                  typeof worker.rejected === 'number' &&
                  Number.isFinite(worker.accepted) &&
                  Number.isFinite(worker.rejected) &&
                  worker.accepted + worker.rejected > 0
                const hasRejectRate = typeof worker.reject_rate_pct === 'number' && Number.isFinite(worker.reject_rate_pct)
                const hasHighestDiff = typeof worker.highest_diff === 'number' && Number.isFinite(worker.highest_diff)
                const hasLastShare = Boolean(worker.last_share_at)
                const hasHashrateTrend = (worker.hashrate_chart || []).length >= 2
                const hasVardiffTrend = (worker.vardiff_chart || []).length >= 2
                const currentVardiff = hasVardiffTrend
                  ? worker.vardiff_chart[worker.vardiff_chart.length - 1]?.y
                  : null

                return (
                  <>
              <CardHeader className="pb-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <CardTitle className="text-base">{formatWorkerName(worker.worker)}</CardTitle>
                  <div className="flex items-center gap-2 text-xs">
                    {hasRejectRate && worker.reject_rate_pct !== null && worker.reject_rate_pct >= 5 ? (
                      <span className="inline-flex items-center gap-1 rounded-full border border-red-700/60 bg-red-900/30 px-2 py-1 text-red-300">
                        <AlertTriangle className="h-3 w-3" /> High reject risk
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 rounded-full border border-emerald-700/50 bg-emerald-900/30 px-2 py-1 text-emerald-300">
                        <CheckCircle2 className="h-3 w-3" /> Active
                      </span>
                    )}
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
                  {hasHashrate && (
                    <div className="rounded-lg border border-slate-700/50 bg-slate-900/30 p-3">
                      <div className="text-xs uppercase tracking-wide text-slate-400">Current hashrate</div>
                      <div className="mt-1 text-sm text-slate-100">{formatHashrateHs(worker.current_hashrate_hs)}</div>
                    </div>
                  )}
                  {hasShares && (
                    <div className="rounded-lg border border-slate-700/50 bg-slate-900/30 p-3">
                      <div className="text-xs uppercase tracking-wide text-slate-400">Shares</div>
                      <div className="mt-1 text-sm text-slate-100">{formatNumber(worker.accepted)} accepted</div>
                      <div className="text-xs text-slate-400">{formatNumber(worker.rejected)} rejected</div>
                    </div>
                  )}
                  {hasRejectRate && (
                    <div className="rounded-lg border border-slate-700/50 bg-slate-900/30 p-3">
                      <div className="text-xs uppercase tracking-wide text-slate-400">Reject rate</div>
                      <div className="mt-1 text-sm text-slate-100">{formatPercent(worker.reject_rate_pct)}</div>
                    </div>
                  )}
                  {hasHighestDiff && (
                    <div className="rounded-lg border border-slate-700/50 bg-slate-900/30 p-3">
                      <div className="text-xs uppercase tracking-wide text-slate-400">Highest diff</div>
                      <div className="mt-1 text-sm text-slate-100">{formatCompactNumber(worker.highest_diff)}</div>
                    </div>
                  )}
                  {hasLastShare && (
                    <div className="rounded-lg border border-slate-700/50 bg-slate-900/30 p-3">
                      <div className="text-xs uppercase tracking-wide text-slate-400">Last share</div>
                      <div className="mt-1 text-sm text-slate-100">{formatTimeAgo(worker.last_share_at)}</div>
                    </div>
                  )}
                </div>

                {(hasHashrateTrend || hasVardiffTrend) && (
                  <div className="grid gap-4 xl:grid-cols-2">
                    {hasHashrateTrend && (
                      <div className="rounded-lg border border-slate-700/50 bg-slate-900/30 p-3">
                        <div className="mb-2 flex items-center gap-2 text-xs uppercase tracking-wide text-slate-400">
                          <Gauge className="h-3.5 w-3.5" /> Hashrate trend
                        </div>
                        <Sparkline points={worker.hashrate_chart || []} colorClass="stroke-cyan-400" />
                      </div>
                    )}

                    {hasVardiffTrend && (
                      <div className="rounded-lg border border-slate-700/50 bg-slate-900/30 p-3">
                        <div className="mb-2 flex items-center justify-between gap-2 text-xs uppercase tracking-wide text-slate-400">
                          <div className="flex items-center gap-2">
                            <Share2 className="h-3.5 w-3.5" /> Vardiff trend
                          </div>
                          <div className="text-[11px] normal-case text-slate-300">Current: {formatVardiff(currentVardiff)}</div>
                        </div>
                        <Sparkline points={worker.vardiff_chart || []} colorClass="stroke-purple-400" />
                      </div>
                    )}
                  </div>
                )}
              </CardContent>
                  </>
                )
              })()}
            </Card>
          ))
        )}
      </div>

      <div className="rounded-lg border border-slate-700/50 bg-slate-900/20 p-3 text-xs text-slate-400">
        <div className="flex flex-wrap items-center gap-4">
          <span className="inline-flex items-center gap-1"><Cpu className="h-3.5 w-3.5" /> Pool: {data.pool.name}</span>
          <span>Source: {data.api_base}</span>
          <span>Updated: {formatTimeAgo(data.fetched_at)}</span>
        </div>
      </div>
    </div>
  )
}
