import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card } from '@/components/ui/card'
import { Trophy, Calendar, Award, ExternalLink, Link2 } from 'lucide-react'
import { MinerTypeBadge } from '@/components/miners/MinerTypeBadge'
import { formatHashrateDisplay } from '@/lib/utils'

interface LeaderboardEntry {
  id: number
  rank: number
  miner_id: number
  miner_name: string
  miner_type: string
  coin: string
  pool_name: string
  difficulty: number
  difficulty_formatted: string
  network_difficulty: number | null
  was_block_solve: boolean
  percent_of_block: number | null
  badge: string | null
  hashrate: number | { display: string; value: number; unit: string } | null
  hashrate_unit: string
  miner_mode: string | null
  timestamp: string
  days_ago: number
}

interface LeaderboardResponse {
  entries: LeaderboardEntry[]
  total_count: number
  filter_coin: string | null
  filter_days: number
}

interface BlockVerificationEntry {
  source_pool_id: number
  source_pool_name: string
  id: number
  timestamp: string
  coin: string
  worker: string | null
  payout_address: string | null
  template_height: number | null
  block_hash: string
  accepted_by_node: boolean
  reject_reason: string | null
  block_explorer_url: string | null
  payout_explorer_url: string | null
}

interface BlockVerificationFeedResponse {
  total: number
  entries: BlockVerificationEntry[]
}

function shortHash(hash: string, start: number = 10, end: number = 8): string {
  if (!hash || hash.length <= start + end + 3) return hash
  return `${hash.slice(0, start)}...${hash.slice(-end)}`
}

export function Leaderboard() {
  const [selectedDays, setSelectedDays] = useState(90)
  const [selectedCoin, setSelectedCoin] = useState<string | null>(null)

  const { data, isLoading } = useQuery<LeaderboardResponse>({
    queryKey: ['leaderboard', selectedDays, selectedCoin],
    queryFn: async () => {
      const params = new URLSearchParams({
        days: selectedDays.toString(),
        limit: '10',
      })
      if (selectedCoin) params.append('coin', selectedCoin)
      
      const response = await fetch(`/api/leaderboard?${params}`)
      if (!response.ok) throw new Error('Failed to fetch leaderboard')
      return response.json()
    },
    refetchInterval: 30000,
  })

  const { data: verificationData, isLoading: verificationLoading } = useQuery<BlockVerificationFeedResponse>({
    queryKey: ['leaderboard-verification-feed'],
    queryFn: async () => {
      const response = await fetch('/api/leaderboard/verification-feed?limit=8&accepted_only=true')
      if (!response.ok) throw new Error('Failed to fetch verification feed')
      return response.json()
    },
    refetchInterval: 30000,
  })

  const getRankIcon = (rank: number) => {
    switch (rank) {
      case 1:
        return '🥇'
      case 2:
        return '🥈'
      case 3:
        return '🥉'
      default:
        return `#${rank}`
    }
  }

  const getBadgeColor = (badge: string | null) => {
    if (!badge) return ''
    if (badge.includes('💀') || badge.includes('Emotional Damage')) {
      return 'bg-red-500/10 text-red-600 dark:text-red-400 border border-red-500/20'
    }
    if (badge.includes('🚨') || badge.includes('Pain')) {
      return 'bg-orange-500/10 text-orange-600 dark:text-orange-400 border border-orange-500/20'
    }
    if (badge.includes('🔥') || badge.includes('So Close')) {
      return 'bg-yellow-500/10 text-yellow-600 dark:text-yellow-400 border border-yellow-500/20'
    }
    return 'bg-gray-500/10 text-gray-600 dark:text-gray-400 border border-gray-500/20'
  }

  const coins = ['BTC', 'BCH', 'BC2', 'DGB']
  const dayOptions = [1, 7, 30, 90, 180, 365]

  // Count actual fails (exclude block solves)
  const epicFailCount = data?.entries.filter(entry => !entry.was_block_solve).length || 0
  const blockWinCount = data?.entries.filter(entry => entry.was_block_solve).length || 0

  if (isLoading) {
    return (
      <div className="space-y-6">
        <h1 className="text-3xl font-bold tracking-tight">Hall of Pain</h1>
        <div className="flex items-center justify-center h-64">
          <div className="text-muted-foreground">Loading leaderboard...</div>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight flex items-center gap-2">
            <Trophy className="h-8 w-8 text-yellow-500" />
            Hall of Pain
          </h1>
          <p className="text-muted-foreground mt-1">
            The highest difficulty shares submitted
          </p>
        </div>
        <div className="text-right">
          <div className="text-2xl font-bold">{epicFailCount}</div>
          <div className="text-sm text-muted-foreground">epic fails</div>
          {blockWinCount > 0 && (
            <div className="text-xs text-green-600 dark:text-green-400 mt-1">
              +{blockWinCount} {blockWinCount === 1 ? 'win' : 'wins'}!
            </div>
          )}
        </div>
      </div>

      {/* Filters */}
      <Card className="p-4">
        <div className="flex flex-wrap gap-4 items-center">
          {/* Time Range Filter */}
          <div className="flex items-center gap-2">
            <Calendar className="h-4 w-4 text-muted-foreground" />
            <span className="text-sm font-medium">Time Range:</span>
            <div className="flex gap-2">
              {dayOptions.map((days) => (
                <button
                  key={days}
                  onClick={() => setSelectedDays(days)}
                  className={`px-3 py-1 text-sm font-medium rounded-md transition-colors ${
                    selectedDays === days
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-secondary text-secondary-foreground hover:bg-secondary/80'
                  }`}
                >
                  {days}d
                </button>
              ))}
            </div>
          </div>

          {/* Coin Filter */}
          <div className="flex items-center gap-2 ml-auto">
            <span className="text-sm font-medium">Coin:</span>
            <div className="flex gap-2">
              <button
                onClick={() => setSelectedCoin(null)}
                className={`px-3 py-1 text-sm font-medium rounded-md transition-colors ${
                  selectedCoin === null
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-secondary text-secondary-foreground hover:bg-secondary/80'
                }`}
              >
                All
              </button>
              {coins.map((coin) => (
                <button
                  key={coin}
                  onClick={() => setSelectedCoin(coin)}
                  className={`px-3 py-1 text-sm font-medium rounded-md transition-colors ${
                    selectedCoin === coin
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-secondary text-secondary-foreground hover:bg-secondary/80'
                  }`}
                >
                  {coin}
                </button>
              ))}
            </div>
          </div>
        </div>
      </Card>

      {/* Quick Verification Feed */}
      <Card className="p-4 space-y-3">
        <div className="flex items-center gap-2">
          <Link2 className="h-4 w-4 text-muted-foreground" />
          <h2 className="text-sm font-semibold">Quick Block Verification</h2>
          <span className="text-xs text-muted-foreground">latest accepted submissions</span>
        </div>

        {verificationLoading && (
          <div className="text-sm text-muted-foreground">Loading recent accepted blocks...</div>
        )}

        {!verificationLoading && (!verificationData || verificationData.entries.length === 0) && (
          <div className="text-sm text-muted-foreground">No accepted block submissions yet.</div>
        )}

        {!verificationLoading && verificationData && verificationData.entries.length > 0 && (
          <div className="space-y-2">
            {verificationData.entries.map((entry) => (
              <div
                key={`${entry.source_pool_id}-${entry.id}`}
                className="border rounded-md p-3 flex flex-col md:flex-row md:items-center md:justify-between gap-2"
              >
                <div className="space-y-1">
                  <div className="text-sm font-medium flex items-center gap-2">
                    <span>{entry.coin}</span>
                    {entry.template_height !== null && (
                      <span className="text-xs text-muted-foreground">height {entry.template_height}</span>
                    )}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {new Date(entry.timestamp).toLocaleString()} • {entry.worker || 'unknown worker'}
                  </div>
                  <div className="text-xs text-muted-foreground">hash {shortHash(entry.block_hash)}</div>
                  {entry.payout_address && (
                    <div className="text-xs text-muted-foreground">payout {entry.payout_address}</div>
                  )}
                </div>

                <div className="flex items-center gap-2">
                  {entry.block_explorer_url && (
                    <a
                      href={entry.block_explorer_url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded border hover:bg-muted"
                    >
                      Block <ExternalLink className="h-3 w-3" />
                    </a>
                  )}
                  {entry.payout_explorer_url && (
                    <a
                      href={entry.payout_explorer_url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded border hover:bg-muted"
                    >
                      Address <ExternalLink className="h-3 w-3" />
                    </a>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Leaderboard Entries */}
      <div className="space-y-3">
        {data?.entries.map((entry) => (
          <Card
            key={entry.id}
            className={`p-6 transition-all hover:shadow-md ${
              entry.rank === 1
                ? 'border-l-4 border-l-yellow-500'
                : entry.rank === 2
                ? 'border-l-4 border-l-gray-400'
                : entry.rank === 3
                ? 'border-l-4 border-l-orange-500'
                : ''
            }`}
          >
            <div className="flex items-start gap-6">
              {/* Rank */}
              <div className="flex items-center justify-center min-w-[60px]">
                <div className="text-3xl font-bold text-muted-foreground">
                  {entry.rank <= 3 ? getRankIcon(entry.rank) : `#${entry.rank}`}
                </div>
              </div>

              {/* Content */}
              <div className="flex-1 space-y-3">
                {/* Miner Info */}
                <div className="space-y-2">
                  <div className="flex items-center gap-2">
                    <h3 className="text-lg font-semibold">{entry.miner_name}</h3>
                    <MinerTypeBadge type={entry.miner_type} size="sm" />
                    {entry.was_block_solve && (
                      <span className="flex items-center gap-1 text-xs px-2 py-0.5 rounded bg-green-500/10 text-green-600 dark:text-green-400 border border-green-500/20">
                        <Award className="h-3 w-3" />
                        Block!
                      </span>
                    )}
                    {entry.badge && (
                      <span className={`text-xs px-2 py-0.5 rounded font-medium ${getBadgeColor(entry.badge)}`}>
                        {entry.badge}
                      </span>
                    )}
                  </div>
                  <div className="text-sm text-muted-foreground">
                    {entry.pool_name} • {entry.coin} • {entry.days_ago}d ago
                  </div>
                </div>

                {/* Stats */}
                <div className="flex items-center gap-6 text-sm">
                  <div>
                    <div className="text-xs text-muted-foreground">Difficulty</div>
                    <div className="text-lg font-semibold">{entry.difficulty_formatted}</div>
                  </div>

                  {entry.percent_of_block !== null && (
                    <div>
                      <div className="text-xs text-muted-foreground">Of Block</div>
                      <div className="text-lg font-semibold text-orange-600 dark:text-orange-400">
                        {entry.percent_of_block.toFixed(1)}%
                      </div>
                    </div>
                  )}

                  {entry.hashrate !== null && (
                    <div>
                      <div className="text-xs text-muted-foreground">Hashrate</div>
                      <div className="text-lg font-semibold">
                        {formatHashrateDisplay(entry.hashrate, entry.hashrate_unit)}
                      </div>
                    </div>
                  )}
                </div>

                {/* Mode */}
                {entry.miner_mode && (
                  <div className="text-xs text-muted-foreground mt-2">
                    Mode: <span className="font-medium text-foreground uppercase">{entry.miner_mode}</span>
                  </div>
                )}
              </div>
            </div>
          </Card>
        ))}
      </div>

      {/* Empty State */}
      {data?.entries.length === 0 && (
        <Card className="p-12 text-center">
          <Trophy className="h-16 w-16 mx-auto text-muted-foreground mb-4" />
          <h3 className="text-lg font-semibold mb-2">No Epic Fails Yet</h3>
          <p className="text-muted-foreground">
            No high difficulty shares found for the selected time range and coin.
          </p>
        </Card>
      )}
    </div>
  )
}
