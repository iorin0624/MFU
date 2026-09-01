import { defineStore } from 'pinia';
import { portalApi, setCsrfToken } from '@/api/client';
import type { EventItem, PortalSession } from '@/types';

export const usePortalStore = defineStore('portal', {
  state: () => ({
    session: null as PortalSession | null,
    events: [] as EventItem[],
    loading: false,
    ready: false,
    error: '',
  }),
  getters: {
    displayName: (state) => state.session?.profile?.nickname || '',
  },
  actions: {
    async bootstrap(force = false) {
      if (this.loading || (this.ready && !force)) return;
      this.loading = true;
      this.error = '';
      try {
        const response = await portalApi.bootstrap();
        this.session = response.session;
        this.events = response.events;
        setCsrfToken(response.session.csrfToken);
        this.ready = true;
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
    },
    async refreshUnread() {
      if (!this.session?.authenticated) return;
      const counts = await portalApi.notificationUnread(this.session.notificationScope);
      this.applyUnread(counts);
    },
    async logout() {
      await portalApi.logout();
      this.session = null;
      this.events = [];
      window.location.assign('/external-login/app/login');
    },
  },
});
