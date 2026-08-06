import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MessageBubble } from './MessageBubble'
import { I18nProvider } from '@/shared/i18n/I18nProvider'
import type { ChatMessage } from '@/shared/local-data/types'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 'm1',
    role: 'assistant',
    content: '今天杭州多云。',
    createdAt: new Date().toISOString(),
    status: 'done',
    ...overrides,
  }
}

describe('MessageBubble meta', () => {
  it('loads generated image Artifacts and renders them inline', async () => {
    const createObjectURL = vi.fn().mockReturnValue('blob:generated-image')
    const revokeObjectURL = vi.fn()
    Object.defineProperties(URL, {
      createObjectURL: { configurable: true, value: createObjectURL },
      revokeObjectURL: { configurable: true, value: revokeObjectURL },
    })
    const body = new Blob(['png'], { type: 'image/png' })
    const load = vi.fn().mockResolvedValue(body)

    const { unmount } = render(
      <I18nProvider>
        <MessageBubble
          message={message({
            agentEvents: [{
              type: 'artifact.created',
              label: '图片',
              artifactId: 'art-image',
              artifactTitle: 'generated-image-1.png',
              artifactTool: 'image.generate',
              artifactMediaType: 'image/png',
            }],
          })}
          onLoadArtifactContent={load}
        />
      </I18nProvider>,
    )

    const image = await screen.findByRole('img', { name: 'generated-image-1.png' })
    expect(image).toHaveAttribute('src', 'blob:generated-image')
    expect(load).toHaveBeenCalledWith('art-image')
    unmount()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:generated-image')
  })

  it('does not render a duplicate Markdown image for a generated Artifact', async () => {
    Object.defineProperties(URL, {
      createObjectURL: { configurable: true, value: vi.fn().mockReturnValue('blob:generated-image') },
      revokeObjectURL: { configurable: true, value: vi.fn() },
    })

    render(
      <I18nProvider>
        <MessageBubble
          message={message({
            content: [
              '小狗图片已生成',
              '![按文件名引用](generated-image-1.png)',
              '![按 Artifact ID 引用](/attachments/art-image)',
            ].join('\n\n'),
            agentEvents: [{
              type: 'artifact.created',
              label: '图片',
              artifactId: 'art-image',
              artifactTitle: 'generated-image-1.png',
              artifactTool: 'image.generate',
              artifactMediaType: 'image/png',
            }],
          })}
          onLoadArtifactContent={vi.fn().mockResolvedValue(new Blob(['png'], { type: 'image/png' }))}
        />
      </I18nProvider>,
    )

    await screen.findByRole('img', { name: 'generated-image-1.png' })
    expect(screen.getAllByRole('img')).toHaveLength(1)
  })

  it('renders Runtime-normalized plugin references and command', () => {
    render(
      <I18nProvider>
        <MessageBubble message={message({
          role: 'user',
          content: '处理附件',
          pluginReferences: [{
            pluginId: 'dev.shejane.fixture.archive',
            name: 'Archive fixture',
            digest: `sha256:${'a'.repeat(64)}`,
          }],
          pluginCommand: {
            pluginId: 'dev.shejane.fixture.archive',
            pluginName: 'Archive fixture',
            commandId: 'extract',
            title: 'Extract archive',
            digest: `sha256:${'a'.repeat(64)}`,
          },
        })} />
      </I18nProvider>,
    )

    expect(screen.getByText('@Archive fixture')).toBeInTheDocument()
    expect(screen.getByText('/Archive fixture: Extract archive')).toBeInTheDocument()
  })

  it('renders Markdown live while streaming (no raw ** flash)', () => {
    vi.useFakeTimers()
    const { container } = render(
      <I18nProvider>
        <MessageBubble message={message({ role: 'assistant', status: 'streaming', content: '**你好**' })} />
      </I18nProvider>,
    )

    act(() => {
      vi.advanceTimersByTime(5000)
    })

    expect(container.querySelector('strong')?.textContent).toBe('你好')
    expect(screen.queryByText('**你好**')).not.toBeInTheDocument()
  })

  it('does not replay streamed text while the same message grows', () => {
    vi.useFakeTimers()
    const renderBubble = (content: string, status: ChatMessage['status'] = 'streaming') => (
      <I18nProvider>
        <MessageBubble message={message({ status, content })} />
      </I18nProvider>
    )
    const { container, rerender } = render(renderBubble('S'))

    for (const content of ['S SESSION', 'S SESSION INTENT', 'S SESSION INTENT\n用户要求整理文件。']) {
      act(() => {
        vi.advanceTimersByTime(22)
      })
      rerender(renderBubble(content))
    }
    rerender(renderBubble('S SESSION INTENT\n用户要求整理文件。', 'done'))
    act(() => {
      vi.advanceTimersByTime(5000)
    })

    expect(container.querySelector('.message-content')?.textContent).toBe('S SESSION INTENT\n用户要求整理文件。')
  })

  it('shows the full buffered reply as soon as the message reaches done', () => {
    vi.useFakeTimers()
    const fullReply = '第一段 第二段 第三段 第四段 第五段 第六段'
    const renderBubble = (content: string, status: ChatMessage['status']) => (
      <I18nProvider>
        <MessageBubble message={message({ status, content })} />
      </I18nProvider>
    )
    const { container, rerender } = render(renderBubble('第一段 ', 'streaming'))

    act(() => {
      vi.advanceTimersByTime(22)
    })
    rerender(renderBubble(fullReply, 'streaming'))
    rerender(renderBubble(fullReply, 'done'))

    expect(container.querySelector('.message-content')?.textContent).toBe(fullReply)
  })

  it('keeps a rejected draft visible until its replacement arrives', () => {
    vi.useFakeTimers()
    const renderBubble = (content: string) => (
      <I18nProvider>
        <MessageBubble message={message({ status: 'streaming', content })} />
      </I18nProvider>
    )
    const { container, rerender } = render(renderBubble('Done.'))
    act(() => {
      vi.advanceTimersByTime(22)
    })
    expect(container.querySelector('.message-content')?.textContent).toBe('Done.')

    rerender(renderBubble(''))
    expect(container.querySelector('.message-content')?.textContent).toBe('Done.')

    rerender(renderBubble('E2E_COMPLETION_REPAIRED'))

    expect(container.querySelector('.message-content')?.textContent).toBe('E2E_COMPLETION_REPAIRED')
  })

  it('does not render legacy raw reasoning', () => {
    const legacyMessage = {
      ...message({ content: '处理完成。' }),
      reasoning: '# SESSION INTENT\n用户要求整理文件。',
    }
    const { container } = render(
      <I18nProvider>
        <MessageBubble message={legacyMessage} />
      </I18nProvider>,
    )

    expect(container.querySelector('.message-reasoning')).not.toBeInTheDocument()
    expect(screen.queryByText('# SESSION INTENT')).not.toBeInTheDocument()
  })

  it('treats a single newline as a line break (remark-breaks)', () => {
    const { container } = render(
      <I18nProvider>
        <MessageBubble message={message({ role: 'assistant', status: 'done', content: '第一行\n第二行' })} />
      </I18nProvider>,
    )
    expect(container.querySelector('br')).toBeInTheDocument()
    const paragraphText = container.querySelector('p')?.textContent ?? ''
    expect(paragraphText).toContain('第一行')
    expect(paragraphText).toContain('第二行')
  })

  it('normalizes ad-hoc heading levels on the finished message', () => {
    const { container } = render(
      <I18nProvider>
        <MessageBubble message={message({ role: 'assistant', status: 'done', content: '### 标题\n正文' })} />
      </I18nProvider>,
    )
    // remark-normalize-headings (finished path) rebalances a lone `###` to h1.
    expect(container.querySelector('h1')?.textContent).toBe('标题')
    expect(container.querySelector('h3')).not.toBeInTheDocument()
  })

  it('shows the relative time and copies the message content', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })

    render(
      <I18nProvider>
        <MessageBubble message={message()} />
      </I18nProvider>,
    )

    expect(screen.getByText('刚刚')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '复制' }))
    expect(writeText).toHaveBeenCalledWith('今天杭州多云。')
    await waitFor(() => expect(screen.getByRole('button', { name: '已复制' })).toBeInTheDocument())
  })

  it('contains a clipboard permission failure without showing false success', async () => {
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
    render(
      <I18nProvider>
        <MessageBubble message={message()} />
      </I18nProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: '复制' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '复制失败' })).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: '已复制' })).not.toBeInTheDocument()
  })

  it('shows diagnostics after the timestamp for every Runtime assistant turn', () => {
    const onOpenDiagnostics = vi.fn()
    const { container } = render(
      <I18nProvider>
        <MessageBubble
          message={message({ runId: 'run-local' })}
          onOpenDiagnostics={onOpenDiagnostics}
        />
      </I18nProvider>,
    )

    const time = container.querySelector('.message-meta-time')
    const diagnostics = screen.getByRole('button', { name: '诊断' })
    expect(time?.nextElementSibling).toBe(diagnostics)
    fireEvent.click(diagnostics)
    expect(onOpenDiagnostics).toHaveBeenCalledWith('run-local')
  })

  it('also lets the user message be copied', () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })

    render(
      <I18nProvider>
        <MessageBubble message={message({ role: 'user', content: '杭州天气怎么样' })} />
      </I18nProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: '复制' }))
    expect(writeText).toHaveBeenCalledWith('杭州天气怎么样')
  })

  it('syntax-highlights a fenced code block', async () => {
    const { container } = render(
      <I18nProvider>
        <MessageBubble message={message({ role: 'assistant', content: '```python\nprint("hi")\n```' })} />
      </I18nProvider>,
    )
    const block = container.querySelector('.code-block')
    expect(block).toBeInTheDocument()
    // highlight.js tokenized the body into colored spans.
    expect(container.querySelector('pre.code-block-pre code.hljs')).toBeInTheDocument()
    await waitFor(() => expect(container.querySelector('.code-block .hljs-string')).toBeInTheDocument())
  })

  it("copies only the raw code from a block's own copy button", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })

    render(
      <I18nProvider>
        <MessageBubble message={message({ role: 'assistant', content: '说明:\n```js\nconst a = 1\n```' })} />
      </I18nProvider>,
    )

    // The block copy button has its own accessible name, distinct from the
    // message-level "复制" button (so the two never collide).
    fireEvent.click(screen.getByRole('button', { name: '复制代码' }))
    expect(writeText).toHaveBeenCalledWith('const a = 1')
    await waitFor(() => expect(screen.getByRole('button', { name: '已复制代码' })).toBeInTheDocument())
  })

  it('regenerates an assistant reply and offers no edit affordance', () => {
    const onRegenerate = vi.fn()
    render(
      <I18nProvider>
        <MessageBubble message={message()} onRegenerate={onRegenerate} onEditResend={vi.fn()} />
      </I18nProvider>,
    )
    // Assistant messages get regenerate, not edit.
    expect(screen.queryByRole('button', { name: '编辑并重发' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重新生成' }))
    expect(onRegenerate).toHaveBeenCalledWith('m1')
  })

  it('hides regenerate while Runtime cleanup is unconfirmed', () => {
    render(
      <I18nProvider>
        <MessageBubble
          message={message({
            status: 'error',
            agentEvents: [
              { type: 'run.cleanup_required', label: '执行清理尚未确认' },
            ],
          })}
          onRegenerate={vi.fn()}
        />
      </I18nProvider>,
    )
    expect(screen.queryByRole('button', { name: '重新生成' })).not.toBeInTheDocument()
  })

  it('restores regenerate after the original owner confirms cleanup', () => {
    render(
      <I18nProvider>
        <MessageBubble
          message={message({
            status: 'error',
            agentEvents: [
              { type: 'run.cleanup_required', label: '执行清理尚未确认' },
              { type: 'run.failed', label: '租约已失效，但清理已经完成' },
            ],
          })}
          onRegenerate={vi.fn()}
        />
      </I18nProvider>,
    )
    expect(screen.getByRole('button', { name: '重新生成' })).toBeInTheDocument()
  })

  it('edits and resends a user message', () => {
    const onEditResend = vi.fn()
    render(
      <I18nProvider>
        <MessageBubble message={message({ role: 'user', content: '原始问题' })} onEditResend={onEditResend} onRegenerate={vi.fn()} />
      </I18nProvider>,
    )
    // User messages get edit, not regenerate.
    expect(screen.queryByRole('button', { name: '重新生成' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '编辑并重发' }))
    const textarea = screen.getByLabelText('编辑并重发')
    fireEvent.change(textarea, { target: { value: '修改后的问题' } })
    fireEvent.click(screen.getByRole('button', { name: '保存并重发' }))
    expect(onEditResend).toHaveBeenCalledWith('m1', '修改后的问题')
  })

  it('cancels an edit without resending', () => {
    const onEditResend = vi.fn()
    render(
      <I18nProvider>
        <MessageBubble message={message({ role: 'user', content: '原始问题' })} onEditResend={onEditResend} />
      </I18nProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: '编辑并重发' }))
    expect(screen.getByRole('textbox')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(onEditResend).not.toHaveBeenCalled()
    // Back to view mode: the textarea is gone, the edit button is back.
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '编辑并重发' })).toBeInTheDocument()
  })

  it('requests deletion of a message', () => {
    const onDelete = vi.fn()
    render(
      <I18nProvider>
        <MessageBubble message={message()} onDelete={onDelete} />
      </I18nProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: '删除' }))
    expect(onDelete).toHaveBeenCalledWith('m1')
  })

  it('disables retry/edit/delete while a run is active', () => {
    const onRegenerate = vi.fn()
    render(
      <I18nProvider>
        <MessageBubble message={message()} onRegenerate={onRegenerate} onDelete={vi.fn()} runActive />
      </I18nProvider>,
    )
    const regenerate = screen.getByRole('button', { name: '重新生成' })
    expect(regenerate).toBeDisabled()
    fireEvent.click(regenerate)
    expect(onRegenerate).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '删除' })).toBeDisabled()
  })

  it('shows token usage on a settled model turn', () => {
    render(
      <I18nProvider>
        <MessageBubble
          message={message({
            tokens: 1234,
          })}
        />
      </I18nProvider>,
    )

    expect(screen.getByText('1,234 个 token')).toBeInTheDocument()
  })

  it('shows token and tool counts when a settled assistant turn used tools', () => {
    render(
      <I18nProvider>
        <MessageBubble
          message={message({
            tokens: 1234,
            agentEvents: [{ type: 'tool.completed', label: 'ran' }],
          })}
        />
      </I18nProvider>,
    )
    expect(screen.getByText('1,234 个 token · 1 次工具')).toBeInTheDocument()
  })

  it('treats failed tools as tool usage in the settled usage chip', () => {
    render(
      <I18nProvider>
        <MessageBubble
          message={message({
            tokens: 1234,
            agentEvents: [{ type: 'tool.failed', label: '工具失败：读取网页' }],
          })}
        />
      </I18nProvider>,
    )

    expect(screen.getByText('1,234 个 token · 1 次工具')).toBeInTheDocument()
  })

  it('shows the concrete model badge on a settled assistant turn', () => {
    render(
      <I18nProvider>
        <MessageBubble
          message={message({
            runMode: { resolved: 'deepseek-v4-pro', reason: '' },
          })}
        />
      </I18nProvider>,
    )

    expect(screen.getByText('deepseek-v4-pro')).toBeInTheDocument()
  })

  it('hides assistant content when it duplicates the failure timeline label', () => {
    render(
      <I18nProvider>
        <MessageBubble
          message={message({
            status: 'error',
            content: 'missing API key',
            agentEvents: [{ type: 'run.failed', label: 'missing API key · 需要你处理' }],
          })}
        />
      </I18nProvider>,
    )

    expect(screen.queryByText('missing API key')).not.toBeInTheDocument()
  })

  it('keeps already displayed model output visible after a durable run failure', () => {
    vi.useFakeTimers()
    const { rerender } = render(
      <I18nProvider>
        <MessageBubble
          message={message({ status: 'streaming', content: '临时模型输出' })}
        />
      </I18nProvider>,
    )
    act(() => {
      vi.advanceTimersByTime(5000)
    })
    expect(screen.getByText(/临时模型/)).toBeInTheDocument()

    rerender(
      <I18nProvider>
        <MessageBubble
          message={message({
            status: 'error',
            content: '临时模型输出',
            agentEvents: [{ type: 'run.failed', label: '模型调用次数已耗尽' }],
          })}
        />
      </I18nProvider>,
    )

    expect(screen.getByText(/临时模型/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '复制' })).toBeInTheDocument()
  })

  it('omits the usage chip when there is no usage data', () => {
    render(
      <I18nProvider>
        <MessageBubble message={message()} />
      </I18nProvider>,
    )
    expect(screen.queryByText(/tokens/)).not.toBeInTheDocument()
    expect(screen.queryByText(/积分/)).not.toBeInTheDocument()
  })

  it('keeps inline code as a plain chip with no copy button', () => {
    const { container } = render(
      <I18nProvider>
        <MessageBubble message={message({ role: 'assistant', content: '用 `npm install` 安装' })} />
      </I18nProvider>,
    )
    expect(container.querySelector('.code-block')).not.toBeInTheDocument()
    expect(container.querySelector('code')?.textContent).toBe('npm install')
    expect(screen.queryByRole('button', { name: '复制代码' })).not.toBeInTheDocument()
  })

  it('omits the copy action when there is no content', () => {
    render(
      <I18nProvider>
        <MessageBubble message={message({ content: '' })} />
      </I18nProvider>,
    )
    expect(screen.queryByRole('button', { name: '复制' })).not.toBeInTheDocument()
    expect(screen.getByText('刚刚')).toBeInTheDocument()
  })

  it('shows the input pause copy when the agent is waiting for a user answer', () => {
    render(
      <I18nProvider>
        <MessageBubble message={message({ content: '', status: 'waiting_input' })} />
      </I18nProvider>,
    )

    expect(screen.getByText('等待你的回答。')).toBeInTheDocument()
  })

  it('renders recognized local filenames as preview buttons that fire with the resolved absolute path', () => {
    const onPreviewLocalFile = vi.fn()
    render(
      <I18nProvider>
        <MessageBubble
          message={message({
            content: 'Files in workspace:\n- report.docx\n- /tmp/numbers.xlsx\n- slides.pptx\n- paper.pdf\n- server.ts\n- notes.txt',
          })}
          workspaceRoot="/Users/me/proj"
          onPreviewLocalFile={onPreviewLocalFile}
        />
      </I18nProvider>,
    )

    // Relative path → resolved against workspaceRoot.
    const reportBtn = screen.getByRole('button', { name: 'report.docx' })
    fireEvent.click(reportBtn)
    expect(onPreviewLocalFile).toHaveBeenLastCalledWith({
      path: '/Users/me/proj/report.docx',
      kind: 'word',
      name: 'report.docx',
    })

    // Absolute path → passes through as-is.
    const numbersBtn = screen.getByRole('button', { name: '/tmp/numbers.xlsx' })
    fireEvent.click(numbersBtn)
    expect(onPreviewLocalFile).toHaveBeenLastCalledWith({
      path: '/tmp/numbers.xlsx',
      kind: 'excel',
      name: 'numbers.xlsx',
    })

    const slidesBtn = screen.getByRole('button', { name: 'slides.pptx' })
    fireEvent.click(slidesBtn)
    expect(onPreviewLocalFile).toHaveBeenLastCalledWith({
      path: '/Users/me/proj/slides.pptx',
      kind: 'powerpoint',
      name: 'slides.pptx',
    })

    fireEvent.click(screen.getByRole('button', { name: 'paper.pdf' }))
    expect(onPreviewLocalFile).toHaveBeenLastCalledWith({
      path: '/Users/me/proj/paper.pdf',
      kind: 'pdf',
      name: 'paper.pdf',
    })

    fireEvent.click(screen.getByRole('button', { name: 'server.ts' }))
    expect(onPreviewLocalFile).toHaveBeenLastCalledWith({
      path: '/Users/me/proj/server.ts',
      kind: 'code',
      name: 'server.ts',
    })

    expect(screen.getByRole('button', { name: 'notes.txt' })).toBeInTheDocument()
  })

  it('opens the native context menu for generated file references', () => {
    const onPreviewLocalFile = vi.fn()
    const onLocalFileContextMenu = vi.fn()
    render(
      <I18nProvider>
        <MessageBubble
          message={message({ content: '结果已保存为 paper.pdf' })}
          workspaceRoot="/Users/me/proj"
          onPreviewLocalFile={onPreviewLocalFile}
          onLocalFileContextMenu={onLocalFileContextMenu}
        />
      </I18nProvider>,
    )

    fireEvent.contextMenu(screen.getByRole('button', { name: 'paper.pdf' }))
    expect(onLocalFileContextMenu).toHaveBeenCalledWith({
      path: '/Users/me/proj/paper.pdf',
      kind: 'pdf',
      name: 'paper.pdf',
    })
  })

  it('resolves quoted and inline-code paths with spaces but leaves tilde paths inert', () => {
    const onPreviewLocalFile = vi.fn()
    render(
      <I18nProvider>
        <MessageBubble
          message={message({ content: 'Open "my report.pdf", `source files/server.ts`, and ~/private.pdf.' })}
          workspaceRoot="/Users/me/proj"
          onPreviewLocalFile={onPreviewLocalFile}
        />
      </I18nProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'my report.pdf' }))
    expect(onPreviewLocalFile).toHaveBeenLastCalledWith({
      path: '/Users/me/proj/my report.pdf',
      kind: 'pdf',
      name: 'my report.pdf',
    })
    fireEvent.click(screen.getByRole('button', { name: 'source files/server.ts' }))
    expect(onPreviewLocalFile).toHaveBeenLastCalledWith({
      path: '/Users/me/proj/source files/server.ts',
      kind: 'code',
      name: 'server.ts',
    })
    expect(screen.queryByRole('button', { name: '~/private.pdf' })).not.toBeInTheDocument()
  })

  it('does not create a misleading partial link for an unquoted absolute path with spaces', () => {
    render(
      <I18nProvider>
        <MessageBubble
          message={message({ content: 'Open /Users/me/My Report.pdf' })}
          workspaceRoot="/Users/me/proj"
          onPreviewLocalFile={vi.fn()}
        />
      </I18nProvider>,
    )

    expect(screen.queryByRole('button', { name: 'Report.pdf' })).not.toBeInTheDocument()
    expect(screen.getByText(/\/Users\/me\/My Report\.pdf/)).toBeInTheDocument()
  })

  it('does not create a misleading partial link for an unquoted relative path with spaces', () => {
    render(
      <I18nProvider>
        <MessageBubble
          message={message({ content: 'Open my reports/Report.pdf' })}
          workspaceRoot="/Users/me/proj"
          onPreviewLocalFile={vi.fn()}
        />
      </I18nProvider>,
    )

    expect(screen.queryByRole('button', { name: 'reports/Report.pdf' })).not.toBeInTheDocument()
    expect(screen.getByText(/my reports\/Report\.pdf/)).toBeInTheDocument()
  })

  it('keeps an unambiguous bare relative path clickable after one prose token', () => {
    const onPreviewLocalFile = vi.fn()
    render(
      <I18nProvider>
        <MessageBubble
          message={message({ content: 'Open docs/report.pdf' })}
          workspaceRoot="/Users/me/proj"
          onPreviewLocalFile={onPreviewLocalFile}
        />
      </I18nProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'docs/report.pdf' }))
    expect(onPreviewLocalFile).toHaveBeenCalledWith({
      path: '/Users/me/proj/docs/report.pdf',
      kind: 'pdf',
      name: 'report.pdf',
    })
  })

  it('activates and opens the native context menu for sent attachment cards', () => {
    const onPreviewLocalFile = vi.fn()
    const onLocalFileContextMenu = vi.fn()
    render(
      <I18nProvider>
        <MessageBubble
          message={message({
            role: 'user',
            content: '请阅读',
            attachments: [{
              path: '/tmp/paper.pdf',
              name: 'paper.pdf',
              runId: 'run-1',
              inputId: 'source',
            }, {
              path: '/tmp/photo.png',
              name: 'photo.png',
            }],
          })}
          onPreviewLocalFile={onPreviewLocalFile}
          onLocalFileContextMenu={onLocalFileContextMenu}
        />
      </I18nProvider>,
    )

    const card = screen.getByRole('button', { name: 'paper.pdf' })
    const attachmentGroup = card.closest('.message-attachments')
    expect(attachmentGroup).toHaveClass('message-attachments-detached')
    expect(card.closest('.message-bubble-inner')).toBeNull()
    expect(screen.getByText('请阅读').closest('.message-bubble-inner')).not.toBeNull()
    expect(card.querySelector('.message-attachment-name')).toHaveTextContent('paper.pdf')
    expect(card.querySelector('.tabler-icon-file-type-pdf')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'photo.png' }).querySelector('.tabler-icon-photo'))
      .toBeInTheDocument()
    fireEvent.click(card)
    expect(onPreviewLocalFile).toHaveBeenCalledWith({
      path: '/tmp/paper.pdf',
      name: 'paper.pdf',
      kind: 'pdf',
      runId: 'run-1',
      inputId: 'source',
    })
    fireEvent.contextMenu(card)
    expect(onLocalFileContextMenu).toHaveBeenCalledWith(expect.objectContaining({
      name: 'paper.pdf',
      runId: 'run-1',
      inputId: 'source',
    }))
  })

  it('finds office filenames wrapped in inline code + bold (regression: the agent emits **`X.docx`**)', () => {
    const onPreviewLocalFile = vi.fn()
    render(
      <I18nProvider>
        <MessageBubble
          message={message({
            // This is exactly the markdown the agent emits for a docx
            // listing — bold + inline code, inside a numbered list.
            content: '当前目录下有 3 个 doc 文件：\n\n1. **`副本软件开发合同模板.docx`**\n2. **`灵活就业登记.docx`**\n3. **`软件开发合同模板.docx`**',
          })}
          workspaceRoot="/Users/me/Downloads/合同"
          onPreviewLocalFile={onPreviewLocalFile}
        />
      </I18nProvider>,
    )

    const firstBtn = screen.getByRole('button', { name: '副本软件开发合同模板.docx' })
    fireEvent.click(firstBtn)
    expect(onPreviewLocalFile).toHaveBeenLastCalledWith({
      path: '/Users/me/Downloads/合同/副本软件开发合同模板.docx',
      kind: 'word',
      name: '副本软件开发合同模板.docx',
    })

    expect(screen.getByRole('button', { name: '灵活就业登记.docx' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '软件开发合同模板.docx' })).toBeInTheDocument()
  })

  it('skips office-link rendering when onPreviewLocalFile is not provided', () => {
    render(
      <I18nProvider>
        <MessageBubble message={message({ content: 'See report.docx' })} workspaceRoot="/x/y" />
      </I18nProvider>,
    )
    // Without a callback, the filename stays plain text — no button.
    expect(screen.queryByRole('button', { name: 'report.docx' })).not.toBeInTheDocument()
    // The text itself is still visible.
    expect(screen.getByText(/See report\.docx/)).toBeInTheDocument()
  })
})
