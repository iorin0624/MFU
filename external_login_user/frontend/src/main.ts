import { createApp } from 'vue';
import { createPinia } from 'pinia';
import App from '@/App.vue';
import { router } from '@/router';
import '@/styles.css';

const app = createApp(App).use(createPinia()).use(router);

// Resolve the initial URL before App chooses the MFU/external chat scope.
router.isReady().then(() => app.mount('#event-portal-app'));
