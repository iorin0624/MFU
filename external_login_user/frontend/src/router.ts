import { createRouter, createWebHistory } from 'vue-router';
import { runtimeConfig } from '@/config';
import EventListView from '@/views/EventListView.vue';
import EventDetailView from '@/views/EventDetailView.vue';
import AlbumView from '@/views/AlbumView.vue';

export const router = createRouter({
  history: createWebHistory(runtimeConfig.basePath.replace(/\/$/, '')),
  scrollBehavior: () => ({ top: 0 }),
  routes: [
    { path: '/', name: 'events', component: EventListView },
    { path: '/events/:uuid', name: 'event', component: EventDetailView },
    { path: '/albums/:albumId', name: 'album', component: AlbumView },
    {
      path: '/albums/:albumId/children/:childId',
      redirect: (to) => ({ path: `/albums/${String(to.params.albumId)}`, query: { child: String(to.params.childId) } }),
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
});
