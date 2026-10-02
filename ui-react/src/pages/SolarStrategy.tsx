import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Layers, Sun, Target, Zap } from 'lucide-react'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

type MinerRecord = {
  id: number
  name: string
  type: string
  enrolled?: boolean
}

type MinersByType = Record<string, MinerRecord[]>

type SolarStrategySettings = {
  enabled: boolean
  solar_surplus_entity_id: string | null
  enrolled_miners: { id: number; name: string; type: string }[]
  miners_by_type: MinersByType
}

type HomeAssistantDeviceRecord = {
  id: number
  entity_id: string
  name: string
  domain: string
}

type FetchError = Error & { detail?: string }

const KNOWN_MINER_GROUP_META: Record<
  string,
  { label: string; icon: React.ComponentType<{ className?: string }>; helper: string; order: number }
> = {
  bitaxe: { label: 'Bitaxe 601', icon: Zap, helper: 'Full control (eco/standard/turbo/oc)', order: 1 },
  nerdqaxe: { label: 'NerdQaxe++', icon: Target, helper: 'Supports aggressive tuning profiles', order: 2 },
  avalon_nano: { label: 'Avalon Nano 3/3S', icon: Layers, helper: 'Uses low / med / high workmodes', order: 3 },
  nmminer: { label: 'NMMiner ESP32', icon: Activity, helper: 'Lottery miners for extreme variance plays', order: 4 },
}

function humanizeMinerType(minerType: string) {
  return minerType
    .replace(/[_-]+/g, ' ')
    .split(' ')
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

async function fetchJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  })

  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const message = body.detail || body.message || `Request failed (${response.status})`
    throw new Error(message)
  }

  if (response.status === 204) {
    return undefined as T
  }

  return response.json() as Promise<T>
}

export default function SolarStrategy() {
  const queryClient = useQueryClient()
  const [selectedMiners, setSelectedMiners] = useState<Set<number>>(new Set())
  const [strategyEnabled, setStrategyEnabled] = useState(false)
  const [solarSurplusEntityId, setSolarSurplusEntityId] = useState<string | undefined>(undefined)
  const [feedback, setFeedback] = useState<{ type: 'success' | 'error'; message: string } | null>(null)

  const {
    data: strategyData,
    isLoading: strategyLoading,
    error: strategyError,
  } = useQuery<SolarStrategySettings>({
    queryKey: ['solar-strategy'],
    queryFn: () => fetchJSON<SolarStrategySettings>('/api/settings/solar-strategy'),
  })

  const { data: haDevicesData } = useQuery<{ devices: HomeAssistantDeviceRecord[] }>({
    queryKey: ['ha-devices-for-solar'],
    queryFn: () => fetchJSON<{ devices: HomeAssistantDeviceRecord[] }>('/api/integrations/homeassistant/devices'),
  })

  const solarSensorOptions = useMemo(
    () => (haDevicesData?.devices ?? []).filter((device) => device.domain === 'sensor'),
    [haDevicesData]
  )

  const minerGroups = useMemo(() => {
    const groups = Object.keys(strategyData?.miners_by_type ?? {}).map((key) => {
      const meta = KNOWN_MINER_GROUP_META[key]
      return {
        key,
        label: meta?.label || humanizeMinerType(key),
        icon: meta?.icon || Activity,
        helper: meta?.helper || 'Driver-managed miner type',
        order: meta?.order ?? 999,
      }
    })
    return groups.sort((a, b) => (a.order !== b.order ? a.order - b.order : a.label.localeCompare(b.label)))
  }, [strategyData?.miners_by_type])

  useEffect(() => {
    if (!strategyData) return
    setStrategyEnabled(strategyData.enabled)
    setSolarSurplusEntityId(strategyData.solar_surplus_entity_id ?? undefined)
    setSelectedMiners(new Set(strategyData.enrolled_miners.map((miner) => miner.id)))
  }, [strategyData])

  const saveMutation = useMutation({
    mutationFn: (payload: {
      enabled: boolean
      solar_surplus_entity_id: string | null
      miner_ids: number[]
    }) =>
      fetchJSON('/api/settings/solar-strategy', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      setFeedback({ type: 'success', message: 'Solar Strategy settings saved' })
      queryClient.invalidateQueries({ queryKey: ['solar-strategy'] })
      queryClient.invalidateQueries({ queryKey: ['price-band-strategy'] })
    },
    onError: (error: FetchError) => {
      setFeedback({ type: 'error', message: error.message })
    },
  })

  const toggleMiner = (minerId: number) => {
    setSelectedMiners((prev) => {
      const next = new Set(prev)
      if (next.has(minerId)) {
        next.delete(minerId)
      } else {
        next.add(minerId)
      }
      return next
    })
  }

  const toggleGroup = (minerIds: number[], shouldSelect: boolean) => {
    setSelectedMiners((prev) => {
      const next = new Set(prev)
      minerIds.forEach((id) => (shouldSelect ? next.add(id) : next.delete(id)))
      return next
    })
  }

  const handleSave = () => {
    saveMutation.mutate({
      enabled: strategyEnabled,
      solar_surplus_entity_id: solarSurplusEntityId ?? null,
      miner_ids: Array.from(selectedMiners),
    })
  }

  if (strategyLoading) {
    return <div className="py-10 text-center text-sm text-muted-foreground">Loading Solar Strategy…</div>
  }

  if (strategyError) {
    return (
      <div className="py-10 text-center text-sm text-red-400">
        Failed to load Solar Strategy settings: {(strategyError as Error).message}
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Sun className="h-6 w-6 text-amber-300" />
        <div>
          <h1 className="text-2xl font-semibold">Solar Strategy</h1>
          <p className="text-sm text-gray-400">
            Runs enrolled miners off live excess solar surplus, independent of Price Band Strategy. A miner can be
            enrolled here or in Price Band Strategy, never both.
          </p>
        </div>
      </div>

      {feedback && (
        <div
          className={`rounded-md border p-3 text-sm ${
            feedback.type === 'success'
              ? 'border-green-500/30 bg-green-500/10 text-green-200'
              : 'border-red-500/30 bg-red-500/10 text-red-200'
          }`}
        >
          {feedback.message}
        </div>
      )}

      <Card>
        <CardHeader>
          <h2 className="text-xl font-semibold">Configuration</h2>
        </CardHeader>
        <CardContent className="space-y-4">
          <label className="flex cursor-pointer items-start gap-3">
            <Checkbox checked={strategyEnabled} onCheckedChange={(checked) => setStrategyEnabled(!!checked)} className="mt-0.5" />
            <div>
              <p className="font-semibold">Enable Solar Strategy</p>
              <p className="text-sm text-gray-400">
                Left off, this has zero effect on your fleet - nothing runs until a surplus sensor and at least one
                miner are configured below. Solar Strategy only ever controls power state and tuning mode - it never
                changes which pool a miner mines on.
              </p>
            </div>
          </label>

          <div>
            <p className="mb-1 text-xs uppercase text-gray-400">Solar surplus sensor</p>
            <Select value={solarSurplusEntityId} onValueChange={setSolarSurplusEntityId}>
              <SelectTrigger className="w-full max-w-md">
                <SelectValue placeholder="Select a Home Assistant sensor" />
              </SelectTrigger>
              <SelectContent>
                {solarSensorOptions.map((device) => (
                  <SelectItem key={device.id} value={device.entity_id}>
                    {device.name} ({device.entity_id})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <h2 className="text-xl font-semibold">Enroll Miners</h2>
            <p className="text-sm text-gray-400">
              A miner enrolled in Price Band Strategy can't also be enrolled here - unenroll it there first.
            </p>
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          {minerGroups.map((group) => {
            const miners = strategyData?.miners_by_type?.[group.key] ?? []
            const selectedCount = miners.filter((miner) => selectedMiners.has(miner.id)).length
            const allSelected = miners.length > 0 && selectedCount === miners.length

            return (
              <div key={group.key} className="rounded-lg border border-gray-800 bg-gray-900/40 p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-2 text-base font-semibold">
                      <group.icon className="h-4 w-4 text-amber-300" /> {group.label}
                    </div>
                    <p className="text-sm text-gray-500">{group.helper}</p>
                  </div>
                  {miners.length > 0 && (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-xs"
                      onClick={() => toggleGroup(miners.map((m) => m.id), !allSelected)}
                    >
                      {allSelected ? 'Clear' : 'Select all'} ({selectedCount}/{miners.length})
                    </Button>
                  )}
                </div>

                {miners.length === 0 ? (
                  <p className="mt-4 rounded-md border border-dashed border-gray-700 p-3 text-sm text-gray-500">
                    No miners available.
                  </p>
                ) : (
                  <div className="mt-4 grid gap-3 md:grid-cols-2">
                    {miners.map((miner) => (
                      <label
                        key={miner.id}
                        className="flex cursor-pointer items-center gap-3 rounded-md border border-gray-800 bg-gray-950/60 p-3 hover:border-amber-500/40"
                      >
                        <Checkbox checked={selectedMiners.has(miner.id)} onCheckedChange={() => toggleMiner(miner.id)} />
                        <div>
                          <p className="font-medium text-sm text-gray-100">{miner.name}</p>
                          <p className="text-xs text-gray-500">ID #{miner.id}</p>
                        </div>
                      </label>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </CardContent>
      </Card>

      <div className="flex justify-end">
        <Button onClick={handleSave} disabled={saveMutation.isPending}>
          {saveMutation.isPending ? 'Saving…' : 'Save Settings'}
        </Button>
      </div>
    </div>
  )
}
