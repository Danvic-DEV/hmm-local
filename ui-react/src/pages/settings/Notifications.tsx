import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  BellRing,
  Loader2,
  RefreshCcw,
  SendHorizontal,
} from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'
import { humanizeKey } from '@/lib/textFormatters'
import {
  notificationsAPI,
  NotificationChannelType,
  AlertConfigItem,
  APIError,
} from '@/lib/api'

type BannerTone = 'success' | 'error' | 'info'

interface ChannelField {
  name: string
  label: string
  placeholder: string
  hint?: string
  type?: 'text' | 'password'
}

interface ChannelMeta {
  type: NotificationChannelType
  label: string
  description: string
  emoji: string
  fields: ChannelField[]
}

interface ChannelFormState {
  enabled: boolean
  config: Record<string, string>
}

type ChannelSecretsState = Record<NotificationChannelType, Record<string, boolean>>

const CHANNELS: ChannelMeta[] = [
  {
    type: 'telegram',
    label: 'Telegram',
    description: 'Send alerts through your Telegram bot',
    emoji: '📱',
    fields: [
      {
        name: 'bot_token',
        label: 'Bot Token',
        placeholder: '123456789:ABCdefGHIjklMNOpqrsTUVwxyz',
        hint: 'Create and manage tokens via @BotFather',
      },
      {
        name: 'chat_id',
        label: 'Chat ID',
        placeholder: '123456789',
        hint: 'Send a message to your bot and call getUpdates to find the chat ID',
      },
    ],
  },
  {
    type: 'discord',
    label: 'Discord',
    description: 'Send alerts to a Discord channel via webhook',
    emoji: '💬',
    fields: [
      {
        name: 'webhook_url',
        label: 'Webhook URL',
        placeholder: 'https://discord.com/api/webhooks/...',
        hint: 'Create a webhook in Server Settings → Integrations → Webhooks',
      },
    ],
  },
]

const ALERT_PRESETS: Record<
  string,
  { label: string; description: string }
> = {
  high_temperature: {
    label: 'High Temperature',
    description: 'Alert when a miner exceeds its safe thermal threshold.',
  },
  block_found: {
    label: 'Block Found 🎉',
    description: 'Celebrate whenever any configured pool reports a block.',
  },
  aggregation_status: {
    label: 'Telemetry Aggregation',
    description: 'Get notified when async aggregation succeeds or fails during off periods.',
  },
  ha_offline: {
    label: 'Home Assistant Offline',
    description: 'Alert when the Home Assistant bridge cannot be reached.',
  },
}

const createDefaultChannelState = (): Record<NotificationChannelType, ChannelFormState> => ({
  telegram: { enabled: false, config: { bot_token: '', chat_id: '' } },
  discord: { enabled: false, config: { webhook_url: '' } },
})

export default function Notifications() {
  const queryClient = useQueryClient()
  const [banner, setBanner] = useState<{ tone: BannerTone; message: string } | null>(null)
  const [channelForms, setChannelForms] = useState(createDefaultChannelState)
  const [savingChannel, setSavingChannel] = useState<NotificationChannelType | null>(null)
  const [testingChannel, setTestingChannel] = useState<NotificationChannelType | null>(null)
  const [updatingAlert, setUpdatingAlert] = useState<string | null>(null)
  const [storedSecrets, setStoredSecrets] = useState<ChannelSecretsState>({ telegram: {}, discord: {} })
  const [changingSecrets, setChangingSecrets] = useState<ChannelSecretsState>({ telegram: {}, discord: {} })

  const channelsQuery = useQuery({
    queryKey: ['notification-channels'],
    queryFn: notificationsAPI.getChannels,
  })

  const alertsQuery = useQuery({
    queryKey: ['notification-alerts'],
    queryFn: notificationsAPI.getAlerts,
  })

  const logsQuery = useQuery({
    queryKey: ['notification-logs'],
    queryFn: () => notificationsAPI.getLogs(50),
    refetchInterval: 30000,
  })

  useEffect(() => {
    if (!channelsQuery.data) return
    const nextStoredSecrets: ChannelSecretsState = { telegram: {}, discord: {} }

    setChannelForms(() => {
      const next = createDefaultChannelState()
      channelsQuery.data?.forEach((channel) => {
        const fields = next[channel.channel_type]
        if (!fields) return
        nextStoredSecrets[channel.channel_type] = channel.stored_secrets || {}
        next[channel.channel_type] = {
          enabled: channel.enabled,
          config: Object.keys(fields.config).reduce((acc, key) => {
            const value = channel.config?.[key]
            acc[key] = value === undefined || value === null ? '' : String(value)
            return acc
          }, {} as Record<string, string>),
        }
      })
      return next
    })
    setStoredSecrets(nextStoredSecrets)
    setChangingSecrets({ telegram: {}, discord: {} })
  }, [channelsQuery.data])

  const saveChannelMutation = useMutation({
    mutationFn: notificationsAPI.upsertChannel,
    onSuccess: (_, variables) => {
      showBanner('success', `${humanizeKey(variables.channel_type)} notifications saved`)
      queryClient.invalidateQueries({ queryKey: ['notification-channels'] })
    },
    onError: (error) => showBanner('error', extractError(error)),
    onSettled: () => setSavingChannel(null),
  })

  const testChannelMutation = useMutation({
    mutationFn: notificationsAPI.testChannel,
    onSuccess: (_, channelType) =>
      showBanner('success', `Test notification sent to ${humanizeKey(channelType)}`),
    onError: (error) => showBanner('error', extractError(error)),
    onSettled: () => setTestingChannel(null),
  })

  const updateAlertMutation = useMutation({
    mutationFn: ({ alertType, enabled }: { alertType: string; enabled: boolean }) =>
      notificationsAPI.updateAlert(alertType, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['notification-alerts'] }),
    onError: (error) => showBanner('error', extractError(error)),
    onSettled: () => setUpdatingAlert(null),
  })

  const alertsWithMeta = useMemo(() => {
    const alerts = alertsQuery.data ?? []
    if (alerts.length === 0) return alerts
    return alerts.sort((a, b) => a.alert_type.localeCompare(b.alert_type))
  }, [alertsQuery.data])

  const logs = logsQuery.data ?? []

  const showBanner = (tone: BannerTone, message: string) => {
    setBanner({ tone, message })
    window.setTimeout(() => setBanner(null), 5000)
  }

  const extractError = (error: unknown) => {
    if (error instanceof APIError) {
      const detail = (error.data as { detail?: string })?.detail
      return detail || error.message
    }
    if (error instanceof Error) {
      return error.message
    }
    return 'Something went wrong'
  }

  const handleToggleChannel = (channel: NotificationChannelType, enabled: boolean) => {
    setChannelForms((prev) => ({
      ...prev,
      [channel]: {
        ...prev[channel],
        enabled,
      },
    }))
  }

  const handleFieldChange = (
    channel: NotificationChannelType,
    field: string,
    value: string
  ) => {
    setChannelForms((prev) => ({
      ...prev,
      [channel]: {
        ...prev[channel],
        config: {
          ...prev[channel].config,
          [field]: value,
        },
      },
    }))
  }

  const handleSaveChannel = (channel: ChannelMeta) => {
    const form = channelForms[channel.type]
    if (!form) return

    const channelStoredSecrets = storedSecrets[channel.type] || {}
    const channelChangingSecrets = changingSecrets[channel.type] || {}

    const isSecretField = (field: ChannelField) =>
      field.type === 'password' || field.name === 'bot_token' || field.name === 'webhook_url'

    if (form.enabled) {
      const missingField = channel.fields.find((field) => {
        if (isSecretField(field)) {
          const hasStoredSecret = Boolean(channelStoredSecrets[field.name])
          const isChangingSecret = Boolean(channelChangingSecrets[field.name])
          if (hasStoredSecret && !isChangingSecret) {
            return false
          }
        }
        return !form.config[field.name]?.trim()
      })
      if (missingField) {
        showBanner('error', `Please fill ${missingField.label} before enabling ${channel.label}`)
        return
      }
    }

    setSavingChannel(channel.type)
    saveChannelMutation.mutate({
      channel_type: channel.type,
      enabled: form.enabled,
      config: form.config,
    })
  }

  const handleTestChannel = (channel: NotificationChannelType) => {
    const form = channelForms[channel]
    if (!form?.enabled) {
      showBanner('error', `${humanizeKey(channel)} channel must be enabled before testing`)
      return
    }
    setTestingChannel(channel)
    testChannelMutation.mutate(channel)
  }

  const handleToggleAlert = (alert: AlertConfigItem, enabled: boolean) => {
    setUpdatingAlert(alert.alert_type)
    updateAlertMutation.mutate({ alertType: alert.alert_type, enabled })
  }

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <div className="flex items-center gap-3 text-3xl font-semibold text-foreground">
          <BellRing className="h-8 w-8 text-blue-400" />
          <span>Notifications</span>
        </div>
        <p className="text-base text-muted-foreground">
          Manage Telegram and Discord delivery, toggle alert types, and review recent notification attempts.
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

      <Card className="border-border/60 bg-muted/5">
        <CardHeader>
          <CardTitle className="text-lg">Notification Channels</CardTitle>
        </CardHeader>
        <CardContent className="space-y-6">
          {CHANNELS.map((channel) => {
            const form = channelForms[channel.type]
            const isLoading = channelsQuery.isLoading
            return (
              <div key={channel.type} className="rounded-2xl border border-border/70 bg-background/60 p-4">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <div>
                    <div className="text-lg font-semibold text-foreground">
                      {channel.emoji} {channel.label}
                    </div>
                    <p className="text-sm text-muted-foreground">{channel.description}</p>
                  </div>
                  <ToggleSwitch
                    checked={form?.enabled ?? false}
                    disabled={isLoading || saveChannelMutation.isPending}
                    onCheckedChange={(checked) => handleToggleChannel(channel.type, checked)}
                  />
                </div>

                {form?.enabled && (
                  <div className="mt-4 space-y-4">
                    {channel.fields.map((field) => (
                      <div key={field.name} className="space-y-2">
                        <Label className="text-sm text-muted-foreground">{field.label}</Label>
                        {((field.type === 'password' || field.name === 'bot_token' || field.name === 'webhook_url') &&
                          storedSecrets[channel.type]?.[field.name] &&
                          !changingSecrets[channel.type]?.[field.name]) ? (
                          <div className="space-y-2 rounded-lg border border-border/70 bg-muted/5 px-3 py-3">
                            <div className="text-sm text-foreground">Stored value is configured.</div>
                            <div className="flex gap-2">
                              <Button
                                type="button"
                                variant="secondary"
                                onClick={() =>
                                  setChangingSecrets((prev) => ({
                                    ...prev,
                                    [channel.type]: {
                                      ...prev[channel.type],
                                      [field.name]: true,
                                    },
                                  }))
                                }
                              >
                                Change value
                              </Button>
                            </div>
                          </div>
                        ) : (
                          <>
                            <input
                              type={field.type || 'text'}
                              value={form.config[field.name] ?? ''}
                              onChange={(event) =>
                                handleFieldChange(channel.type, field.name, event.target.value)
                              }
                              className="w-full rounded-lg border border-border/70 bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-blue-500/40"
                              placeholder={field.placeholder}
                            />
                            {(field.type === 'password' || field.name === 'bot_token' || field.name === 'webhook_url') &&
                              storedSecrets[channel.type]?.[field.name] &&
                              changingSecrets[channel.type]?.[field.name] && (
                                <div className="flex justify-end">
                                  <button
                                    type="button"
                                    className="text-xs text-muted-foreground underline-offset-4 hover:underline"
                                    onClick={() => {
                                      setChangingSecrets((prev) => ({
                                        ...prev,
                                        [channel.type]: {
                                          ...prev[channel.type],
                                          [field.name]: false,
                                        },
                                      }))
                                      handleFieldChange(channel.type, field.name, '')
                                    }}
                                  >
                                    Keep existing value
                                  </button>
                                </div>
                              )}
                          </>
                        )}
                        {field.hint && (
                          <p className="text-xs text-muted-foreground">{field.hint}</p>
                        )}
                      </div>
                    ))}

                    <div className="flex flex-wrap gap-2 pt-2">
                      <Button
                        onClick={() => handleSaveChannel(channel)}
                        disabled={savingChannel === channel.type && saveChannelMutation.isPending}
                      >
                        {savingChannel === channel.type && saveChannelMutation.isPending && (
                          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        )}
                        Save configuration
                      </Button>
                      <Button
                        type="button"
                        variant="secondary"
                        onClick={() => handleTestChannel(channel.type)}
                        disabled={testingChannel === channel.type && testChannelMutation.isPending}
                      >
                        {testingChannel === channel.type && testChannelMutation.isPending ? (
                          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        ) : (
                          <SendHorizontal className="mr-2 h-4 w-4" />
                        )}
                        Send test
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </CardContent>
      </Card>

      <Card className="border-border/60 bg-muted/5">
        <CardHeader>
          <CardTitle className="text-lg">Alert Types</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {alertsQuery.isLoading && <SkeletonRows count={4} />}
          {alertsQuery.isError && (
            <ErrorMessage
              message="Failed to load alert configuration"
              onRetry={() => alertsQuery.refetch()}
            />
          )}
          {!alertsQuery.isLoading && !alertsQuery.isError && alertsWithMeta.length === 0 && (
            <p className="rounded-xl border border-dashed border-border/60 p-6 text-sm text-muted-foreground">
              No alert definitions found. Ensure the backend seeded default alerts.
            </p>
          )}
          {alertsWithMeta.map((alert) => {
            const meta = ALERT_PRESETS[alert.alert_type] ?? {
              label: alert.alert_type,
              description: 'Custom alert',
            }
            return (
              <div
                key={alert.alert_type}
                className="flex flex-col gap-3 rounded-2xl border border-border/70 bg-background/60 p-4 sm:flex-row sm:items-center sm:justify-between"
              >
                <div>
                  <p className="font-semibold text-foreground">{meta.label}</p>
                  <p className="text-sm text-muted-foreground">{meta.description}</p>
                </div>
                <ToggleSwitch
                  checked={alert.enabled}
                  disabled={updatingAlert === alert.alert_type && updateAlertMutation.isPending}
                  onCheckedChange={(checked) => handleToggleAlert(alert, checked)}
                />
              </div>
            )
          })}
        </CardContent>
      </Card>

      <Card className="border-border/60 bg-muted/5">
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-lg">Recent Notifications</CardTitle>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => logsQuery.refetch()}
            disabled={logsQuery.isFetching}
          >
            {logsQuery.isFetching ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <RefreshCcw className="mr-2 h-4 w-4" />
            )}
            Refresh
          </Button>
        </CardHeader>
        <CardContent>
          {logsQuery.isError && (
            <ErrorMessage message="Unable to load notification logs" onRetry={() => logsQuery.refetch()} />
          )}
          {!logsQuery.isError && (
            <div className="overflow-x-auto rounded-xl border border-border/70">
              <table className="w-full text-sm">
                <thead className="bg-background/70 text-xs uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3 text-left font-semibold">Time</th>
                    <th className="px-4 py-3 text-left font-semibold">Channel</th>
                    <th className="px-4 py-3 text-left font-semibold">Alert</th>
                    <th className="px-4 py-3 text-left font-semibold">Message</th>
                    <th className="px-4 py-3 text-left font-semibold">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {logs.length === 0 && !logsQuery.isLoading && (
                    <tr>
                      <td colSpan={5} className="px-4 py-6 text-center text-muted-foreground">
                        No notifications sent yet.
                      </td>
                    </tr>
                  )}
                  {logsQuery.isLoading && <SkeletonTableRows rows={3} columns={5} />}
                  {!logsQuery.isLoading &&
                    logs.map((log) => (
                      <tr key={log.id} className="border-t border-border/50 text-sm">
                        <td className="px-4 py-3 text-foreground">{formatTimestamp(log.timestamp)}</td>
                        <td className="px-4 py-3">{humanizeKey(log.channel_type)}</td>
                        <td className="px-4 py-3 text-muted-foreground">{humanizeKey(log.alert_type)}</td>
                        <td className="px-4 py-3 text-muted-foreground">
                          {truncate(log.message, 80)}
                          {log.error && (
                            <span className="ml-2 text-xs text-red-300">({log.error})</span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          <StatusBadge success={log.success} />
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function ToggleSwitch({
  checked,
  onCheckedChange,
  disabled,
}: {
  checked: boolean
  onCheckedChange: (checked: boolean) => void
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      onClick={() => !disabled && onCheckedChange(!checked)}
      className={cn(
        'relative inline-flex h-7 w-12 items-center rounded-full border border-border/60 bg-gray-800 transition',
        checked && 'border-blue-500 bg-blue-600/60',
        disabled && 'cursor-not-allowed opacity-50'
      )}
      aria-pressed={checked}
      aria-label="Toggle"
      disabled={disabled}
    >
      <span
        className={cn(
          'inline-block h-5 w-5 rounded-full bg-white transition-all',
          checked ? 'translate-x-6' : 'translate-x-1'
        )}
      />
    </button>
  )
}

function SkeletonRows({ count }: { count: number }) {
  return (
    <div className="space-y-3">
      {Array.from({ length: count }).map((_, idx) => (
        <div key={idx} className="h-14 animate-pulse rounded-2xl bg-muted/20" />
      ))}
    </div>
  )
}

function SkeletonTableRows({ rows, columns }: { rows: number; columns: number }) {
  return (
    <>
      {Array.from({ length: rows }).map((_, rowIdx) => (
        <tr key={rowIdx} className="border-t border-border/50">
          {Array.from({ length: columns }).map((__, colIdx) => (
            <td key={colIdx} className="px-4 py-3">
              <div className="h-4 w-full animate-pulse rounded bg-muted/20" />
            </td>
          ))}
        </tr>
      ))}
    </>
  )
}

function ErrorMessage({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="flex items-center justify-between rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-100">
      <div className="flex items-center gap-2">
        <AlertCircle className="h-4 w-4" />
        <span>{message}</span>
      </div>
      <Button size="sm" variant="secondary" onClick={onRetry}>
        Retry
      </Button>
    </div>
  )
}

function StatusBadge({ success }: { success: boolean }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-3 py-1 text-xs font-semibold uppercase',
        success ? 'bg-emerald-500/20 text-emerald-200' : 'bg-red-500/20 text-red-200'
      )}
    >
      {success ? 'Sent' : 'Failed'}
    </span>
  )
}

function truncate(text: string, maxLength: number) {
  if (text.length <= maxLength) return text
  return `${text.slice(0, maxLength)}…`
}

function formatTimestamp(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}
