<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue';
import { useRouter } from 'vue-router';
import { portalApi } from '@/api/client';
import { usePortalStore } from '@/stores/portal';

const router = useRouter(); const store = usePortalStore();
const form = reactive({ nickname: '', xId: '', instagramId: '', email: '', paymentMode: 'manual', notifyAlbumUpload: true, notifyAlbumProcess: true, hasCard: false, cardSummary: '' });
const avatar = ref(''); const avatarFile = ref<File | null>(null); const busy = ref(false); const message = ref(''); const error = ref('');
onMounted(async () => { try { const r = await portalApi.profile(); const p = r.profile as any; Object.assign(form, p); avatar.value = p.avatarUrl || ''; } catch (e) { error.value = e instanceof Error ? e.message : 'プロフィールを取得できません。'; } });
function choose(files: FileList | null) { const file = files?.[0]; if (!file) return; avatarFile.value = file; avatar.value = URL.createObjectURL(file); }
async function save() { busy.value = true; error.value = ''; message.value = ''; try { const data = new FormData(); Object.entries(form).forEach(([key, value]) => data.append(key, typeof value === 'boolean' ? (value ? '1' : '0') : String(value ?? ''))); if (avatarFile.value) data.append('avatar', avatarFile.value); const r = await portalApi.saveProfile(data); message.value = r.emailVerificationRequired ? '保存しました。新しいメールアドレスへ確認コードを送信しました。' : '保存しました。'; await store.bootstrap(true); } catch (e) { error.value = e instanceof Error ? e.message : '保存できません。'; } finally { busy.value = false; } }
</script>
<template>
  <button class="back-link" type="button" @click="router.push('/')">← イベント一覧</button>
  <section class="page-heading"><div><p class="eyebrow">ACCOUNT</p><h1>プロフィール編集</h1><p>イベントで使用する表示名や連絡先を管理します。</p></div></section>
  <form class="profile-form" @submit.prevent="save">
    <section class="panel profile-avatar"><img v-if="avatar" :src="avatar" alt=""><div><strong>プロフィール画像</strong><label class="button secondary compact">画像を選択<input type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden @change="choose(($event.target as HTMLInputElement).files)"></label></div></section>
    <section class="panel form-grid"><h2>基本情報</h2><label>ニックネーム<input v-model="form.nickname" required maxlength="100"></label><label>X ID（@なし）<input v-model="form.xId" maxlength="15"></label><label>Instagram ID（@なし）<input v-model="form.instagramId" maxlength="30"></label><label class="wide-field">メールアドレス<input v-model="form.email" type="email" required><small>変更すると再度メール確認が必要です。</small></label></section>
    <section class="panel"><h2>決済方法</h2><div class="choice-row"><label><input v-model="form.paymentMode" type="radio" value="manual">手動決済</label><label><input v-model="form.paymentMode" type="radio" value="auto" :disabled="!form.hasCard">自動決済</label></div><p class="muted">{{ form.cardSummary || '自動決済用カードは未登録です。' }}</p></section>
    <section class="panel"><h2>通知設定</h2><label class="toggle-line"><input v-model="form.notifyAlbumUpload" type="checkbox">アルバムのアップロード通知</label><label class="toggle-line"><input v-model="form.notifyAlbumProcess" type="checkbox">加工依頼・加工完了の通知</label></section>
    <div v-if="error" class="alert error">{{ error }}</div><div v-if="message" class="alert success">{{ message }}</div><div class="form-actions"><button type="button" class="button secondary" @click="router.push('/')">キャンセル</button><button class="button primary" :disabled="busy">{{ busy ? '保存中…' : '保存' }}</button></div>
  </form>
</template>
