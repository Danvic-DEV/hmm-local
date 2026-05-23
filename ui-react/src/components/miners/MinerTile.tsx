import { Activity, Power, DollarSign, Gauge, Network, TrendingUp, AlertCircle } from 'lucide-react';
import { Link } from 'react-router-dom';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { MinerTypeAvatar, MinerTypeBadge } from '@/components/miners/MinerTypeBadge';
import type { Miner } from '@/types/miner';
import { formatHashrateDisplay } from '@/lib/utils';

interface MinerTileProps {
  miner: Miner;
  modePowerStats?: {
    miner_id: number;
    current_mode: string | null;
    current_mode_stats: {
      mode: string;
      sample_count: number;
      avg_power_watts: number | null;
      ema_power_watts: number | null;
      min_power_watts: number | null;
      max_power_watts: number | null;
      last_sample_at: string | null;
      resets_count: number;
    } | null;
    modes: {
      mode: string;
      sample_count: number;
      avg_power_watts: number | null;
      ema_power_watts: number | null;
      min_power_watts: number | null;
      max_power_watts: number | null;
      last_sample_at: string | null;
      resets_count: number;
    }[];
  } | null;
  selected: boolean;
  highlight?: boolean;
  onToggleSelect: () => void;
}

const getBestDiffLabel = (minerType: string) => {
  const type = minerType.toLowerCase();
  if (type === 'avalon_nano') return 'Best Share';
  if (type === 'nmminer') return 'Best Diff';
  return 'Best Session';
};

const formatBestDiff = (bestDiff: unknown) => {
  const value = typeof bestDiff === 'number' ? bestDiff : Number(bestDiff);
  if (!Number.isFinite(value) || value <= 0) return '—';

  // Format large numbers with suffixes
  if (value >= 1000000000) return `${(value / 1000000000).toFixed(2)}B`;
  if (value >= 1000000) return `${(value / 1000000).toFixed(2)}M`;
  if (value >= 1000) return `${(value / 1000).toFixed(2)}K`;
  return value.toFixed(0);
};

const getNanoStateMeta = (state?: string | null) => {
  switch (state) {
    case 'ok':
      return { label: 'Nano State: OK', className: 'text-green-400 border-green-500/20 bg-green-500/10' };
    case 'calibration':
      return { label: 'Nano State: Calibration/Lock', className: 'text-orange-400 border-orange-500/20 bg-orange-500/10' };
    case 'drift':
      return { label: 'Nano State: Drift', className: 'text-yellow-400 border-yellow-500/20 bg-yellow-500/10' };
    case 'rejected':
      return { label: 'Nano State: Rejected', className: 'text-red-400 border-red-500/20 bg-red-500/10' };
    default:
      return { label: 'Nano State: Unknown', className: 'text-gray-400 border-gray-500/20 bg-gray-500/10' };
  }
};

const getExpectedModesForMinerType = (minerType: string) => {
  const type = (minerType || '').toLowerCase();
  if (type === 'avalon_nano') return ['low', 'med', 'high'];
  if (type === 'bitaxe' || type === 'nerdqaxe') return ['eco', 'standard', 'turbo', 'oc'];
  if (type === 'nmminer') return ['low', 'med', 'high'];
  return [];
};

const formatTimeAgo = (isoTimestamp?: string | null) => {
  if (!isoTimestamp) return '—';
  const timestamp = new Date(isoTimestamp).getTime();
  if (!Number.isFinite(timestamp)) return '—';

  const deltaSeconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (deltaSeconds < 60) return `${deltaSeconds}s ago`;
  const deltaMinutes = Math.floor(deltaSeconds / 60);
  if (deltaMinutes < 60) return `${deltaMinutes}m ago`;
  const deltaHours = Math.floor(deltaMinutes / 60);
  if (deltaHours < 24) return `${deltaHours}h ago`;
  const deltaDays = Math.floor(deltaHours / 24);
  return `${deltaDays}d ago`;
};

export default function MinerTile({ miner, modePowerStats, selected, highlight, onToggleSelect }: MinerTileProps) {
  const hasHealthIssue = miner.health_score !== null && miner.health_score < 50;
  const showNanoState = miner.miner_type === 'avalon_nano';
  const nanoStateMeta = getNanoStateMeta(miner.nano_state);
  const currentModeStats = modePowerStats?.current_mode_stats;
  const expectedModes = getExpectedModesForMinerType(miner.miner_type);
  const allKnownModes = modePowerStats?.modes ?? [];
  const statsByMode = new Map(allKnownModes.map((row) => [row.mode, row]));
  const displayModes = [
    ...expectedModes,
    ...allKnownModes.map((row) => row.mode).filter((mode) => !expectedModes.includes(mode)),
  ];
  const showNanoDiagnostic = showNanoState && (miner.nano_state === 'calibration' || miner.nano_state === 'rejected');
  const nanoDiagnostic = showNanoDiagnostic
    ? [
        typeof miner.mode_switch_last_code === 'number' ? `Code ${miner.mode_switch_last_code}` : null,
        miner.mode_switch_last_message || null,
      ]
        .filter(Boolean)
        .join(' · ')
    : '';

  return (
    <Card
      className={`
        relative transition-all
        ${miner.is_offline ? 'opacity-60 bg-gray-800/30' : ''}
        ${hasHealthIssue ? 'border-l-4 border-l-red-500' : ''}
        ${selected ? 'ring-2 ring-blue-500 ring-offset-2 ring-offset-gray-900' : ''}
        ${highlight ? 'ring-2 ring-emerald-400/70 shadow-emerald-500/20 animate-pulse' : ''}
      `}
    >
      {/* Selection checkbox */}
      <div className="absolute top-3 right-3 z-10">
        <Checkbox
          checked={selected}
          onCheckedChange={onToggleSelect}
          className="border-gray-600"
        />
      </div>

      <CardHeader className="pb-3">
        <div className="flex items-start gap-3 pr-8">
          <MinerTypeAvatar type={miner.miner_type} size="lg" className="flex-shrink-0 shadow-inner shadow-black/20" />
          <div className="flex-1 min-w-0">
            <h3 className="font-semibold text-lg truncate mb-1">{miner.name}</h3>
            <div className="flex items-center gap-2 flex-wrap">
              <MinerTypeBadge type={miner.miner_type} />
              <span className={`px-2 py-0.5 rounded text-xs font-medium ${miner.enabled ? 'bg-green-500/10 text-green-400 border border-green-500/20' : 'bg-gray-500/10 text-gray-400 border border-gray-500/20'}`}>
                {miner.enabled ? 'Enabled' : 'Disabled'}
              </span>
              {miner.is_offline && (
                <span className="px-2 py-0.5 rounded text-xs font-medium bg-orange-500/10 text-orange-400 border border-orange-500/20">
                  Offline
                </span>
              )}
            </div>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-3">
        {/* Stats grid */}
        <div className="grid grid-cols-2 gap-3">
          {/* Hashrate */}
          <div className="bg-gray-800/50 rounded-lg p-3 border border-gray-700/50">
            <div className="flex items-center gap-2 text-gray-400 text-xs mb-1">
              <Activity className="h-3 w-3" />
              <span className="uppercase tracking-wide">Hashrate</span>
            </div>
            <p className="font-semibold text-sm">
              {formatHashrateDisplay(miner.hashrate, miner.hashrate_unit)}
            </p>
          </div>

          {/* Power */}
          <div className="bg-gray-800/50 rounded-lg p-3 border border-gray-700/50">
            <div className="flex items-center gap-2 text-gray-400 text-xs mb-1">
              <Power className="h-3 w-3" />
              <span className="uppercase tracking-wide">Power</span>
            </div>
            <p className="font-semibold text-sm">{miner.power > 0 ? `${miner.power.toFixed(1)} W` : '—'}</p>
          </div>

          {/* Pool */}
          <div className="bg-gray-800/50 rounded-lg p-3 border border-gray-700/50">
            <div className="flex items-center gap-2 text-gray-400 text-xs mb-1">
              <Network className="h-3 w-3" />
              <span className="uppercase tracking-wide">Pool</span>
            </div>
            <p className="font-semibold text-xs truncate">{miner.pool || '—'}</p>
          </div>

          {/* 24h Cost */}
          <div className="bg-gray-800/50 rounded-lg p-3 border border-gray-700/50">
            <div className="flex items-center gap-2 text-gray-400 text-xs mb-1">
              <DollarSign className="h-3 w-3" />
              <span className="uppercase tracking-wide">24h Cost</span>
            </div>
            <p className="font-semibold text-sm">£{miner.cost_24h.toFixed(2)}</p>
          </div>

          {/* Mode */}
          <div className="bg-gray-800/50 rounded-lg p-3 border border-gray-700/50">
            <div className="flex items-center gap-2 text-gray-400 text-xs mb-1">
              <Gauge className="h-3 w-3" />
              <span className="uppercase tracking-wide">Mode</span>
            </div>
            <p className="font-semibold text-xs">{miner.current_mode || '—'}</p>
            {showNanoState && (
              <>
                <p className={`mt-2 px-2 py-0.5 inline-flex rounded border text-[10px] font-medium ${nanoStateMeta.className}`}>
                  {nanoStateMeta.label}
                </p>
                {nanoDiagnostic && (
                  <p className="mt-1 text-[10px] text-gray-400 leading-tight" title={nanoDiagnostic}>
                    {nanoDiagnostic}
                  </p>
                )}
              </>
            )}
          </div>

          {/* Best Diff */}
          <div className="bg-gray-800/50 rounded-lg p-3 border border-gray-700/50">
            <div className="flex items-center gap-2 text-gray-400 text-xs mb-1">
              <TrendingUp className="h-3 w-3" />
              <span className="uppercase tracking-wide">{getBestDiffLabel(miner.miner_type)}</span>
            </div>
            <p className="font-semibold text-xs">{formatBestDiff(miner.best_diff)}</p>
          </div>
        </div>

        {/* Extended stats */}
        {currentModeStats && (
          <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-3 space-y-2">
            <div className="flex items-start justify-between gap-2">
              <div>
                <p className="text-[11px] uppercase tracking-wide text-blue-300 font-medium">Extended Stats</p>
                <p className="text-[11px] text-gray-400 mt-0.5">
                  Profile: <span className="text-gray-200 font-medium">{currentModeStats.mode || 'unknown'}</span>
                </p>
              </div>
              <div className="text-right text-[11px] text-gray-400">
                <p>Updated: {formatTimeAgo(currentModeStats.last_sample_at)}</p>
                {currentModeStats.resets_count > 0 && (
                  <p className="text-amber-300">Resets: {currentModeStats.resets_count}</p>
                )}
              </div>
            </div>
            {currentModeStats.sample_count < 10 && (
              <div className="rounded border border-amber-500/25 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-200">
                Low-confidence estimate: fewer than 10 samples for this mode.
              </div>
            )}
            <div className="grid grid-cols-2 gap-2 text-xs">
              <div>
                <p className="text-gray-400">Mode Avg</p>
                <p className="font-semibold">
                  {currentModeStats.avg_power_watts !== null ? `${currentModeStats.avg_power_watts.toFixed(1)} W` : '—'}
                </p>
              </div>
              <div>
                <p className="text-gray-400">Mode EMA</p>
                <p className="font-semibold">
                  {currentModeStats.ema_power_watts !== null ? `${currentModeStats.ema_power_watts.toFixed(1)} W` : '—'}
                </p>
              </div>
              <div>
                <p className="text-gray-400">Samples</p>
                <p className="font-semibold">{currentModeStats.sample_count}</p>
              </div>
              <div>
                <p className="text-gray-400">Range</p>
                <p className="font-semibold">
                  {currentModeStats.min_power_watts !== null && currentModeStats.max_power_watts !== null
                    ? `${currentModeStats.min_power_watts.toFixed(0)}-${currentModeStats.max_power_watts.toFixed(0)} W`
                    : '—'}
                </p>
              </div>
            </div>

            {displayModes.length > 0 && (
              <div className="border-t border-blue-500/20 pt-2">
                <p className="text-[11px] uppercase tracking-wide text-gray-400 mb-1">All Modes</p>
                <div className="space-y-1">
                  {displayModes.map((mode) => {
                    const modeStats = statsByMode.get(mode);
                    const hasData = Boolean(modeStats && modeStats.sample_count > 0 && modeStats.avg_power_watts !== null);
                    return (
                      <div key={mode} className="flex items-center justify-between text-[11px]">
                        <span className="text-gray-300 uppercase">{mode}</span>
                        {hasData ? (
                          <span className="text-gray-200">
                            {modeStats?.avg_power_watts?.toFixed(1)} W ({modeStats?.sample_count} samples)
                          </span>
                        ) : (
                          <span className="text-amber-200">Not enough data yet</span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Health warning */}
        {hasHealthIssue && (
          <div className="flex items-center gap-2 text-red-400 text-xs bg-red-500/10 rounded-lg p-2 border border-red-500/20">
            <AlertCircle className="h-3 w-3 flex-shrink-0" />
            <span>Health score: {miner.health_score}%</span>
          </div>
        )}

        {/* Actions */}
        <div className="flex gap-2 pt-2 border-t border-gray-700/50">
          <Button
            variant="outline"
            size="sm"
            className="flex-1 text-xs"
            asChild
          >
            <Link to={`/miners/${miner.id}`}>View</Link>
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="flex-1 text-xs"
            asChild
          >
            <Link to={`/miners/${miner.id}/edit`}>Edit</Link>
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
