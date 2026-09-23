<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/lib/api'
const route=useRoute(); const router=useRouter(); const documents=ref<Record<string, any>>({})
const terms=ref(false); const privacy=ref(false); const error=ref(''); const pending=ref(false)
onMounted(async()=>{ try { documents.value=(await api<{documents:Record<string,any>}>('/legal/documents')).documents } catch { error.value='文書を読み込めません。' } })
async function submit(){ pending.value=true; error.value=''; try { await api('/legal/consents',{method:'POST',body:{terms_accepted:terms.value,privacy_accepted:privacy.value,terms_document_id:documents.value.terms.public_id,privacy_document_id:documents.value.privacy.public_id}}); await router.push(typeof route.query.next==='string'?route.query.next:'/') } catch(cause:any){error.value=cause?.message??'保存に失敗しました。'} finally {pending.value=false} }
</script>
<template><section class="panel"><h1>利用規約等の更新</h1><p>サービスを続けてご利用いただくには、現行版をご確認ください。</p><form class="form-stack" @submit.prevent="submit"><label class="field-check"><input v-model="terms" type="checkbox" required><RouterLink to="/legal/terms" target="_blank">利用規約</RouterLink>に同意する</label><label class="field-check"><input v-model="privacy" type="checkbox" required><RouterLink to="/legal/privacy" target="_blank">プライバシーポリシー</RouterLink>に同意する</label><p v-if="error" class="error">{{error}}</p><button class="button" :disabled="pending">同意して続ける</button></form></section></template>
