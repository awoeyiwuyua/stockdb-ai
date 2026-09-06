// use-cockpit.ts — 驾驶舱接线层（W1 单页驾驶舱，docs/design/webui-cockpit-redesign.md）。
//
// 0.10.27 起本文件只做"store → 灯推导"的接线：取数统一走全局 store（App 层轮询
// /api/snapshot 单通道），判定口径全部在 domain/lights.ts 纯函数里（Vitest 独打）。
// 页面级轮询节拍仍由视图层 usePolling 驱动（tick = store.refresh()，在途去重兜底）。
import { computed } from 'vue'
import { useGlobalStore } from '../stores/global'
import { deriveLights, worstTone, aggregateWord, hasWhError24h } from '../domain/lights'

export function useCockpit() {
  const store = useGlobalStore()

  // 24h 内的 warehouse error 告警 → 仓库红灯（口径与阈值在 domain/lights）
  const whError24h = computed(() => hasWhError24h(store.alertsRecent))

  const lights = computed(() => deriveLights({
    lagDays: store.lagDays,
    healthLatest: store.health?.latest ?? '',
    healthNote: store.health?.note ?? '',
    schedule: store.schedule,
    warehouse: store.warehouse,
    disk: store.status?.disk,
    whError24h: whError24h.value,
  }))
  const worst = computed(() => worstTone(lights.value.map((l) => l.tone)))
  const aggWord = computed(() => aggregateWord(lights.value))

  return { lights, worst, aggWord }
}
