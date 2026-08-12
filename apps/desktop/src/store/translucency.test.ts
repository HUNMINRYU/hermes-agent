// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from 'vitest'

import {
  $translucency,
  $translucencyMode,
  GLASS_SUPPORTED,
  glassSurfaceKeep,
  setTranslucency,
  setTranslucencyMode
} from './translucency'

// jsdom reports a mac platform in CI and locally alike only when the host is a
// mac; the glass-specific assertions gate on the same flag the store uses so
// the suite is honest on every platform.
describe('translucency store', () => {
  beforeEach(() => {
    setTranslucency(0)
    setTranslucencyMode('clear')
  })

  it('clamps intensity', () => {
    setTranslucency(140)
    expect($translucency.get()).toBe(100)
    setTranslucency(-3)
    expect($translucency.get()).toBe(0)
  })

  it('mirrors intensity and mode to the desktop bridge', () => {
    const calls: Array<{ intensity: number; mode?: string }> = []
    window.hermesDesktop = { setTranslucency: (payload: { intensity: number; mode?: 'clear' | 'glass' }) => calls.push(payload) } as never

    setTranslucency(40)
    expect(calls.at(-1)).toEqual({ intensity: 40, mode: 'clear' })
  })

  it('rejects glass off macOS and applies it on macOS', () => {
    setTranslucency(50)
    setTranslucencyMode('glass')

    if (GLASS_SUPPORTED) {
      expect($translucencyMode.get()).toBe('glass')
      expect(document.documentElement.hasAttribute('data-hermes-glass')).toBe(true)
      expect(document.documentElement.style.getPropertyValue('--translucency-glass-keep')).toBe('65%')
    } else {
      expect($translucencyMode.get()).toBe('clear')
      expect(document.documentElement.hasAttribute('data-hermes-glass')).toBe(false)
    }
  })

  it('removes the glass attribute at zero intensity or back on clear', () => {
    setTranslucency(50)
    setTranslucencyMode('glass')
    setTranslucency(0)
    expect(document.documentElement.hasAttribute('data-hermes-glass')).toBe(false)

    setTranslucency(50)
    setTranslucencyMode('clear')
    expect(document.documentElement.hasAttribute('data-hermes-glass')).toBe(false)
  })
})

describe('glassSurfaceKeep', () => {
  it('mirrors the clear-mode opacity ramp with its 30% floor', () => {
    expect(glassSurfaceKeep(0)).toBe(100)
    expect(glassSurfaceKeep(50)).toBe(65)
    expect(glassSurfaceKeep(100)).toBeCloseTo(30)
  })
})
