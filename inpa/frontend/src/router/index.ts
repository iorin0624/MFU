import { createRouter, createWebHistory } from 'vue-router'

import HomeView from '@/views/HomeView.vue'
import LoginView from '@/views/LoginView.vue'
import RegisterCompleteView from '@/views/RegisterCompleteView.vue'
import RegisterView from '@/views/RegisterView.vue'
import ProfileView from '@/views/ProfileView.vue'
import VisitsView from '@/views/VisitsView.vue'
import ShareManageView from '@/views/ShareManageView.vue'
import SharedView from '@/views/SharedView.vue'
import ConnectionsView from '@/views/ConnectionsView.vue'
import CalendarView from '@/views/CalendarView.vue'
import LegalDocumentView from '@/views/LegalDocumentView.vue'
import LegalConsentView from '@/views/LegalConsentView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView },
    { path: '/login', name: 'login', component: LoginView },
    { path: '/register', name: 'register', component: RegisterView },
    { path: '/register/complete', name: 'register-complete', component: RegisterCompleteView },
    { path: '/profile', name: 'profile', component: ProfileView },
    { path: '/visits', name: 'visits', component: VisitsView },
    { path: '/share', name: 'share-manage', component: ShareManageView },
    { path: '/share/:token', name: 'shared', component: SharedView },
    { path: '/connections', name: 'connections', component: ConnectionsView },
    { path: '/calendar', name: 'calendar', component: CalendarView },
    { path: '/legal/consent', name: 'legal-consent', component: LegalConsentView },
    { path: '/legal/:type(terms|privacy)', name: 'legal-document', component: LegalDocumentView },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

export default router
