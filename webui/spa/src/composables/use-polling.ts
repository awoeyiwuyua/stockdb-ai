// use-polling.ts — 可见性感知轮询（composables 层，0.10.18 从 App.vue 提取）。
//
// 设计逻辑（哲学 #2 复杂度定向转移 / #3 依赖方向）：
//   「可见时 30s 快轮询、切后台 5min 慢轮询、回前台立即补拉一次」这套节拍
//   原先只写在 App.vue（全局 store 刷新用），OpsSync 等页面各自裸写无降频的
//   setInterval——同一模式两种实现。提取后：节拍策略全局一处，页面/壳只声明
//   「轮询什么」，不再持有定时器生命周期。
//
// 用法：
//   usePolling(() => { loadA(); loadB() })                 // 默认 30s / 5min
//   usePolling(fn, { immediate: true, fast: 15_000 })      // 挂载即拉 + 自定快拍
//
// 生命周期安全：onMounted/onUnmounted 内部处理（必须在 setup 同步上下文调用）。
import { onMounted, onUnmounted } from 'vue'

const DEFAULT_FAST = 30_000   // 标签页可见：30 秒一次（与全局约定一致）
const DEFAULT_SLOW = 300_000  // 切到后台：放宽到 5 分钟（省请求）

export interface PollingOptions {
  immediate?: boolean
  fast?: number
  slow?: number
}

export function usePolling(tick: () => void, { immediate = false, fast = DEFAULT_FAST, slow = DEFAULT_SLOW }: PollingOptions = {}) {
  let timer: ReturnType<typeof setInterval> | null = null
  let stopped = false

  function start() {
    if (stopped || timer) return
    timer = setInterval(tick, document.hidden ? slow : fast)
  }
  function stop() {
    if (timer) clearInterval(timer)
    timer = null
  }
  // 换间隔前必须先清旧定时器，否则会叠加出多个（App.vue 原注释同样提醒）
  function respeed() {
    stop()
    start()
  }

  function pageVisible() {
    return !document.hidden
  }

  function onVisibilityChange() {
    if (stopped) return
    if (pageVisible()) {
      tick() // 回前台：立即补拉一次，再恢复快拍
      respeed()
    } else {
      respeed() // 切后台：只放宽间隔
    }
  }

  onMounted(() => {
    stopped = false
    if (immediate) tick()
    start()
    document.addEventListener('visibilitychange', onVisibilityChange)
  })
  onUnmounted(() => {
    stopped = true
    stop()
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })
}
