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
    displayName: (state) => state.session?.profile?.nickname || state.session?.mfuUsername || '',
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
    async logout() {
      await portalApi.logout();
      this.session = null;
      this.events = [];
      window.location.assign('/external-login/');
    },
  },
});
