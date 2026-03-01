import { Fragment, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { cn } from '@/lib/utils'
import { humanizeKey } from '@/lib/textFormatters'
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Copy,
  Filter,
  Loader2,
  PauseCircle,
  PlusCircle,
  RefreshCcw,
  Trash2,
  Zap,
} from 'lucide-react'

async function fetchJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  })

  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const detail = body.detail || body.message || `Request failed (${response.status})`
    throw new Error(detail)
  }

  if (response.status === 204) {
    return undefined as T
  }

  return response.json() as Promise<T>
}

type AutomationRule = {
  id: number
  name: string
  enabled: boolean
  trigger_type: string
  trigger_config: Record<string, unknown> | null
  action_type: string
  action_config: Record<string, unknown> | null
  priority: number
}

type StatusFilter = 'all' | 'enabled' | 'paused'

type Banner = { type: 'success' | 'error'; message: string } | null

const TRIGGER_LABELS: Record<string, string> = {
  price_threshold: 'Energy Price Threshold',
  time_window: 'Time Window',
  miner_overheat: 'Miner Overheat',
}

const ACTION_LABELS: Record<string, string> = {
  apply_mode: 'Apply Miner Mode',
  switch_pool: 'Switch Pool',
  send_alert: 'Send Alert',
  log_event: 'Log Event',
}

function resolveLabel(map: Record<string, string>, value: string) {
  return map[value] || humanizeKey(value)
}

export default function AutomationRules() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [searchTerm, setSearchTerm] = useState('')
  const [expandedRules, setExpandedRules] = useState<Set<number>>(new Set())
  const [banner, setBanner] = useState<Banner>(null)

  const {
    data: rules = [],
    isLoading,
    error,
  } = useQuery<AutomationRule[]>({
    queryKey: ['automation-rules'],
    queryFn: () => fetchJSON('/api/automation/'),
    refetchInterval: 60000,
  })

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      fetchJSON(`/api/automation/${id}`, {
        method: 'PUT',
        body: JSON.stringify({ enabled }),
      }),
    onSuccess: (_, variables) => {
      setBanner({ type: 'success', message: variables.enabled ? 'Rule enabled' : 'Rule paused' })
      queryClient.invalidateQueries({ queryKey: ['automation-rules'] })
    },
    onError: (mutationError: Error) => setBanner({ type: 'error', message: mutationError.message }),
  })

  const duplicateMutation = useMutation({
    mutationFn: (id: number) => fetchJSON<AutomationRule>(`/api/automation/${id}/duplicate`, { method: 'POST' }),
    onSuccess: (newRule) => {
      setBanner({ type: 'success', message: `Duplicated as ${newRule.name}` })
      queryClient.invalidateQueries({ queryKey: ['automation-rules'] })
    },
    onError: (mutationError: Error) => setBanner({ type: 'error', message: mutationError.message }),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: number) => fetchJSON(`/api/automation/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      setBanner({ type: 'success', message: 'Rule deleted' })
      queryClient.invalidateQueries({ queryKey: ['automation-rules'] })
    },
    onError: (mutationError: Error) => setBanner({ type: 'error', message: mutationError.message }),
  })

  const filteredRules = useMemo(() => {
    const normalizedSearch = searchTerm.trim().toLowerCase()

    return [...rules]
      .filter((rule) => {
        if (statusFilter === 'enabled' && !rule.enabled) return false
        if (statusFilter === 'paused' && rule.enabled) return false
        if (!normalizedSearch) return true
        return (
          rule.name.toLowerCase().includes(normalizedSearch) ||
          rule.trigger_type.toLowerCase().includes(normalizedSearch) ||
          rule.action_type.toLowerCase().includes(normalizedSearch)
        )
      })
      .sort((a, b) => a.priority - b.priority)
  }, [rules, statusFilter, searchTerm])

  const summary = useMemo(() => {
    const total = rules.length
    const enabled = rules.filter((rule) => rule.enabled).length
    const paused = total - enabled
    return { total, enabled, paused }
  }, [rules])

  const handleToggleExpand = (ruleId: number) => {
    setExpandedRules((prev) => {
      const next = new Set(prev)
      if (next.has(ruleId)) {
        next.delete(ruleId)
      } else {
        next.add(ruleId)
      }
      return next
    })
  }

  const handleToggleRule = (rule: AutomationRule) => {
    const nextState = !rule.enabled
    const verb = nextState ? 'enable' : 'pause'
    if (!window.confirm(`Are you sure you want to ${verb} "${rule.name}"?`)) return
    toggleMutation.mutate({ id: rule.id, enabled: nextState })
  }

  const handleDuplicate = (rule: AutomationRule) => {
    duplicateMutation.mutate(rule.id)
  }

  const handleDelete = (rule: AutomationRule) => {
    if (!window.confirm(`Delete "${rule.name}"? This cannot be undone.`)) return
    deleteMutation.mutate(rule.id)
  }

  return (
    <div className="space-y-8">
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-3 text-sm uppercase tracking-widest text-blue-300">
          <Zap className="h-5 w-5" />
          <span>Automation</span>
        </div>
        <h1 className="text-2xl font-semibold">Automation Rules</h1>
        <p className="text-sm text-gray-400">
          Create deterministic responses to price events, miner health issues, and pool failures. Each rule executes in priority order.
        </p>
        <div className="flex flex-wrap gap-3 pt-4">
          <Button className="flex items-center gap-2" onClick={() => navigate('/automation/add')}>
            <PlusCircle className="h-4 w-4" />
            Add Rule
          </Button>
          <Button
            variant="outline"
            className="flex items-center gap-2"
            onClick={() => queryClient.invalidateQueries({ queryKey: ['automation-rules'] })}
          >
            <RefreshCcw className="h-4 w-4" />
            Refresh
          </Button>
        </div>
      </div>

      {banner && (
        <div
          className={cn(
            'rounded-md border px-4 py-3 text-sm',
            banner.type === 'success'
              ? 'border-green-500/40 bg-green-500/10 text-green-200'
              : 'border-red-500/40 bg-red-500/10 text-red-200'
          )}
        >
          {banner.message}
        </div>
      )}

      {error && (
        <div className="flex items-center gap-3 rounded-md border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
          <AlertTriangle className="h-4 w-4" />
          <span>{error instanceof Error ? error.message : 'Unable to load rules'}</span>
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardContent className="p-5">
            <p className="text-xs uppercase text-gray-500">Total Rules</p>
            <p className="mt-2 text-2xl font-semibold">{summary.total}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-5">
            <p className="text-xs uppercase text-gray-500">Enabled</p>
            <div className="mt-2 flex items-baseline gap-2">
              <p className="text-2xl font-semibold text-green-300">{summary.enabled}</p>
              <span className="text-sm text-gray-500">active</span>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-5">
            <p className="text-xs uppercase text-gray-500">Paused</p>
            <div className="mt-2 flex items-baseline gap-2">
              <p className="text-2xl font-semibold text-amber-300">{summary.paused}</p>
              <span className="text-sm text-gray-500">waiting</span>
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <h2 className="text-xl font-semibold">Rules</h2>
              <p className="text-sm text-gray-400">Search, filter, and inspect trigger/action details.</p>
            </div>
            <div className="flex w-full flex-col gap-3 md:flex-row md:items-center md:justify-end">
              <div className="flex items-center gap-2 text-sm text-gray-400">
                <Filter className="h-4 w-4" />
                Status
              </div>
              <div className="flex rounded-md border border-gray-800 bg-gray-900/40">
                {(
                  [
                    { label: 'All', value: 'all' },
                    { label: 'Enabled', value: 'enabled' },
                    { label: 'Paused', value: 'paused' },
                  ] as { label: string; value: StatusFilter }[]
                ).map((filter) => (
                  <button
                    key={filter.value}
                    onClick={() => setStatusFilter(filter.value)}
                    className={cn(
                      'px-3 py-1 text-sm font-medium transition-colors',
                      statusFilter === filter.value
                        ? 'bg-blue-500/20 text-blue-200'
                        : 'text-gray-400 hover:text-gray-200'
                    )}
                  >
                    {filter.label}
                  </button>
                ))}
              </div>
              <input
                type="search"
                value={searchTerm}
                onChange={(event) => setSearchTerm(event.target.value)}
                placeholder="Search name, trigger, action…"
                className="w-full rounded-md border border-gray-800 bg-gray-950 px-3 py-2 text-sm text-gray-100 placeholder:text-gray-500 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-600/40 md:w-64"
              />
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="flex min-h-[280px] items-center justify-center text-gray-400">
              <Loader2 className="h-5 w-5 animate-spin" />
            </div>
          ) : filteredRules.length === 0 ? (
            <div className="rounded-md border border-dashed border-gray-800 p-8 text-center">
              <p className="text-gray-300 font-medium">No rules match your filters</p>
              <p className="mt-2 text-sm text-gray-500">Create a rule or adjust the search criteria.</p>
              <Button className="mt-4" onClick={() => navigate('/automation/add')}>
                Create Rule
              </Button>
            </div>
          ) : (
            <div className="overflow-hidden rounded-lg border border-gray-800">
              <table className="w-full text-sm">
                <thead className="bg-gray-950/70 text-left text-xs uppercase text-gray-500">
                  <tr>
                    <th className="px-4 py-3">Rule</th>
                    <th className="px-4 py-3">Trigger</th>
                    <th className="px-4 py-3">Action</th>
                    <th className="px-4 py-3">Priority</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredRules.map((rule) => {
                    const expanded = expandedRules.has(rule.id)
                    return (
                      <Fragment key={rule.id}>
                        <tr className="border-t border-gray-900/60">
                          <td className="px-4 py-3 text-gray-200">
                            <div className="font-semibold">{rule.name}</div>
                            <button
                              className="mt-1 flex items-center gap-1 text-xs text-blue-300"
                              onClick={() => handleToggleExpand(rule.id)}
                            >
                              <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', expanded && 'rotate-180')} />
                              Details
                            </button>
                          </td>
                          <td className="px-4 py-3 text-gray-400">{resolveLabel(TRIGGER_LABELS, rule.trigger_type)}</td>
                          <td className="px-4 py-3 text-gray-400">{resolveLabel(ACTION_LABELS, rule.action_type)}</td>
                          <td className="px-4 py-3 text-gray-300">#{rule.priority}</td>
                          <td className="px-4 py-3">
                            <span
                              className={cn(
                                'rounded-full px-2 py-0.5 text-xs font-semibold',
                                rule.enabled ? 'bg-green-500/10 text-green-300' : 'bg-gray-700/40 text-gray-400'
                              )}
                            >
                              {rule.enabled ? 'Enabled' : 'Paused'}
                            </span>
                          </td>
                          <td className="px-4 py-3">
                            <div className="flex items-center justify-end gap-2">
                              <Button
                                variant="secondary"
                                size="sm"
                                className="flex items-center gap-1"
                                onClick={() => handleToggleRule(rule)}
                                disabled={toggleMutation.isPending}
                              >
                                {rule.enabled ? (
                                  <>
                                    <PauseCircle className="h-4 w-4" /> Pause
                                  </>
                                ) : (
                                  <>
                                    <CheckCircle2 className="h-4 w-4" /> Enable
                                  </>
                                )}
                              </Button>
                              <Button
                                variant="outline"
                                size="sm"
                                className="flex items-center gap-1"
                                onClick={() => handleDuplicate(rule)}
                                disabled={duplicateMutation.isPending}
                              >
                                <Copy className="h-4 w-4" /> Duplicate
                              </Button>
                              <Button
                                variant="destructive"
                                size="sm"
                                className="flex items-center gap-1"
                                onClick={() => handleDelete(rule)}
                                disabled={deleteMutation.isPending}
                              >
                                <Trash2 className="h-4 w-4" /> Delete
                              </Button>
                            </div>
                          </td>
                        </tr>
                        {expanded && (
                          <tr className="border-t border-gray-900/60">
                            <td colSpan={6} className="bg-gray-950/40 px-4 py-4">
                              <div className="grid gap-4 md:grid-cols-2">
                                <div className="rounded border border-gray-800 bg-gray-900/40 p-3">
                                  <p className="text-xs uppercase text-gray-500">Trigger Config</p>
                                  <pre className="mt-2 max-h-48 overflow-auto text-xs text-gray-300">
                                    {JSON.stringify(rule.trigger_config ?? {}, null, 2)}
                                  </pre>
                                </div>
                                <div className="rounded border border-gray-800 bg-gray-900/40 p-3">
                                  <p className="text-xs uppercase text-gray-500">Action Config</p>
                                  <pre className="mt-2 max-h-48 overflow-auto text-xs text-gray-300">
                                    {JSON.stringify(rule.action_config ?? {}, null, 2)}
                                  </pre>
                                </div>
                              </div>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
