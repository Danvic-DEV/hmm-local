import { StatsCard } from "@/components/widgets/StatsCard";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { dashboardAPI, poolsAPI, type DashboardData, type PoolTilesResponse } from "@/lib/api";
import { useNavigate } from "react-router-dom";
import { formatHashrate } from "@/lib/utils";
import { useEffect, useState } from "react";
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  DragEndEvent,
} from "@dnd-kit/core";
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { GripVertical } from "lucide-react";

// Coin-specific colors (Tailwind classes for borders/backgrounds)
const getCoinColor = (coin: string): { border: string; bg: string } => {
  const colors: Record<string, { border: string; bg: string }> = {
    BTC: { border: "border-orange-500/90", bg: "bg-orange-500/10" },
    BCH: { border: "border-green-500/90", bg: "bg-green-500/10" },
    DGB: { border: "border-blue-500/90", bg: "bg-blue-500/10" },
    BC2: { border: "border-purple-500/90", bg: "bg-purple-500/10" },
    LTC: { border: "border-gray-500/90", bg: "bg-gray-500/10" },
  };
  return colors[coin.toUpperCase()] || { border: "border-orange-500/90", bg: "bg-orange-500/10" };
};

// Coin-specific RGBA colors for sparkline charts
const getCoinSparklineColor = (coin: string): string => {
  const colors: Record<string, string> = {
    BTC: "rgba(249, 115, 22, 0.3)",   // orange-500 with 30% opacity
    BCH: "rgba(34, 197, 94, 0.3)",    // green-500 with 30% opacity
    DGB: "rgba(59, 130, 246, 0.3)",   // blue-500 with 30% opacity
    BC2: "rgba(168, 85, 247, 0.3)",   // purple-500 with 30% opacity
    LTC: "rgba(107, 114, 128, 0.3)",  // gray-500 with 30% opacity
  };
  return colors[coin.toUpperCase()] || "rgba(59, 130, 246, 0.3)";
};

// Format network difficulty with appropriate units
const formatNetworkDifficulty = (diff: number): string => {
  if (diff >= 1e12) return `${(diff / 1e12).toFixed(2)} T`;  // Trillion
  if (diff >= 1e9) return `${(diff / 1e9).toFixed(2)} B`;    // Billion
  if (diff >= 1e6) return `${(diff / 1e6).toFixed(2)} M`;    // Million
  if (diff >= 1e3) return `${(diff / 1e3).toFixed(2)} K`;    // Thousand
  return diff.toFixed(0);
};

// Get color for luck percentage
const getLuckColor = (luckPercentage: number | null | undefined): string => {
  if (luckPercentage === null || luckPercentage === undefined) return '';
  
  if (luckPercentage <= 100) return 'text-green-500';
  if (luckPercentage <= 200) return 'text-yellow-500';
  if (luckPercentage <= 300) return 'text-orange-500';
  return 'text-red-500';
};

const formatPoolWarning = (warning: string): string => {
  switch (warning) {
    case 'driver_unresolved':
      return 'Driver unresolved';
    case 'driver_not_loaded':
      return 'Driver not loaded';
    default:
      return warning;
  }
};

// Format time since last block
const formatTimeSince = (timestamp: string): string => {
  const now = new Date().getTime();
  const then = new Date(timestamp).getTime();
  const diffMs = now - then;
  const diffMins = Math.floor(diffMs / 60000);

  if (diffMins < 60) {
    return `${diffMins}m ago`;
  }

  const hours = Math.floor(diffMins / 60);
  const mins = diffMins % 60;

  if (hours < 24) {
    return mins > 0 ? `${hours}h ${mins}m ago` : `${hours}h ago`;
  }

  const days = Math.floor(hours / 24);
  const remainingHours = hours % 24;
  const remainingMins = diffMins % 60;

  if (remainingHours > 0 && remainingMins > 0) {
    return `${days}d ${remainingHours}h ${remainingMins}m ago`;
  } else if (remainingHours > 0) {
    return `${days}d ${remainingHours}h ago`;
  } else if (remainingMins > 0) {
    return `${days}d ${remainingMins}m ago`;
  }
  return `${days}d ago`;
};

// Sortable Pool Tile Component
interface SortablePoolTileProps {
  poolId: string;
  pool: {
    display_name: string;
    pool_type: string;
    sort_order?: number;
    warnings?: string[];
    supports_coins?: string[];
    tile_1_health?: {
      health_status: boolean;
      health_message?: string | null;
      latency_ms?: number | null;
    };
    tile_2_network?: {
      network_difficulty?: number | null;
      pool_hashrate?: number | { display: string; value: number; unit: string } | null;
    };
    tile_3_shares?: {
      shares_valid?: number | null;
      shares_invalid?: number | null;
      shares_stale?: number | null;
      reject_rate?: number | null;
    };
    tile_4_blocks?: {
      blocks_found_24h?: number | null;
      estimated_earnings_24h?: number | null;
      currency?: string | null;
      last_block_found?: string | null;
      confirmed_balance?: number | null;
      pending_balance?: number | null;
      luck_percentage?: number | null;
    };
    supports_earnings?: boolean;
    supports_balance?: boolean;
  };
  poolHashrateHistory?: { x: number; y: number }[];
  isDragging: boolean;
}

function SortablePoolTile({ poolId, pool, poolHashrateHistory, isDragging }: SortablePoolTileProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging: isThisDragging,
  } = useSortable({ id: poolId });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isThisDragging ? 0.5 : 1,
  };

  return (
    <div ref={setNodeRef} style={style} className="mb-4">
      <div className="flex items-center gap-2 mb-3 group">
        <button
          {...attributes}
          {...listeners}
          className={`cursor-grab active:cursor-grabbing p-1 rounded hover:bg-muted transition-colors ${
            isDragging ? "opacity-100" : "opacity-0 group-hover:opacity-100"
          }`}
          title="Drag to reorder"
        >
          <GripVertical className="h-5 w-5 text-muted-foreground" />
        </button>
        <h2 className="text-xl font-semibold">{pool.display_name}</h2>
        <span className="text-sm text-muted-foreground">({pool.pool_type})</span>
        {pool.warnings && pool.warnings.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {pool.warnings.map((warning) => (
              <span
                key={`${poolId}-${warning}`}
                className="inline-block rounded bg-amber-500/20 px-2 py-0.5 text-xs font-medium text-amber-700 dark:text-amber-300"
                title={formatPoolWarning(warning)}
              >
                {formatPoolWarning(warning)}
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4">
        {/* Tile 1: Health */}
        <StatsCard
          label="Pool Health"
          value={pool.tile_1_health?.health_status ? "Healthy" : "Unhealthy"}
          borderColor={pool.supports_coins && pool.supports_coins.length > 0 ? getCoinColor(pool.supports_coins[0]).border : undefined}
          badge={
            <span className={`inline-block px-1.5 py-0.5 text-xs font-semibold rounded ${
              pool.tile_1_health?.health_status ? "bg-green-500 text-white" : "bg-red-500 text-white"
            }`}>
              {pool.tile_1_health?.health_status ? "OK" : "ERROR"}
            </span>
          }
          subtext={
            <>
              {pool.tile_1_health?.health_message && (
                <div>{pool.tile_1_health.health_message}</div>
              )}
              {pool.warnings && pool.warnings.length > 0 && (
                <div className="text-amber-600 dark:text-amber-400">
                  {pool.warnings.map((warning) => formatPoolWarning(warning)).join(' • ')}
                </div>
              )}
              {pool.tile_1_health?.latency_ms !== null && pool.tile_1_health?.latency_ms !== undefined && (
                <div className="text-xs">Latency: {Number(pool.tile_1_health.latency_ms).toFixed(0)}ms</div>
              )}
            </>
          }
        />

        {/* Tile 2: Network */}
        <StatsCard
          label="Pool Hashrate"
          value={pool.tile_2_network?.pool_hashrate !== null && pool.tile_2_network?.pool_hashrate !== undefined 
            ? (typeof pool.tile_2_network.pool_hashrate === 'object' 
                ? pool.tile_2_network.pool_hashrate.display 
                : formatHashrate(pool.tile_2_network.pool_hashrate * 1000)) 
            : "N/A"}
          chartData={poolHashrateHistory || []}
          chartColor={pool.supports_coins && pool.supports_coins.length > 0 ? getCoinSparklineColor(pool.supports_coins[0]) : 'rgba(59, 130, 246, 0.3)'}
          subtext={
            pool.tile_2_network?.network_difficulty !== null && pool.tile_2_network?.network_difficulty !== undefined ? (
              <div className="text-xs">
                Network: {formatNetworkDifficulty(pool.tile_2_network.network_difficulty)}
              </div>
            ) : null
          }
        />

        {/* Tile 3: Shares */}
        <StatsCard
          label="Shares"
          value={pool.tile_3_shares?.shares_valid !== null && pool.tile_3_shares?.shares_valid !== undefined ? Number(pool.tile_3_shares.shares_valid).toLocaleString() : "N/A"}
          subtext={
            <>
              {pool.tile_3_shares?.shares_invalid !== null && pool.tile_3_shares?.shares_invalid !== undefined && pool.tile_3_shares.shares_invalid > 0 && (
                <div className="text-red-500">Invalid: {Number(pool.tile_3_shares.shares_invalid).toLocaleString()}</div>
              )}
              {pool.tile_3_shares?.shares_stale !== null && pool.tile_3_shares?.shares_stale !== undefined && pool.tile_3_shares.shares_stale > 0 && (
                <div className="text-yellow-500">Stale: {Number(pool.tile_3_shares.shares_stale).toLocaleString()}</div>
              )}
              {pool.tile_3_shares?.reject_rate !== null && pool.tile_3_shares?.reject_rate !== undefined && (
                <div className="text-xs">Reject: {Number(pool.tile_3_shares.reject_rate).toFixed(2)}%</div>
              )}
            </>
          }
        />

        {/* Tile 4: Blocks/Earnings */}
        <StatsCard
          label={pool.supports_earnings ? "Earnings (24h)" : "Blocks (24h)"}
          value={
            pool.supports_earnings
              ? `${Number(pool.tile_4_blocks?.confirmed_balance || 0).toFixed(8)} ${pool.tile_4_blocks?.currency || "BTC"}`
              : pool.tile_4_blocks?.blocks_found_24h !== null && pool.tile_4_blocks?.blocks_found_24h !== undefined
              ? `${pool.tile_4_blocks.blocks_found_24h} blocks`
              : "N/A"
          }
          subtext={
            <>
              {pool.tile_4_blocks?.last_block_found && (
                <div className="text-xs">Last: {formatTimeSince(pool.tile_4_blocks.last_block_found)}</div>
              )}
              {!pool.supports_earnings && pool.tile_4_blocks?.luck_percentage !== null && pool.tile_4_blocks?.luck_percentage !== undefined && (
                <div className={`text-xs font-semibold ${getLuckColor(pool.tile_4_blocks.luck_percentage)}`}>
                  Luck: {pool.tile_4_blocks.luck_percentage.toFixed(2)}%
                </div>
              )}
              {pool.supports_balance && (
                <>
                  {pool.tile_4_blocks?.confirmed_balance !== null && pool.tile_4_blocks?.confirmed_balance !== undefined && (
                    <div className="text-xs">Confirmed: {Number(pool.tile_4_blocks.confirmed_balance).toFixed(8)}</div>
                  )}
                  {pool.tile_4_blocks?.pending_balance !== null && pool.tile_4_blocks?.pending_balance !== undefined && (
                    <div className="text-xs">Pending: {Number(pool.tile_4_blocks.pending_balance).toFixed(8)}</div>
                  )}
                </>
              )}
            </>
          }
        />
      </div>
    </div>
  );
}

export function Dashboard() {
  console.log('[Dashboard] Component mounted - Chart.js should NOT be loaded')
  
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  
  // Pool ordering state
  const [poolOrder, setPoolOrder] = useState<string[]>([]);
  const [isDragging, setIsDragging] = useState(false);

  // Drag and drop sensors
  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    })
  );

  const { data, isLoading, error } = useQuery<DashboardData>({
    queryKey: ["dashboard", "all"],
    queryFn: () => dashboardAPI.getAll("asic"),
    refetchInterval: 10000, // Refresh every 10 seconds
  });

  // Fetch per-pool tiles
  const { data: poolTiles } = useQuery<PoolTilesResponse>({
    queryKey: ["pools", "tiles"],
    queryFn: () => poolsAPI.getPoolTiles(),
    refetchInterval: 30000, // Match backend cache (30 seconds)
  });

  // Fetch 24-hour hashrate history for all pools (for sparklines)
  const { data: poolHashrateHistory } = useQuery<Record<string, { x: number; y: number }[]>>({
    queryKey: ["pools", "hashrate-history"],
    queryFn: async () => {
      if (!poolTiles) return {};
      
      const poolIds = Object.keys(poolTiles);
      const histories: Record<string, { x: number; y: number }[]> = {};
      
      await Promise.all(
        poolIds.map(async (poolId) => {
          try {
            const response = await fetch(`/api/pools/${poolId}/hashrate/history?hours=24`);
            if (response.ok) {
              const data = await response.json();
              histories[poolId] = data.data || [];
            }
          } catch (error) {
            console.error(`Failed to fetch hashrate history for pool ${poolId}:`, error);
          }
        })
      );
      
      return histories;
    },
    enabled: !!poolTiles,
    refetchInterval: 60000, // Refresh every minute
  });
  
  // Initialize pool order when poolTiles loads, sorted by sort_order
  useEffect(() => {
    if (poolTiles && poolOrder.length === 0) {
      // Sort pool IDs by their sort_order value
      const sortedPoolIds = Object.keys(poolTiles).sort((a, b) => {
        const orderA = poolTiles[a]?.sort_order ?? 0;
        const orderB = poolTiles[b]?.sort_order ?? 0;
        return orderA - orderB;
      });
      setPoolOrder(sortedPoolIds);
    }
  }, [poolTiles, poolOrder.length]);
  
  // Mutation for reordering pools
  const reorderMutation = useMutation({
    mutationFn: (items: { pool_id: number; sort_order: number }[]) =>
      poolsAPI.reorderPools(items),
    onSuccess: () => {
      // Invalidate queries to refresh data with new order
      queryClient.invalidateQueries({ queryKey: ["pools", "tiles"] });
    },
  });
  
  // Handle drag end
  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    setIsDragging(false);
    
    if (!over || active.id === over.id) {
      return;
    }
    
    const oldIndex = poolOrder.indexOf(active.id as string);
    const newIndex = poolOrder.indexOf(over.id as string);
    
    const newOrder = arrayMove(poolOrder, oldIndex, newIndex);
    setPoolOrder(newOrder);
    
    // Create reorder payload
    const reorderItems = newOrder.map((poolId, index) => ({
      pool_id: parseInt(poolId),
      sort_order: index,
    }));
    
    // Call backend to persist
    reorderMutation.mutate(reorderItems);
  };

  const defaultBestShare: DashboardData["stats"]["best_share_24h"] = {
    difficulty: 0,
    coin: "",
    network_difficulty: 0,
    percentage: 0,
    timestamp: "",
    time_ago_seconds: 0,
  };

  const defaultStats: DashboardData["stats"] = {
    online_miners: 0,
    total_hashrate_ghs: 0,
    total_pool_hashrate_ghs: 0,
    pool_efficiency_percent: 0,
    total_power_watts: 0,
    avg_efficiency_wth: 0,
    total_cost_24h_pounds: 0,
    avg_price_per_kwh_pence: 0,
    current_energy_price_pence: 0,
    best_share_24h: defaultBestShare,
  };

  if (isLoading) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-center h-64">
          <div className="text-muted-foreground">Loading dashboard...</div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-center h-64">
          <div className="text-destructive">
            Error loading dashboard: {error.message}
          </div>
        </div>
      </div>
    );
  }

  const stats = data?.stats ?? defaultStats;
  const bestShare = stats.best_share_24h ?? defaultBestShare;

  // Format network difficulty
  const formatNetworkDiff = (diff: number | null) => {
    if (!diff) return "Unavailable";
    const numDiff = Number(diff);
    if (numDiff >= 1_000_000_000) {
      return `${(numDiff / 1_000_000_000).toFixed(1)}B`;
    } else if (numDiff >= 1_000_000) {
      return `${(numDiff / 1_000_000).toFixed(0)}M`;
    }
    return numDiff.toFixed(0);
  };

  // Format time ago
  const formatTimeAgo = (seconds: number | null) => {
    if (seconds === null) return "Unavailable";
    if (seconds < 60) {
      return `${seconds}s ago`;
    } else if (seconds < 3600) {
      const mins = Math.floor(seconds / 60);
      const secs = seconds % 60;
      return `${mins}m ${secs}s ago`;
    } else if (seconds < 86400) {
      const hours = Math.floor(seconds / 3600);
      const mins = Math.floor((seconds % 3600) / 60);
      return `${hours}h ${mins}m ago`;
    } else {
      const days = Math.floor(seconds / 86400);
      return `${days}d ago`;
    }
  };

  // Pool hashrate can be object {value, unit, display} or number (GH/s)
  const poolHashrateDisplay = stats.total_pool_hashrate_ghs
    ? (typeof stats.total_pool_hashrate_ghs === 'object' 
        ? stats.total_pool_hashrate_ghs.display 
        : formatHashrate(stats.total_pool_hashrate_ghs * 1000))
    : "Unavailable";

  const resolvedEfficiency = (stats.pool_efficiency_percent && stats.pool_efficiency_percent > 0)
    ? stats.pool_efficiency_percent
    : null;

  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <StatsCard
          label="Workers Online"
          value={stats.online_miners || 0}
          onClick={() => navigate("/miners")}
          subtext={
            <>
              <div>
                Pool: {poolHashrateDisplay}
              </div>
              <div className="text-xs">
                ⚡ {(() => {
                  if (!resolvedEfficiency || resolvedEfficiency <= 0) {
                    return "Unavailable";
                  }
                  let color = "";
                  if (resolvedEfficiency >= 95) {
                    color = "text-green-500";
                  } else if (resolvedEfficiency >= 85) {
                    color = "text-yellow-500";
                  } else {
                    color = "text-red-500";
                  }
                  return <span className={color}>{Number(resolvedEfficiency).toFixed(0)}% of expected</span>;
                })()}
              </div>
            </>
          }
        />

        <StatsCard
          label="Power Use"
          value={`${Math.round(stats.total_power_watts || 0)} W`}
          onClick={() => navigate("/miners")}
          subtext={
            <div>
              Efficiency: {stats.avg_efficiency_wth 
                ? `${Number(stats.avg_efficiency_wth).toFixed(1)} J/TH` 
                : "Unavailable"}
            </div>
          }
        />

        <StatsCard
          label="Cost (24h)"
          value={`£${Number(stats.total_cost_24h_pounds || 0).toFixed(2)}`}
          subtext={
            <div>
              Avg: {stats.avg_price_per_kwh_pence 
                ? `${Number(stats.avg_price_per_kwh_pence).toFixed(1)}p / kWh` 
                : "Unavailable"}
            </div>
          }
        />

        <StatsCard
          label="Best Share (24h)"
          value={`${bestShare.percentage || 0}%`}
          badge={
            bestShare.coin ? (
              <span className={`inline-block px-1.5 py-0.5 text-xs font-semibold rounded ${
                bestShare.coin === "BTC" ? "bg-orange-500 text-white" :
                bestShare.coin === "BCH" ? "bg-green-500 text-white" :
                bestShare.coin === "DGB" ? "bg-blue-500 text-white" :
                bestShare.coin === "BC2" ? "bg-purple-500 text-white" :
                "bg-gray-500 text-white"
              }`}>
                {bestShare.coin}
              </span>
            ) : null
          }
          subtext={
            <>
              <div>
                Network diff: {formatNetworkDiff(bestShare.network_difficulty)}
              </div>
              <div className="text-xs">
                {formatTimeAgo(bestShare.time_ago_seconds)}
              </div>
            </>
          }
        />
      </div>

      {/* NEW: Per-Pool Tiles - Plugin-Based Architecture with Drag-and-Drop */}
      <div className="space-y-4 border-t pt-4 mt-4">
        {poolTiles && poolOrder.length > 0 && (
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            onDragStart={() => setIsDragging(true)}
            onDragEnd={handleDragEnd}
          >
            <SortableContext items={poolOrder} strategy={verticalListSortingStrategy}>
              {poolOrder.map((poolId) => {
                const pool = poolTiles[poolId];
                if (!pool) return null;
                
                return (
                  <SortablePoolTile
                    key={poolId}
                    poolId={poolId}
                    pool={pool}
                    poolHashrateHistory={poolHashrateHistory?.[poolId]}
                    isDragging={isDragging}
                  />
                );
              })}
            </SortableContext>
          </DndContext>
        )}
      </div>
    </div>
  );
}
