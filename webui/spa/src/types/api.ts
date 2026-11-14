// types/api.ts — 后端 HTTP 契约类型（0.10.27「从零四件套」之二：TypeScript 化）。
//
// 手写镜像后端载荷（snapshot_payload 等）。守约方式：
//   - 后端 test_ops.SnapshotPayloadTest 断言 snapshot 键集；
//   - 本文件守前端消费面：后端加字段不破前端（TS 可选），改/删字段必须同步这里。
// 原则：只对「前端真正消费的字段」写严，其余用索引签名放行（后端字段远多于消费面，
// 全量镜像只会制造维护税）。数据块内已消费字段全部可空化——后端瞬时失败时载荷
// 子块为 null（views-null-safety 防线的前提）。

// ---------- 基础块 ----------

export interface DiskUsage {
  total_gb?: number
  used_gb?: number
  free_gb?: number
  [k: string]: unknown
}

export interface SyncTrigger {
  t?: string
  exit?: number
  [k: string]: unknown
}

export interface ScheduleInfo {
  enabled?: boolean
  times?: string[]
  trading_only?: boolean
  retry_pending?: string | null
  stale_retry_pending?: string | null
  next_trigger?: string | null
  last_trigger?: SyncTrigger | null
  fired?: Record<string, unknown>
  [k: string]: unknown
}

export interface WarehouseStatus {
  available?: boolean
  watermark_daily?: string | null
  last_result?: { ok?: boolean; [k: string]: unknown } | null
  [k: string]: unknown
}

export interface StatusPayload {
  container?: { ok?: boolean; status?: string; note?: string; image?: string | null; started?: number | null } | null
  source?: string
  sync_running?: boolean
  sync_phase?: string
  sync_started?: number | null // epoch 秒（SyncStatusCard 已运行时长）
  exit_code?: number | null
  data_latest?: string | null
  code_stats?: { stock?: number; etf?: number; other?: number; hk?: number; latency_ms?: number } | null
  coverage?: { earliest?: string; latest?: string } | null
  sync_cap?: { ok?: boolean; checks?: Record<string, { ok?: boolean; warn?: boolean; detail?: string }> } | null
  mirror?: string | null
  webui?: { version: string; started: string; heartbeat: string }
  data_dir?: string
  last_sync?: Record<string, unknown> | null
  schedule?: ScheduleInfo
  calendar?: { through?: string; days?: number }
  disk?: DiskUsage
  scheduler_alive?: boolean
  trading_today?: boolean
}

export interface HealthStatus {
  latest?: string | null
  lag_days?: number | null
  status?: string // ok | stale | unknown（use-health 状态卡）
  note?: string
  mirror?: string | null
  [k: string]: unknown
}

// 同步历史行（/api/history 与 TimelineDay.sync 共用）
export interface SyncHistoryRow {
  ts?: string
  trigger?: string
  exit_code?: number | null
  verified?: string | null
  duration_sec?: number | null
  data_latest?: string | null
  warn?: string | null // 后端原文（0.10.38 起为字符串：exit=0 但"未生效"的说明）
  downloads?: number | null
  // 0.10.38 分类字段（后端 sync_failure_class 输出；旧后端缺省 → 前端按 tone() 兜底）
  reason?: string | null
  class?: string | null
  label?: string | null
  needs_action?: boolean
  detail?: string | null
  [k: string]: unknown
}

export interface AlertItem {
  ts?: string
  source?: string
  level?: string
  msg?: string
  [k: string]: unknown
}

export interface VersionPayload {
  webui?: { version: string }
  image?: { tag: string | null }
  upstream?: { tag_name?: string; [k: string]: unknown } | null
  stale?: boolean
  msg?: string
  [k: string]: unknown
}

export interface OverviewPayload {
  generated_at?: string
  health?: HealthStatus | null
  alerts?: {
    count: number
    recent: AlertItem[]
    // 0.10.38 静音状态：只影响提醒强度，count 恒定（事实不隐藏）
    muted?: boolean
    mute_until?: number | null
    mute_preset?: string | null
  } | null
  mcp?: Record<string, unknown> | null
  version?: VersionPayload | null
}

// ---------- 时间线 / 资产（/api/timeline、snapshot.timeline） ----------

export interface TimelineDay {
  date: string
  sediment?: { rows?: number | null; ok?: boolean } | null
  // 后端 load_timeline 恒写入 sync（[] 兜底）与 alerts（0 计数兜底）——两块必填
  sync: SyncHistoryRow[]
  backups?: { count: number; last: string } | null
  alerts: { count: number; err: number; warn: number }
  // 0.10.38 日级汇总（后端恒写；旧后端缺省 → 前端按行内分类兜底）
  needs_action?: boolean
  needs_action_count?: number
  awaiting?: boolean
  action_hint?: string | null
}

export interface WarehouseTotals {
  sediment_days: number
  weeks: number
  months: number
  backups: { count: number; last_mtime: number }
}

// ---------- 资产卡真身（snapshot.assets，0.10.38） ----------
// 原则：每个字段都有真实来源；后端取不到时给 null/0，前端显示「未监控」而不是编数字。

export interface ResearchDbStats {
  available?: boolean
  mode?: string // sqlite | mydb（RESEARCH_STORE 回滚）
  path?: string | null
  bytes?: number | null
  metrics?: number
  series?: number
  lists?: number
  snapshots?: number
}

export interface BackupFamily {
  count?: number
  last_mtime?: number | null
  bytes?: number
}

export interface BackupStats {
  warehouse?: BackupFamily
  research?: BackupFamily
  total_bytes?: number
}

export interface DiskDetail {
  groups?: Record<string, number> // market_data / warehouse / research_db / mydb
  total_bytes?: number
  volume?: DiskUsage | null
}

export interface WarehouseDatasetItem {
  key: string
  type_key: string
  label: string
  layer: 'standard' | 'leveldb' | string
  layer_label?: string
  source: string
  is_index: boolean
  date_min: string | null
  date_max: string | null
  range_text: string
  periods?: number | null
  units_text?: string | null
  rows: number
  codes?: number | null
  bytes: number
  status: string
}

export interface WarehouseHighlightItem {
  key: string
  label: string
  range_text: string
  units_text?: string | null
  rows: number
  bytes: number
  source: string
  status: string
}

export interface WarehouseChangeEntry {
  event_id: string
  recorded_at: string
  trigger_kind: string
  snapshot_id?: string | null
  source_db?: string | null
  total_rows: number
  total_bytes: number
  delta_rows: number
  delta_bytes: number
  summary_text: string
}

export interface BulkImportJobSummary {
  snapshot_id: string
  source_db: string
  status: string
  expected_batches?: number | null
  committed_batches: number
  committed_rows: number
  committed_bytes: number
  created_at?: string
  updated_at?: string
  completed_at?: string | null
  verified_at?: string | null
  verify_summary?: Record<string, unknown> | null
}

export interface WarehouseInventory {
  ok: boolean
  available?: boolean
  generated_at?: string
  datasets: WarehouseDatasetItem[]
  highlights: WarehouseHighlightItem[]
  index_rows?: number
  jobs: BulkImportJobSummary[]
  active_jobs: BulkImportJobSummary[]
  changes: WarehouseChangeEntry[]
  latest_change?: WarehouseChangeEntry | null
  reconcile?: {
    daily?: { date?: string; ok?: boolean; traded?: number; stored?: number; recorded_at?: string } | null
    bulk?: {
      job_count: number
      verified_count: number
      active_count: number
      latest_job?: BulkImportJobSummary | null
    } | null
  } | null
  totals?: {
    total_rows: number
    standard_rows: number
    leveldb_rows: number
    standard_bytes: number
    leveldb_disk_bytes: number
    leveldb_raw_bytes: number
    duckdb_bytes: number
    total_bytes: number
  } | null
}

export interface AssetsPayload {
  research?: ResearchDbStats | null
  backups?: BackupStats | null
  disk?: DiskDetail | null
  warehouse_inventory?: WarehouseInventory | null
  generated_at?: string
}

// ---------- 聚合快照（GET /api/snapshot，0.10.27 四件套之一） ----------

export interface Snapshot {
  generated_at: string
  overview: OverviewPayload | null
  status: StatusPayload | null
  schedule: ScheduleInfo | null
  warehouse: WarehouseStatus | null
  timeline: { days: TimelineDay[]; totals: WarehouseTotals | null }
  assets?: AssetsPayload | null
}
