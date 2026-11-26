import type {
  AuthorSignal,
  AuthorSignals,
  Clock,
  Disposer
} from '../../recording/types';

/** The part of JupyterLab's command registry the signals need. */
export interface CommandsLike {
  commandExecuted: {
    connect(cb: (sender: unknown, args: { id: string }) => void): unknown;
    disconnect(cb: (sender: unknown, args: { id: string }) => void): unknown;
  };
}

export interface SignalDeps {
  commands: CommandsLike;
  clock: Clock;
  /** The id of the cell being edited right now (the active cell of the current notebook), if any. */
  activeCellId: () => string | undefined;
  /** The provider named on the ghost text most recently seen on screen, if any. */
  lastProvider: () => string | undefined;
  /** Whether an inline completion provider is an AI one; default: its id names AI. */
  isAiProvider?: (providerId: string) => boolean;
}

const AI_COMMANDS = 'jupyterlab-ai-commands:';
const INLINE_ACCEPT = 'inline-completer:accept';

/** Provider ids that name an AI (for example `jupyter-ai:...`); any other provider is left unmarked. */
export function defaultIsAiProvider(id: string): boolean {
  return /jupyter[-_]?ai|(^|[^a-z])(ai|llm)([^a-z]|$)/i.test(id);
}

/**
 * Author signals for JupyterLab (S5-L): the AI tool commands, and accepting an inline
 * suggestion from an AI provider. The command runs in the tab that made the edit.
 * Anything else (a person typing, a provider that is not AI) sends no signal.
 */
export function createSignalsFor(d: SignalDeps): AuthorSignals {
  const isAi = d.isAiProvider ?? defaultIsAiProvider;
  return {
    onSignal(cb): Disposer {
      const handler = (_: unknown, args: { id: string }): void => {
        let ai = false;
        if (args.id.startsWith(AI_COMMANDS)) {
          ai = true;
        } else if (args.id === INLINE_ACCEPT) {
          const p = d.lastProvider();
          ai = p !== undefined && isAi(p);
        }
        if (ai) {
          const s: AuthorSignal = {
            kind: 'ai',
            confidence: 'known',
            atMs: d.clock.nowMs(),
            cellId: d.activeCellId()
          };
          cb(s);
        }
      };
      d.commands.commandExecuted.connect(handler);
      return () => void d.commands.commandExecuted.disconnect(handler);
    }
  };
}

interface AppLike {
  commands: CommandsLike;
  shell: { currentWidget?: unknown };
}

/** Wires the signals to the real page: the active cell and the ghost-text provider are read from the DOM and the shell. */
export function createSignals(app?: AppLike): AuthorSignals | undefined {
  if (!app || typeof document === 'undefined') {
    return undefined;
  }
  let provider: { id: string; at: number } | undefined;
  const look = (): void => {
    const el = document.querySelector('[data-provided-by]');
    const id = el?.getAttribute('data-provided-by');
    if (id) {
      provider = { id, at: Date.now() };
    }
  };
  new MutationObserver(look).observe(document.body, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ['data-provided-by']
  });
  return createSignalsFor({
    commands: app.commands,
    clock: { nowMs: () => Date.now() },
    activeCellId: () => {
      const w = app.shell.currentWidget as
        { content?: { activeCell?: { model?: { id?: string } } } } | undefined;
      return w?.content?.activeCell?.model?.id || undefined;
    },
    lastProvider: () =>
      provider && Date.now() - provider.at < 10000 ? provider.id : undefined
  });
}
