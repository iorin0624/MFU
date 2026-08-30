<script setup lang="ts">
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { portalApi } from '@/api/client';
import { usePortalStore } from '@/stores/portal';
const pin = ref(''); const message = ref(''); const busy = ref(false); const router = useRouter(); const store = usePortalStore();
async function send() { busy.value = true; try { const r = await portalApi.sendEmailVerification(); message.value = `${r.email} へ確認コードを送信しました。`; } catch (e) { message.value = e instanceof Error ? e.message : '送信できません。'; } finally { busy.value = false; } }
async function verify() { busy.value = true; try { const result = await portalApi.verifyEmail(pin.value); await store.bootstrap(true); if (result.nextUrl) window.location.assign(result.nextUrl); else await router.push('/'); } catch (e) { message.value = e instanceof Error ? e.message : '確認できません。'; } finally { busy.value = false; } }
</script>
<template><section class="auth-shell"><div class="auth-card"><p class="eyebrow">EMAIL VERIFY</p><h1>メール認証</h1><p>登録済みメールアドレスに送信する6桁のコードを入力してください。</p><button class="button secondary wide" :disabled="busy" @click="send">確認コードを送信</button><label>6桁の確認コード<input v-model="pin" inputmode="numeric" maxlength="6" autocomplete="one-time-code"></label><button class="button primary wide" :disabled="busy || pin.length !== 6" @click="verify">認証する</button><div v-if="message" class="inline-notice">{{ message }}</div></div></section></template>
