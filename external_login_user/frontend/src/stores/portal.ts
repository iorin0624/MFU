import { defineStore } from 'pinia';
import { portalApi, setChatAuthScope as configureChatAuthScope, setCsrfToken } from '@/api/client';
import type { ChatVueSession, EventItem, PortalSession } from '@/types';
import { setPortalChatAuthScope } from '@/services/portalRealtime';
import { updatePwaBadge, type NotificationScope } from '@/services/pwaBadge';

export const usePortalStore = defineStore('portal', {
  state: () => ({
    session: null as PortalSession | null,
    events: [] as EventItem[],
    loading: false,
    ready: false,
    error: '',
    chatAuthScope: '' as '' | 'mfu',
    chatSession: null as ChatVueSession | null,
  }),
  getters: {
    displayName: (state) => state.session?.profile?.nickname || state.chatSession?.actor?.display_name || '',
    mfuChatAuthenticated: (state) => Boolean(
      state.chatAuthScope === 'mfu'
      && state.chatSession?.authenticated
      && ['admin', 'acl'].includes(String(state.chatSession?.actor?.actor_type || ''))
    ),
    effectiveNotificationScope: (state): NotificationScope => (
      state.chatAuthScope === 'mfu'
      && state.chatSession?.authenticated
      && ['admin', 'acl'].includes(String(state.chatSession?.actor?.actor_type || ''))
        ? 'mfu'
        : (state.session?.notificationScope === 'mfu' ? 'mfu' : 'external')
    ),
    notificationAuthenticated(): boolean {
      return Boolean(this.session?.authenticated || this.mfuChatAuthenticated);
    },
  },
  actions: {
    setChatAuthScope(scope: string) {
      this.chatAuthScope = scope === 'mfu' ? 'mfu' : '';
      configureChatAuthScope(this.chatAuthScope);
      setPortalChatAuthScope(this.chatAuthScope);
    },
    async bootstrap(force = false) {
      if (this.loading || (this.ready && !force)) return;
      this.loading = true;
      this.error = '';
      try {
        const response = await portalApi.bootstrap();
        this.session = response.session;
        this.events = response.events;
        setCsrfToken(response.session.csrfToken);
        this.chatSession = this.chatAuthScope === 'mfu' ? await portalApi.chatSession() : null;
        this.ready = true;
        this.applyUnread(response.session.unread);
        if (this.mfuChatAuthenticated) await this.refreshUnread();
      } catch (error) {
        this.error = error instanceof Error ? error.message : '初期データを取得できませんでした。';
      } finally {
        this.loading = false;
      }
    },
    async refreshEvents(scope = 'all') {
      const response = await portalApi.events(scope);
      this.events = response.events;
    },
    applyUnread(payload: Partial<{ total: number; notifications: number; chat: number }>) {
      if (!this.session) return;
      const current = this.session.unread || { total: 0, notifications: 0, chat: 0 };
      const notifications = Math.max(0, Number(payload.notifications ?? current.notifications ?? 0));
      const chat = Math.max(0, Number(payload.chat ?? current.chat ?? 0));
      this.session.unread = {
        notifications,
        chat,
        total: Math.max(0, Number(payload.total ?? (notifications + chat))),
      };
      void updatePwaBadge(this.session.unread.total, this.effectiveNotificationScope);
    },
    async refreshUnread() {
      if (!this.notificationAuthenticated) return;
      const counts = await portalApi.notificationUnread(this.effectiveNotificationScope);
      this.applyUnread(counts);
    },
    async logout() {
      await portalApi.logout();
      await updatePwaBadge(0, this.effectiveNotificationScope);
      this.session = null;
      this.events = [];
      window.location.assign('/external-login/app/login');
    },
  },
});
