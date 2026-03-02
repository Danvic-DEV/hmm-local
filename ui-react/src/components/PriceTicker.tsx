import React from "react";
import { useQuery } from "@tanstack/react-query";

interface PriceTickerProps {
  className?: string;
}

export function PriceTicker({ className = "" }: PriceTickerProps) {
  const { data: cryptoPrices } = useQuery({
    queryKey: ["crypto-prices"],
    queryFn: async () => {
      const response = await fetch("/api/settings/crypto-prices");
      return response.json();
    },
    refetchInterval: 30000, // 30 seconds
  });

  const { data: dashboardData } = useQuery({
    queryKey: ["dashboard", "ticker"],
    queryFn: async () => {
      const response = await fetch("/api/dashboard/all?dashboard_type=all");
      return response.json();
    },
    refetchInterval: 30000, // 30 seconds
  });

  const getEnergyPriceColor = (price: number) => {
    if (price < 0) return "#3b82f6"; // blue (negative pricing!)
    if (price >= 30) return "#ef4444"; // red
    if (price >= 20) return "#f59e0b"; // orange
    return "#10b981"; // green
  };

  if (!cryptoPrices?.success && !dashboardData) return null;

  const prices: React.ReactNode[] = [];

  // Energy price
  const energyPrice = dashboardData?.stats?.current_energy_price_pence;
  if (energyPrice !== null && energyPrice !== undefined) {
    const color = getEnergyPriceColor(energyPrice);
    prices.push(
      <span key="energy" style={{ color }}>
        {Number(energyPrice).toFixed(2)}p/kWh
      </span>
    );
  }

  // Extract configured coins from pools
  const configuredCoins = new Set<string>();
  if (dashboardData?.pools && Array.isArray(dashboardData.pools)) {
    dashboardData.pools.forEach((pool: any) => {
      if (pool.coin) {
        configuredCoins.add(pool.coin.toUpperCase());
      }
    });
  }

  // If no pools configured, show all coins (fallback)
  const showAllCoins = configuredCoins.size === 0;

  // Crypto prices (filtered by configured pools)
  if (cryptoPrices?.success) {
    if (cryptoPrices.bitcoin > 0 && (showAllCoins || configuredCoins.has("BTC"))) {
      prices.push(
        <span key="btc">
          BTC £{Number(cryptoPrices.bitcoin).toFixed(0).replace(/\B(?=(\d{3})+(?!\d))/g, ",")}
        </span>
      );
    }

    if (cryptoPrices["bitcoin-cash"] > 0 && (showAllCoins || configuredCoins.has("BCH"))) {
      prices.push(
        <span key="bch">
          BCH £{Number(cryptoPrices["bitcoin-cash"]).toFixed(0)}
        </span>
      );
    }

    if (cryptoPrices.bitcoinii > 0 && (showAllCoins || configuredCoins.has("BC2"))) {
      prices.push(
        <span key="bc2">
          BC2 £{Number(cryptoPrices.bitcoinii).toFixed(6)}
        </span>
      );
    }

    if (cryptoPrices.digibyte > 0 && (showAllCoins || configuredCoins.has("DGB"))) {
      prices.push(
        <span key="dgb">
          DGB £{Number(cryptoPrices.digibyte).toFixed(4)}
        </span>
      );
    }
  }

  if (prices.length === 0) return null;

  return (
    <div className={`flex items-center text-sm ${className}`}>
      {prices.map((price, index) => (
        <React.Fragment key={index}>
          {price}
          {index < prices.length - 1 && (
            <span className="mx-2 text-muted-foreground">|</span>
          )}
        </React.Fragment>
      ))}
    </div>
  );
}
