<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ApiError, api } from '@/lib/api'
import { formatJapaneseDate } from '@/lib/date'
type Season={public_id:string;name:string;start_date:string;end_date:string}
type Entry={user_public_id:string;display_name:string;park?:string;costume?:string;memo?:string}
type Day={date:string;count:number;land_count:number;sea_count:number;entries:Entry[]}
type Restriction={public_id:string;name:string;start_date:string;end_date:string;park_scope:string;description:string|null}
const now=new Date(); const month=ref(`${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}`)
const seasons=ref<Season[]>([]); const seasonId=ref(''); const myselfOnly=ref(false); const personId=ref('')
const days=ref<Day[]>([]); const selected=ref<Day|null>(null); const error=ref('')
const restrictions=ref<Restriction[]>([])
const title=computed(()=>seasons.value.find(s=>s.public_id===seasonId.value)?.name??'')
const message=(value:unknown)=>value instanceof ApiError?value.message:'通信に失敗しました。'
async function load(){if(!seasonId.value)return; const [year,m]=month.value.split('-'); try{const query=new URLSearchParams({season_id:seasonId.value,year,month:String(Number(m)),myself_only:myselfOnly.value?'1':'0'});if(personId.value.trim())query.set('person_id',personId.value.trim());const result=await api<{days:Day[];restrictions:Restriction[]}>(`/calendar?${query}`);days.value=result.days;restrictions.value=result.restrictions;selected.value=null}catch(value){error.value=message(value)}}
onMounted(async()=>{try{seasons.value=(await api<{seasons:Season[]}>('/seasons')).seasons;if(seasons.value[0]){seasonId.value=seasons.value[0].public_id;await load()}}catch(value){error.value=message(value)}})
</script>
<template><section class="panel calendar-panel"><h1>統合カレンダー</h1><p v-if="error" class="error">{{error}}</p><form class="calendar-filters" @submit.prevent="load"><label class="field">シーズン<select v-model="seasonId"><option v-for="season in seasons" :key="season.public_id" :value="season.public_id">{{season.name}}</option></select></label><label class="field">月<input v-model="month" type="month"></label><label class="field">つながりID（任意）<input v-model="personId" maxlength="9" placeholder="ABCD-EFGH" autocapitalize="characters"></label><label class="field-check"><input v-model="myselfOnly" type="checkbox">自分の予定だけ</label><button class="button">表示</button></form><h2>{{title}}</h2><div v-for="warning in restrictions" :key="warning.public_id" class="warning-notice"><strong>{{warning.name}}</strong><p>{{warning.description || '期間中の注意事項を確認してください。'}}</p><small>{{formatJapaneseDate(warning.start_date)}}〜{{formatJapaneseDate(warning.end_date)}} / {{warning.park_scope}}</small></div><div class="calendar-days"><button v-for="day in days" :key="day.date" class="calendar-day" @click="selected=day"><strong>{{formatJapaneseDate(day.date)}}</strong><span>{{day.count}}人</span><small>ランド {{day.land_count}} / シー {{day.sea_count}}</small></button><p v-if="!days.length" class="helper">この月に表示できる予定はありません。</p></div><div v-if="selected" class="visit-list"><h2>{{formatJapaneseDate(selected.date)}}の予定</h2><article v-for="entry in selected.entries" :key="entry.user_public_id" class="visit-card"><h3>{{entry.display_name}}</h3><p v-if="entry.park">{{entry.park}}</p><p v-if="entry.costume">服装 {{entry.costume}}</p><p v-if="entry.memo">{{entry.memo}}</p></article></div></section></template>
