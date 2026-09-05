import { FormEvent, useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  AlertTriangle,
  CheckCircle2,
  Link as LinkIcon,
  Loader2,
  Plug,
  RefreshCw,
  ShieldAlert,
  Trash2,
  Zap
} from 'lucide-react'
import { MinerTypeBadge } from '@/components/miners/MinerTypeBadge'
import { formatMinerTypeLabel } from '@/lib/minerTypes'

interface HaConfigResponse {
  configured: boolean
  id?: number
  name?: string
  base_url?: string
  has_access_token?: boolean
  enabled?: boolean
  keepalive_enabled?: boolean
  last_test?: string | null
  last_test_success?: boolean | null
}

interface SaveConfigPayload {
  name: string
  base_url: string
  access_token: string | null
  enabled: boolean
  keepalive_enabled: boolean
}

interface Device {
  id: number
  entity_id: string
  name: string
  domain: string
  miner_id: number | null
  enrolled: boolean
  never_auto_control: boolean
  current_state: string | null
}

interface DevicesResponse {
  devices: Device[]
}

interface Miner {
  id: number
  name: string
  miner_type: string
}

interface ApiResponse {
  success: boolean
  message?: string
}

async function fetchJSON<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const response = await fetch(input, init)
  if (!response.ok) {
    let detail = 'Request failed'
    try {
      const body = await response.json()
      detail = body.detail || body.message || detail
    } catch {
      /* swallow */
    }
    throw new Error(detail)
  }
  return response.json()
}

const connectionBadge = (config?: HaConfigResponse) => {
  if (!config?.configured) {
    return <span className="inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-xs">Not configured</span>
  }
  if (config.last_test_success === true) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs font-medium text-emerald-300">
        <CheckCircle2 className="h-3.5 w-3.5" /> Connected
      </span>
    )
  }
  if (config.last_test_success === false) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-red-500/15 px-2 py-0.5 text-xs font-medium text-red-300">
        <AlertTriangle className="h-3.5 w-3.5" /> Connection failed
      </span>
    )
  }
  return <span className="inline-flex items-center rounded-full bg-amber-500/10 px-2 py-0.5 text-xs text-amber-200">Not tested</span>
}

export default function HomeAssistant() {
  const queryClient = useQueryClient()
  const [banner, setBanner] = useState<{ tone: 'success' | 'error' | 'info'; message: string } | null>(null)
  const [formState, setFormState] = useState({
    name: 'Home Assistant',
    base_url: '',
    access_token: '',
    enabled: true,
    keepalive_enabled: false,
  })
  const [showAllDevices, setShowAllDevices] = useState(false)
  const [isChangingAccessToken, setIsChangingAccessToken] = useState(false)
  const [linkModal, setLinkModal] = useState<{ device: Device | null; minerId: string }>({ device: null, minerId: '' })
  const [linkOpen, setLinkOpen] = useState(false)

  const configQuery = useQuery<HaConfigResponse>({
    queryKey: ['ha-config'],
    queryFn: () => fetchJSON<HaConfigResponse>('/api/integrations/homeassistant/config'),
  })

  const configured = configQuery.data?.configured ?? false

  const devicesQuery = useQuery<DevicesResponse>({
    queryKey: ['ha-devices', showAllDevices],
    queryFn: () =>
      fetchJSON<DevicesResponse>(
        `/api/integrations/homeassistant/devices${showAllDevices ? '' : '?enrolled_only=true'}`
      ),
    enabled: configured,
    refetchInterval: configured ? 30000 : false,
  })

  const minersQuery = useQuery<Miner[]>({
    queryKey: ['miners-basic'],
    queryFn: () => fetchJSON<Miner[]>('/api/miners/'),
  })

  useEffect(() => {
    if (configQuery.data?.configured) {
      setFormState((current) => ({
        ...current,
        name: configQuery.data?.name || 'Home Assistant',
        base_url: configQuery.data?.base_url || '',
        enabled: Boolean(configQuery.data?.enabled),
        keepalive_enabled: Boolean(configQuery.data?.keepalive_enabled),
        access_token: '',
      }))
      setIsChangingAccessToken(false)
    }
  }, [configQuery.data])

  const showToast = (tone: 'success' | 'error' | 'info', message: string) => {
    setBanner({ tone, message })
    setTimeout(() => setBanner(null), 5000)
  }

  const saveMutation = useMutation({
    mutationFn: async () => {
      const enteredToken = formState.access_token.trim()
      const hasStoredToken = Boolean(configQuery.data?.has_access_token)
      const tokenRequired = formState.enabled && (!hasStoredToken || isChangingAccessToken)

      if (tokenRequired && !enteredToken) {
        throw new Error('Access token is required when Home Assistant is enabled')
      }

      const payload: SaveConfigPayload = {
        name: formState.name,
        base_url: formState.base_url,
        access_token: enteredToken ? enteredToken : null,
        enabled: formState.enabled,
        keepalive_enabled: formState.keepalive_enabled,
      }
      return fetchJSON<ApiResponse>('/api/integrations/homeassistant/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['ha-config'] })
      setIsChangingAccessToken(false)
      setFormState((current) => ({ ...current, access_token: '' }))
      showToast(data.success ? 'success' : 'error', data.message || 'Configuration saved')
    },
    onError: (error: Error) => showToast('error', error.message),
  })

  const testMutation = useMutation({
    mutationFn: () =>
      fetchJSON<ApiResponse>('/api/integrations/homeassistant/test', {
        method: 'POST',
      }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['ha-config'] })
      showToast(data.success ? 'success' : 'error', data.message || 'Test completed')
    },
    onError: (error: Error) => showToast('error', error.message),
  })

  const deleteMutation = useMutation({
    mutationFn: () =>
      fetchJSON<ApiResponse>('/api/integrations/homeassistant/config', {
        method: 'DELETE',
      }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['ha-config'] })
      queryClient.invalidateQueries({ queryKey: ['ha-devices'] })
      showToast('success', data.message || 'Configuration deleted')
    },
    onError: (error: Error) => showToast('error', error.message),
  })

  const discoverMutation = useMutation({
    mutationFn: () =>
      fetchJSON<ApiResponse>('/api/integrations/homeassistant/discover', {
        method: 'POST',
      }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['ha-devices'] })
      showToast(data.success ? 'success' : 'error', data.message || 'Discovery finished')
    },
    onError: (error: Error) => showToast('error', error.message),
  })

  const enrollMutation = useMutation({
    mutationFn: ({ deviceId, enrolled }: { deviceId: number; enrolled: boolean }) =>
      fetchJSON<ApiResponse>(`/api/integrations/homeassistant/devices/${deviceId}/enroll`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enrolled }),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['ha-devices'] }),
    onError: (error: Error) => showToast('error', error.message),
  })

  const controlMutation = useMutation({
    mutationFn: ({ deviceId, action }: { deviceId: number; action: 'turn_on' | 'turn_off' }) =>
      fetchJSON<ApiResponse>(`/api/integrations/homeassistant/devices/${deviceId}/control?action=${action}`, {
        method: 'POST',
      }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['ha-devices'] })
      showToast(data.success ? 'success' : 'error', data.message || 'Command sent')
    },
    onError: (error: Error) => showToast('error', error.message),
  })

  const refreshMutation = useMutation({
    mutationFn: (deviceId: number) =>
      fetchJSON<ApiResponse>(`/api/integrations/homeassistant/devices/${deviceId}/state`),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['ha-devices'] })
      showToast(data.success ? 'success' : 'error', data.message || 'State refreshed')
    },
    onError: (error: Error) => showToast('error', error.message),
  })

  const linkMutation = useMutation({
    mutationFn: ({ deviceId, minerId }: { deviceId: number; minerId: string }) =>
      fetchJSON<ApiResponse>(`/api/integrations/homeassistant/devices/${deviceId}/link`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ miner_id: minerId && minerId !== 'none' ? Number(minerId) : null }),
      }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['ha-devices'] })
      showToast(data.success ? 'success' : 'error', data.message || 'Link updated')
      setLinkOpen(false)
    },
    onError: (error: Error) => showToast('error', error.message),
  })

  const switches = useMemo(() => {
    const raw = devicesQuery.data?.devices ?? []
    return raw.filter((device) => device.domain === 'switch')
  }, [devicesQuery.data])

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    saveMutation.mutate()
  }

  const handleDelete = () => {
    if (!window.confirm('Delete Home Assistant configuration?')) return
    deleteMutation.mutate()
  }

  const openLinkModal = (device: Device) => {
    setLinkModal({ device, minerId: device.miner_id ? String(device.miner_id) : '' })
    setLinkOpen(true)
  }

  const handleLinkSave = () => {
    if (!linkModal.device) return
    const selectedMinerId = linkModal.minerId ? Number(linkModal.minerId) : null
    if (selectedMinerId === linkModal.device.miner_id) {
      setLinkOpen(false)
      return
    }
    linkMutation.mutate({ deviceId: linkModal.device.id, minerId: linkModal.minerId || '' })
  }

  const hasStoredAccessToken = Boolean(configQuery.data?.has_access_token)

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <p className="text-xs uppercase tracking-[0.3em] text-blue-300">Integrations</p>
        <h1 className="text-2xl font-semibold">Home Assistant</h1>
        <p className="text-sm text-muted-foreground">
          Connect smart switches and routers so energy automation can turn miners on or off based on pricing and safety.
        </p>
      </div>

      {banner && (
        <div
          className={`rounded-md border px-4 py-3 text-sm ${
            banner.tone === 'success'
              ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-100'
              : banner.tone === 'error'
              ? 'border-red-500/40 bg-red-500/10 text-red-100'
              : 'border-blue-500/40 bg-blue-500/10 text-blue-100'
          }`}
        >
          {banner.message}
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-[2fr,1fr]">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <div>
              <CardTitle>Configuration</CardTitle>
              <p className="text-sm text-muted-foreground">Use a long-lived access token from your Home Assistant profile.</p>
            </div>
            {connectionBadge(configQuery.data)}
          </CardHeader>
          <CardContent>
            <form className="space-y-4" onSubmit={handleSubmit}>
              <div>
                <label className="text-sm font-medium text-foreground">Name</label>
                <input
                  className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
                  value={formState.name}
                  onChange={(event) => setFormState({ ...formState, name: event.target.value })}
                  required
                />
              </div>
              <div>
                <label className="text-sm font-medium text-foreground">Base URL</label>
                <input
                  className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
                  placeholder="http://homeassistant.local:8123"
                  value={formState.base_url}
                  onChange={(event) => setFormState({ ...formState, base_url: event.target.value })}
                  required
                />
              </div>
              <div>
                <label className="text-sm font-medium text-foreground">Access Token</label>
                {hasStoredAccessToken && !isChangingAccessToken ? (
                  <div className="mt-1 space-y-2 rounded-md border border-border bg-muted/20 px-3 py-3">
                    <p className="text-sm text-foreground">Stored access token is configured.</p>
                    <Button type="button" variant="secondary" onClick={() => setIsChangingAccessToken(true)}>
                      Change token
                    </Button>
                  </div>
                ) : (
                  <>
                    <input
                      className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
                      placeholder={configured ? 'Paste new token' : 'Paste long-lived token'}
                      value={formState.access_token}
                      onChange={(event) => setFormState({ ...formState, access_token: event.target.value })}
                      type="password"
                    />
                    <div className="mt-1 flex items-center justify-between gap-2">
                      <p className="text-xs text-muted-foreground">
                        {hasStoredAccessToken ? 'Leave blank to keep existing token.' : 'Required for first-time setup.'}
                      </p>
                      {hasStoredAccessToken && isChangingAccessToken && (
                        <button
                          type="button"
                          className="text-xs text-muted-foreground underline-offset-4 hover:underline"
                          onClick={() => {
                            setIsChangingAccessToken(false)
                            setFormState((current) => ({ ...current, access_token: '' }))
                          }}
                        >
                          Keep existing token
                        </button>
                      )}
                    </div>
                  </>
                )}
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <label className="inline-flex items-center gap-2 text-sm font-medium">
                  <input
                    type="checkbox"
                    className="h-4 w-4 rounded border-border"
                    checked={formState.enabled}
                    onChange={(event) => setFormState({ ...formState, enabled: event.target.checked })}
                  />
                  Enable integration
                </label>
                <label className="inline-flex items-center gap-2 text-sm font-medium">
                  <input
                    type="checkbox"
                    className="h-4 w-4 rounded border-border"
                    checked={formState.keepalive_enabled}
                    onChange={(event) => setFormState({ ...formState, keepalive_enabled: event.target.checked })}
                  />
                  Keep-alive monitoring
                </label>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button type="submit" disabled={saveMutation.isPending}>
                  {saveMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}Save Configuration
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => testMutation.mutate()}
                  disabled={!configured || testMutation.isPending}
                >
                  {testMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}Test Connection
                </Button>
                <Button
                  type="button"
                  variant="destructive"
                  onClick={handleDelete}
                  disabled={!configured || deleteMutation.isPending}
                >
                  {deleteMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  <Trash2 className="mr-2 h-4 w-4" />Delete
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>

        <Card className="bg-muted/40">
          <CardHeader>
            <CardTitle>Setup Guide</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 text-sm text-muted-foreground">
            <div>
              <p className="font-medium text-foreground">Generate token</p>
              <ol className="mt-2 list-inside list-decimal space-y-1">
                <li>Open Home Assistant and click your profile.</li>
                <li>Create a Long-Lived Access Token.</li>
                <li>Copy the token and paste it here.</li>
              </ol>
            </div>
            <div className="space-y-2">
              <p className="font-medium text-foreground">Best practices</p>
              <ul className="list-inside list-disc space-y-1">
                <li>Only enroll switches that directly control miner power.</li>
                <li>Keep the keep-alive watchdog on so outages alert you.</li>
                <li>Link each switch to its miner for targeted automation.</li>
              </ul>
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Miner Power Switches</CardTitle>
            <p className="text-sm text-muted-foreground">Only switch entities are shown. Enroll the ones automation should control.</p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <label className="inline-flex items-center gap-2 text-xs uppercase tracking-wide text-muted-foreground">
              <input
                type="checkbox"
                className="h-4 w-4 rounded border-border"
                checked={showAllDevices}
                onChange={(event) => setShowAllDevices(event.target.checked)}
                disabled={!configured}
              />
              Show all devices
            </label>
            <Button
              type="button"
              onClick={() => discoverMutation.mutate()}
              disabled={!configured || discoverMutation.isPending}
            >
              {discoverMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}Discover Devices
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {!configured ? (
            <div className="flex min-h-[200px] items-center justify-center text-sm text-muted-foreground">
              Configure Home Assistant first to pull devices.
            </div>
          ) : devicesQuery.isLoading ? (
            <div className="flex min-h-[200px] items-center justify-center text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" />
            </div>
          ) : switches.length === 0 ? (
            <div className="flex min-h-[200px] flex-col items-center justify-center text-center text-sm text-muted-foreground">
              <Plug className="mb-2 h-8 w-8" />
              {showAllDevices ? 'No switches discovered yet. Run discovery to import entities.' : 'No enrolled switches. Enable "Show all" to review candidates.'}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-muted/40 text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3">Device</th>
                    <th className="px-4 py-3">Entity</th>
                    <th className="px-4 py-3">State</th>
                    <th className="px-4 py-3">Linked Miner</th>
                    <th className="px-4 py-3">Controls</th>
                    <th className="px-4 py-3">Enrolled</th>
                  </tr>
                </thead>
                <tbody>
                  {switches.map((device) => (
                    <tr key={device.id} className="border-b border-border/60">
                      <td className="px-4 py-4 text-foreground">
                        <div className="font-medium">{device.name}</div>
                        <p className="text-xs text-muted-foreground">Switch</p>
                      </td>
                      <td className="px-4 py-4 text-xs text-muted-foreground">{device.entity_id}</td>
                      <td className="px-4 py-4">
                        {device.current_state ? (
                          <span
                            className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold ${
                              device.current_state === 'on'
                                ? 'bg-emerald-500/15 text-emerald-200'
                                : 'bg-slate-700/40 text-slate-200'
                            }`}
                          >
                            {device.current_state}
                          </span>
                        ) : (
                          <span className="text-xs text-muted-foreground">Unknown</span>
                        )}
                      </td>
                      <td className="px-4 py-4">
                        {device.miner_id ? (
                          <span className="inline-flex items-center rounded-full bg-primary/10 px-2 py-0.5 text-xs text-primary">
                            {minersQuery.data?.find((miner) => miner.id === device.miner_id)?.name || 'Linked'}
                          </span>
                        ) : (
                          <span className="text-xs text-muted-foreground">Not linked</span>
                        )}
                      </td>
                      <td className="px-4 py-4">
                        <div className="flex flex-wrap gap-2">
                          <Button
                            size="sm"
                            variant="secondary"
                            className="gap-1"
                            onClick={() => controlMutation.mutate({ deviceId: device.id, action: 'turn_on' })}
                            disabled={controlMutation.isPending}
                          >
                            <Zap className="h-4 w-4" /> On
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            className="gap-1"
                            onClick={() => controlMutation.mutate({ deviceId: device.id, action: 'turn_off' })}
                            disabled={controlMutation.isPending}
                          >
                            <ShieldAlert className="h-4 w-4" /> Off
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="gap-1"
                            onClick={() => refreshMutation.mutate(device.id)}
                            disabled={refreshMutation.isPending}
                          >
                            <RefreshCw className="h-4 w-4" />
                          </Button>
                          <Button size="sm" variant="outline" className="gap-1" onClick={() => openLinkModal(device)}>
                            <LinkIcon className="h-4 w-4" /> Link
                          </Button>
                        </div>
                      </td>
                      <td className="px-4 py-4">
                        <label className="inline-flex items-center gap-2 text-xs font-medium">
                          <input
                            type="checkbox"
                            className="h-4 w-4 rounded border-border"
                            checked={device.enrolled}
                            onChange={(event) =>
                              enrollMutation.mutate({ deviceId: device.id, enrolled: event.target.checked })
                            }
                          />
                          Enrolled
                        </label>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <Dialog open={linkOpen} onOpenChange={(open) => setLinkOpen(open)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Link Device to Miner</DialogTitle>
            <DialogDescription>
              Select which miner this switch controls so automation can target it directly.
            </DialogDescription>
          </DialogHeader>
          {linkModal.device && (
            <div className="space-y-4">
              <div className="rounded-md border border-border bg-muted/30 px-3 py-2 text-sm">
                <p className="font-medium">{linkModal.device.name}</p>
                <p className="text-xs text-muted-foreground">{linkModal.device.entity_id}</p>
              </div>
              <div className="space-y-2">
                <label className="text-sm font-medium text-foreground">Miner</label>
                <Select
                  value={linkModal.minerId || 'none'}
                  onValueChange={(value) => setLinkModal((current) => ({ ...current, minerId: value === 'none' ? '' : value }))}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Not linked" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">Not linked</SelectItem>
                    {minersQuery.data?.map((miner) => (
                      <SelectItem key={miner.id} value={String(miner.id)}>
                        <div className="flex items-center justify-between gap-3">
                          <div>
                            <p className="text-sm font-medium leading-tight">{miner.name}</p>
                            <p className="text-[11px] text-muted-foreground">{formatMinerTypeLabel(miner.miner_type)}</p>
                          </div>
                          <MinerTypeBadge type={miner.miner_type} size="sm" />
                        </div>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex justify-end gap-2">
                <Button variant="outline" onClick={() => setLinkOpen(false)}>
                  Cancel
                </Button>
                <Button onClick={handleLinkSave} disabled={linkMutation.isPending}>
                  {linkMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}Save Link
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
