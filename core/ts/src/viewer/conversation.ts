/**
 * The chat hooks. Nothing writes the AI conversation yet (S7), so these read the
 * real fields (`RebuiltState.chat`, an event's `links.chatTurnId`) and show a calm
 * placeholder when they are empty. They light up as soon as the chat is recorded.
 * All text is set as text.
 */
import type { ChatMessageState } from 'blaym-notebook-types';

export const CHAT_PLACEHOLDER = 'AI conversation · coming in S7';

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string
): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (className) {
    e.className = className;
  }
  if (text !== undefined) {
    e.textContent = text;
  }
  return e;
}

const role = (m: ChatMessageState): string =>
  m.senderKind === 'person' ? 'user' : 'AI';

function time(ms: number): string {
  return new Date(ms).toISOString().slice(5, 19).replace('T', ' ');
}

function message(m: ChatMessageState): HTMLElement {
  const row = el('div', 'bv-chat-message');
  const head = el('div', 'bv-chat-head');
  head.append(
    el(
      'span',
      `bv-chip bv-chip-${m.senderKind === 'person' ? 'you' : 'ai'}`,
      role(m)
    ),
    el('span', 'bv-muted', time(m.chatTimeMs))
  );
  row.append(head, el('div', 'bv-chat-text', m.body));
  return row;
}

/** The conversation as it stood at the shown moment: the messages, or the placeholder. */
export function renderConversation(
  target: HTMLElement,
  chat: readonly ChatMessageState[] | undefined
): void {
  target.replaceChildren();
  if (!chat || chat.length === 0) {
    target.append(el('p', 'bv-muted bv-chat-placeholder', CHAT_PLACEHOLDER));
    return;
  }
  for (const m of chat) {
    target.append(message(m));
  }
}

/** The "prompt" section of the context panel: the turn linked to the selected change, or the placeholder. */
export function promptSection(
  chat: readonly ChatMessageState[] | undefined,
  chatTurnId: string | null | undefined
): HTMLElement {
  const section = el('div', 'bv-prompt');
  section.append(el('div', 'bv-label', 'prompt'));
  if (!chatTurnId) {
    section.append(el('p', 'bv-muted bv-chat-placeholder', CHAT_PLACEHOLDER));
    return section;
  }
  const turn = chat?.find(m => m.messageId === chatTurnId);
  if (turn) {
    section.append(message(turn));
  } else {
    section.append(
      el('p', 'bv-muted', `linked to chat turn ${chatTurnId} (not loaded)`)
    );
  }
  return section;
}
