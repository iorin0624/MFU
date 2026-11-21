export const replyRoot = '..';

export function isReplyFolder(folder: string) {
  return folder === replyRoot || folder.startsWith(`${replyRoot}/`);
}
