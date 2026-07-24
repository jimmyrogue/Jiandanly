import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import { PluginsHub, type PluginsHubTab } from './PluginsHub'

function Harness() {
  const [tab, setTab] = useState<PluginsHubTab>('plugins')
  return (
    <I18nProvider>
      <PluginsHub activeTab={tab} onTabChange={setTab}>
        <span>{`panel:${tab}`}</span>
      </PluginsHub>
    </I18nProvider>
  )
}

describe('PluginsHub', () => {
  afterEach(cleanup)

  it('switches between the three capability tabs', () => {
    render(<Harness />)

    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((tab) => tab.textContent)).toEqual(['插件', 'Skill', 'MCP'])
    expect(screen.getByRole('tab', { name: '插件' })).toHaveAttribute('aria-selected', 'true')

    fireEvent.click(screen.getByRole('tab', { name: 'Skill' }))
    expect(screen.getByRole('tab', { name: 'Skill' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel')).toHaveTextContent('panel:skills')
  })

  it('supports arrow and boundary-key navigation', () => {
    render(<Harness />)

    const plugins = screen.getByRole('tab', { name: '插件' })
    plugins.focus()
    fireEvent.keyDown(plugins, { key: 'ArrowRight' })
    expect(screen.getByRole('tab', { name: 'Skill' })).toHaveFocus()
    expect(screen.getByRole('tab', { name: 'Skill' })).toHaveAttribute('aria-selected', 'true')

    fireEvent.keyDown(screen.getByRole('tab', { name: 'Skill' }), { key: 'Home' })
    expect(screen.getByRole('tab', { name: '插件' })).toHaveFocus()
  })
})
