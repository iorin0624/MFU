<script setup lang="ts">
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { portalApi } from '@/api/client';
import { usePortalStore } from '@/stores/portal';

const email = ref(''); const pin = ref(''); const sent = ref(false); const busy = ref(false); const message = ref('');
const router = useRouter(); const store = usePortalStore();
async function send() { busy.value = true; try { const r = await portalApi.requestLoginPin(email.value); sent.value = true; message.value = r.message; } catch (e) { message.value = e instanceof Error ? e.message : 'PINを送信できません。'; } finally { busy.value = false; } }
async function login() { busy.value = true; try { await portalApi.loginWithPin(email.value, pin.value); await store.bootstrap(true); await router.push('/'); } catch (e) { message.value = e instanceof Error ? e.message : 'ログインできません。'; } finally { busy.value = false; } }
</script>
<template>
  <section class="auth-shell"><div class="auth-card"><p class="eyebrow">SIGN IN</p><h1>ログイン</h1><p>普段はLINEログインをご利用ください。</p><a class="button line wide" href="/external-login/line/login">トーク LINEでログイン</a><div class="auth-divider"><span>またはメールPIN</span></div><label>メールアドレス<input v-model="email" type="email" autocomplete="email"></label><button class="button secondary wide" :disabled="busy || !email" @click="send">6桁のPINを送信</button><template v-if="sent"><label>PINコード<input v-model="pin" inputmode="numeric" maxlength="6" autocomplete="one-time-code"></label><button class="button primary wide" :disabled="busy || pin.length !== 6" @click="login">ログイン</button></template><div v-if="message" class="inline-notice">{{ message }}</div></div></section>
</template>
