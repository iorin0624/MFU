<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { onBeforeRouteLeave } from 'vue-router';
import { uploadMediaWithProgress } from '@/api/client';
import { buildUploadBatches, uploadPercent } from '@/utils/albumUpload';

const props = defineProps<{albumId:string;childId:string;childName:string;files:File[]}>();
const emit = defineEmits<{close:[];completed:[childId:string]}>();
const batches = computed(() => buildUploadBatches(props.files));
const totalBytes = computed(() => props.files.reduce((sum,file)=>sum+file.size,0));
const busy = ref(false); const done = ref(false); const error = ref('');
const completedFiles = ref(0); const batchNumber = ref(0); const percent = ref(0);
const page = ref(0); const previews = ref<Array<{file:File;url:string}>>([]);
const pageSize = 40;
const controller = new AbortController();
let disposed = false;
function releasePreviews() { previews.value.forEach(item => { if(item.url) URL.revokeObjectURL(item.url); }); }
watch(page, () => {
  releasePreviews();
  previews.value = props.files.slice(page.value*pageSize,(page.value+1)*pageSize).map(file=>({file,url:file.type.startsWith('image/') ? URL.createObjectURL(file) : ''}));
}, {immediate:true});
function beforeUnload(event:BeforeUnloadEvent) { if(busy.value){event.preventDefault();event.returnValue='';} }
window.addEventListener('beforeunload',beforeUnload);
onBeforeRouteLeave(() => !busy.value);
onBeforeUnmount(()=>{disposed=true;controller.abort();releasePreviews();window.removeEventListener('beforeunload',beforeUnload);});
async function start() {
  if(busy.value || done.value || error.value)return;
  busy.value=true;
  let completedBytes=0;
  try {
    for(let index=0;index<batches.value.length;index++){
      if(disposed)return;
      const batch=batches.value[index];
      const bytes=batch.reduce((sum,file)=>sum+file.size,0);
      batchNumber.value=index+1;
      await uploadMediaWithProgress(props.albumId,props.childId,batch,(fraction)=>{percent.value=uploadPercent(completedBytes,bytes*fraction,totalBytes.value);},controller.signal);
      completedBytes+=bytes;completedFiles.value+=batch.length;
      percent.value=uploadPercent(completedBytes,0,totalBytes.value);
    }
    percent.value=100;done.value=true;
  } catch(reason){error.value=reason instanceof Error?reason.message:'アップロードできませんでした。';}
  finally{busy.value=false;if(!disposed)emit('completed',props.childId);}
}
</script>
<template>
  <div class="modal-backdrop" @click.self="!busy && emit('close')" @keydown.esc="!busy && emit('close')">
    <section class="modal-card album-upload-dialog" role="dialog" aria-modal="true" aria-label="アップロード確認">
      <h2>アップロード確認</h2><p>送信先：{{ childName }}</p>
      <p>{{ files.length }}ファイル・{{ (totalBytes / 1024 / 1024).toFixed(1) }} MB ／ {{ batches.length }}回に分割</p>
      <p class="muted">80ファイル／350MBを目安に分割します。350MBを超える単一ファイルは、そのファイルだけで送信します。</p>
      <div class="upload-preview-grid"><figure v-for="(item,index) in previews" :key="page*pageSize+index"><img v-if="item.url" :src="item.url" alt="送信前プレビュー" @error="($event.target as HTMLImageElement).style.visibility='hidden'"><span v-else class="upload-file-icon">{{item.file.type.startsWith('video/')?'🎬':'📄'}}</span><figcaption>{{item.file.name}}<small>{{(item.file.size/1024/1024).toFixed(1)}} MB</small></figcaption></figure></div>
      <div v-if="files.length>pageSize" class="form-actions"><button class="button secondary compact" :disabled="page===0" @click="page--">前へ</button><span>{{page+1}} / {{Math.ceil(files.length/pageSize)}}</span><button class="button secondary compact" :disabled="(page+1)*pageSize>=files.length" @click="page++">次へ</button></div>
      <div v-if="busy || done || error" role="status" aria-live="polite"><progress :value="percent" max="100"></progress><p>{{percent}}% ／ {{batchNumber}}・{{batches.length}}バッチ中 ／ {{completedFiles}}ファイル保存確認済み</p><p v-if="busy && percent>=99">サーバーで保存処理中です…</p><p v-if="done">アップロードが完了しました。</p></div>
      <div v-if="error" class="alert error">{{error}}<p>確認済みのバッチは再送しません。閉じて一覧を確認してから、必要なファイルだけを選び直してください。</p></div>
      <div class="form-actions"><button class="button secondary" :disabled="busy" @click="emit('close')">{{done||error?'閉じる':'キャンセル'}}</button><button v-if="!done && !error" class="button primary" :disabled="busy" @click="start">{{busy?'アップロード中…':'アップロード開始'}}</button></div>
    </section>
  </div>
</template>
