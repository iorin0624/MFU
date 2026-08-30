import { createRouter, createWebHistory } from 'vue-router';
import { runtimeConfig } from '@/config';
import EventListView from '@/views/EventListView.vue';
import EventDetailView from '@/views/EventDetailView.vue';
import EventPassView from '@/views/EventPassView.vue';
import EventMembersView from '@/views/EventMembersView.vue';
import EventSocialView from '@/views/EventSocialView.vue';
import AlbumView from '@/views/AlbumView.vue';
import NotificationsView from '@/views/NotificationsView.vue';
import ProfileView from '@/views/ProfileView.vue';
import LoginView from '@/views/LoginView.vue';
import EmailVerifyView from '@/views/EmailVerifyView.vue';
import JoinEventView from '@/views/JoinEventView.vue';
import EventPaymentView from '@/views/EventPaymentView.vue';

export const router = createRouter({
  history: createWebHistory(runtimeConfig.basePath.replace(/\/$/, '')),
  scrollBehavior: () => ({ top: 0 }),
  routes: [
    { path: '/', name: 'events', component: EventListView },
    { path: '/login', name: 'login', component: LoginView },
    { path: '/email-verify', name: 'email-verify', component: EmailVerifyView },
    { path: '/notifications', name: 'notifications', component: NotificationsView },
    { path: '/profile', name: 'profile', component: ProfileView },
    { path: '/events/:uuid/join', name: 'event-join', component: JoinEventView },
    { path: '/events/:uuid/payment', name: 'event-payment', component: EventPaymentView },
    { path: '/events/:uuid', name: 'event', component: EventDetailView },
    { path: '/events/:uuid/pass', name: 'event-pass', component: EventPassView },
    { path: '/events/:uuid/members', name: 'event-members', component: EventMembersView },
    { path: '/events/:uuid/social', name: 'event-social', component: EventSocialView },
    { path: '/albums/:albumId', name: 'album', component: AlbumView },
    {
      path: '/albums/:albumId/children/:childId',
      name: 'album-child',
      component: AlbumView,
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
});
