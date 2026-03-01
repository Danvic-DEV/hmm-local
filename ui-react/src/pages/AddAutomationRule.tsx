import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'

type RulePayload = {
  name: string
  enabled: boolean
  trigger_type: string
  trigger_config: Record<string, unknown>
  action_type: string
  action_config: Record<string, unknown>
  priority: number
}

const TRIGGER_OPTIONS = [
  { value: 'price_threshold', label: 'Energy Price Threshold' },
  { value: 'time_window', label: 'Time Window' },
  { value: 'miner_overheat', label: 'Miner Overheat' },
]

const ACTION_OPTIONS = [
  { value: 'apply_mode', label: 'Apply Miner Mode' },
  { value: 'switch_pool', label: 'Switch Pool' },
  { value: 'send_alert', label: 'Send Alert' },
  { value: 'log_event', label: 'Log Event' },
]

function parseConfig(value: string, fieldName: string): Record<string, unknown> {
  const trimmed = value.trim()
  if (!trimmed) return {}

  let parsed: unknown
  try {
    parsed = JSON.parse(trimmed)
  } catch {
    throw new Error(`${fieldName} must be valid JSON`)
  }

  if (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object') {
    throw new Error(`${fieldName} must be a JSON object`) 
  }

  return parsed as Record<string, unknown>
}

export default function AddAutomationRule() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [name, setName] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [triggerType, setTriggerType] = useState(TRIGGER_OPTIONS[0].value)
  const [actionType, setActionType] = useState(ACTION_OPTIONS[0].value)
  const [priority, setPriority] = useState('0')
  const [triggerConfigText, setTriggerConfigText] = useState('{}')
  const [actionConfigText, setActionConfigText] = useState('{}')
  const [formError, setFormError] = useState<string | null>(null)

  const createMutation = useMutation({
    mutationFn: async (payload: RulePayload) => {
      const response = await fetch('/api/automation/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })

      if (!response.ok) {
        const errorBody = await response.json().catch(() => ({}))
        throw new Error(errorBody.detail || 'Failed to create automation rule')
      }

      return response.json()
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['automation-rules'] })
      navigate('/automation')
    },
  })

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    setFormError(null)

    if (!name.trim()) {
      setFormError('Rule name is required.')
      return
    }

    const parsedPriority = Number.parseInt(priority, 10)
    if (Number.isNaN(parsedPriority)) {
      setFormError('Priority must be a valid integer.')
      return
    }

    let triggerConfig: Record<string, unknown>
    let actionConfig: Record<string, unknown>

    try {
      triggerConfig = parseConfig(triggerConfigText, 'Trigger config')
      actionConfig = parseConfig(actionConfigText, 'Action config')
    } catch (error) {
      setFormError(error instanceof Error ? error.message : 'Invalid JSON config')
      return
    }

    const payload: RulePayload = {
      name: name.trim(),
      enabled,
      trigger_type: triggerType,
      trigger_config: triggerConfig,
      action_type: actionType,
      action_config: actionConfig,
      priority: parsedPriority,
    }

    try {
      await createMutation.mutateAsync(payload)
    } catch (error) {
      setFormError(error instanceof Error ? error.message : 'Failed to create automation rule')
    }
  }

  return (
    <div className="p-6">
      <div className="max-w-3xl mx-auto">
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1">
              <p className="text-sm uppercase tracking-wider text-blue-300">Automation</p>
              <h1 className="text-2xl font-semibold">Add Automation Rule</h1>
              <p className="text-gray-400 text-sm">
                Create a new rule by selecting trigger/action types and providing JSON configs.
              </p>
            </div>
          </CardHeader>
          <CardContent>
            <form className="space-y-6" onSubmit={handleSubmit}>
              <div className="space-y-2">
                <Label htmlFor="rule-name">Rule Name</Label>
                <input
                  id="rule-name"
                  type="text"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="e.g., Cheap Power Turbo"
                  className="w-full rounded-md border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-gray-100 placeholder:text-gray-500 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-600/40"
                  required
                />
              </div>

              <div className="grid gap-6 md:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="trigger-type">Trigger Type</Label>
                  <select
                    id="trigger-type"
                    value={triggerType}
                    onChange={(event) => setTriggerType(event.target.value)}
                    className="w-full rounded-md border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-600/40"
                  >
                    {TRIGGER_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="action-type">Action Type</Label>
                  <select
                    id="action-type"
                    value={actionType}
                    onChange={(event) => setActionType(event.target.value)}
                    className="w-full rounded-md border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-600/40"
                  >
                    {ACTION_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              <div className="grid gap-6 md:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="priority">Priority</Label>
                  <input
                    id="priority"
                    type="number"
                    value={priority}
                    onChange={(event) => setPriority(event.target.value)}
                    className="w-full rounded-md border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-600/40"
                  />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="enabled" className="block">Initial Status</Label>
                  <select
                    id="enabled"
                    value={enabled ? 'enabled' : 'paused'}
                    onChange={(event) => setEnabled(event.target.value === 'enabled')}
                    className="w-full rounded-md border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-gray-100 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-600/40"
                  >
                    <option value="enabled">Enabled</option>
                    <option value="paused">Paused</option>
                  </select>
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="trigger-config">Trigger Config (JSON object)</Label>
                <textarea
                  id="trigger-config"
                  value={triggerConfigText}
                  onChange={(event) => setTriggerConfigText(event.target.value)}
                  rows={6}
                  className="w-full rounded-md border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-gray-100 placeholder:text-gray-500 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-600/40"
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="action-config">Action Config (JSON object)</Label>
                <textarea
                  id="action-config"
                  value={actionConfigText}
                  onChange={(event) => setActionConfigText(event.target.value)}
                  rows={6}
                  className="w-full rounded-md border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-gray-100 placeholder:text-gray-500 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-600/40"
                />
              </div>

              {formError && (
                <div className="rounded-md border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-200">{formError}</div>
              )}

              <div className="flex items-center justify-end gap-3">
                <Button type="button" variant="outline" onClick={() => navigate('/automation')}>
                  Cancel
                </Button>
                <Button type="submit" disabled={createMutation.isPending}>
                  {createMutation.isPending ? 'Creating…' : 'Create Rule'}
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
