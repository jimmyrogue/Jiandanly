import { cleanup, render } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { afterEach, describe, expect, it } from 'vitest'
import { AlertDialog, AlertDialogContent, AlertDialogTitle } from './alert-dialog'
import { Button } from './button'
import { Dialog, DialogContent, DialogTitle } from './dialog'
import { Sheet, SheetContent, SheetTitle } from './sheet'
import { Switch } from './switch'

afterEach(cleanup)

describe('shared motion language', () => {
  it('routes modal surfaces and controls through the semantic motion classes', () => {
    render(
      <>
        <Dialog open>
          <DialogContent>
            <DialogTitle>普通弹窗</DialogTitle>
          </DialogContent>
        </Dialog>
        <AlertDialog open>
          <AlertDialogContent>
            <AlertDialogTitle>确认弹窗</AlertDialogTitle>
          </AlertDialogContent>
        </AlertDialog>
        <Sheet open>
          <SheetContent>
            <SheetTitle>侧边预览</SheetTitle>
          </SheetContent>
        </Sheet>
        <Button>操作</Button>
        <Switch aria-label="切换" />
      </>,
    )

    expect(document.querySelector('[data-slot="dialog-overlay"]')).toHaveClass('sj-motion-overlay')
    expect(document.querySelector('[data-slot="dialog-content"]')).toHaveClass('sj-motion-dialog')
    expect(document.querySelector('[data-slot="alert-dialog-overlay"]')).toHaveClass('sj-motion-overlay')
    expect(document.querySelector('[data-slot="alert-dialog-content"]')).toHaveClass('sj-motion-dialog')
    expect(document.querySelector('[data-slot="sheet-overlay"]')).toHaveClass('sj-motion-overlay')
    expect(document.querySelector('[data-slot="sheet-content"]')).toHaveClass('sj-motion-sheet')
    const action = document.querySelector('[data-slot="button"]')
    const toggle = document.querySelector('[data-slot="switch"]')
    expect(action).toHaveClass('sj-motion-action')
    expect(action?.className).not.toContain('translate-y-px')
    expect(toggle).toHaveClass('sj-motion-toggle')
  })

  it('keeps the motion system behind the reduced-motion preference', () => {
    const css = readFileSync('src/styles.css', 'utf8')
    const positionedKeyframes = css.slice(
      css.indexOf('@keyframes sj-motion-dialog-in'),
      css.indexOf(".sj-motion-overlay[data-state='open']"),
    )

    expect(css).toContain('.sj-motion-dialog')
    expect(css).toContain('.sj-motion-popover')
    expect(css).toContain('@media (prefers-reduced-motion: reduce)')
    expect(positionedKeyframes).toContain('transform:')
    expect(positionedKeyframes).not.toMatch(/^\s*translate:/m)
  })
})
