import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AssetsCard from './AssetsCard.vue'

describe('AssetsCard 分析数仓监控展示', () => {
  it('按数据类型清晰展示范围、持久化对账与最近导入增量', async () => {
    const wrapper = mount(AssetsCard, {
      props: {
        warehouse: { available: true, watermark_daily: '20261112' },
        totals: { sediment_days: 243, weeks: 50, months: 11, backups: { count: 14, last_mtime: 0 } },
        assets: {
          backups: { warehouse: { count: 14, last_mtime: 0 }, research: { count: 0, last_mtime: 0 } },
          disk: { groups: { warehouse: 192_000_000 } },
          warehouse_inventory: {
            ok: true,
            datasets: [],
            highlights: [
              {
                key: 'std:daily',
                label: '日线',
                range_text: '2025-11-12 ~ 2026-11-12',
                units_text: '243 个交易日',
                rows: 1_256_705,
                bytes: 190_000_000,
                source: '标准数仓',
                status: 'ready',
              },
              {
                key: 'ldb:mydb:khk日k',
                label: '港股日线',
                range_text: '2024-11-12 ~ 2026-11-12',
                units_text: 'mydb · 701 行',
                rows: 701,
                bytes: 90_000,
                source: 'mydb',
                status: 'verified',
              },
            ],
            index_rows: 1522,
            jobs: [],
            active_jobs: [],
            changes: [],
            latest_change: {
              event_id: 'ev1',
              recorded_at: '2026-11-12T13:47:21Z',
              trigger_kind: 'bulk_complete',
              total_rows: 1_259_049,
              total_bytes: 192_000_000,
              delta_rows: 2344,
              delta_bytes: 210_000,
              summary_text: '新增 港股日线(701)',
            },
            reconcile: {
              daily: { date: '20261112', ok: true, traded: 5196, stored: 5196 },
              bulk: { job_count: 1, verified_count: 1, active_count: 0 },
            },
          },
        },
      },
    })

    const text = wrapper.text()
    expect(text).toContain('日线')
    expect(text).toContain('2025-11-12 ~ 2026-11-12')
    expect(text).toContain('港股日线')
    expect(text).toContain('2024-11-12 ~ 2026-11-12')
    expect(text).toContain('索引 1,522 行')
    expect(text).toContain('最近 +2,344 行')
    expect(text).toContain('日频通过（5196 只） · 导入已核验 1 批任务')

    await wrapper.find('.ac-more-btn').trigger('click')
    expect(wrapper.emitted('openWarehouse')).toBeTruthy()
  })

  it('导入中优先展示实时导入任务进度且空载荷不崩', () => {
    const emptyWrapper = mount(AssetsCard)
    expect(emptyWrapper.text()).toContain('未监控')

    const activeWrapper = mount(AssetsCard, {
      props: {
        warehouse: { available: true, watermark_daily: '20261112' },
        assets: {
          warehouse_inventory: {
            ok: true,
            datasets: [],
            highlights: [],
            jobs: [],
            active_jobs: [
              {
                snapshot_id: 'snap_2y',
                source_db: 'data',
                status: 'uploading',
                committed_batches: 12,
                committed_rows: 600_000,
                committed_bytes: 50_000_000,
              },
            ],
            changes: [],
          },
        },
      },
    })
    expect(activeWrapper.text()).toContain('导入中（data 12 批）')
    expect(activeWrapper.text()).toContain('导入中 data（600,000 行）')
  })
})
