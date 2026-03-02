import { Card, CardContent } from "@/components/ui/card";
import { ExternalLink } from "lucide-react";
import { cn } from "@/lib/utils";
import { useEffect, useRef } from "react";

interface PoolTileProps {
  coin: "BTC" | "BCH" | "DGB" | "BC2" | "NERDMINERS";
  workersOnline: number;
  hashrate: string;
  currentLuck: number | null;
  ettb: string | null;
  lastBlockTime: string | null;
  lastBlockTimestamp: number | null;
  blocks24h: number;
  blocks7d: number;
  blocks30d: number;
  shares: number;
  lastShare: string | null;
  lastShareTimestamp: number | null;
  totalPaid: string;
  paidValue: string;
  accountUrl: string;
  isStrategyActive?: boolean;
  isStrategyInactive?: boolean;
  chartData?: Array<{ x: number; y: number }>;
}

const coinConfig = {
  BTC: { name: "Bitcoin", color: "border-orange-500/90", bg: "bg-orange-500/10", logo: "₿" },
  BCH: { name: "Bitcoin Cash", color: "border-green-500/90", bg: "bg-green-500/10", logo: "BCH" },
  DGB: { name: "DigiByte", color: "border-blue-500/90", bg: "bg-blue-500/10", logo: "DGB" },
  BC2: { name: "BitcoinII", color: "border-purple-500/90", bg: "bg-purple-500/10", logo: "BC2" },
  NERDMINERS: { name: "NerdMiners", color: "border-orange-500/90", bg: "bg-orange-500/10", logo: "NM" },
};

export function PoolTile({
  coin,
  workersOnline,
  hashrate,
  currentLuck,
  ettb,
  lastBlockTime,
  lastBlockTimestamp,
  blocks24h,
  blocks7d,
  blocks30d,
  shares,
  lastShare,
  lastShareTimestamp,
  totalPaid,
  paidValue,
  accountUrl,
  isStrategyActive,
  isStrategyInactive,
  chartData,
}: PoolTileProps) {
  const config = coinConfig[coin];
  const canvasRef = useRef<HTMLCanvasElement>(null);
  
  // Draw sparkline chart
  useEffect(() => {
    if (!canvasRef.current || !chartData || chartData.length === 0) return;
    
    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    
    const parent = canvas.parentElement;
    if (!parent) return;
    
    const rect = parent.getBoundingClientRect();
    const width = Math.max(rect.width, 100);
    const height = Math.max(rect.height, 50);
    
    // Set canvas size with device pixel ratio
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.scale(dpr, dpr);
    
    // Sort data by timestamp
    const sortedData = [...chartData]
      .filter(d => d && d.y !== null && d.y !== undefined)
      .sort((a, b) => a.x - b.x);
    
    if (sortedData.length < 2) return;
    
    // Get min/max for scaling
    const yValues = sortedData.map(d => d.y);
    const minY = Math.min(...yValues);
    const maxY = Math.max(...yValues);
    const range = maxY - minY || 1;
    
    // Coin-specific colors
    const colors: Record<string, { fill: string; stroke: string }> = {
      DGB: { fill: 'rgba(59, 130, 246, 0.15)', stroke: 'rgba(59, 130, 246, 0.8)' },
      BCH: { fill: 'rgba(34, 197, 94, 0.15)', stroke: 'rgba(34, 197, 94, 0.8)' },
      BC2: { fill: 'rgba(249, 115, 22, 0.15)', stroke: 'rgba(249, 115, 22, 0.8)' },
      BTC: { fill: 'rgba(234, 179, 8, 0.15)', stroke: 'rgba(234, 179, 8, 0.8)' },
    };
    
    const color = colors[coin] || colors.DGB;
    
    // Draw area fill
    ctx.beginPath();
    ctx.moveTo(0, height);
    
    sortedData.forEach((point, i) => {
      const x = (i / (sortedData.length - 1)) * width;
      const y = height - ((point.y - minY) / range) * height * 0.9 - height * 0.05;
      if (i === 0) {
        ctx.lineTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    });
    
    ctx.lineTo(width, height);
    ctx.closePath();
    ctx.fillStyle = color.fill;
    ctx.fill();
    
    // Draw line
    ctx.beginPath();
    sortedData.forEach((point, i) => {
      const x = (i / (sortedData.length - 1)) * width;
      const y = height - ((point.y - minY) / range) * height * 0.9 - height * 0.05;
      if (i === 0) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.strokeStyle = color.stroke;
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }, [chartData, coin]);
  
  const formatLuck = (luck: number | null) => {
    if (luck === null || luck === 0) return "0%";
    if (luck >= 1000) return `${(luck / 1000).toFixed(1)}k%`;
    return `${luck.toFixed(0)}%`;
  };

  // Get color for time since last block based on ETTB
  const getBlockTimeColor = (lastBlockTimestamp: number | null, ettbFormatted: string | null) => {
    if (!lastBlockTimestamp || !ettbFormatted) return "text-muted-foreground";
    
    // Parse ETTB seconds from formatted string (e.g., "12h 34m" or "5d 3h")
    let ettbSeconds = 0;
    const ettbMatch = ettbFormatted.match(/(\d+)d\s*(\d+)h|(\d+)h\s*(\d+)m|(\d+)m/);
    if (ettbMatch) {
      if (ettbMatch[1]) { // days and hours
        ettbSeconds = parseInt(ettbMatch[1]) * 86400 + parseInt(ettbMatch[2]) * 3600;
      } else if (ettbMatch[3]) { // hours and minutes
        ettbSeconds = parseInt(ettbMatch[3]) * 3600 + parseInt(ettbMatch[4]) * 60;
      } else if (ettbMatch[5]) { // minutes only
        ettbSeconds = parseInt(ettbMatch[5]) * 60;
      }
    }
    
    const now = Math.floor(Date.now() / 1000);
    const timeSinceBlock = now - lastBlockTimestamp;
    
    if (ettbSeconds === 0) return "text-muted-foreground";
    
    const ratio = timeSinceBlock / ettbSeconds;
    if (ratio <= 1) return "text-green-500";
    if (ratio <= 2) return "text-yellow-500";
    if (ratio <= 3) return "text-orange-500";
    return "text-red-500";
  };

  // Get color for time since last share
  const getShareTimeColor = (lastShareTimestamp: number | null) => {
    if (!lastShareTimestamp) return "text-muted-foreground";
    const secondsAgo = Math.floor(Date.now() / 1000) - lastShareTimestamp;
    const minutesAgo = secondsAgo / 60;
    if (minutesAgo < 15) return "text-green-500";
    if (minutesAgo < 30) return "text-orange-500";
    return "text-red-500";
  };

  return (
    <div
      className={cn(
        "rounded-lg p-1 transition-all",
        isStrategyActive && `border-4 ${config.color}`,
        isStrategyInactive && "border-2 border-muted opacity-60"
      )}
    >
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
        {/* Workers Online */}
        <Card className={cn("hover:shadow-md transition-all", config.bg)}>
          <CardContent className="p-4 relative overflow-hidden">
            <canvas
              ref={canvasRef}
              className="absolute top-0 left-0 w-full h-full opacity-25 pointer-events-none"
              style={{ zIndex: 0 }}
            />
            <div className="relative" style={{ zIndex: 1 }}>
              <div className="text-xs font-medium text-muted-foreground mb-1">
                {config.logo} Workers Online
              </div>
              <div className="text-2xl font-bold">{workersOnline}</div>
              {hashrate && (
                <div className="text-xs text-muted-foreground mt-1">{hashrate}</div>
              )}
            </div>
          </CardContent>
        </Card>

        {/* Current Round Luck */}
        <Card
          className="hover:shadow-md transition-all cursor-pointer"
          onClick={() => window.open(accountUrl, "_blank")}
        >
          <CardContent className="p-4">
            <div className="text-xs font-medium text-muted-foreground mb-1 flex items-center gap-1">
              {config.logo} Current Round Luck
              <ExternalLink className="h-3 w-3" />
            </div>
            <div className="text-2xl font-bold">{formatLuck(currentLuck)}</div>
            {ettb && (
              <div className="text-xs text-muted-foreground mt-1">
                ETTB: {ettb}
                {lastBlockTime && (
                  <div className={cn("text-xs mt-0.5 font-medium", getBlockTimeColor(lastBlockTimestamp, ettb))}>
                    {lastBlockTime}
                  </div>
                )}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Blocks Found */}
        <Card
          className="hover:shadow-md transition-all cursor-pointer"
          onClick={() => window.open(accountUrl, "_blank")}
        >
          <CardContent className="p-4">
            <div className="text-xs font-medium text-muted-foreground mb-1 flex items-center gap-1">
              {config.logo} Blocks (24h/7d/30d)
              <ExternalLink className="h-3 w-3" />
            </div>
            <div className="text-2xl font-bold">
              {blocks24h} / {blocks7d} / {blocks30d}
            </div>
            <div className="text-xs text-muted-foreground mt-1">
              Shares: {shares.toLocaleString()}
              {lastShare && <div className={cn("text-xs mt-0.5", getShareTimeColor(lastShareTimestamp))}>{lastShare}</div>}
            </div>
          </CardContent>
        </Card>

        {/* Total Paid */}
        <Card
          className="hover:shadow-md transition-all cursor-pointer"
          onClick={() => window.open(accountUrl, "_blank")}
        >
          <CardContent className="p-4">
            <div className="text-xs font-medium text-muted-foreground mb-1 flex items-center gap-1">
              {config.logo} Total Paid
              <ExternalLink className="h-3 w-3" />
            </div>
            <div className="text-lg font-bold">{totalPaid}</div>
            <div className="text-xs text-muted-foreground mt-1">{paidValue}</div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
