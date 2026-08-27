<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { runtimeConfig } from '@/config';
import { requestJson } from '@/api/client';

const loginId = ref('');
const password = ref('');
const passwordSaved = ref(false);
const vncPassword = ref('');
const status = ref('');
const busy = ref(false);

interface BrowserStartResult {
  ok: boolean;
  url?: string;
  vncPassword?: string;
  state?: string;
  message?: string;
}

async function startBrowser() {
  busy.value = true;
  status.value = 'Instagramログイン用ブラウザを起動中...';
  const popup = window.open('about:blank', 'mfu_instagram_login', 'width=1320,height=980');
  try {
    const data = await requestJson<BrowserStartResult>(runtimeConfig.instagramBrowserStartUrl, {
      method: 'POST', body: JSON.stringify({}),
    });
    if (data.url) {
      if (popup) popup.location.href = data.url;
      else window.open(data.url, 'mfu_instagram_login', 'width=1320,height=980');
    }
    vncPassword.value = data.vncPassword || '';
    if (data.state === 'logged_in') {
      status.value = 'Instagramにログイン済みです。ログイン情報も取得処理へ保存しました。';
    } else if (data.state === 'otp_required') {
      status.value = data.message || 'ID・パスワードは入力済みです。noVNCでOTPを入力してください。';
    } else {
      status.value = data.message || '認証情報を保存後、noVNCでInstagramの状態を確認してください。';
    }
  } catch (error) {
    popup?.close();
    status.value = error instanceof Error ? error.message : 'Instagramログイン用ブラウザを起動できませんでした。';
  } finally { busy.value = false; }
}

async function saveCredentials() {
  if (!loginId.value.trim() || !password.value) {
    status.value = 'Instagram IDとパスワードを入力してください。';
    return;
  }
  busy.value = true;
  status.value = 'Instagram認証情報を暗号化して保存中...';
  try {
    await requestJson(runtimeConfig.instagramCredentialsSaveUrl, {
      method: 'POST', body: JSON.stringify({ loginId: loginId.value.trim(), password: password.value }),
    });
    password.value = '';
    passwordSaved.value = true;
    status.value = '認証情報を保存しました。ブラウザー起動時に自動入力します。OTPだけnoVNCで入力してください。';
  } catch (error) {
    status.value = error instanceof Error ? error.message : 'Instagram認証情報を保存できませんでした。';
  } finally { busy.value = false; }
}

async function saveBrowserLogin() {
  busy.value = true;
  status.value = 'Instagramログイン情報を保存中...';
  try {
    const data = await requestJson<{ok: boolean; cookieCount?: number}>(runtimeConfig.instagramBrowserSaveUrl, {
      method: 'POST', body: JSON.stringify({}),
    });
    status.value = `Instagramログインを保存しました。Cookie ${data.cookieCount || 0}件`;
  } catch (error) {
    status.value = error instanceof Error ? error.message : 'Instagramログイン情報を保存できませんでした。';
  } finally { busy.value = false; }
}

async function copyVncPassword() {
  if (!vncPassword.value) return;
  try {
    await navigator.clipboard.writeText(vncPassword.value);
    status.value = 'VNC Passwordをコピーしました。';
  } catch {
    status.value = 'VNC Passwordをコピーできませんでした。手動で選択してください。';
  }
}

onMounted(async () => {
  if (runtimeConfig.isAdmin) {
    try {
      const data = await requestJson<{ok: boolean; configured?: boolean; login_id?: string}>(
        runtimeConfig.instagramCredentialsStatusUrl,
      );
      if (data.configured) {
        loginId.value = data.login_id || '';
        passwordSaved.value = true;
      }
    } catch { /* Browser login still works without credential editing permission. */ }
  }
  void startBrowser();
});
</script>

<template>
  <section class="instagram-auth-body">
    <div class="ig-auth-row">
      <button type="button" :disabled="busy" @click="startBrowser">noVNCを開く</button>
      <button type="button" :disabled="busy" @click="saveBrowserLogin">ログイン保存</button>
    </div>
    <div v-if="vncPassword" class="ig-auth-row">
      <input v-model="vncPassword" type="text" readonly aria-label="VNC Password">
      <button type="button" @click="copyVncPassword">コピー</button>
    </div>
    <div v-if="runtimeConfig.isAdmin" class="ig-auth-row">
      <input v-model="loginId" type="text" autocomplete="off" placeholder="Instagram ID / email">
    </div>
    <div v-if="runtimeConfig.isAdmin" class="ig-auth-row">
      <input v-model="password" type="password" autocomplete="new-password" :placeholder="passwordSaved ? '保存済み（変更時のみ入力）' : 'Instagram password'">
      <button type="button" :disabled="busy" @click="saveCredentials">認証情報を保存</button>
    </div>
    <div class="ig-auth-status">{{ status }}</div>
  </section>
</template>
