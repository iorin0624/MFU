export function composerHeight(contentHeight: number, lineHeight: number, extras: number, mobile: boolean, availableHeight: number) {
  const minimum = lineHeight + extras;
  const maximum = Math.max(minimum, Math.min(lineHeight * (mobile ? 4 : 20) + extras, availableHeight));
  const height = Math.max(minimum, Math.min(contentHeight, maximum));
  return { height, overflow: contentHeight > height + 1 ? 'auto' : 'hidden' };
}

export function resizeChatComposer(textarea: HTMLTextAreaElement, shell: HTMLElement, list: HTMLElement, mobile: boolean) {
  const style = getComputedStyle(textarea);
  const number = (value: string) => parseFloat(value) || 0;
  const line = number(style.lineHeight) || 22;
  const border = number(style.borderTopWidth) + number(style.borderBottomWidth);
  const extras = number(style.paddingTop) + number(style.paddingBottom) + border;
  const form = textarea.closest('form')!;
  const formStyle = getComputedStyle(form);
  const formExtras = number(formStyle.paddingTop) + number(formStyle.paddingBottom)
    + number(formStyle.borderTopWidth) + number(formStyle.borderBottomWidth);
  const otherHeight = [...shell.children].reduce((sum, child) => {
    if (child === form || child === list || getComputedStyle(child).position === 'absolute') return sum;
    return sum + child.getBoundingClientRect().height;
  }, 0);
  // Reserve some message space even with the software keyboard open.
  const available = shell.clientHeight - otherHeight - formExtras - Math.min(48, shell.clientHeight * .15);
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 180;
  textarea.style.height = '0px';
  textarea.style.overflowY = 'hidden';
  const size = composerHeight(textarea.scrollHeight + border, line, extras, mobile, available);
  textarea.style.height = `${size.height}px`;
  textarea.style.overflowY = size.overflow;
  if (atBottom) list.scrollTop = list.scrollHeight;
  return Math.max(12, shell.getBoundingClientRect().bottom - list.getBoundingClientRect().bottom + 12);
}
