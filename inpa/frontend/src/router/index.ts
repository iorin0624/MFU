import { createRouter, createWebHistory } from 'vue-router'

import HomeView from '@/views/HomeView.vue'
import LoginView from '@/views/LoginView.vue'
import RegisterCompleteView from '@/views/RegisterCompleteView.vue'
import RegisterView from '@/views/RegisterView.vue'
import ProfileView from '@/views/ProfileView.vue'
import VisitsView from '@/views/VisitsView.vue'
import ShareManageView from '@/views/ShareManageView.vue'
import SharedView from '@/views/SharedView.vue'

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
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

export default router
