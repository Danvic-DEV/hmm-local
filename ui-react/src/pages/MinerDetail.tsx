import { useState } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { 
  Settings, Activity, Sliders, Network, Power, RefreshCw, 
  Trash2, Edit, AlertCircle, CheckCircle, Clock 
} from 'lucide-react';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Label } from '@/components/ui/label';
import { formatHashrate } from '@/lib/utils';
import type { Miner } from '@/types/miner';
import type { MinerTelemetry, MinerModes, Pool, DevicePool } from '@/types/telemetry';

interface ModePowerStatsRow {
  mode: string;
  sample_count: number;
  avg_power_watts: number | null;
  ema_power_watts: number | null;
  min_power_watts: number | null;
  max_power_watts: number | null;
  last_power_watts: number | null;
  last_sample_at: string | null;
  resets_count: number;
}

interface MinerModePowerStatsResponse {
  miner_id: number;
  current_mode: string | null;
  current_mode_stats: ModePowerStatsRow | null;
  modes: ModePowerStatsRow[];
}

const getExpectedModesForMinerType = (minerType?: string) => {
  const type = (minerType || '').toLowerCase();
  if (type === 'avalon_nano') return ['low', 'med', 'high'];
  if (type === 'bitaxe' || type === 'nerdqaxe') return ['eco', 'standard', 'turbo', 'oc'];
  if (type === 'nmminer') return ['low', 'med', 'high'];
  return [];
};

export default function MinerDetail() {
  const { minerId } = useParams<{ minerId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  
  const [selectedMode, setSelectedMode] = useState<string>('');
  const [selectedPoolId, setSelectedPoolId] = useState<number | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);

  // Fetch miner details from dashboard (for basic info)
  const { data: allMiners } = useQuery<{ miners: Miner[] }>({
    queryKey: ['miners'],
    queryFn: async () => {
      const response = await fetch(`/api/dashboard/all?dashboard_type=all`);
      if (!response.ok) throw new Error('Failed to fetch miners');
      return response.json();
    },
  });

  const miner = allMiners?.miners.find(m => m.id === parseInt(minerId || '0'));

  // Fetch full miner details (includes IP, port, config)
  const { data: minerDetails } = useQuery({
    queryKey: ['minerDetails', minerId],
    queryFn: async () => {
      const response = await fetch(`/api/miners/${minerId}`);
      if (!response.ok) throw new Error('Failed to fetch miner details');
      return response.json();
    },
    enabled: !!minerId,
  });

  // Fetch telemetry with auto-refresh
  const { data: telemetry, isLoading: telemetryLoading, error: telemetryError } = useQuery<MinerTelemetry>({
    queryKey: ['telemetry', minerId],
    queryFn: async () => {
      const response = await fetch(`/api/miners/${minerId}/telemetry`);
      if (!response.ok) throw new Error('Failed to fetch telemetry');
      return response.json();
    },
    refetchInterval: autoRefresh ? 5000 : false, // Auto-refresh every 5 seconds
    enabled: !!minerId,
  });

  const extra = telemetry?.extra_data ?? {};
  const currentMode = extra.current_mode;
  const uptimeSeconds = extra.uptime_seconds ?? extra.uptime;
  const frequencyMhz = extra.frequency_mhz ?? extra.frequency;
  const voltageMv = extra.voltage_mv ?? extra.voltage;
  const bestShareDiff = extra.best_share_diff ?? extra.best_diff;
  const poolDifficulty = extra.pool_difficulty ?? extra.difficulty;
  const poolResponseMs = extra.pool_response_ms ?? extra.response_time;
  const errorRatePct = extra.error_rate_pct ?? extra.error_percentage;
  const firmwareVersion = extra.firmware_version ?? extra.version;
  const coreVoltageMv = extra.core_voltage_mv ?? extra.core_voltage;
  const coreVoltageActualMv = extra.core_voltage_actual_mv ?? extra.core_voltage_actual;
  const vrTempC = extra.vr_temp_c ?? extra.vr_temp;
  const freeHeapBytes = extra.free_heap_bytes ?? extra.free_heap;
  const wifiRssi = extra.wifi_rssi ?? extra.rssi;
  const hardwareErrors = extra.hardware_errors ?? extra.hw_errors;

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

  // Fetch available modes
  const { data: modesData } = useQuery<MinerModes>({
    queryKey: ['modes', minerId],
    queryFn: async () => {
      const response = await fetch(`/api/miners/${minerId}/modes`);
      if (!response.ok) throw new Error('Failed to fetch modes');
      return response.json();
    },
    enabled: !!minerId,
  });

  const { data: modePowerStats } = useQuery<MinerModePowerStatsResponse>({
    queryKey: ['mode-power-stats', minerId],
    queryFn: async () => {
      const response = await fetch(`/api/miners/${minerId}/mode-power-stats`);
      if (!response.ok) throw new Error('Failed to fetch mode power stats');
      return response.json();
    },
    enabled: !!minerId,
    refetchInterval: autoRefresh ? 30000 : false,
  });

  const expectedModes = getExpectedModesForMinerType(miner?.miner_type);
  const modeStatsRows = modePowerStats?.modes ?? [];
  const modeStatsMap = new Map(modeStatsRows.map((row) => [row.mode, row]));
  const displayModes = [
    ...expectedModes,
    ...modeStatsRows.map((row) => row.mode).filter((mode) => !expectedModes.includes(mode)),
  ];

  // Fetch pools
  const { data: pools = [] } = useQuery<Pool[]>({
    queryKey: ['pools'],
    queryFn: async () => {
      const response = await fetch(`/api/pools/`);
      if (!response.ok) throw new Error('Failed to fetch pools');
      return response.json();
    },
  });

  // Fetch device pools (for current pool selection)
  useQuery<DevicePool[]>({
    queryKey: ['devicePools', minerId],
    queryFn: async () => {
      const response = await fetch(`/api/miners/${minerId}/device-pools`);
      if (!response.ok) return [];
      return response.json();
    },
    enabled: !!minerId,
  });

  // Set mode mutation
  const setModeMutation = useMutation({
    mutationFn: async (mode: string) => {
      const response = await fetch(`/api/miners/${minerId}/mode?mode=${mode}`, {
        method: 'POST',
      });
      if (!response.ok) throw new Error('Failed to set mode');
      return response.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['telemetry', minerId] });
      queryClient.invalidateQueries({ queryKey: ['miners'] });
    },
  });

  // Switch pool mutation
  const switchPoolMutation = useMutation({
    mutationFn: async (poolId: number) => {
      const response = await fetch(`/api/miners/${minerId}/pool?pool_id=${poolId}`, {
        method: 'POST',
      });
      if (!response.ok) throw new Error('Failed to switch pool');
      return response.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['telemetry', minerId] });
      queryClient.invalidateQueries({ queryKey: ['devicePools', minerId] });
    },
  });

  // Restart miner mutation
  const restartMutation = useMutation({
    mutationFn: async () => {
      const response = await fetch(`/api/miners/${minerId}/restart`, {
        method: 'POST',
      });
      if (!response.ok) throw new Error('Failed to restart miner');
      return response.json();
    },
  });

  // Delete miner mutation
  const deleteMutation = useMutation({
    mutationFn: async () => {
      const response = await fetch(`/api/miners/${minerId}`, {
        method: 'DELETE',
      });
      if (!response.ok) throw new Error('Failed to delete miner');
      return response.json();
    },
    onSuccess: () => {
      navigate('/miners');
    },
  });

  const handleSetMode = () => {
    if (selectedMode) {
      setModeMutation.mutate(selectedMode);
    }
  };

  const handleSwitchPool = () => {
    if (selectedPoolId !== null) {
      switchPoolMutation.mutate(selectedPoolId);
    }
  };

  const handleRestart = () => {
    if (confirm('Are you sure you want to restart this miner?')) {
      restartMutation.mutate();
    }
  };

  const handleDelete = () => {
    if (confirm(`Are you sure you want to delete ${miner?.name}? This cannot be undone.`)) {
      deleteMutation.mutate();
    }
  };

  const formatUptime = (seconds: number) => {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    
    if (days > 0) return `${days}d ${hours}h ${mins}m`;
    if (hours > 0) return `${hours}h ${mins}m`;
    return `${mins}m`;
  };

  const formatNumber = (num: number | string | undefined | null, decimals = 2) => {
    if (num === undefined || num === null) return '—';
    
    // Convert string to number if needed
    const value = typeof num === 'string' ? parseFloat(num) : num;
    
    // Check if conversion failed or not a number
    if (isNaN(value)) return '—';
    
    if (value >= 1000000000) return `${(value / 1000000000).toFixed(2)}B`;
    if (value >= 1000000) return `${(value / 1000000).toFixed(2)}M`;
    if (value >= 1000) return `${(value / 1000).toFixed(2)}K`;
    return value.toFixed(decimals);
  };

  // Show loading or not found state
  if (!allMiners) {
    return (
      <div className="p-6">
        <Card>
          <CardContent className="p-6">
            <div className="flex items-center gap-3 text-gray-400">
              <div className="animate-spin h-5 w-5 border-2 border-gray-400 border-t-transparent rounded-full"></div>
              <p>Loading...</p>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!miner) {
    return (
      <div className="p-6">
        <Card className="border-red-500/20 bg-red-500/5">
          <CardContent className="p-6">
            <div className="flex items-center gap-3 text-red-400">
              <AlertCircle className="h-5 w-5" />
              <p>Miner not found</p>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{miner.name}</h1>
          <p className="text-gray-400 text-sm mt-1">{miner.miner_type}</p>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setAutoRefresh(!autoRefresh)}
            className="gap-2"
          >
            <RefreshCw className={`h-4 w-4 ${autoRefresh ? 'animate-spin' : ''}`} />
            {autoRefresh ? 'Auto' : 'Manual'}
          </Button>
          <Button
            variant="outline"
            size="sm"
            asChild
          >
            <Link to={`/miners/${minerId}/edit`}>
              <Edit className="h-4 w-4 mr-2" />
              Edit
            </Link>
          </Button>
          <Button
            variant="destructive"
            size="sm"
            onClick={handleDelete}
            disabled={deleteMutation.isPending}
          >
            <Trash2 className="h-4 w-4 mr-2" />
            Delete
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Configuration Card */}
        <Card>
          <CardHeader className="flex flex-row items-center gap-3 pb-3">
            <Settings className="h-5 w-5 text-gray-400" />
            <h3 className="font-semibold">Configuration</h3>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm text-gray-400">Status</span>
              <span className={`px-2 py-1 rounded text-xs font-medium ${miner.enabled ? 'bg-green-500/10 text-green-400 border border-green-500/20' : 'bg-gray-500/10 text-gray-400 border border-gray-500/20'}`}>
                {miner.enabled ? 'Enabled' : 'Disabled'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm text-gray-400">Type</span>
              <span className="text-sm font-medium">{miner.miner_type}</span>
            </div>
            {minerDetails?.ip_address && (
              <div className="flex items-center justify-between">
                <span className="text-sm text-gray-400">IP Address</span>
                <span className="text-sm font-medium">{minerDetails.ip_address}</span>
              </div>
            )}
            {minerDetails?.ip_address && (
              <div className="flex items-center justify-between">
                <span className="text-sm text-gray-400">URL</span>
                <a 
                  href={`http://${minerDetails.ip_address}`}
                  target="_blank" 
                  rel="noopener noreferrer"
                  className="text-sm text-blue-400 hover:text-blue-300 hover:underline"
                >
                  Open Device
                </a>
              </div>
            )}
            {currentMode && (
              <div className="flex items-center justify-between">
                <span className="text-sm text-gray-400">Current Mode</span>
                <span className="text-sm font-medium">{currentMode}</span>
              </div>
            )}
            <div className="flex items-center justify-between">
              <span className="text-sm text-gray-400">Current Pool</span>
              <span className="text-sm font-medium truncate max-w-[150px]">
                {telemetry?.pool_in_use || '—'}
              </span>
            </div>
          </CardContent>
        </Card>

        {/* Live Telemetry - Primary Stats */}
        <Card className="lg:col-span-2">
          <CardHeader className="flex flex-row items-center gap-3 pb-3">
            <Activity className="h-5 w-5 text-gray-400" />
            <h3 className="font-semibold">Live Telemetry</h3>
            {telemetry && (
              <span className="text-xs text-gray-500 ml-auto">
                <Clock className="h-3 w-3 inline mr-1" />
                {new Date(telemetry.timestamp).toLocaleTimeString()}
              </span>
            )}
          </CardHeader>
          <CardContent>
            {telemetryLoading && (
              <div className="text-center py-8 text-gray-400">Loading telemetry...</div>
            )}
            
            {telemetryError && (
              <div className="flex items-center gap-3 text-red-400 p-4 bg-red-500/10 rounded-lg border border-red-500/20">
                <AlertCircle className="h-5 w-5 flex-shrink-0" />
                <p className="text-sm">Failed to load telemetry data</p>
              </div>
            )}

            {telemetry && (
              <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                {telemetry.hashrate !== null && telemetry.hashrate !== undefined && (
                  <StatBox label="Hashrate" value={typeof telemetry.hashrate === 'object' ? telemetry.hashrate.display : formatHashrate(telemetry.hashrate)} />
                )}
                {telemetry.temperature !== null && telemetry.temperature !== undefined && (
                  <StatBox label="Temperature" value={`${telemetry.temperature.toFixed(1)}°C`} />
                )}
                {telemetry.power_watts !== null && telemetry.power_watts !== undefined && (
                  <StatBox label="Power" value={`${telemetry.power_watts.toFixed(1)} W`} />
                )}
                {telemetry.shares_accepted !== null && telemetry.shares_accepted !== undefined && (
                  <StatBox label="Accepted" value={telemetry.shares_accepted.toString()} />
                )}
                {telemetry.shares_rejected !== null && telemetry.shares_rejected !== undefined && (
                  <StatBox label="Rejected" value={telemetry.shares_rejected.toString()} />
                )}
                <StatBox label="Uptime" value={uptimeSeconds ? formatUptime(uptimeSeconds) : '—'} />
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Extended Stats */}
      {telemetry && (
        <Card>
          <CardHeader>
            <h3 className="font-semibold">Extended Stats</h3>
          </CardHeader>
          <CardContent>
            {modePowerStats?.current_mode_stats && (
              <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-3 mb-4">
                <div className="flex items-start justify-between gap-3 mb-3">
                  <div>
                    <p className="text-xs uppercase tracking-wide text-blue-300 font-medium">Mode Power Profile</p>
                    <p className="text-xs text-gray-400 mt-1">
                      Profile: <span className="text-gray-200 font-medium">{modePowerStats.current_mode_stats.mode || 'unknown'}</span>
                    </p>
                  </div>
                  <div className="text-right text-xs text-gray-400">
                    <p>Updated: {formatTimeAgo(modePowerStats.current_mode_stats.last_sample_at)}</p>
                    {modePowerStats.current_mode_stats.resets_count > 0 && (
                      <p className="text-amber-300">Resets: {modePowerStats.current_mode_stats.resets_count}</p>
                    )}
                  </div>
                </div>
                {modePowerStats.current_mode_stats.sample_count < 10 && (
                  <div className="rounded border border-amber-500/25 bg-amber-500/10 px-2 py-1 mb-3 text-xs text-amber-200">
                    Low-confidence estimate: fewer than 10 samples for this mode.
                  </div>
                )}
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                  <StatBox
                    label="Mode Avg"
                    value={modePowerStats.current_mode_stats.avg_power_watts !== null
                      ? `${modePowerStats.current_mode_stats.avg_power_watts.toFixed(1)} W`
                      : '—'}
                  />
                  <StatBox
                    label="Mode EMA"
                    value={modePowerStats.current_mode_stats.ema_power_watts !== null
                      ? `${modePowerStats.current_mode_stats.ema_power_watts.toFixed(1)} W`
                      : '—'}
                  />
                  <StatBox label="Samples" value={`${modePowerStats.current_mode_stats.sample_count}`} />
                  <StatBox
                    label="Range"
                    value={modePowerStats.current_mode_stats.min_power_watts !== null && modePowerStats.current_mode_stats.max_power_watts !== null
                      ? `${modePowerStats.current_mode_stats.min_power_watts.toFixed(0)}-${modePowerStats.current_mode_stats.max_power_watts.toFixed(0)} W`
                      : '—'}
                  />
                </div>

                {displayModes.length > 0 && (
                  <div className="mt-3 border-t border-blue-500/20 pt-3">
                    <p className="text-xs uppercase tracking-wide text-gray-400 mb-2">All Modes</p>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                      {displayModes.map((mode) => {
                        const row = modeStatsMap.get(mode);
                        const hasData = Boolean(row && row.sample_count > 0 && row.avg_power_watts !== null);
                        return (
                          <div key={mode} className="flex items-center justify-between rounded border border-gray-700/60 bg-gray-900/40 px-3 py-2 text-sm">
                            <span className="uppercase text-gray-300">{mode}</span>
                            {hasData ? (
                              <span className="text-gray-100">
                                {row?.avg_power_watts?.toFixed(1)} W ({row?.sample_count} samples)
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
            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
              <StatBox label="Frequency" value={frequencyMhz ? `${frequencyMhz} MHz` : '—'} />
              <StatBox label="Voltage" value={voltageMv !== null && voltageMv !== undefined ? `${(voltageMv / 1000).toFixed(2)} V` : '—'} />
              <StatBox label="Best Diff" value={formatNumber(bestShareDiff)} />
              <StatBox label="Best Session" value={formatNumber(telemetry.extra_data.best_session_diff)} />
              <StatBox label="Pool Difficulty" value={formatNumber(poolDifficulty)} />
              <StatBox label="Network Diff" value={formatNumber(telemetry.extra_data.network_difficulty)} />
              <StatBox label="Pool Response" value={poolResponseMs !== undefined && poolResponseMs !== null ? `${poolResponseMs.toFixed(0)} ms` : '—'} />
              <StatBox label="Error Rate" value={errorRatePct !== undefined && errorRatePct !== null ? `${errorRatePct.toFixed(2)}%` : '—'} />
              <StatBox label="WiFi Signal" value={wifiRssi ? String(wifiRssi) : '—'} />
              <StatBox label="Firmware" value={firmwareVersion ? String(firmwareVersion) : '—'} />
              {telemetry.extra_data.found_blocks !== undefined && (
                <StatBox label="Found Blocks 🎉" value={telemetry.extra_data.found_blocks.toString()} />
              )}
              {hardwareErrors !== undefined && hardwareErrors !== null && (
                <StatBox label="HW Errors" value={String(hardwareErrors)} />
              )}
              {coreVoltageMv && (
                <StatBox label="Core Voltage" value={`${coreVoltageMv} mV`} />
              )}
              {coreVoltageActualMv && (
                <StatBox label="Core V Actual" value={`${coreVoltageActualMv} mV`} />
              )}
              {vrTempC !== undefined && (
                <StatBox label="VR Temp" value={`${vrTempC}°C`} />
              )}
              {telemetry.extra_data.small_core_count && (
                <StatBox label="Small Cores" value={telemetry.extra_data.small_core_count.toString()} />
              )}
              {telemetry.extra_data.asic_model && (
                <StatBox label="ASIC Model" value={telemetry.extra_data.asic_model} />
              )}
              {telemetry.extra_data.block_height && (
                <StatBox label="Block Height" value={formatNumber(telemetry.extra_data.block_height, 0)} />
              )}
              {telemetry.extra_data.stratum_suggested_difficulty && (
                <StatBox label="Stratum Diff" value={formatNumber(telemetry.extra_data.stratum_suggested_difficulty)} />
              )}
              {freeHeapBytes !== null && freeHeapBytes !== undefined && (
                <StatBox label="Free Heap" value={`${(freeHeapBytes / 1024 / 1024).toFixed(1)} MB`} />
              )}
              {telemetry.extra_data.fan_rpm !== null && telemetry.extra_data.fan_rpm !== undefined && (
                <StatBox label="Fan RPM" value={telemetry.extra_data.fan_rpm.toString()} />
              )}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Controls */}
      <Card>
        <CardHeader className="flex flex-row items-center gap-3 pb-3">
          <Sliders className="h-5 w-5 text-gray-400" />
          <h3 className="font-semibold">Controls</h3>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* Mode Control */}
          <div className="space-y-3">
              <Label>Operating Mode</Label>
              <div className="flex gap-2">
                <Select value={selectedMode} onValueChange={setSelectedMode}>
                  <SelectTrigger className="flex-1">
                    <SelectValue placeholder="Select mode..." />
                  </SelectTrigger>
                  <SelectContent>
                    {modesData?.modes.map(mode => (
                      <SelectItem key={mode} value={mode}>
                        {mode.charAt(0).toUpperCase() + mode.slice(1)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button 
                  onClick={handleSetMode} 
                  disabled={!selectedMode || setModeMutation.isPending}
                >
                  {setModeMutation.isPending ? 'Applying...' : 'Apply Mode'}
                </Button>
              </div>
              {setModeMutation.isSuccess && (
                <div className="flex items-center gap-2 text-green-400 text-sm">
                  <CheckCircle className="h-4 w-4" />
                  Mode updated successfully
                </div>
              )}
            </div>

            {/* Pool Control */}
            <div className="space-y-3">
              <Label>Pool Selection</Label>
              <div className="space-y-2 max-h-60 overflow-y-auto">
                {pools.map(pool => (
                  <label
                    key={pool.id}
                    className="flex items-center gap-3 p-3 rounded-lg border border-gray-700 hover:bg-gray-800/50 cursor-pointer transition-colors"
                  >
                    <input
                      type="radio"
                      name="pool"
                      value={pool.id}
                      checked={selectedPoolId === pool.id}
                      onChange={() => setSelectedPoolId(pool.id)}
                      className="text-blue-500"
                    />
                    <div className="flex-1 min-w-0">
                      <p className="font-medium text-sm">{pool.name}</p>
                      <p className="text-xs text-gray-400 truncate">{pool.url}:{pool.port}</p>
                    </div>
                  </label>
                ))}
              </div>
              <Button 
                onClick={handleSwitchPool}
                disabled={selectedPoolId === null || switchPoolMutation.isPending}
                className="w-full"
              >
                <Network className="h-4 w-4 mr-2" />
                {switchPoolMutation.isPending ? 'Switching...' : 'Switch Pool'}
              </Button>
              {switchPoolMutation.isSuccess && (
                <div className="flex items-center gap-2 text-green-400 text-sm">
                  <CheckCircle className="h-4 w-4" />
                  Pool switched successfully
                </div>
              )}
            </div>

            {/* Restart Control */}
            <div className="space-y-3">
              <Label>Device Control</Label>
              <Button 
                onClick={handleRestart}
                disabled={restartMutation.isPending}
                variant="outline"
                className="w-full gap-2 text-orange-400 hover:text-orange-300 border-orange-500/20"
              >
                <Power className="h-4 w-4" />
                {restartMutation.isPending ? 'Restarting...' : 'Restart Miner'}
              </Button>
              {restartMutation.isSuccess && (
                <div className="flex items-center gap-2 text-green-400 text-sm">
                  <CheckCircle className="h-4 w-4" />
                  Restart command sent
                </div>
              )}
            </div>
          </CardContent>
        </Card>
    </div>
  );
}

interface StatBoxProps {
  label: string;
  value: string;
  className?: string;
}

function StatBox({ label, value, className = '' }: StatBoxProps) {
  return (
    <div className={`bg-gray-800/50 rounded-lg p-3 border border-gray-700/50 ${className}`}>
      <div className="text-xs text-gray-400 uppercase tracking-wide mb-1">{label}</div>
      <div className="font-semibold text-sm truncate">{value}</div>
    </div>
  );
}
