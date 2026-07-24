import { useEffect, useMemo, useState } from 'react'
import { IconArrowLeft, IconArrowRight, IconCheck, IconPlayerStopFilled, IconPlayerTrackNext } from '@tabler/icons-react'
import { Button } from '@/components/ui/button'
import { useI18n } from '@/shared/i18n/i18n'
import type { PendingQuestion } from '../pendingQuestion'

const OTHER = '__other__'

type PendingQuestionBarProps = {
  question: PendingQuestion | null
  onAnswer: (messageID: string, requestID: string, answers: Record<string, string[]>) => void
  /** Skip the question — replies to the runtime with empty answers so
   *  the user.ask tool returns "" to the agent, letting the run
   *  continue without an explicit choice. */
  onSkip?: (messageID: string, requestID: string) => void
  /** Cancel the run entirely. The card disappears and the assistant
   *  message lands in `canceled` state. */
  onCancel?: (messageID: string) => void
}

type ActivePendingQuestionBarProps = Omit<PendingQuestionBarProps, 'question'> & {
  question: PendingQuestion
}

function usePendingQuestionBarViewModel({
  question,
  onAnswer,
  onSkip,
  onCancel,
}: ActivePendingQuestionBarProps) {
  const { t } = useI18n()
  const [selected, setSelected] = useState<Record<number, string[]>>({})
  const [otherText, setOtherText] = useState<Record<number, string>>({})
  const [step, setStep] = useState(0)

  const computeAnswers = (
    sel: Record<number, string[]>,
    oth: Record<number, string>,
  ): Record<string, string[]> | null => {
    if (!question) {
      return null
    }
    const result: Record<string, string[]> = {}
    for (let qi = 0; qi < question.questions.length; qi += 1) {
      const q = question.questions[qi]
      const picks = sel[qi] ?? []
      const labels = picks.filter((value) => value !== OTHER)
      const free = (oth[qi] ?? '').trim()
      if (free) {
        labels.push(free)
      }
      if (labels.length === 0) {
        return null
      }
      result[q.question] = labels
    }
    return result
  }

  const answers = useMemo(
    () => computeAnswers(selected, otherText),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [question, selected, otherText],
  )

  const total = question.questions.length
  const activeStep = Math.min(step, total - 1)
  const item = question.questions[activeStep]
  const multi = item.multiSelect === true
  const picks = selected[activeStep] ?? []
  const isLast = activeStep >= total - 1
  const otherValue = otherText[activeStep] ?? ''

  const finish = (sel: Record<number, string[]>, oth: Record<number, string>) => {
    const built = computeAnswers(sel, oth)
    if (built) {
      onAnswer(question.messageID, question.requestID, built)
    }
  }

  const advance = (sel: Record<number, string[]>, oth: Record<number, string>) => {
    if (isLast) {
      finish(sel, oth)
    } else {
      setStep(activeStep + 1)
    }
  }

  // Single-select: a click is the answer — commit and move on automatically.
  const chooseSingle = (value: string) => {
    const nextSelected = { ...selected, [activeStep]: [value] }
    const nextOther = { ...otherText, [activeStep]: '' }
    setSelected(nextSelected)
    setOtherText(nextOther)
    advance(nextSelected, nextOther)
  }

  const toggleMulti = (value: string) => {
    setSelected((current) => {
      const existing = current[activeStep] ?? []
      const next = existing.includes(value)
        ? existing.filter((entry) => entry !== value)
        : [...existing, value]
      return { ...current, [activeStep]: next }
    })
  }

  // The free-text path can't auto-advance, so it keeps an explicit confirm.
  const confirmOther = () => {
    const free = otherValue.trim()
    if (!free) {
      return
    }
    const nextSelected = { ...selected, [activeStep]: [OTHER] }
    setSelected(nextSelected)
    advance(nextSelected, otherText)
  }

  const currentAnswered = multi
    ? picks.length > 0 || otherValue.trim().length > 0
    : false

  /** Number-key shortcuts (1–9) map to options on the current question.
   *  Single-select: number picks + advances. Multi-select: number
   *  toggles the option. Skipped when the user is typing in an input
   *  / textarea / contenteditable so it doesn't fire while they're
   *  filling in the "Other" field or the composer. Re-attached every
   *  render so closures over `picks` / `multi` stay fresh. */
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) {
        return
      }
      const target = event.target as HTMLElement | null
      if (target?.matches?.('input, textarea, [contenteditable="true"]')) {
        return
      }
      const num = Number(event.key)
      if (!Number.isInteger(num) || num < 1 || num > 9) {
        return
      }
      const idx = num - 1
      if (idx >= item.options.length) {
        return
      }
      event.preventDefault()
      const value = item.options[idx].label
      if (multi) {
        toggleMulti(value)
      } else {
        chooseSingle(value)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  })

  return { activeStep, answers, chooseSingle, confirmOther, currentAnswered, finish, isLast, item, multi, onCancel, onSkip, otherText, otherValue, picks, question, selected, setOtherText, setStep, t, toggleMulti, total }
}

export function PendingQuestionBar(props: PendingQuestionBarProps) {
  return props.question ? <ActivePendingQuestionBar {...props} question={props.question} /> : null
}

function ActivePendingQuestionBar(props: ActivePendingQuestionBarProps) {
  return <PendingQuestionBarView view={usePendingQuestionBarViewModel(props)} />
}

function PendingQuestionBarView({ view }: { view: ReturnType<typeof usePendingQuestionBarViewModel> }) {
  const { activeStep, answers, chooseSingle, confirmOther, currentAnswered, finish, isLast, item, multi, onCancel, onSkip, otherText, otherValue, picks, question, selected, setOtherText, setStep, t, toggleMulti, total } = view
  const pickedLabels = new Set(picks)
  return (
    <div className="question-bar" role="region" aria-label={t('agent.question.title')}>
      <div className="question-bar-list">
        <div className="question-block" key={`${item.question}-${activeStep}`}>
          <div className="question-block-head">
            {/* `header` is the short chip-label the agent CAN attach to a
                question (e.g. "Days" / "Budget"). It's optional — when
                absent (the common case for user.ask), we don't render
                an empty chip so the question text isn't shifted right
                by a phantom box. */}
            {item.header ? <span className="question-chip">{item.header}</span> : null}
            <h3 className="question-text">{item.question}</h3>
            <div className="question-meta">
              {total > 1 ? (
                <span className="question-progress">
                  {t('agent.question.progress', { current: activeStep + 1, total })}
                </span>
              ) : null}
              {multi ? (
                <span className="question-multi-hint">{t('agent.question.multiHint')}</span>
              ) : null}
            </div>
          </div>
          <div className="question-options" role={multi ? 'group' : 'radiogroup'}>
            {item.options.map((option, idx) => {
              const active = pickedLabels.has(option.label)
              const shortcut = idx < 9 ? idx + 1 : null
              return (
                <button
                  type="button"
                  key={option.label}
                  className="question-option"
                  data-active={active}
                  role={multi ? 'checkbox' : 'radio'}
                  aria-checked={active}
                  aria-keyshortcuts={shortcut ? String(shortcut) : undefined}
                  onClick={() => (multi ? toggleMulti(option.label) : chooseSingle(option.label))}
                >
                  {shortcut ? (
                    <kbd className="question-option-key" aria-hidden="true">{shortcut}</kbd>
                  ) : null}
                  <span className="question-option-main">
                    <span className="question-option-label">{option.label}</span>
                    {option.description ? (
                      <span className="question-option-desc">{option.description}</span>
                    ) : null}
                  </span>
                  {active ? (
                    <IconCheck className="question-option-check" size={15} aria-hidden="true" />
                  ) : null}
                </button>
              )
            })}
          </div>
          {/* "Other" free-text is always visible — no need to pick an option
              first. It keeps an explicit confirm since text can't auto-submit. */}
          <div className="question-other-row">
            <input
              className="question-other-input"
              type="text"
              value={otherValue}
              placeholder={t('agent.question.otherPlaceholder')}
              aria-label={t('agent.question.other')}
              onChange={(event) =>
                setOtherText((current) => ({ ...current, [activeStep]: event.target.value }))
              }
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !multi) {
                  event.preventDefault()
                  confirmOther()
                }
              }}
            />
            {!multi ? (
              // Plain <button>, NOT the shadcn Button — the latter pins a
              // fixed h-7 (28px) via its `sm` size variant, which is
              // shorter than the input next to it. We want the confirm
              // to match the input's exact height so the row visually
              // reads as one control. `align-self: stretch` does that
              // via the parent's `align-items: stretch`.
              <button
                type="button"
                className="question-other-confirm"
                disabled={!otherValue.trim()}
                onClick={confirmOther}
                aria-label={isLast ? t('agent.question.submit') : t('agent.question.next')}
              >
                {isLast ? <IconCheck size={15} aria-hidden="true" /> : <IconArrowRight size={15} aria-hidden="true" />}
              </button>
            ) : null}
          </div>
        </div>
      </div>
      {/* Action row, always shown. Left side carries the escape
          hatches — Skip (continue the run without picking) and Cancel
          (kill the run). Right side carries the multi-step navigation
          when applicable (Back / Next / Submit). Single-select runs
          show only Skip + Cancel on the left and nothing on the right. */}
      <div className="question-bar-actions">
        <div className="question-bar-actions-left">
          {onCancel ? (
            <Button
              size="sm"
              variant="ghost"
              className="question-bar-cancel"
              onClick={() => onCancel(question.messageID)}
              aria-label={t('agent.question.cancel')}
            >
              <IconPlayerStopFilled size={14} aria-hidden="true" />
              {t('agent.question.cancel')}
            </Button>
          ) : null}
          {onSkip ? (
            <Button
              size="sm"
              variant="ghost"
              className="question-bar-skip"
              onClick={() => onSkip(question.messageID, question.requestID)}
              aria-label={t('agent.question.skip')}
            >
              <IconPlayerTrackNext size={14} aria-hidden="true" />
              {t('agent.question.skip')}
            </Button>
          ) : null}
        </div>
        <div className="question-bar-actions-right">
          {activeStep > 0 ? (
            <Button size="sm" variant="ghost" onClick={() => setStep(activeStep - 1)}>
              <IconArrowLeft size={14} />
              {t('agent.question.back')}
            </Button>
          ) : null}
          {multi ? (
            isLast ? (
              <Button size="sm" disabled={!answers} onClick={() => finish(selected, otherText)}>
                <IconCheck size={14} />
                {t('agent.question.submit')}
              </Button>
            ) : (
              <Button
                size="sm"
                disabled={!currentAnswered}
                onClick={() => setStep(activeStep + 1)}
              >
                {t('agent.question.next')}
                <IconArrowRight size={14} />
              </Button>
            )
          ) : null}
        </div>
      </div>
    </div>
  )
}
