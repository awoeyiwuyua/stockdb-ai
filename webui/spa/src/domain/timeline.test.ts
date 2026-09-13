// domain/timeline.test.ts — 0.10.38 改版核心语义的离线护栏
//
// 覆盖（用 NAS 真身形态）：
//   - 09-11：超时失败(自愈) + 2 条"等上游" + 成功 ⇒ 全折进折叠，行不外推
//   - 09-07：pass + pass + 被打断 ⇒ 折叠 3 条，needs_action=false（改版起因）
//   - 真失败（无自愈）⇒ 外推 + needsAction=true
//   - 旧后端形态（无 class 字段）⇒ 按 exit/verified/warn 兜底
import { describe, expect, it } from 'vitest'
import {
  awaitingToday,
  pendingAlerts,
  summarizeSyncRow,
  syncStepImportant,
  syncStepTone,
} from './timeline'
import type { TimelineDay } from '../types/api'

function day(partial: Partial<TimelineDay>): TimelineDay {
  return {
    date: '20260911',
    sediment: null,
    sync: [],
    backups: null,
    alerts: { count: 0, err: 0, warn: 0 },
    ...partial,
  }
}

describe('syncStepTone / syncStepImportant', () => {
  it('优先后端 class 判定色调', () => {
    expect(syncStepTone({ class: 'ok' })).toBe('ok')
    expect(syncStepTone({ class: 'self_healed' })).toBe('muted')
    expect(syncStepTone({ class: 'run_interrupted' })).toBe('muted')
    expect(syncStepTone({ class: 'awaiting_mirror' })).toBe('warn')
    expect(syncStepTone({ class: 'verify_failed' })).toBe('err')
    expect(syncStepTone({ class: 'data_source_error' })).toBe('err')
  })

  it('无 class（旧后端）按 exit/verified/warn 兜底', () => {
    expect(syncStepTone({ exit_code: 1 })).toBe('err')
    expect(syncStepTone({ verified: 'fail' })).toBe('err')
    expect(syncStepTone({ warn: '同步未生效：下载 0 文件' })).toBe('warn')
    expect(syncStepTone({ exit_code: 0, verified: 'pass' })).toBe('ok')
    expect(syncStepImportant({ exit_code: 0, verified: 'pass' })).toBe(false)
    expect(syncStepImportant({ exit_code: 1 })).toBe(true)
  })

  it('needs_action 强制外推', () => {
    expect(syncStepImportant({ class: 'ok', needs_action: true })).toBe(true)
  })
})

describe('summarizeSyncRow — 09-11 真身（超时自愈 + 等上游 + 成功）', () => {
  const d = day({
    date: '20260911',
    needs_action: false,
    action_hint: null,
    sync: [
      { ts: '2026-09-11 16:17:51', trigger: 'scheduled', exit_code: 0, verified: 'fail',
        duration_sec: 1660.7, data_latest: null, class: 'self_healed', label: '已自愈',
        needs_action: false, detail: '数据完整性验证未通过，后续重试已成功' },
      { ts: '2026-09-11 16:48:18', trigger: 'scheduled-stale-retry', exit_code: 0,
        verified: 'pass', duration_sec: 7.4, data_latest: '20260807',
        class: 'awaiting_mirror', label: '等上游发布', needs_action: false },
      { ts: '2026-09-11 17:18:47', trigger: 'scheduled-stale-retry', exit_code: 0,
        verified: 'pass', duration_sec: 6.8, data_latest: '20260807',
        class: 'awaiting_mirror', label: '等上游发布', needs_action: false },
      { ts: '2026-09-11 17:50:38', trigger: 'scheduled-stale-retry', exit_code: 0,
        verified: 'pass', duration_sec: 87.6, data_latest: '20260911',
        class: 'ok', label: '正常', needs_action: false },
    ],
  })

  it('全部折进 +N 次成功，不产生外推胶囊', () => {
    const s = summarizeSyncRow(d)
    expect(s.important).toHaveLength(0)
    expect(s.foldedCount).toBe(4)
    expect(s.foldedText).toBe('+4 次成功')
    // 行整体色调 warn：当日含「等上游」的迟到过程（值得知道，但不是 err）
    expect(s.tone).toBe('warn')
  })

  it('「等上游」不是 err 也不进外推（只是迟到说明）', () => {
    const s = summarizeSyncRow(d)
    expect(s.important.some((x) => x.tone === 'err')).toBe(false)
    expect(s.tone).not.toBe('err')
  })

  it('保留最后一次成功的时间（折叠态也能看到"最后一次成功"）', () => {
    const s = summarizeSyncRow(d)
    expect(s.lastOk?.hhmm).toBe('17:50')
  })

  it('不自造未决问题（needs_action=false）', () => {
    const s = summarizeSyncRow(d)
    expect(s.needsAction).toBe(false)
    expect(pendingAlerts([d])).toHaveLength(0)
  })
})

describe('summarizeSyncRow — 09-07 真身（被打断的手动重试）', () => {
  const d = day({
    date: '20260907',
    needs_action: false,
    action_hint: null,
    sync: [
      { ts: '2026-09-07 15:50:34', trigger: 'scheduled', exit_code: 0, verified: 'skipped',
        duration_sec: 11.7, data_latest: '20260904', class: 'awaiting_mirror',
        label: '等上游发布', needs_action: false },
      { ts: '2026-09-07 21:54:20', trigger: 'manual', exit_code: 0, verified: 'pass',
        duration_sec: 6.0, data_latest: '20260904', class: 'ok', label: '正常',
        needs_action: false },
      { ts: '2026-09-07 21:58:37', trigger: 'manual', exit_code: 0, verified: 'fail',
        duration_sec: 6.4, data_latest: null, class: 'run_interrupted', label: '运行被打断',
        needs_action: false, detail: '手动运行 6.4s 后失败（容器重启/部署打断）；当日已有成功运行' },
    ],
  })

  it('被打断不被当成需处理（改版起因）', () => {
    const s = summarizeSyncRow(d)
    expect(s.important).toHaveLength(0)
    expect(s.foldedCount).toBe(3)
    expect(s.needsAction).toBe(false)
    expect(pendingAlerts([d])).toHaveLength(0)
  })
})

describe('summarizeSyncRow — 真失败外推', () => {
  const d = day({
    date: '20260908',
    needs_action: true,
    needs_action_count: 1,
    action_hint: '数据完整性验证未通过（数据未前进）',
    sync: [
      { ts: '2026-09-08 15:54:22', trigger: 'scheduled', exit_code: 0, verified: 'fail',
        duration_sec: 262.4, data_latest: null, class: 'verify_failed', label: '数据未更新',
        needs_action: true, detail: '数据完整性验证未通过（数据未前进）' },
    ],
  })

  it('失败外推且带中文标签（不折进 "+N 次成功"）', () => {
    const s = summarizeSyncRow(d)
    expect(s.important).toHaveLength(1)
    expect(s.important[0].tone).toBe('err')
    expect(s.important[0].text).toContain('✗')
    expect(s.important[0].text).toContain('数据未更新')
    expect(s.foldedText).toBe('')
    expect(s.needsAction).toBe(true)
    expect(s.tone).toBe('err')
  })

  it('横幅呈现该日与提示', () => {
    const alerts = pendingAlerts([d])
    expect(alerts).toHaveLength(1)
    expect(alerts[0].date).toBe('20260908')
    expect(alerts[0].hint).toContain('验证未通过')
  })
})

describe('pendingAlerts / awaitingToday 边界', () => {
  it('空输入不抛且返回空', () => {
    expect(pendingAlerts(null)).toEqual([])
    expect(pendingAlerts(undefined)).toEqual([])
    expect(pendingAlerts([])).toEqual([])
    expect(awaitingToday(null)).toBeNull()
  })

  it('多条需处理日按输入顺序（新→旧）返回', () => {
    const days = [
      day({ date: '20260911', needs_action: true, sync: [{ class: 'verify_failed', needs_action: true }] }),
      day({ date: '20260910', needs_action: false, sync: [{ class: 'ok' }] }),
      day({ date: '20260909', needs_action: true, sync: [{ class: 'data_source_error', needs_action: true }] }),
    ]
    const out = pendingAlerts(days)
    expect(out.map((a) => a.date)).toEqual(['20260911', '20260909'])
  })

  it('行内 needs_action 也可触发（后端日级字段缺失时的兜底）', () => {
    const d = day({ date: '20260911', sync: [{ class: 'verify_failed', needs_action: true }] })
    expect(pendingAlerts([d])).toHaveLength(1)
  })

  it('等待态单独可查（不占告警位）', () => {
    const d = day({ date: '20260914', awaiting: true, action_hint: '等待 15:50 定时同步' })
    expect(awaitingToday([d])?.date).toBe('20260914')
    expect(pendingAlerts([d])).toHaveLength(0)
  })
})
