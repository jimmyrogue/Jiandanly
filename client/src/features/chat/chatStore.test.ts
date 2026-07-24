import { describe, expect, it } from 'vitest'
import { projectTransientAssistantText, timelineItem, toolDetail } from './chatStore'

describe('runtime timeline', () => {
  it('derives a stable event identity from the Runtime sequence when id is absent', () => {
    expect(timelineItem({
      event_type: 'tool.started',
      run_id: 'run-1',
      seq: 7,
      payload: { tool: 'fs.read' },
    })?.eventId).toBe('run-1:7')
  })

  it('keeps only the current model round text across a tool call', () => {
    let content = projectTransientAssistantText('', {
      event_type: 'llm.delta',
      payload: { content: '准备读取附件' },
    })
    content = projectTransientAssistantText(content, {
      event_type: 'tool.requested',
      payload: { tool: 'read_file' },
    })
    content = projectTransientAssistantText(content, {
      event_type: 'llm.delta',
      payload: { content: '这是附件摘要。' },
    })

    expect(content).toBe('这是附件摘要。')
    expect(projectTransientAssistantText(content, { event_type: 'run.failed' })).toBe('这是附件摘要。')
  })

  it('replaces the transient draft when LangGraph starts another model round', () => {
    let content = projectTransientAssistantText('', {
      event_type: 'llm.delta',
      payload: { content: 'SESSION INTENT\n旧一轮草稿' },
    })
    content = projectTransientAssistantText(content, { event_type: 'llm.round.started' })
    content = projectTransientAssistantText(content, {
      event_type: 'llm.delta',
      payload: { content: '这是最终回答。' },
    })

    expect(content).toBe('这是最终回答。')
  })

  it('renders universal primitive tool events with user-facing action names', () => {
    expect(timelineItem({ event_type: 'permission.required', payload: { request_id: 'perm-url', tool: 'open.url' } })).toMatchObject({
      label: '需要权限：用系统浏览器打开网页',
      permissionTool: '用系统浏览器打开网页',
    })
    expect(timelineItem({ event_type: 'permission.required', payload: { request_id: 'perm-fallback', tool: 'shell.run', review_source: 'fallback', review_reason: 'reviewer unavailable' } })).toMatchObject({
      permissionSource: 'fallback',
      permissionReason: 'reviewer unavailable',
    })
    expect(timelineItem({ event_type: 'permission.required', payload: { request_id: 'perm-write', tool: 'fs.write', allow_run_scope: true } })).toMatchObject({
      label: '需要权限：写入文件',
      permissionTool: '写入文件',
      permissionCanGrantForRun: true,
    })
    expect(timelineItem({ event_type: 'tool.requested', payload: { tool: 'fs.list' } })).toMatchObject({
      label: '调用工具：列出文件',
    })
    expect(timelineItem({ event_type: 'tool.requested', payload: { tool: 'glob' } })).toMatchObject({
      label: '调用工具：查找文件',
    })
    expect(timelineItem({ event_type: 'tool.requested', payload: { tool: 'grep' } })).toMatchObject({
      label: '调用工具：搜索文件',
    })
    expect(timelineItem({ event_type: 'verification.completed', payload: { tool: 'task.verify', status: 'passed' } })).toMatchObject({
      label: '验证通过：验证任务结果',
    })
  })

  it('projects file-exists as a recoverable conflict instead of a generic failure', () => {
    expect(timelineItem({
      event_type: 'tool.failed',
      payload: {
        tool: 'write_file',
        tool_call_id: 'write-1',
        error_code: 'file_exists',
        content: JSON.stringify({ path: 'snake.html' }),
      },
    })).toMatchObject({
      label: '发现文件名冲突：snake.html',
      errorCode: 'file_exists',
      target: 'snake.html',
      toolDetail: { kind: 'text', text: 'snake.html' },
    })
  })

  it('keeps plugin progress correlated with its tool call', () => {
    expect(timelineItem({
      event_type: 'tool.progress',
      payload: {
        tool: 'plugin.dev.shejane.media.inspect',
        tool_call_id: 'call-media',
        message: 'Decoding media',
        completed: 5,
        total: 10,
        unit: 'frames',
      },
    })).toMatchObject({
      type: 'tool.progress',
      tool: 'plugin.dev.shejane.media.inspect',
      toolCallId: 'call-media',
      toolDetail: { kind: 'text', text: 'Decoding media · 5/10 frames' },
    })
  })

  it('uses the runtime run.failed error text in the timeline', () => {
    expect(timelineItem({ event_type: 'run.failed', payload: { error: 'missing API key', type: 'ModelProviderError' } })).toMatchObject({
      label: 'missing API key',
    })
  })

  it('renders an unconfirmed cleanup as a non-retryable quarantined run', () => {
    expect(
      timelineItem({
        event_type: 'run.cleanup_required',
        payload: {
          error: 'Runtime cleanup is still unconfirmed.',
          category: 'execution_cleanup_unconfirmed',
        },
      }),
    ).toMatchObject({
      label: 'Runtime cleanup is still unconfirmed.',
      failureCategory: 'execution_cleanup_unconfirmed',
      failureRetryable: false,
    })
  })

  it('adds the runtime failure policy hint to run.failed timeline labels', () => {
    expect(
      timelineItem({
        event_type: 'run.failed',
        payload: {
          error: 'missing API key',
          type: 'ModelProviderError',
          category: 'configuration',
          retryable: false,
          action_kind: 'user_action',
          recovery_action: 'diagnostics',
          suggested_action: 'Configure the missing key, then retry.',
        },
      }),
    ).toMatchObject({
      label: 'missing API key · 需要你处理',
      failureCategory: 'configuration',
      failureRetryable: false,
      failureActionKind: 'user_action',
      failureRecoveryAction: 'diagnostics',
      failureSuggestedAction: 'Configure the missing key, then retry.',
    })
  })

  it('keeps run.waiting handoff ledger state for pause recovery context', () => {
    expect(
      timelineItem({
        event_type: 'run.waiting',
        payload: {
          handoff: {
            ledger_state: 'stale',
            ledger_message: 'Progress ledger stale after tool.completed.',
          },
        },
      }),
    ).toMatchObject({
      label: '任务已暂停',
      handoffLedgerState: 'stale',
      handoffLedgerMessage: 'Progress ledger stale after tool.completed.',
    })
  })

  it('renders browser and environment observation events with user-facing labels', () => {
    expect(timelineItem({ event_type: 'browser.observed', payload: { title: 'Example Report', url: 'https://example.com/report' } })).toMatchObject({
      label: '观察网页：Example Report',
    })
    expect(timelineItem({ event_type: 'environment.observed', payload: { foreground_app: 'Preview', window_title: 'Invoice.pdf' } })).toMatchObject({
      label: '观察环境：Preview - Invoice.pdf',
    })
    expect(timelineItem({ event_type: 'ui.action.requested', payload: { tool: 'browser.open' } })).toMatchObject({
      label: '请求操作：打开受控网页',
    })
    expect(timelineItem({ event_type: 'ui.action.completed', payload: { tool: 'browser.open' } })).toMatchObject({
      label: '操作完成：打开受控网页',
    })
    expect(timelineItem({ event_type: 'tool.requested', payload: { tool: 'browser.search' } })).toMatchObject({
      label: '调用工具：搜索网页',
    })
    expect(timelineItem({ event_type: 'tool.requested', payload: { tool: 'browser.read' } })).toMatchObject({
      label: '调用工具：阅读网页正文',
    })
    expect(timelineItem({ event_type: 'tool.requested', payload: { tool: 'browser.verify' } })).toMatchObject({
      label: '调用工具：验证网页',
    })
    expect(timelineItem({ event_type: 'tool.requested', payload: { tool: 'browser.screenshot' } })).toMatchObject({
      label: '调用工具：页面截图',
    })
    expect(timelineItem({ event_type: 'permission.required', payload: { request_id: 'perm-click', tool: 'browser.click' } })).toMatchObject({
      label: '需要权限：点击网页元素',
      permissionTool: '点击网页元素',
    })
    expect(timelineItem({ event_type: 'permission.required', payload: { request_id: 'perm-type', tool: 'browser.type' } })).toMatchObject({
      label: '需要权限：输入网页文本',
      permissionTool: '输入网页文本',
    })
    expect(timelineItem({ event_type: 'tool.completed', payload: { tool: 'browser.scroll' } })).toMatchObject({
      label: '工具完成：滚动网页',
    })
  })

  it('renders collected browser sources with title and url', () => {
    expect(timelineItem({ event_type: 'source.collected', payload: { title: 'Example Source', url: 'https://example.com/source', artifact_id: 'artifact-source' } })).toMatchObject({
      label: '收集来源：Example Source',
      sourceTitle: 'Example Source',
      sourceUrl: 'https://example.com/source',
      artifactId: 'artifact-source',
    })
  })

  it('renders local harness budget warnings with a readable label', () => {
    expect(timelineItem({ event_type: 'run.budget_warning', payload: { reason: 'max_steps_reached', max_steps: 12 } })).toMatchObject({
      label: '工具步数达到上限，正在整理已有结果',
    })
    expect(timelineItem({ event_type: 'run.budget_warning', payload: { reason: 'long_running', step: 20 } })).toMatchObject({
      label: '任务较长，仍在继续执行',
    })
  })

  it('renders repair workflow events with user-facing labels and source metadata', () => {
    expect(
      timelineItem({
        event_type: 'repair.workflow',
        payload: {
          status: 'started',
          attempt: 2,
          max_attempts: 3,
          source_run_id: 'run-original',
          source_message_id: 'msg-failed',
        },
      }),
    ).toMatchObject({
      label: '修复开始：第 2/3 次',
      repairAttempt: 2,
      repairSourceRunId: 'run-original',
      repairSourceMessageId: 'msg-failed',
    })

    expect(
      timelineItem({
        event_type: 'repair.workflow',
        payload: { status: 'rejected', attempt: 4, max_attempts: 3 },
      }),
    ).toMatchObject({
      label: '修复已停止：第 4/3 次',
      repairAttempt: 4,
    })
  })

  it('renders run-scoped permission approvals and automatic approvals', () => {
    expect(timelineItem({ event_type: 'permission.resolved', payload: { request_id: 'perm-shell', decision: 'approve', tool: 'shell.run', scope: 'run' } })).toMatchObject({
      label: '本会话已允许：运行命令',
      permissionScope: 'run',
    })
    expect(timelineItem({ event_type: 'permission.auto_approved', payload: { request_id: 'perm-auto-rule', tool: 'shell.run', source: 'rule', reason: '工作区内操作', scope: 'run' } })).toMatchObject({
      label: '规则自动允许：运行命令',
      permissionRequestId: 'perm-auto-rule',
      permissionSource: 'rule',
      permissionReason: '工作区内操作',
      permissionScope: 'run',
    })
    expect(timelineItem({ event_type: 'permission.auto_approved', payload: { request_id: 'perm-auto-llm', tool: 'shell.run', source: 'llm', reason: '与当前任务一致', scope: 'run' } })).toMatchObject({
      label: '智能自动允许：运行命令',
      permissionRequestId: 'perm-auto-llm',
      permissionSource: 'llm',
      permissionReason: '与当前任务一致',
      permissionScope: 'run',
    })
  })

  it('renders mid-run steering injection events', () => {
    expect(
      timelineItem({
        event_type: 'steering.injected',
        payload: { count: 1 },
      }),
    ).toMatchObject({
      type: 'steering.injected',
      label: '已追加指示到当前任务',
    })
  })

  it('renders plan approval lifecycle events', () => {
    expect(
      timelineItem({
        event_type: 'plan.approval_required',
        payload: {
          request_id: 'plan-1',
          todos: [
            { content: 'Write tests', status: 'pending' },
            { content: 'Implement fix', status: 'pending' },
          ],
        },
      }),
    ).toMatchObject({
      type: 'plan.approval_required',
      label: '等待你批准计划',
      planApprovalRequestId: 'plan-1',
      planTodos: [
        { content: 'Write tests', status: 'pending' },
        { content: 'Implement fix', status: 'pending' },
      ],
    })

    expect(
      timelineItem({
        event_type: 'plan.approval_resolved',
        payload: { request_id: 'plan-1', decision: 'modify' },
      }),
    ).toMatchObject({
      type: 'plan.approval_resolved',
      label: '计划需要修改',
      planApprovalRequestId: 'plan-1',
      planApprovalDecision: 'modify',
    })
  })

  // Regression: bare-string options used to be silently filtered out
  // because parseQuestionPayload required `option.label`. The runtime
  // now normalizes to {label} at its boundary, but the parser stays
  // tolerant so any future emitter / older runtime still works.
  describe('question.asked option-shape tolerance', () => {
    it('keeps a free-text question when the agent provides no preset options', () => {
      const item = timelineItem({
        event_type: 'question.asked',
        payload: {
          request_id: 'q-free-text',
          questions: [
            {
              question: '请告诉我需要补充的内容',
              options: [],
            },
          ],
        },
      })

      expect(item).not.toBeNull()
      expect(item?.questions).toEqual([
        {
          question: '请告诉我需要补充的内容',
          header: '',
          multiSelect: false,
          options: [],
        },
      ])
    })

    it('accepts options as plain strings (legacy runtime shape)', () => {
      const item = timelineItem({
        event_type: 'question.asked',
        payload: {
          request_id: 'q1',
          questions: [
            {
              question: '你想在普吉岛待几天？',
              options: ['3天', '5天', '7天'],
            },
          ],
        },
      })
      expect(item).not.toBeNull()
      expect(item?.questions?.[0].options).toEqual([
        { label: '3天' },
        { label: '5天' },
        { label: '7天' },
      ])
    })

    it('accepts options as {label, description?} objects (canonical shape)', () => {
      const item = timelineItem({
        event_type: 'question.asked',
        payload: {
          request_id: 'q2',
          questions: [
            {
              question: '选择模式',
              options: [
                { label: 'Fast', description: '快速回答' },
                { label: 'Pro' },
              ],
            },
          ],
        },
      })
      expect(item?.questions?.[0].options).toEqual([
        { label: 'Fast', description: '快速回答' },
        { label: 'Pro' },
      ])
    })

    it('drops empty strings and option-objects with no label', () => {
      const item = timelineItem({
        event_type: 'question.asked',
        payload: {
          request_id: 'q3',
          questions: [
            {
              question: '混杂输入',
              options: ['  valid  ', '', { label: '', description: 'no label' }, { label: '保留' }],
            },
          ],
        },
      })
      expect(item?.questions?.[0].options).toEqual([{ label: 'valid' }, { label: '保留' }])
    })
  })

  // Rich per-tool primary-argument badge. The renderer reads
  // `event.toolDetail` and draws "{label} · {detail.text}" — with an
  // optional globe icon for web tools. These tests pin the mapping
  // from raw runtime args to the displayed shape.
  describe('toolDetail per tool', () => {
    it('web.search → text + truncated query, no icon', () => {
      const detail = toolDetail({ arguments: { query: '普吉岛雨季天气' } }, 'web.search')
      expect(detail).toEqual({
        kind: 'text',
        text: '普吉岛雨季天气',
        tooltip: '普吉岛雨季天气',
      })
    })

    it('web.fetch → host with globe icon + url tooltip', () => {
      const detail = toolDetail(
        { arguments: { url: 'https://weather.com/today?city=phuket' } },
        'web.fetch',
      )
      expect(detail).toEqual({
        kind: 'host',
        text: 'weather.com',
        tooltip: 'https://weather.com/today?city=phuket',
        showWebIcon: true,
      })
    })

    it('web.fetch with www.X.com strips the www prefix', () => {
      const detail = toolDetail({ arguments: { url: 'https://www.example.com/' } }, 'web.fetch')
      expect(detail?.text).toBe('example.com')
    })

    it('web.fetch falls back to truncated raw URL when URL parsing fails', () => {
      const detail = toolDetail({ arguments: { url: 'not a real url ::: !!!' } }, 'web.fetch')
      expect(detail?.kind).toBe('text') // no host extraction, no globe icon
      expect(detail?.showWebIcon).toBeUndefined()
    })

    it('read_file → basename + full path tooltip from deepagents file_path', () => {
      const detail = toolDetail(
        { arguments: { file_path: '/Users/me/project/src/App.tsx' } },
        'read_file',
      )
      expect(detail).toEqual({
        kind: 'text',
        text: 'App.tsx',
        tooltip: '/Users/me/project/src/App.tsx',
      })
    })

    it('ls / fs.list / workspace.open get a trailing slash on the basename', () => {
      const args = { arguments: { path: '/Users/me/project/src' } }
      expect(toolDetail(args, 'ls')?.text).toBe('src/')
      expect(toolDetail(args, 'fs.list')?.text).toBe('src/')
      expect(toolDetail(args, 'workspace.open')?.text).toBe('src/')
      // read_file etc do NOT get a slash
      expect(toolDetail(args, 'read_file')?.text).toBe('src')
    })

    it('execute / shell.run → command, truncated to 40 chars', () => {
      const command = 'ls -la /tmp/some/very/long/path/and/then/some/more'
      const detail = toolDetail({ arguments: { command } }, 'execute')
      expect(detail?.text.length).toBeLessThanOrEqual(40)
      expect(detail?.text).toContain('ls -la')
      expect(detail?.tooltip).toBe(command)
    })

    it('user.ask → question, tighter 30-char truncation', () => {
      const longQ = 'A'.repeat(120)
      const detail = toolDetail({ arguments: { question: longQ } }, 'user.ask')
      expect(detail?.text.length).toBeLessThanOrEqual(30)
      expect(detail?.text.endsWith('…')).toBe(true)
    })

    it('write_todos → count-kind with todos.length', () => {
      const detail = toolDetail(
        { arguments: { todos: [1, 2, 3, 4, 5] } },
        'write_todos',
      )
      expect(detail).toEqual({ kind: 'count', text: '5' })
    })

    it('task.verify → count-kind with checks.length', () => {
      const detail = toolDetail(
        { arguments: { checks: [{ kind: 'file_exists' }, { kind: 'shell_exit_code' }] } },
        'task.verify',
      )
      expect(detail).toEqual({ kind: 'count', text: '2' })
    })

    it('no args + no payload fallback → undefined (renderer shows just the verb)', () => {
      expect(toolDetail({}, 'time.now')).toBeUndefined()
      expect(toolDetail({})).toBeUndefined()
    })

    it('falls through to payload.title for source.collected / browser.observed', () => {
      const detail = toolDetail({ title: '普吉岛旅行攻略 2025 - Klook' })
      expect(detail?.kind).toBe('text')
      expect(detail?.text).toContain('普吉岛')
    })

    it('image.generate → prompt text', () => {
      const detail = toolDetail(
        { arguments: { prompt: 'A sunset over Phuket beach', size: '1024x1024' } },
        'image.generate',
      )
      expect(detail?.kind).toBe('text')
      expect(detail?.text).toBe('A sunset over Phuket beach')
    })

    it('subagent task call uses real deepagents arg names (description + subagent_type)', () => {
      const detail = toolDetail(
        { arguments: { subagent_type: 'researcher', description: 'Find current Phuket weather' } },
        'task',
      )
      // description (truncated) wins over subagent_type for the visible
      // text — the user-facing badge reads better as the actual prompt
      // than as the subagent label.
      expect(detail?.text).toContain('Phuket weather')
      // tooltip carries the full prompt prefixed by the subagent type
      // so hover reveals which subagent was dispatched.
      expect(detail?.tooltip).toContain('researcher')
      expect(detail?.tooltip).toContain('Phuket weather')
    })

    it('task call back-compat: old field names (subagent_name + task_description) still render', () => {
      // Earlier versions of this file guessed the wrong field names.
      // Any persisted timeline items from that era should still render
      // sensibly — the aliases keep working.
      const detail = toolDetail(
        { arguments: { subagent_name: 'researcher', task_description: 'Find current Phuket weather' } },
        'task',
      )
      expect(detail?.text).toContain('Phuket weather')
    })

    it('task call with only subagent_type falls back to subagent name', () => {
      const detail = toolDetail({ arguments: { subagent_type: 'writer' } }, 'task')
      expect(detail?.text).toBe('writer')
    })
  })
})
