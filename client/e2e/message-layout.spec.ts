import { expect, test } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'

const styles = fs.readFileSync(path.resolve(import.meta.dirname, '../src/styles.css'), 'utf8')

test('assistant actions do not overlap the next message', async ({ page }) => {
  await page.setContent(`
    <style>${styles}</style>
    <main class="messages" style="width: 900px; height: 600px">
      <article class="message assistant">
        <div class="message-bubble-inner">
          <div class="message-content"><p>上一条回答</p></div>
        </div>
        <div class="message-meta" style="opacity: 1">
          <button class="message-meta-action">复制</button>
          <button class="message-meta-action">重试</button>
          <button class="message-meta-action">删除</button>
          <span class="message-meta-usage">15,804 个 token</span>
          <span class="message-meta-dot">·</span>
          <span class="message-meta-time">2 小时前</span>
        </div>
      </article>
      <article class="message user">
        <div class="message-bubble-inner"><div class="message-content">下一条问题</div></div>
      </article>
    </main>
  `)

  const actions = await page.locator('.message-meta').boundingBox()
  const nextMessage = await page.locator('.message.user').boundingBox()

  expect(actions).not.toBeNull()
  expect(nextMessage).not.toBeNull()
  expect(actions!.y + actions!.height).toBeLessThanOrEqual(nextMessage!.y)
})

test('subtask card keeps compact bottom and following progress spacing', async ({ page }) => {
  await page.setContent(`
    <style>${styles}</style>
    <main class="messages" style="width: 900px; height: 600px">
      <article class="message assistant">
        <div class="message-bubble-inner">
          <div class="message-content"><p>3 个子 agent 已并行启动</p></div>
          <div class="agent-progress-stages mt-4">
            <div class="tool-card agent-progress agent-progress-stage agent-progress-working agent-progress-tool-card">
              <div class="tool-card-header agent-progress-summary agent-progress-summary-static">
                <span class="name">派发子任务</span>
                <span class="agent-progress-target">3 个子任务进行中</span>
              </div>
              <ul class="agent-progress-tasks">
                <li class="agent-progress-task-item">子任务 1</li>
                <li class="agent-progress-task-item">子任务 2</li>
                <li class="agent-progress-task-item">子任务 3</li>
              </ul>
            </div>
          </div>
        </div>
        <div class="message-meta" style="opacity: 1"><span>1 分钟前</span></div>
      </article>
      <div class="thinking-indicator"><span>正在思考...</span></div>
    </main>
  `)

  const card = await page.locator('.agent-progress').boundingBox()
  const lastTask = await page.locator('.agent-progress-task-item').last().boundingBox()
  const meta = await page.locator('.message-meta').boundingBox()
  const thinking = await page.locator('.thinking-indicator').boundingBox()

  expect(card).not.toBeNull()
  expect(lastTask).not.toBeNull()
  expect(meta).not.toBeNull()
  expect(thinking).not.toBeNull()
  expect.soft(card!.y + card!.height - (lastTask!.y + lastTask!.height)).toBeGreaterThanOrEqual(6)
  expect.soft(thinking!.y - (meta!.y + meta!.height)).toBeLessThanOrEqual(20)
})

test('model and thinking selectors stay independent and leave room for descenders', async ({ page }) => {
  await page.setContent(`
    <style>${styles}</style>
    <div class="composer-toolbar">
      <button class="composer-mode-trigger">
        <span class="composer-mode-trigger-label">gpt-5.6-luna</span>
      </button>
      <button class="composer-mode-trigger composer-reasoning-trigger">
        <svg width="14" height="14"></svg>
        <span class="composer-mode-trigger-label">极高</span>
        <svg class="composer-mode-trigger-chevron" width="12" height="12"></svg>
      </button>
    </div>
  `)

  const metrics = await page.locator('.composer-mode-trigger-label').first().evaluate((element) => {
    const style = getComputedStyle(element)
    return {
      fontSize: Number.parseFloat(style.fontSize),
      lineHeight: Number.parseFloat(style.lineHeight),
      overflowY: style.overflowY,
    }
  })
  const model = await page.locator('.composer-mode-trigger').first().boundingBox()
  const thinking = await page.locator('.composer-reasoning-trigger').boundingBox()

  expect(metrics.overflowY === 'visible' || metrics.lineHeight > metrics.fontSize).toBe(true)
  expect(model).not.toBeNull()
  expect(thinking).not.toBeNull()
  expect(thinking!.x).toBeGreaterThanOrEqual(model!.x + model!.width)
  expect(thinking!.width).toBeLessThanOrEqual(76)
})

test('thinking menu reuses the compact model-menu surface', async ({ page }) => {
  await page.setContent(`
    <style>${styles}</style>
    <div class="composer-mode-menu model-menu">模型</div>
    <div class="composer-mode-menu composer-reasoning-menu">
      <div class="composer-mode-item composer-mode-model-item composer-reasoning-item is-active">快速</div>
      <div class="composer-mode-item composer-mode-model-item composer-reasoning-item">高</div>
      <div class="composer-mode-item composer-mode-model-item composer-reasoning-item">极高</div>
    </div>
  `)

  const modelStyle = await page.locator('.model-menu').evaluate((element) => {
    const style = getComputedStyle(element)
    return { borderRadius: style.borderRadius, boxShadow: style.boxShadow }
  })
  const menu = page.locator('.composer-reasoning-menu')
  const menuStyle = await menu.evaluate((element) => {
    const style = getComputedStyle(element)
    return { borderRadius: style.borderRadius, boxShadow: style.boxShadow }
  })
  const menuBox = await menu.boundingBox()
  const itemBox = await menu.locator('.composer-reasoning-item').first().boundingBox()

  expect(menuBox).not.toBeNull()
  expect(itemBox).not.toBeNull()
  expect(menuBox!.width).toBe(128)
  expect(itemBox!.height).toBeLessThanOrEqual(28)
  expect(menuStyle.borderRadius).toBe(modelStyle.borderRadius)
  expect(menuStyle.boxShadow).toBe(modelStyle.boxShadow)
})

test('model capability settings follow a vertical hierarchy', async ({ page }) => {
  await page.setContent(`
    <style>${styles}</style>
    <div class="settings-model-picker-row selected" style="width: 600px">
      <div class="settings-model-picker-config">
        <div class="settings-model-picker-field primary-field">
          <span>用途</span>
          <button data-slot="select-trigger">Agent 对话</button>
        </div>
        <details class="settings-model-picker-advanced" open>
          <summary>高级设置</summary>
          <div class="settings-model-picker-field protocol-field">
            <span>接口格式</span>
            <button data-slot="select-trigger">OpenAI Chat</button>
          </div>
        </details>
      </div>
    </div>
  `)

  const primary = await page.locator('.primary-field').boundingBox()
  const advanced = await page.locator('.settings-model-picker-advanced').boundingBox()
  const protocol = await page.locator('.protocol-field').boundingBox()

  expect(primary).not.toBeNull()
  expect(advanced).not.toBeNull()
  expect(protocol).not.toBeNull()
  expect(advanced!.y).toBeGreaterThanOrEqual(primary!.y + primary!.height)
  expect(protocol!.x).toBe(primary!.x)
})

test('adjacent model states keep a visible gap', async ({ page }) => {
  await page.setContent(`
    <style>${styles}</style>
    <div class="composer-mode-model-list" style="width: 270px">
      <div>
        <div class="composer-mode-item composer-mode-model-item is-active">DeepSeek V4 Flash</div>
        <div class="composer-mode-item composer-mode-model-item">DeepSeek V4 Pro</div>
      </div>
    </div>
  `)

  const selected = await page.locator('.composer-mode-model-item').first().boundingBox()
  const hovered = await page.locator('.composer-mode-model-item').last().boundingBox()

  expect(selected).not.toBeNull()
  expect(hovered).not.toBeNull()
  expect(hovered!.y - (selected!.y + selected!.height)).toBeGreaterThanOrEqual(2)
})

test('scroll-to-bottom button is centered above the composer', async ({ page }) => {
  await page.setContent(`
    <style>${styles}</style>
    <style>.scroll-to-bottom { animation: none; }</style>
    <main style="width: 900px">
      <section class="chat-surface" style="width: 900px; height: 640px; position: relative; flex: none">
        <div class="messages" style="width: 100%; height: 640px">
          <article class="message assistant">
            <div class="message-bubble-inner"><div class="message-content"><p>最后一条消息</p></div></div>
          </article>
        </div>
        <button type="button" class="scroll-to-bottom" aria-label="回到底部">
          <svg aria-hidden="true"></svg>
        </button>
      </section>
      <div class="composer" style="position: relative; margin: 0 auto 16px; width: 760px; height: 84px"></div>
    </main>
  `)

  const buttonLocator = page.locator('.scroll-to-bottom')
  const button = await buttonLocator.boundingBox()
  const surface = await page.locator('.chat-surface').boundingBox()
  const composer = await page.locator('.composer').boundingBox()

  expect(button).not.toBeNull()
  expect(surface).not.toBeNull()
  expect(composer).not.toBeNull()
  const buttonCenter = button!.x + button!.width / 2
  const composerCenter = composer!.x + composer!.width / 2
  expect(Math.abs(buttonCenter - composerCenter)).toBeLessThanOrEqual(1)
  // The button stays inside the chat surface with a compact gap above the composer.
  expect(button!.y).toBeGreaterThanOrEqual(surface!.y)
  expect(button!.x).toBeGreaterThanOrEqual(surface!.x)
  expect(button!.x + button!.width).toBeLessThanOrEqual(surface!.x + surface!.width)
  const composerGap = composer!.y - (button!.y + button!.height)
  expect(composerGap).toBeGreaterThanOrEqual(8)
  expect(composerGap).toBeLessThanOrEqual(12)

  await buttonLocator.hover()
  await page.mouse.down()
  const pressedButton = await buttonLocator.boundingBox()
  await page.mouse.up()
  expect(pressedButton).not.toBeNull()
  const pressedCenter = pressedButton!.x + pressedButton!.width / 2
  expect(Math.abs(pressedCenter - buttonCenter)).toBeLessThanOrEqual(1)
})
