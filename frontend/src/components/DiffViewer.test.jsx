import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import DiffViewer from './DiffViewer'

it('downloads the displayed patch without fetching a potentially changed artifact', async () => {
  const patch = 'diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+已核对\n'
  const user = userEvent.setup()
  let bytes
  const OriginalBlob = globalThis.Blob
  vi.stubGlobal('Blob', class extends OriginalBlob { constructor(parts, options) { super(parts, options); bytes = parts } })
  const makeUrl = vi.fn(() => 'blob:checked-patch')
  vi.stubGlobal('URL', Object.assign(class extends URL {}, { createObjectURL: makeUrl, revokeObjectURL: vi.fn() }))
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  const clicked = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
    expect(this.download).toBe('patch.diff')
    expect(this.href).toBe('blob:checked-patch')
  })
  try {
    render(<DiffViewer patch={patch} embedded downloadable />)
    await user.click(screen.getByRole('button', { name: '下载补丁' }))
    expect(bytes).toEqual([patch])
    expect(makeUrl).toHaveBeenCalledOnce()
    expect(clicked).toHaveBeenCalledOnce()
    expect(fetch).not.toHaveBeenCalled()
    expect(document.querySelector('a[download="patch.diff"]')).toBeNull()
  } finally {
    // The component revokes the URL after the browser has started the download.
    await new Promise((resolve) => setTimeout(resolve, 1050))
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  }
})
