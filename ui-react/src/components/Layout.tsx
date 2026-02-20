import { Link, useLocation } from 'react-router-dom'
import {
  LayoutDashboard,
  Activity,
  BarChart3,
  Trophy,
  Coins,
  ChevronDown,
  Cpu,
  Waves,
  Settings,
  Target,
  Bot,
  Lightbulb,
  Home,
  Cloud,
  Radar,
  SlidersHorizontal,
  BellRing,
  ClipboardList,
  ShieldCheck,
  RefreshCw,
  Gauge,
  Package,
  FolderOpen,
  Server,
  DollarSign
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Logo } from './Logo'
import { PriceTicker } from './PriceTicker'
import { NotificationBell } from './NotificationBell'
import { useQuery } from '@tanstack/react-query'
import { poolsAPI } from '@/lib/api'

export function Layout({ children }: { children: React.ReactNode }) {
  const location = useLocation()
  const [openSection, setOpenSection] = useState<'dashboard' | 'hardware' | 'automation' | 'integrations' | 'insights' | 'leaderboards' | 'system' | 'config' | null>(null)

  const isDashboardRoute = location.pathname === '/' || location.pathname === '' || location.pathname.startsWith('/dashboard')

  const { data: poolRecoveryStatus } = useQuery({
    queryKey: ['pools', 'recovery-status', 'layout-header'],
    queryFn: () => poolsAPI.getRecoveryStatus(24),
    enabled: isDashboardRoute,
    refetchInterval: 30000,
    refetchOnWindowFocus: false,
    staleTime: 25000,
  })

  useEffect(() => {
    if (location.pathname === '/' || location.pathname === '' || location.pathname.startsWith('/dashboard')) {
      setOpenSection('dashboard')
    }
  }, [location.pathname])

  const dashboardItems = [
    { path: '/', icon: LayoutDashboard, label: 'Overview' },
    { path: '/dashboard/operations', icon: Gauge, label: 'Operations' },
  ]
  
  const hardwareItems = [
    { path: '/miners', icon: Cpu, label: 'Miners' },
    { path: '/pools', icon: Waves, label: 'Pools' },
  ]
  
  const automationItems = [
    { path: '/automation', icon: Bot, label: 'Automation Rules' },
    { path: '/settings/price-band-strategy', icon: Target, label: 'Price Band Strategy' },
  ]
  
  const integrationItems = [
    { path: '/settings/integrations/homeassistant', icon: Home, label: 'Home Assistant' },
    { path: '/settings/energy', icon: Lightbulb, label: 'Energy Pricing' },
    { path: '/settings/cloud', icon: Cloud, label: 'Cloud Settings' },
  ]
  
  const insightsItems = [
    { path: '/health', icon: Activity, label: 'Health' },
    { path: '/analytics', icon: BarChart3, label: 'Analytics' },
    { path: '/insights/costs', icon: DollarSign, label: 'Costs' },
  ]
  
  const leaderboardItems = [
    { path: '/leaderboard', icon: Trophy, label: 'Hall of Pain' },
    { path: '/coin-hunter', icon: Coins, label: 'Coin Hunter' },
  ]
  
  const systemItems = [
    { path: '/settings/discovery', icon: Radar, label: 'Network Discovery' },
    { path: '/settings/tuning', icon: SlidersHorizontal, label: 'Tuning Profiles' },
    { path: '/settings/drivers', icon: Package, label: 'Driver Updates' },
    { path: '/settings/platform', icon: RefreshCw, label: 'Platform Updates' },
    { path: '/settings/files', icon: FolderOpen, label: 'File Manager' },
  ]
  
  const configItems = [
    { path: '/settings/notifications', icon: BellRing, label: 'Notifications' },
    { path: '/settings/openai', icon: Bot, label: 'AI Settings' },
    { path: '/settings/logs', icon: ClipboardList, label: 'System Logs' },
    { path: '/settings/audit', icon: ShieldCheck, label: 'Audit Logs' },
    { path: '/settings/restart', icon: RefreshCw, label: 'Restart Container' },
  ]

  const sectionStates = useMemo(
    () => ({
      dashboardOpen: openSection === 'dashboard',
      hardwareOpen: openSection === 'hardware',
      automationOpen: openSection === 'automation',
      integrationsOpen: openSection === 'integrations',
      insightsOpen: openSection === 'insights',
      leaderboardsOpen: openSection === 'leaderboards',
      systemOpen: openSection === 'system',
      configOpen: openSection === 'config',
    }),
    [openSection]
  )

  const toggleSection = (section: 'dashboard' | 'hardware' | 'automation' | 'integrations' | 'insights' | 'leaderboards' | 'system' | 'config') => {
    setOpenSection((current) => (current === section ? null : section))
  }

  return (
    <div className="min-h-screen bg-background">
      {/* Mobile Header */}
      <header className="lg:hidden sticky top-0 z-50 w-full border-b bg-background/95 backdrop-blur">
        <div className="flex h-14 items-center px-4 gap-2">
          <Logo className="h-8 w-8" />
          <span className="font-bold text-lg">HMM Local</span>
        </div>
      </header>

      {/* Desktop Sidebar */}
      <aside className="hidden lg:fixed lg:inset-y-0 lg:flex lg:w-64 lg:flex-col border-r">
        <div className="flex flex-col gap-2 p-4">
          <div className="flex h-14 items-center border-b px-2 mb-4 gap-3">
            <Logo className="h-10 w-10" />
            <div className="flex flex-col">
              <span className="font-bold text-xl">HMM Local</span>
              <span className="text-xs text-muted-foreground">Home Miner Manager</span>
            </div>
          </div>
          <nav className="flex flex-col gap-1">
            {/* Dashboard Category */}
            <div className="mt-2">
              <button
                onClick={() => toggleSection('dashboard')}
                className="flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground"
              >
                <div className="flex items-center gap-3">
                  <LayoutDashboard className="h-5 w-5" />
                  <span>Dashboard</span>
                </div>
                <ChevronDown
                  className={`h-4 w-4 transition-transform ${
                    sectionStates.dashboardOpen ? 'rotate-180' : ''
                  }`}
                />
              </button>

              {sectionStates.dashboardOpen && (
                <div className="ml-4 mt-1 flex flex-col gap-1 border-l-2 border-border pl-4">
                  {dashboardItems.map(({ path, icon: Icon, label }) => {
                    const isActive = location.pathname === path
                    return (
                      <Link
                        key={path}
                        to={path}
                        className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                          isActive
                            ? 'bg-primary text-primary-foreground'
                            : 'hover:bg-accent hover:text-accent-foreground'
                        }`}
                      >
                        <Icon className="h-4 w-4" />
                        {label}
                      </Link>
                    )}
                  )}
                </div>
              )}
            </div>

            {/* Manage Category */}
            <div className="mt-2">
              <button
                onClick={() => toggleSection('hardware')}
                className="flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground"
              >
                <div className="flex items-center gap-3">
                  <Cpu className="h-5 w-5" />
                  <span>Hardware</span>
                </div>
                <ChevronDown
                  className={`h-4 w-4 transition-transform ${
                    sectionStates.hardwareOpen ? 'rotate-180' : ''
                  }`}
                />
              </button>

              {sectionStates.hardwareOpen && (
                <div className="ml-4 mt-1 flex flex-col gap-1 border-l-2 border-border pl-4">
                  {hardwareItems.map(({ path, icon: Icon, label }) => {
                    const isActive = location.pathname === path
                    return (
                      <Link
                        key={path}
                        to={path}
                        className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                          isActive
                            ? 'bg-primary text-primary-foreground'
                            : 'hover:bg-accent hover:text-accent-foreground'
                        }`}
                      >
                        <Icon className="h-4 w-4" />
                        {label}
                      </Link>
                    )
                  })}
                </div>
              )}
            </div>

            {/* Automation Category */}
            <div className="mt-2">
              <button
                onClick={() => toggleSection('automation')}
                className="flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground"
              >
                <div className="flex items-center gap-3">
                  <Bot className="h-5 w-5" />
                  <span>Automation</span>
                </div>
                <ChevronDown
                  className={`h-4 w-4 transition-transform ${
                    sectionStates.automationOpen ? 'rotate-180' : ''
                  }`}
                />
              </button>

              {sectionStates.automationOpen && (
                <div className="ml-4 mt-1 flex flex-col gap-1 border-l-2 border-border pl-4">
                  {automationItems.map(({ path, icon: Icon, label }) => {
                    const isActive = location.pathname === path
                    return (
                      <Link
                        key={path}
                        to={path}
                        className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                          isActive
                            ? 'bg-primary text-primary-foreground'
                            : 'hover:bg-accent hover:text-accent-foreground'
                        }`}
                      >
                        <Icon className="h-4 w-4" />
                        {label}
                      </Link>
                    )
                  })}
                </div>
              )}
            </div>

            {/* Integrations Category */}
            <div className="mt-2">
              <button
                onClick={() => toggleSection('integrations')}
                className="flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground"
              >
                <div className="flex items-center gap-3">
                  <Cloud className="h-5 w-5" />
                  <span>Integrations</span>
                </div>
                <ChevronDown
                  className={`h-4 w-4 transition-transform ${
                    sectionStates.integrationsOpen ? 'rotate-180' : ''
                  }`}
                />
              </button>

              {sectionStates.integrationsOpen && (
                <div className="ml-4 mt-1 flex flex-col gap-1 border-l-2 border-border pl-4">
                  {integrationItems.map(({ path, icon: Icon, label }) => {
                    const isActive = location.pathname === path
                    return (
                      <Link
                        key={path}
                        to={path}
                        className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                          isActive
                            ? 'bg-primary text-primary-foreground'
                            : 'hover:bg-accent hover:text-accent-foreground'
                        }`}
                      >
                        <Icon className="h-4 w-4" />
                        {label}
                      </Link>
                    )
                  })}
                </div>
              )}
            </div>

            {/* Insights Category */}
            <div className="mt-2">
              <button
                onClick={() => toggleSection('insights')}
                className="flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground"
              >
                <div className="flex items-center gap-3">
                  <Activity className="h-5 w-5" />
                  <span>Insights</span>
                </div>
                <ChevronDown
                  className={`h-4 w-4 transition-transform ${
                    sectionStates.insightsOpen ? 'rotate-180' : ''
                  }`}
                />
              </button>

              {sectionStates.insightsOpen && (
                <div className="ml-4 mt-1 flex flex-col gap-1 border-l-2 border-border pl-4">
                  {insightsItems.map(({ path, icon: Icon, label }) => {
                    const isActive = location.pathname === path
                    return (
                      <Link
                        key={path}
                        to={path}
                        className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                          isActive
                            ? 'bg-primary text-primary-foreground'
                            : 'hover:bg-accent hover:text-accent-foreground'
                        }`}
                      >
                        <Icon className="h-4 w-4" />
                        {label}
                      </Link>
                    )
                  })}
                </div>
              )}
            </div>
            
            {/* Leaderboards Category */}
            <div className="mt-2">
              <button
                onClick={() => toggleSection('leaderboards')}
                className="flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground"
              >
                <div className="flex items-center gap-3">
                  <Trophy className="h-5 w-5" />
                  <span>Leaderboards</span>
                </div>
                <ChevronDown
                  className={`h-4 w-4 transition-transform ${
                    sectionStates.leaderboardsOpen ? 'rotate-180' : ''
                  }`}
                />
              </button>
              
              {sectionStates.leaderboardsOpen && (
                <div className="ml-4 mt-1 flex flex-col gap-1 border-l-2 border-border pl-4">
                  {leaderboardItems.map(({ path, icon: Icon, label }) => {
                    const isActive = location.pathname === path
                    return (
                      <Link
                        key={path}
                        to={path}
                        className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                          isActive
                            ? 'bg-primary text-primary-foreground'
                            : 'hover:bg-accent hover:text-accent-foreground'
                        }`}
                      >
                        <Icon className="h-4 w-4" />
                        {label}
                      </Link>
                    )
                  })}
                </div>
              )}
            </div>

            {/* System Category */}
            <div className="mt-2">
              <button
                onClick={() => toggleSection('system')}
                className="flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground"
              >
                <div className="flex items-center gap-3">
                  <Server className="h-5 w-5" />
                  <span>System</span>
                </div>
                <ChevronDown
                  className={`h-4 w-4 transition-transform ${
                    sectionStates.systemOpen ? 'rotate-180' : ''
                  }`}
                />
              </button>

              {sectionStates.systemOpen && (
                <div className="ml-4 mt-1 flex flex-col gap-1 border-l-2 border-border pl-4">
                  {systemItems.map(({ path, icon: Icon, label }) => {
                    const isActive = location.pathname === path
                    return (
                      <Link
                        key={path}
                        to={path}
                        className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                          isActive
                            ? 'bg-primary text-primary-foreground'
                            : 'hover:bg-accent hover:text-accent-foreground'
                        }`}
                      >
                        <Icon className="h-4 w-4" />
                        {label}
                      </Link>
                    )
                  })}
                </div>
              )}
            </div>

            {/* Configuration Category */}
            <div className="mt-2">
              <button
                onClick={() => toggleSection('config')}
                className="flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground"
              >
                <div className="flex items-center gap-3">
                  <Settings className="h-5 w-5" />
                  <span>Configuration</span>
                </div>
                <ChevronDown
                  className={`h-4 w-4 transition-transform ${
                    sectionStates.configOpen ? 'rotate-180' : ''
                  }`}
                />
              </button>

              {sectionStates.configOpen && (
                <div className="ml-4 mt-1 flex flex-col gap-1 border-l-2 border-border pl-4">
                  {configItems.map(({ path, icon: Icon, label }) => {
                    const isActive = location.pathname === path
                    return (
                      <Link
                        key={path}
                        to={path}
                        className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                          isActive
                            ? 'bg-primary text-primary-foreground'
                            : 'hover:bg-accent hover:text-accent-foreground'
                        }`}
                      >
                        <Icon className="h-4 w-4" />
                        {label}
                      </Link>
                    )
                  })}
                </div>
              )}
            </div>
          </nav>
        </div>
      </aside>

      {/* Main Content */}
      <main className="lg:pl-64">
        <div className="container mx-auto p-4 md:p-6 lg:p-8 pb-80 lg:pb-8">
          {/* Price Ticker with Notification Bell */}
          <div className="flex justify-between items-center gap-4 mb-4">
            <div className="min-h-[28px] flex items-center">
              {isDashboardRoute && poolRecoveryStatus && (
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span className="font-medium">Driver recovery (24h):</span>
                  <span className="rounded bg-green-500/15 px-2 py-0.5 text-green-700 dark:text-green-300">
                    recovered {poolRecoveryStatus.totals.recovered}
                  </span>
                  <span className="rounded bg-amber-500/15 px-2 py-0.5 text-amber-700 dark:text-amber-300">
                    unresolved {poolRecoveryStatus.totals.unresolved}
                  </span>
                </div>
              )}
            </div>

            <div className="flex justify-end items-center gap-4">
              <NotificationBell />
              <PriceTicker />
            </div>
          </div>
          
          {children}
        </div>
      </main>

      {/* Mobile Bottom Nav */}
      <nav className="lg:hidden fixed bottom-0 z-50 w-full border-t bg-background/95 backdrop-blur">
        <div className="grid grid-cols-5 items-center gap-2 min-h-16 py-2">
          {dashboardItems.map(({ path, icon: Icon, label }) => {
            const isActive = location.pathname === path
            return (
              <Link
                key={path}
                to={path}
                className={`flex flex-col items-center gap-1 px-2 py-2 text-xs transition-colors ${
                  isActive
                    ? 'text-primary'
                    : 'text-muted-foreground'
                }`}
              >
                <Icon className="h-5 w-5" />
                <span className="truncate w-full text-center">{label}</span>
              </Link>
            )
          })}
          {hardwareItems.map(({ path, icon: Icon, label }) => {
            const isActive = location.pathname === path
            return (
              <Link
                key={path}
                to={path}
                className={`flex flex-col items-center gap-1 px-2 py-2 text-xs transition-colors ${
                  isActive
                    ? 'text-primary'
                    : 'text-muted-foreground'
                }`}
              >
                <Icon className="h-5 w-5" />
                <span className="truncate w-full text-center">{label}</span>
              </Link>
            )
          })}
          {insightsItems.map(({ path, icon: Icon, label }) => {
            const isActive = location.pathname === path
            return (
              <Link
                key={path}
                to={path}
                className={`flex flex-col items-center gap-1 px-2 py-2 text-xs transition-colors ${
                  isActive
                    ? 'text-primary'
                    : 'text-muted-foreground'
                }`}
              >
                <Icon className="h-5 w-5" />
                <span className="truncate w-full text-center">{label}</span>
              </Link>
            )
          })}
          {leaderboardItems.map(({ path, icon: Icon, label }) => {
            const isActive = location.pathname === path
            return (
              <Link
                key={path}
                to={path}
                className={`flex flex-col items-center gap-1 px-2 py-2 text-xs transition-colors ${
                  isActive
                    ? 'text-primary'
                    : 'text-muted-foreground'
                }`}
              >
                <Icon className="h-5 w-5" />
                <span className="truncate w-full text-center">{label}</span>
              </Link>
            )
          })}
          {configItems.slice(0, 2).map(({ path, icon: Icon, label }) => {
            const isActive = location.pathname === path
            return (
              <Link
                key={path}
                to={path}
                className={`flex flex-col items-center gap-1 px-2 py-2 text-xs transition-colors ${
                  isActive
                    ? 'text-primary'
                    : 'text-muted-foreground'
                }`}
              >
                <Icon className="h-5 w-5" />
                <span className="truncate w-full text-center">{label}</span>
              </Link>
            )
          })}
        </div>
      </nav>
    </div>
  )
}
