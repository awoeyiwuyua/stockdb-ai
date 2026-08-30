// use-polling.test.js — 可见性感知轮询 composable 单测（0.10.18）。
// 验证：立即首拉 / 节拍切换（可见快拍、后台慢拍）/ 回前台补拉 / 卸载清理。
// happy-dom 下 document.hidden 可直接写；定时器用 vi.useFakeTimers 推进。
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { defineComponent, h } from 'vue'
import { mount } from '@vue/test-utils'
import { usePolling } from './use-polling.js'

function mountWith(fn, opts) {
  let unmounted = false
  const comp = defineComponent({
    setup() {
      usePolling(fn, opts)
      return () => h('div')
    },
    unmounted() { unmounted = true },
  })
  const wrapper = mount(comp)
  return { wrapper, isUnmounted: () => unmounted }
}

describe('usePolling 可见性感知轮询', () => {
  let hiddenSpy
  const setHidden = (v) => { hiddenSpy.mockReturnValue(v) }

  beforeEach(() => {
    vi.useFakeTimers()
    // happy-dom 的 document.hidden 只有 getter：spy 掉 mock 可见性
    hiddenSpy = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
  })
  afterEach(() => {
    hiddenSpy.mockRestore()
    vi.useRealTimers()
  })

  it('immediate=true 挂载即拉一次；否则不拉', () => {
    const fn = vi.fn()
    const { wrapper } = mountWith(fn, { immediate: true })
    expect(fn).toHaveBeenCalledTimes(1)
    wrapper.unmount()
    const fn2 = vi.fn()
    const w2 = mountWith(fn2, {})
    expect(fn2).toHaveBeenCalledTimes(0)
    w2.wrapper.unmount()
  })

  it('可见时按 fast 节拍（默认 30s）', () => {
    const fn = vi.fn()
    const { wrapper } = mountWith(fn)
    const calls = fn.mock.calls.length
    vi.advanceTimersByTime(30_000)
    expect(fn.mock.calls.length).toBe(calls + 1)
    vi.advanceTimersByTime(30_000)
    expect(fn.mock.calls.length).toBe(calls + 2)
    wrapper.unmount()
  })

  it('切后台放宽到 slow；回前台立即补拉并恢复 fast', () => {
    const fn = vi.fn()
    const { wrapper } = mountWith(fn, { fast: 30_000, slow: 300_000 })
    const base = fn.mock.calls.length
    setHidden(true)
    document.dispatchEvent(new Event('visibilitychange'))
    const afterHidden = fn.mock.calls.length // 后台不补拉
    vi.advanceTimersByTime(60_000)
    expect(fn.mock.calls.length).toBe(afterHidden) // 60s 内不触发（慢拍 5min）
    setHidden(false)
    document.dispatchEvent(new Event('visibilitychange'))
    expect(fn.mock.calls.length).toBe(afterHidden + 1) // 回前台立即补拉
    vi.advanceTimersByTime(30_000)
    expect(fn.mock.calls.length).toBe(afterHidden + 2) // 恢复快拍
    wrapper.unmount()
  })

  it('卸载后清理：不再触发、移除监听', () => {
    const removeSpy = vi.spyOn(document, 'removeEventListener')
    const fn = vi.fn()
    const { wrapper, isUnmounted } = mountWith(fn, { immediate: true })
    wrapper.unmount()
    expect(isUnmounted()).toBe(true)
    expect(removeSpy).toHaveBeenCalledWith('visibilitychange', expect.any(Function))
    const calls = fn.mock.calls.length
    vi.advanceTimersByTime(120_000)
    expect(fn.mock.calls.length).toBe(calls) // 无新触发
  })
})
