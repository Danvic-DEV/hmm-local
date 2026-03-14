import { FormEvent, useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Cloud, UploadCloud, Info, Loader2, RefreshCw } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { APIError, cloudAPI, CloudConfigResponse, UpdateCloudConfigPayload } from '@/lib/api'
import { cn } from '@/lib/utils'

type BannerTone = 'success' | 'error' | 'info'

const ENDPOINT_PRESETS = [
  { label: 'Staging (stage-ingest.miningpool.uk)', value: 'https://stage-ingest.miningpool.uk/ingest' },
  { label: 'Production (cloud-ingest.miningpool.uk)', value: 'https://cloud-ingest.miningpool.uk/ingest' },
  { label: 'Local development', value: 'http://localhost:8082/ingest' },
]

interface FormState {
  enabled: boolean
  apiKey: string
  endpoint: string
  installationName: string
  installationLocation: string
  pushIntervalMinutes: number
}

const initialFormState: FormState = {
  enabled: false,
  apiKey: '',
  endpoint: ENDPOINT_PRESETS[0].value,
  installationName: 'My Home Mining Setup',
  installationLocation: '',
  pushIntervalMinutes: 5,
}

export default function CloudSettings() {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<FormState>(initialFormState)
  const [formError, setFormError] = useState<string | null>(null)
  const [banner, setBanner] = useState<{ tone: BannerTone; message: string } | null>(null)
  const [isChangingApiKey, setIsChangingApiKey] = useState(false)

  const configQuery = useQuery<CloudConfigResponse>({
    queryKey: ['cloud-config'],
    queryFn: cloudAPI.getConfig,
  })

  const config = configQuery.data

  useEffect(() => {
    if (config) {
      setForm((current) => ({
        ...current,
        enabled: config.enabled,
        endpoint: config.endpoint,
        installationName: config.installation_name,
        installationLocation: config.installation_location ?? '',
        pushIntervalMinutes: config.push_interval_minutes,
        apiKey: '',
      }))
      setIsChangingApiKey(false)
    }
  }, [config])

  const showBanner = (tone: BannerTone, message: string) => {
    setBanner({ tone, message })
    setTimeout(() => setBanner(null), 5000)
  }

  const extractError = (error: unknown) => {
    if (error instanceof APIError) {
      if (error.data && typeof error.data === 'object') {
        const detail = (error.data as { detail?: unknown }).detail
        if (typeof detail === 'string') {
          return detail
        }
      }
      return error.message
    }
    if (error instanceof Error) return error.message
    return 'Something went wrong'
  }

  const updateMutation = useMutation({
    mutationFn: (payload: UpdateCloudConfigPayload) => cloudAPI.updateConfig(payload),
    onSuccess: (response) => {
      showBanner('success', response.message || 'Cloud configuration saved')
      queryClient.invalidateQueries({ queryKey: ['cloud-config'] })
      setForm((current) => ({ ...current, apiKey: '' }))
      setIsChangingApiKey(false)
    },
    onError: (error) => {
      showBanner('error', extractError(error))
    },
  })

  const testMutation = useMutation({
    mutationFn: () => cloudAPI.testConnection(),
    onSuccess: (response) => {
      showBanner(response.success ? 'success' : 'error', response.message || 'Connection test completed')
    },
    onError: (error) => showBanner('error', extractError(error)),
  })

  const pushMutation = useMutation({
    mutationFn: () => cloudAPI.manualPush(),
    onSuccess: (response) => {
      const tone: BannerTone = response.status === 'success' ? 'success' : 'error'
      showBanner(tone, response.message || 'Manual push triggered')
    },
    onError: (error) => showBanner('error', extractError(error)),
  })

  const handleInputChange = (field: keyof FormState) => (value: string | number | boolean) => {
    setForm((current) => ({
      ...current,
      [field]: value,
    }))
  }

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setFormError(null)

    const hasStoredApiKey = Boolean(config?.api_key)
    const enteredApiKey = form.apiKey.trim()
    const keyRequired = form.enabled && (!hasStoredApiKey || isChangingApiKey)

    if (!form.installationName.trim()) {
      setFormError('Installation name is required')
      return
    }

    if (!form.endpoint.trim()) {
      setFormError('Cloud endpoint is required')
      return
    }

    if (form.pushIntervalMinutes < 1 || form.pushIntervalMinutes > 60) {
      setFormError('Push interval must be between 1 and 60 minutes')
      return
    }

    if (keyRequired && !enteredApiKey) {
      setFormError('Enter your API key to enable cloud sync')
      return
    }

    const payload: UpdateCloudConfigPayload = {
      enabled: form.enabled,
      api_key: enteredApiKey ? enteredApiKey : null,
      endpoint: form.endpoint.trim(),
      installation_name: form.installationName.trim(),
      installation_location: form.installationLocation.trim() || null,
      push_interval_minutes: form.pushIntervalMinutes,
    }

    updateMutation.mutate(payload)
  }

  const statusBadge = useMemo(() => {
    if (!config) return null
    const enabled = config.enabled
    return (
      <span
        className={cn(
          'inline-flex items-center gap-1 rounded-full px-3 py-1 text-xs font-semibold',
          enabled ? 'bg-emerald-500/15 text-emerald-300' : 'bg-amber-500/15 text-amber-200'
        )}
      >
        {enabled ? <CheckCircle2 className="h-4 w-4" /> : <AlertTriangle className="h-4 w-4" />}
        {enabled ? 'Enabled' : 'Disabled'}
      </span>
    )
  }, [config])

  if (configQuery.isLoading) {
    return <CloudSettingsSkeleton />
  }

  if (configQuery.isError) {
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-3 text-2xl font-semibold">
          <Cloud className="h-8 w-8 text-blue-400" />
          Cloud Settings
        </div>
        <ErrorState message={extractError(configQuery.error)} onRetry={() => configQuery.refetch()} />
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-3 text-3xl font-semibold text-foreground">
          <Cloud className="h-8 w-8 text-blue-400" />
          <span>Cloud Settings</span>
        </div>
        <p className="text-base text-muted-foreground">
          Push telemetry to HMM Cloud Aggregator and monitor every installation from one centralized dashboard.
        </p>
      </div>

      {banner && (
        <div
          className={cn(
            'rounded-xl border px-4 py-3 text-sm',
            banner.tone === 'success' && 'border-emerald-500/40 bg-emerald-500/10 text-emerald-100',
            banner.tone === 'error' && 'border-red-500/40 bg-red-500/10 text-red-100',
            banner.tone === 'info' && 'border-blue-500/40 bg-blue-500/10 text-blue-100'
          )}
        >
          {banner.message}
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[1.5fr,1fr]">
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-3 text-lg">
                <UploadCloud className="h-5 w-5 text-blue-300" /> Connection status
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-5">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-semibold text-foreground">
                    {config?.enabled ? 'Cloud sync enabled' : 'Cloud sync disabled'}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {config?.enabled
                      ? `Telemetry pushes every ${config?.push_interval_minutes ?? 5} minutes`
                      : 'Enable to begin pushing miner telemetry to HMM Cloud'}
                  </p>
                </div>
                {statusBadge}
              </div>
              <dl className="grid gap-4 text-sm md:grid-cols-2">
                <div>
                  <dt className="text-xs uppercase tracking-wide text-muted-foreground">Endpoint</dt>
                  <dd className="font-semibold text-foreground">{config?.endpoint}</dd>
                </div>
                <div>
                  <dt className="text-xs uppercase tracking-wide text-muted-foreground">Push interval</dt>
                  <dd className="font-semibold text-foreground">{config?.push_interval_minutes} min</dd>
                </div>
                <div>
                  <dt className="text-xs uppercase tracking-wide text-muted-foreground">Installation name</dt>
                  <dd className="font-semibold text-foreground">{config?.installation_name}</dd>
                </div>
                <div>
                  <dt className="text-xs uppercase tracking-wide text-muted-foreground">API key</dt>
                  <dd className="font-semibold text-foreground">
                    {config?.api_key ? 'Stored securely' : 'Not configured'}
                  </dd>
                </div>
              </dl>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-lg">Configuration</CardTitle>
            </CardHeader>
            <CardContent>
              <form className="space-y-6" onSubmit={handleSubmit}>
                <div className="flex items-start justify-between gap-4 rounded-xl border border-border/60 bg-muted/5 px-4 py-3">
                  <div>
                    <p className="text-sm font-semibold text-foreground">Enable cloud sync</p>
                    <p className="text-xs text-muted-foreground">
                      When enabled, miner telemetry and events are pushed to the selected endpoint.
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => handleInputChange('enabled')(!form.enabled)}
                    className={cn(
                      'flex h-6 w-12 items-center rounded-full border border-border px-0.5 transition-colors',
                      form.enabled ? 'bg-blue-500/80 border-blue-400' : 'bg-gray-800'
                    )}
                    aria-pressed={form.enabled}
                  >
                    <span
                      className={cn(
                        'h-5 w-5 rounded-full bg-white shadow transition-transform',
                        form.enabled ? 'translate-x-6' : 'translate-x-0'
                      )}
                    />
                  </button>
                </div>

                <div className="space-y-2">
                  <label className="text-sm font-medium text-foreground">API key</label>
                  {config?.api_key && !isChangingApiKey ? (
                    <div className="space-y-2 rounded-lg border border-border/70 bg-muted/5 px-3 py-3">
                      <div className="text-sm text-foreground">Stored API key is configured.</div>
                      <div className="flex gap-2">
                        <Button type="button" variant="secondary" onClick={() => setIsChangingApiKey(true)}>
                          Change API key
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <input
                        type="password"
                        value={form.apiKey}
                        onChange={(event) => handleInputChange('apiKey')(event.target.value)}
                        className="w-full rounded-lg border border-border/70 bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-blue-500/40"
                        placeholder="sk_live_xxx"
                      />
                      <div className="flex items-center justify-between gap-2">
                        <p className="text-xs text-muted-foreground">
                          API keys are stored securely and are never shown.
                        </p>
                        {config?.api_key && isChangingApiKey && (
                          <button
                            type="button"
                            className="text-xs text-muted-foreground underline-offset-4 hover:underline"
                            onClick={() => {
                              setIsChangingApiKey(false)
                              setForm((current) => ({ ...current, apiKey: '' }))
                            }}
                          >
                            Keep existing key
                          </button>
                        )}
                      </div>
                    </>
                  )}
                </div>

                <div className="space-y-2">
                  <label className="text-sm font-medium text-foreground">Endpoint URL</label>
                  <input
                    type="text"
                    value={form.endpoint}
                    onChange={(event) => handleInputChange('endpoint')(event.target.value)}
                    className="w-full rounded-lg border border-border/70 bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-blue-500/40"
                    placeholder="https://stage-ingest.miningpool.uk/ingest"
                  />
                  <div className="flex flex-wrap gap-2 pt-1">
                    {ENDPOINT_PRESETS.map((preset) => (
                      <button
                        key={preset.value}
                        type="button"
                        onClick={() => handleInputChange('endpoint')(preset.value)}
                        className={cn(
                          'rounded-full border px-3 py-1 text-xs font-medium transition-colors',
                          form.endpoint === preset.value
                            ? 'border-blue-400 bg-blue-500/20 text-blue-100'
                            : 'border-border/70 text-muted-foreground hover:border-blue-400 hover:text-blue-200'
                        )}
                      >
                        {preset.label}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-2">
                    <label className="text-sm font-medium text-foreground">Installation name</label>
                    <input
                      type="text"
                      value={form.installationName}
                      onChange={(event) => handleInputChange('installationName')(event.target.value)}
                      className="w-full rounded-lg border border-border/70 bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-blue-500/40"
                      placeholder="Garage ASIC Fleet"
                    />
                  </div>
                  <div className="space-y-2">
                    <label className="text-sm font-medium text-foreground">Location (optional)</label>
                    <input
                      type="text"
                      value={form.installationLocation}
                      onChange={(event) => handleInputChange('installationLocation')(event.target.value)}
                      className="w-full rounded-lg border border-border/70 bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-blue-500/40"
                      placeholder="Home office, Shed, etc."
                    />
                  </div>
                </div>

                <div className="space-y-2">
                  <label className="text-sm font-medium text-foreground">Push interval (minutes)</label>
                  <input
                    type="number"
                    min={1}
                    max={60}
                    value={form.pushIntervalMinutes}
                    onChange={(event) => handleInputChange('pushIntervalMinutes')(Number(event.target.value))}
                    className="w-full rounded-lg border border-border/70 bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-blue-500/40"
                  />
                  <p className="text-xs text-muted-foreground">Between 1 and 60 minutes. Default: every 5 minutes.</p>
                </div>

                {formError && <p className="text-sm text-red-300">{formError}</p>}

                <div className="flex flex-wrap gap-3">
                  <Button type="submit" disabled={updateMutation.isPending}>
                    {updateMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                    Save configuration
                  </Button>
                  <Button
                    type="button"
                    variant="secondary"
                    disabled={testMutation.isPending}
                    onClick={() => testMutation.mutate()}
                  >
                    {testMutation.isPending ? (
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    ) : (
                      <RefreshCw className="mr-2 h-4 w-4" />
                    )}
                    Test connection
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    disabled={!config?.enabled || pushMutation.isPending}
                    onClick={() => pushMutation.mutate()}
                  >
                    {pushMutation.isPending ? (
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    ) : (
                      <UploadCloud className="mr-2 h-4 w-4" />
                    )}
                    Manual push
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>
        </div>

        <div className="space-y-6">
          <Card className="h-fit">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <Info className="h-4 w-4 text-blue-300" /> How to set up
              </CardTitle>
            </CardHeader>
            <CardContent>
              <ol className="space-y-4 text-sm text-muted-foreground">
                <li>
                  <span className="font-semibold text-foreground">1. Register on HMM Cloud</span>
                  <p>Create your account with WebAuthn passkeys and add a device to generate an API key.</p>
                </li>
                <li>
                  <span className="font-semibold text-foreground">2. Paste the API key</span>
                  <p>Enter the key once, then only change it when needed. Keys are stored securely on disk.</p>
                </li>
                <li>
                  <span className="font-semibold text-foreground">3. Select an ingest endpoint</span>
                  <p>Use staging for tests, production for live monitoring, or point to your own ingest service.</p>
                </li>
                <li>
                  <span className="font-semibold text-foreground">4. Test and verify</span>
                  <p>Use the test and manual push buttons to confirm the connection before enabling automation.</p>
                </li>
              </ol>
            </CardContent>
          </Card>

          <Card className="h-fit">
            <CardHeader>
              <CardTitle className="text-base">Need to restart?</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-sm text-muted-foreground">
              <p>
                After changing endpoints or API keys, allow one interval for telemetry to appear in the cloud. If data
                still does not arrive, restart the container from the Restart page and try another manual push.
              </p>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}

function CloudSettingsSkeleton() {
  return (
    <div className="space-y-6">
      <div className="h-8 w-64 animate-pulse rounded bg-muted/40" />
      <div className="grid gap-6 lg:grid-cols-[1.5fr,1fr]">
        {[1, 2, 3].map((item) => (
          <div key={item} className="space-y-3 rounded-2xl border border-border/40 bg-muted/5 p-6">
            <div className="h-5 w-1/3 animate-pulse rounded bg-muted/40" />
            <div className="space-y-2">
              {[...Array(4)].map((_, index) => (
                <div key={index} className="h-10 animate-pulse rounded bg-muted/30" />
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <Card className="border-red-500/40 bg-red-500/10">
      <CardContent className="flex flex-col gap-4 py-6">
        <div className="flex items-center gap-2 text-sm text-red-100">
          <AlertTriangle className="h-5 w-5" />
          {message}
        </div>
        <div>
          <Button variant="secondary" onClick={onRetry}>
            Retry
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
