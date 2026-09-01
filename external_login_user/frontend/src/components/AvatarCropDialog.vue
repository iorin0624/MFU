<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue';
import { cropRect } from '@/utils/avatarCrop';
const props=defineProps<{file:File}>();
const emit=defineEmits<{close:[];apply:[file:File]}>();
const source=URL.createObjectURL(props.file);
const image=ref<HTMLImageElement|null>(null);
const surface=ref<HTMLElement|null>(null);
const width=ref(0);const height=ref(0);const zoom=ref(1);const x=ref(0);const y=ref(0);
const busy=ref(false);const error=ref('');
const rect=computed(()=>cropRect(width.value,height.value,zoom.value,x.value,y.value));
const style=computed(()=>width.value&&height.value?{
  width:`${width.value/rect.value.size*100}%`,height:`${height.value/rect.value.size*100}%`,
  left:`${-rect.value.x/rect.value.size*100}%`,top:`${-rect.value.y/rect.value.size*100}%`,
}:{});
const pointers=new Map<number,{x:number;y:number}>();
function distance(){const values=[...pointers.values()];return values.length===2?Math.hypot(values[0].x-values[1].x,values[0].y-values[1].y):0;}
function down(event:PointerEvent){if(event.button!==0)return;event.preventDefault();surface.value?.setPointerCapture(event.pointerId);pointers.set(event.pointerId,{x:event.clientX,y:event.clientY});}
function move(event:PointerEvent){
  const old=pointers.get(event.pointerId);if(!old)return;
  const before=distance();pointers.set(event.pointerId,{x:event.clientX,y:event.clientY});
  if(pointers.size===2){const after=distance();if(before)zoom.value=Math.max(1,Math.min(4,zoom.value*after/before));return;}
  const size=surface.value?.getBoundingClientRect().width||1;
  const factor=size/rect.value.size;
  const rangeX=(width.value-rect.value.size)*factor/2;
  const rangeY=(height.value-rect.value.size)*factor/2;
  if(rangeX)x.value=Math.max(-1,Math.min(1,x.value-(event.clientX-old.x)/rangeX));
  if(rangeY)y.value=Math.max(-1,Math.min(1,y.value-(event.clientY-old.y)/rangeY));
}
function up(event:PointerEvent){pointers.delete(event.pointerId);}
function loaded(){width.value=image.value?.naturalWidth||0;height.value=image.value?.naturalHeight||0;if(!width.value||!height.value)error.value='画像を読み込めませんでした。';}
async function apply(){
  if(!image.value||!width.value||busy.value)return;
  busy.value=true;
  try{
    const canvas=document.createElement('canvas');canvas.width=512;canvas.height=512;
    const context=canvas.getContext('2d');if(!context)throw new Error('画像を加工できません。');
    context.fillStyle='#fff';context.fillRect(0,0,512,512);
    context.drawImage(image.value,rect.value.x,rect.value.y,rect.value.size,rect.value.size,0,0,512,512);
    const blob=await new Promise<Blob>((resolve,reject)=>canvas.toBlob(value=>value?resolve(value):reject(new Error('画像を保存できません。')),'image/jpeg',0.92));
    emit('apply',new File([blob],'avatar.jpg',{type:'image/jpeg'}));
  }catch(reason){error.value=reason instanceof Error?reason.message:'切り取りできません。';}
  finally{busy.value=false;}
}
onBeforeUnmount(()=>{URL.revokeObjectURL(source);pointers.clear();});
</script>
<template>
  <div class="modal-backdrop" @click.self="!busy && emit('close')" @keydown.esc="!busy && emit('close')"><section class="modal-card avatar-crop-dialog" role="dialog" aria-modal="true" aria-label="プロフィール画像の切り取り">
    <h2>プロフィール画像の切り取り</h2><p>枠内をドラッグして位置を調整できます。ピンチまたはスライダーで拡大します。</p>
    <div ref="surface" class="avatar-crop-surface" @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="up" @lostpointercapture="up"><img ref="image" :src="source" :style="style" alt="切り取り範囲" draggable="false" @load="loaded" @error="error='対応している画像を読み込めませんでした。別の画像を選んでください。'"><div class="crop-grid"></div></div>
    <label>拡大 {{zoom.toFixed(1)}}倍<input v-model.number="zoom" type="range" min="1" max="4" step="0.01"></label>
    <label>左右の位置<input v-model.number="x" type="range" min="-1" max="1" step="0.01"></label><label>上下の位置<input v-model.number="y" type="range" min="-1" max="1" step="0.01"></label>
    <div v-if="error" class="alert error">{{error}}</div><div class="form-actions"><button class="button secondary" :disabled="busy" @click="zoom=1;x=0;y=0">中央に戻す</button><button class="button secondary" :disabled="busy" @click="emit('close')">キャンセル</button><button class="button primary" :disabled="busy||!width||!!error" @click="apply">切り取り適用</button></div>
  </section></div>
</template>
