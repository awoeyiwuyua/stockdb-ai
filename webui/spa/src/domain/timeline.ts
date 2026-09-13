// domain/timeline.ts — 驾驶舱同步矩阵与告警横幅的纯判定（0.10.38 改版）
//
// 为什么独立成纯函数：折叠语义（哪些步骤外推、哪些折进 "+N 次成功"）与告警口径
// （哪条算"需处理"）是这批改版的核心，必须能离线测（Vitest），不埋进模板表达式。
//
// 与后端的分工：后端 sync_failure_class 给每条记录 class/label/needs_action/detail
// （语义判定），前端只做**展示分组**（外推哪些胶囊、折几条）——不在前端重判语义。

import type { SyncHistoryRow, TimelineDay } from '../types/api'
import type { LightTone } from '../types/ui'

/** 展示用色调：ok 成功 / err 真失败 / warn 需要注意但非故障 / muted 中性 */
export type SyncTone = 'ok' | 'err' | 'warn' | 'muted'

/** 需要"外推"（始终显示、不折叠）的类：真问题与需关注项 */
const IMPORTANT_CLASSES = new Set([
  'verify_failed',
  'data_source_error',
  'not_effective',
  'unknown',
])

/** 已知的良性/中性类（折叠或淡显） */
const BENIGN_CLASSES = new Set(['ok', 'self_healed', 'awaiting_mirror', 'run_interrupted'])

export interface SyncStep {
  row: SyncHistoryRow
  hhmm: string
  tone: SyncTone
  /** 胶囊文字：如 "16:17 ✗ 数据未更新" */
  text: string
  /** 是否需要外推显示（不折进 "+N 次成功"） */
  important: boolean
  /** 后端分类（缺失时为空串，走本地兜底 tone） */
  cls: string
}

export interface SyncRowSummary {
  /** 外推显示的步骤（失败/需关注；按时间升序） */
  important: SyncStep[]
  /** 被折叠的成功/中性步骤数 */
  foldedCount: number
  /** 最近一次成功步骤（折叠时展示它的时间，让"最后一次成功"仍可见） */
  lastOk: SyncStep | null
  /** 折叠文案：如 "+5 次成功"；无折叠返回空串 */
  foldedText: string
  /** 任意一条 needs_action → 行需处理 */
  needsAction: boolean
  /** 行级提示（后端 action_hint 优先，否则取首条 important 的 detail） */
  hint: string
  /** 行整体色调：有真失败 err / 仅需注意 warn / 全绿 ok / 无记录 muted */
  tone: SyncTone
}

function hhmmOf(ts: unknown): string {
  return typeof ts === 'string' && ts.length >= 16 ? ts.slice(11, 16) : '—'
}

/** 单步色调：优先后端 class，其次旧字段兜底（兼容旧后端/本地 dev） */
export function syncStepTone(row: SyncHistoryRow): SyncTone {
  const cls = typeof row.class === 'string' ? row.class : ''
  if (cls === 'ok') return 'ok'
  if (cls === 'self_healed' || cls === 'run_interrupted') return 'muted'
  if (cls === 'awaiting_mirror') return 'warn'
  if (IMPORTANT_CLASSES.has(cls)) return 'err'
  // 旧后端兜底：exit≠0 或 verified=fail → err；warn → warn；其余 ok
  const bad = (row.exit_code ?? 0) !== 0 || row.verified === 'fail'
  if (bad) return 'err'
  if (row.warn) return 'warn'
  return 'ok'
}

/** 单步是否外推（不折叠）：需处理项或需要人看一眼的类 */
export function syncStepImportant(row: SyncHistoryRow): boolean {
  const cls = typeof row.class === 'string' ? row.class : ''
  if (row.needs_action) return true
  if (IMPORTANT_CLASSES.has(cls)) return true
  if (cls && BENIGN_CLASSES.has(cls)) return false
  // 旧后端兜底：exit≠0 / verified=fail / 有 warn → 外推
  return (row.exit_code ?? 0) !== 0 || row.verified === 'fail' || Boolean(row.warn)
}

function stepOf(row: SyncHistoryRow): SyncStep {
  const cls = typeof row.class === 'string' ? row.class : ''
  const tone = syncStepTone(row)
  const mark = tone === 'err' ? '✗' : tone === 'warn' ? '!' : '✓'
  const label = typeof row.label === 'string' && row.label ? row.label : ''
  const hhmm = hhmmOf(row.ts)
  // 外推项带上「为什么」（后端分类中文标签），折叠项只给时间+勾
  const text = syncStepImportant(row) && label ? `${hhmm} ${mark} ${label}` : `${hhmm} ${mark}`
  return { row, hhmm, tone, text, important: syncStepImportant(row), cls }
}

/**
 * 单日同步矩阵汇总（胶囊折叠 + 失败外推）。
 *
 * 折叠规则：只把「成功/已自愈/被打断/等上游」折进 `+N 次成功`（它们都不需要动作），
 * 但**保留最后一次成功的时间**（否则用户看不出"最后一次成功是什么时候"）；
 * 需处理项（verify_failed / data_source_error / not_effective / unknown）恒外推。
 */
export function summarizeSyncRow(day: TimelineDay): SyncRowSummary {
  const rows = Array.isArray(day?.sync) ? day.sync : []
  const steps = rows.map(stepOf)
  const important = steps.filter((s) => s.important)
  const benign = steps.filter((s) => !s.important)
  const oks = benign.filter((s) => s.tone === 'ok')
  const hasErr = steps.some((s) => s.tone === 'err')
  const hasWarn = steps.some((s) => s.tone === 'warn')
  const needsAction = Boolean(day?.needs_action) || important.some((s) => s.row.needs_action)
  const hint =
    (typeof day?.action_hint === 'string' && day.action_hint) ||
    (important[0]?.row?.detail as string) ||
    (important[0]?.row?.reason as string) ||
    ''
  return {
    important,
    foldedCount: benign.length,
    lastOk: oks.length ? oks[oks.length - 1] : null,
    foldedText: benign.length ? `+${benign.length} 次成功` : '',
    needsAction,
    hint,
    tone: hasErr ? 'err' : needsAction || hasWarn ? 'warn' : steps.length ? 'ok' : 'muted',
  }
}

export interface PendingAlert {
  date: string
  count: number
  hint: string
  /** 最近一条待处理记录的原文说明（给横幅第二行） */
  detail: string
}

/**
 * 告警横幅内容：只收「需处理」的日子（needs_action=true），按日期新→旧。
 * 空数组 = 无未决问题 → 横幅整体不渲染（不再有"全部正常"占位）。
 */
export function pendingAlerts(days: TimelineDay[] | null | undefined): PendingAlert[] {
  if (!Array.isArray(days)) return []
  const out: PendingAlert[] = []
  for (const day of days) {
    const summary = summarizeSyncRow(day)
    if (!summary.needsAction) continue
    out.push({
      date: day.date,
      count: typeof day.needs_action_count === 'number' && day.needs_action_count > 0
        ? day.needs_action_count
        : summary.important.length,
      hint: summary.hint,
      detail: summary.important.map((s) => s.text).join(' · '),
    })
  }
  return out
}

/** 等待态提示（今日尚未到同步点）——横幅下方一行弱提示，不占告警位 */
export function awaitingToday(days: TimelineDay[] | null | undefined): TimelineDay | null {
  if (!Array.isArray(days)) return null
  return days.find((d) => d?.awaiting) ?? null
}
