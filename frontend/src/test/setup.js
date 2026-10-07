import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

afterEach(cleanup)

if (!window.matchMedia) {
  window.matchMedia = () => ({
    matches: false, addListener() {}, removeListener() {},
    addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false },
  })
}

// JSDOM does not implement these layout/pointer APIs used by portalled controls.
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}
if (!Element.prototype.hasPointerCapture) Element.prototype.hasPointerCapture = () => false
if (!Element.prototype.setPointerCapture) Element.prototype.setPointerCapture = () => {}
if (!Element.prototype.releasePointerCapture) Element.prototype.releasePointerCapture = () => {}
if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {}
