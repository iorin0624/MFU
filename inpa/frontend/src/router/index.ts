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
import FeedbackView from '@/views/FeedbackView.vue'
import UpdatesView from '@/views/UpdatesView.vue'
import PersonView from '@/views/PersonView.vue'
import OnboardingView from '@/views/OnboardingView.vue'
import { api } from '@/lib/api'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView },
    { path: '/login', name: 'login', component: LoginView },
    { path: '/register', name: 'register', component: RegisterView },
    { path: '/register/complete', name: 'register-complete', component: RegisterCompleteView },
    { path: '/onboarding', name: 'onboarding', component: OnboardingView },
    { path: '/profile', name: 'profile', component: ProfileView },
    { path: '/visits', name: 'visits', component: VisitsView },
    { path: '/share', name: 'share-manage', component: ShareManageView },
    { path: '/share/:token', name: 'shared', component: SharedView },
    { path: '/connections', name: 'connections', component: ConnectionsView },
    { path: '/people/:publicId', name: 'person', component: PersonView },
    { path: '/calendar', name: 'calendar', component: CalendarView },
    { path: '/feedback', name: 'feedback', component: FeedbackView },
    { path: '/updates', name: 'updates', component: UpdatesView },
    { path: '/legal/consent', name: 'legal-consent', component: LegalConsentView },
    { path: '/legal/:type(terms|privacy)', name: 'legal-document', component: LegalDocumentView },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  if (to.path === '/legal/consent' || to.path.startsWith('/legal/')) return true
  try {
    const result = await api<{ user: { legal_consent_required: boolean } }>('/auth/me')
    if (result.user.legal_consent_required) {
      return { path: '/legal/consent', query: { next: to.fullPath }, replace: true }
    }
    const onboarding = await api<{ required: boolean }>('/onboarding')
    if (onboarding.required && to.path !== '/onboarding' && to.path !== '/register/complete') {
      return { path: '/onboarding', replace: true }
    }
    if (!onboarding.required && to.path === '/onboarding') return { path: '/', replace: true }
  } catch {
    // Public routes and each protected API retain their existing authentication handling.
  }
  return true
})

export default router
