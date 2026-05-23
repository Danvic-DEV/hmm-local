import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { LayoutGrid, List, Plus, AlertCircle } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import MinerTile from '@/components/miners/MinerTile';
import MinerTable from '@/components/miners/MinerTable';
import BulkActionsBar from '@/components/miners/BulkActionsBar';
import BulkModeModal from '@/components/miners/BulkModeModal';
import BulkPoolModal from '@/components/miners/BulkPoolModal';
import type { MinersResponse, ViewMode } from '@/types/miner';

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

interface MinerModePowerStats {
  miner_id: number;
  current_mode: string | null;
  current_mode_stats: ModePowerStatsRow | null;
  modes: ModePowerStatsRow[];
}

interface ModePowerStatsResponse {
  miners: Record<string, MinerModePowerStats>;
}

export default function Miners() {
  // View mode state (persisted to localStorage)
  const [viewMode, setViewMode] = useState<ViewMode>(() => {
    return (localStorage.getItem('minersView') as ViewMode) || 'tiles';
  });

  // Selection state
  const [selectedMiners, setSelectedMiners] = useState<Set<number>>(new Set());

  // Live update highlight
  const [liveMinerId, setLiveMinerId] = useState<number | null>(null);

  // Modal state
  const [showModeModal, setShowModeModal] = useState(false);
  const [showPoolModal, setShowPoolModal] = useState(false);

  useEffect(() => {
    const handleRealtimeUpdate = (event: Event) => {
      const detail = (event as CustomEvent).detail as { data?: { miner_id?: number } } | undefined;
      const minerId = detail?.data?.miner_id;
      if (!minerId) return;

      setLiveMinerId(minerId);
      window.setTimeout(() => {
        setLiveMinerId((current) => (current === minerId ? null : current));
      }, 2000);
    };

    window.addEventListener('realtime-update', handleRealtimeUpdate as EventListener);
    return () => {
      window.removeEventListener('realtime-update', handleRealtimeUpdate as EventListener);
    };
  }, []);

  // Fetch miners data
  const { data, isLoading, error, refetch } = useQuery<MinersResponse>({
    queryKey: ['miners'],
    queryFn: async () => {
      const response = await fetch('/api/dashboard/all?dashboard_type=all');
      if (!response.ok) throw new Error('Failed to fetch miners');
      return response.json();
    },
    refetchInterval: 30000, // Poll every 30 seconds
  });

  const { data: modePowerStatsData } = useQuery<ModePowerStatsResponse>({
    queryKey: ['mode-power-stats'],
    queryFn: async () => {
      const response = await fetch('/api/miners/mode-power-stats');
      if (!response.ok) throw new Error('Failed to fetch mode power stats');
      return response.json();
    },
    refetchInterval: 60000,
  });

  // Sort miners alphabetically
  const sortedMiners = useMemo(() => {
    if (!data?.miners) return [];
    
    const ASIC_TYPES = ['avalon_nano', 'bitaxe', 'nerdqaxe', 'nmminer'];
    
    // Filter to only show supported ASIC types
    const asicMiners = data.miners.filter(m => ASIC_TYPES.includes(m.miner_type));
    
    return [...asicMiners].sort((a, b) => {
      const aIsASIC = ASIC_TYPES.includes(a.miner_type);
      const bIsASIC = ASIC_TYPES.includes(b.miner_type);
      
      // ASIC miners first
      if (aIsASIC && !bIsASIC) return -1;
      if (!aIsASIC && bIsASIC) return 1;
      
      // Within same type, sort alphabetically
      return a.name.toLowerCase().localeCompare(b.name.toLowerCase());
    });
  }, [data?.miners]);

  // Handle view toggle
  const handleViewChange = (mode: ViewMode) => {
    setViewMode(mode);
    localStorage.setItem('minersView', mode);
  };

  // Handle selection
  const toggleSelection = (minerId: number) => {
    setSelectedMiners(prev => {
      const next = new Set(prev);
      if (next.has(minerId)) {
        next.delete(minerId);
      } else {
        next.add(minerId);
      }
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (selectedMiners.size === sortedMiners.length) {
      setSelectedMiners(new Set());
    } else {
      setSelectedMiners(new Set(sortedMiners.map(m => m.id)));
    }
  };

  const clearSelection = () => {
    setSelectedMiners(new Set());
  };

  // Bulk operations
  const handleBulkOperation = async (operation: string, payload?: Record<string, unknown>) => {
    const minerIds = Array.from(selectedMiners);
    
    try {
      const response = await fetch(`/api/bulk/${operation}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ miner_ids: minerIds, ...payload }),
      });
      
      if (!response.ok) throw new Error(`Bulk operation failed: ${operation}`);
      
      // Refetch data and clear selection
      await refetch();
      clearSelection();
    } catch (error) {
      console.error('Bulk operation error:', error);
      // TODO: Show toast notification
    }
  };

  // Loading state
  if (isLoading) {
    return (
      <div className="p-6">
        <div className="flex items-center justify-center min-h-[400px]">
          <div className="text-center">
            <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-500 mx-auto mb-4"></div>
            <p className="text-gray-400">Loading miners...</p>
          </div>
        </div>
      </div>
    );
  }

  // Error state
  if (error) {
    return (
      <div className="p-6">
        <Card className="border-red-500/20 bg-red-500/5">
          <CardContent className="p-6">
            <div className="flex items-center gap-3 text-red-400">
              <AlertCircle className="h-5 w-5 flex-shrink-0" />
              <div>
                <p className="font-medium">Failed to load miners</p>
                <p className="text-sm text-gray-400 mt-1">{error instanceof Error ? error.message : 'Unknown error'}</p>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  // Empty state
  if (!sortedMiners.length) {
    return (
      <div className="p-6">
        <Card>
          <CardContent className="p-12 text-center">
            <div className="max-w-md mx-auto">
              <div className="w-16 h-16 rounded-full bg-gray-800 flex items-center justify-center mx-auto mb-4">
                <LayoutGrid className="h-8 w-8 text-gray-400" />
              </div>
              <h3 className="text-lg font-semibold mb-2">No miners configured</h3>
              <p className="text-gray-400 mb-6">
                Use <a href="/settings/discovery" className="text-blue-400 hover:text-blue-300">Network Discovery</a> to scan for miners, or add one manually.
              </p>
              <Button asChild>
                <Link to="/miners/add">
                  <Plus className="h-4 w-4 mr-2" />
                  Add Miner
                </Link>
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      {/* Header with view toggle */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Miners</h1>
          <p className="text-gray-400 text-sm mt-1">
            {sortedMiners.length} miner{sortedMiners.length !== 1 ? 's' : ''} · {data?.stats.online_miners || 0} online
          </p>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant={viewMode === 'tiles' ? 'default' : 'outline'}
            size="sm"
            onClick={() => handleViewChange('tiles')}
            className="gap-2"
          >
            <LayoutGrid className="h-4 w-4" />
            Tiles
          </Button>
          <Button
            variant={viewMode === 'table' ? 'default' : 'outline'}
            size="sm"
            onClick={() => handleViewChange('table')}
            className="gap-2"
          >
            <List className="h-4 w-4" />
            Table
          </Button>
        </div>
      </div>

      {/* Bulk actions bar */}
      {selectedMiners.size > 0 && (
        <BulkActionsBar
          selectedCount={selectedMiners.size}
          onClear={clearSelection}
          onEnable={() => handleBulkOperation('enable')}
          onDisable={() => handleBulkOperation('disable')}
          onRestart={() => handleBulkOperation('restart')}
          onSetMode={() => setShowModeModal(true)}
          onSwitchPool={() => setShowPoolModal(true)}
        />
      )}

      {/* Miners grid/table */}
      {viewMode === 'tiles' ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {sortedMiners.map(miner => (
            <MinerTile
              key={miner.id}
              miner={miner}
              modePowerStats={modePowerStatsData?.miners?.[String(miner.id)] ?? null}
              selected={selectedMiners.has(miner.id)}
              highlight={liveMinerId === miner.id}
              onToggleSelect={() => toggleSelection(miner.id)}
            />
          ))}
          
          {/* Add miner tile */}
          <Link
            to="/miners/add"
            className="border-2 border-dashed border-gray-700 rounded-lg p-6 flex flex-col items-center justify-center min-h-[200px] hover:border-blue-500 hover:bg-gray-800/30 transition-all group"
          >
            <div className="w-12 h-12 rounded-full bg-gray-800 flex items-center justify-center mb-3 group-hover:bg-blue-500/20 transition-colors">
              <Plus className="h-6 w-6 text-gray-400 group-hover:text-blue-400" />
            </div>
            <p className="font-medium text-gray-300 group-hover:text-white">Add Miner</p>
            <p className="text-sm text-gray-500 mt-1">Configure a new miner</p>
          </Link>
        </div>
      ) : (
        <MinerTable
          miners={sortedMiners}
          selectedMiners={selectedMiners}
          onToggleSelect={toggleSelection}
          onToggleSelectAll={toggleSelectAll}
        />
      )}

      {/* Bulk operation modals */}
      <BulkModeModal
        open={showModeModal}
        onClose={() => setShowModeModal(false)}
        onSubmit={(mode) => {
          handleBulkOperation('set-mode', { mode });
          setShowModeModal(false);
        }}
      />

      <BulkPoolModal
        open={showPoolModal}
        onClose={() => setShowPoolModal(false)}
        onSubmit={(poolId) => {
          handleBulkOperation('switch-pool', { pool_id: poolId });
          setShowPoolModal(false);
        }}
      />
    </div>
  );
}
