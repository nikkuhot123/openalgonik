import { useEffect, useState, useCallback } from 'react'
import {
  ChevronDown,
  ChevronUp,
  Activity,
  TrendingUp,
  TrendingDown,
  Target,
  Shield,
} from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Collapsible, CollapsibleTrigger, CollapsibleContent } from '@/components/ui/collapsible'
import { pythonStrategyApi } from '@/api/python-strategy'
import type { PythonStrategy, StrategyStatus, ActiveTrade } from '@/types/python-strategy'

const STATE_BADGE_CLASSES: Record<string, string> = {
  IDLE: 'bg-blue-500/15 text-blue-700 border-blue-500/25 dark:text-blue-400',
  IN_TRADE: 'bg-green-500/15 text-green-700 border-green-500/25 dark:text-green-400',
  DONE: 'bg-gray-500/15 text-gray-600 border-gray-500/25 dark:text-gray-400',
  INACTIVE: 'bg-muted text-muted-foreground border-muted',
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

function computeGaugeProgress(trade: ActiveTrade): number {
  const { direction, current_price, stop_loss, target } = trade
  if (current_price == null || stop_loss == null || target == null) return 0

  if (direction === 'CE') {
    const range = target - stop_loss
    if (range <= 0) return 0
    return clamp(((current_price - stop_loss) / range) * 100, 0, 100)
  }

  // PE (short)
  const range = stop_loss - target
  if (range <= 0) return 0
  return clamp(((stop_loss - current_price) / range) * 100, 0, 100)
}

function isProfit(trade: ActiveTrade): boolean {
  if (trade.current_price == null || trade.entry_price == null) return false
  if (trade.direction === 'CE') return trade.current_price >= trade.entry_price
  if (trade.direction === 'PE') return trade.current_price <= trade.entry_price
  return false
}

function entryMarkerPercent(trade: ActiveTrade): number {
  const { direction, entry_price, stop_loss, target } = trade
  if (entry_price == null || stop_loss == null || target == null) return 50

  if (direction === 'CE') {
    const range = target - stop_loss
    if (range <= 0) return 50
    return clamp(((entry_price - stop_loss) / range) * 100, 0, 100)
  }

  const range = stop_loss - target
  if (range <= 0) return 50
  return clamp(((stop_loss - entry_price) / range) * 100, 0, 100)
}

function TradeGauge({ trade }: { trade: ActiveTrade }) {
  const progress = computeGaugeProgress(trade)
  const entryPct = entryMarkerPercent(trade)
  const profitable = isProfit(trade)
  const pnlColor = profitable ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'
  const dotColor = profitable ? 'bg-green-500' : 'bg-red-500'

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium">{trade.symbol}</span>
        <Badge
          variant="outline"
          className={
            trade.direction === 'CE'
              ? 'bg-green-500/15 text-green-700 border-green-500/25 dark:text-green-400'
              : trade.direction === 'PE'
                ? 'bg-red-500/15 text-red-700 border-red-500/25 dark:text-red-400'
                : ''
          }
        >
          {trade.direction}
        </Badge>
      </div>

      {/* Gauge bar */}
      <div className="relative h-3 w-full rounded-full bg-muted overflow-hidden">
        {/* Fill */}
        <div
          className="absolute inset-y-0 left-0 rounded-full bg-gradient-to-r from-red-400 via-yellow-400 to-green-400 transition-all duration-300"
          style={{ width: `${progress}%` }}
        />
        {/* Entry marker */}
        <div
          className="absolute top-0 h-full w-0.5 bg-foreground/70"
          style={{ left: `${entryPct}%` }}
        />
        {/* Current price dot */}
        <div
          className={`absolute top-1/2 -translate-y-1/2 -translate-x-1/2 h-3 w-3 rounded-full border-2 border-background ${dotColor} transition-all duration-300`}
          style={{ left: `${progress}%` }}
        />
      </div>

      {/* Labels */}
      <div className="flex justify-between text-xs text-muted-foreground">
        <span className="flex items-center gap-1">
          <Shield className="h-3 w-3" />
          SL: {trade.stop_loss?.toFixed(2) ?? '—'}
        </span>
        <span>Entry: {trade.entry_price?.toFixed(2) ?? '—'}</span>
        <span className="flex items-center gap-1">
          <Target className="h-3 w-3" />
          Tgt: {trade.target?.toFixed(2) ?? '—'}
        </span>
      </div>

      <div className={`text-sm font-medium text-center ${pnlColor}`}>
        LTP: {trade.current_price?.toFixed(2) ?? '—'}
        {trade.current_price != null && trade.entry_price != null && (
          <span className="ml-2">
            ({profitable ? 'Profit' : 'Loss'}{' '}
            {Math.abs(trade.current_price - trade.entry_price).toFixed(2)})
          </span>
        )}
      </div>
    </div>
  )
}

interface StrategyStatusPanelProps {
  strategy: PythonStrategy
}

export default function StrategyStatusPanel({ strategy }: StrategyStatusPanelProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [status, setStatus] = useState<StrategyStatus | null>(null)

  const fetchStatus = useCallback(async () => {
    try {
      const data = await pythonStrategyApi.getStrategyStatus(strategy.id)
      setStatus(data)
    } catch {
      // Silently ignore fetch errors — panel will show stale or null data
    }
  }, [strategy.id])

  useEffect(() => {
    if (!isOpen) return

    // Fetch immediately on open
    fetchStatus()

    const intervalId = setInterval(fetchStatus, 3000)
    return () => clearInterval(intervalId)
  }, [isOpen, fetchStatus])

  const isRunning = strategy.status === 'running'
  const state = status?.state ?? (isRunning ? 'IDLE' : 'INACTIVE')
  const badgeClasses = STATE_BADGE_CLASSES[state] ?? STATE_BADGE_CLASSES.INACTIVE

  return (
    <Collapsible open={isOpen} onOpenChange={setIsOpen}>
      <Card className="py-0 gap-0 overflow-hidden">
        <CollapsibleTrigger asChild>
          <button
            type="button"
            className="flex w-full items-center justify-between px-4 py-3 text-left hover:bg-muted/50 transition-colors"
          >
            <div className="flex items-center gap-3">
              <Activity
                className={`h-4 w-4 ${isRunning ? 'text-green-500 animate-pulse' : 'text-muted-foreground'}`}
              />
              <span className="text-sm font-medium">{strategy.name}</span>
              <Badge variant="outline" className={badgeClasses}>
                {state.replace('_', ' ')}
              </Badge>
            </div>
            {isOpen ? (
              <ChevronUp className="h-4 w-4 text-muted-foreground" />
            ) : (
              <ChevronDown className="h-4 w-4 text-muted-foreground" />
            )}
          </button>
        </CollapsibleTrigger>

        <CollapsibleContent>
          <CardContent className="border-t px-4 py-4">
            {status == null ? (
              <p className="text-sm text-muted-foreground text-center py-4">No data</p>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                {/* Left: Active Positions */}
                <div className="space-y-4">
                  <h4 className="text-sm font-semibold">Active Positions</h4>
                  {status.active_trades.length === 0 ? (
                    <p className="text-sm text-muted-foreground py-2">No active trades</p>
                  ) : (
                    <div className="space-y-4">
                      {status.active_trades.map((trade, i) => (
                        <TradeGauge key={`${trade.symbol}-${i}`} trade={trade} />
                      ))}
                    </div>
                  )}
                </div>

                {/* Right: Signals & Indicators */}
                <div className="space-y-4">
                  <h4 className="text-sm font-semibold">Signals &amp; Indicators</h4>
                  <div className="grid grid-cols-2 gap-3">
                    <IndicatorCell
                      label="Regime"
                      value={status.indicators.regime}
                      icon={
                        status.indicators.regime?.toUpperCase().includes('UP') ||
                        status.indicators.regime?.toUpperCase().includes('GREEN') ? (
                          <TrendingUp className="h-3.5 w-3.5 text-green-500" />
                        ) : status.indicators.regime?.toUpperCase().includes('DOWN') ||
                          status.indicators.regime?.toUpperCase().includes('RED') ? (
                          <TrendingDown className="h-3.5 w-3.5 text-red-500" />
                        ) : null
                      }
                    />
                    <IndicatorCell label="Phase" value={status.indicators.phase} />
                    <IndicatorCell label="Velocity" value={status.indicators.velocity} />
                    <IndicatorCell label="ATR" value={status.indicators.atr} />
                  </div>

                  {/* Last log message strip */}
                  {status.last_log_message && (
                    <div className="rounded-md bg-black px-3 py-2 overflow-x-auto">
                      <code className="text-xs text-green-400 font-mono whitespace-pre">
                        {status.last_log_message}
                      </code>
                    </div>
                  )}
                </div>
              </div>
            )}
          </CardContent>
        </CollapsibleContent>
      </Card>
    </Collapsible>
  )
}

function IndicatorCell({
  label,
  value,
  icon,
}: {
  label: string
  value: string | undefined
  icon?: React.ReactNode
}) {
  return (
    <div className="rounded-md border px-3 py-2">
      <p className="text-xs text-muted-foreground">{label}</p>
      <div className="flex items-center gap-1.5 mt-0.5">
        {icon}
        <span className="text-sm font-medium">{value ?? '—'}</span>
      </div>
    </div>
  )
}
