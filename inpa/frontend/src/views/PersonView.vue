<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, api } from '@/lib/api'
import { formatJapaneseDate } from '@/lib/date'
import { parkLabel, parkScopeLabel } from '@/lib/park'

type Person = { public_id: string; connection_id: string; display_name: string; following: boolean; follows_me: boolean; x_handle?: string; instagram_handle?: string }
type Entry = { user_public_id: string; display_name: string; park?: string; costume?: string; memo?: string }
type ParkCounts = { both: number; land: number; sea: number; undecided: number }
type Day = { date: string; count: number; park_counts: ParkCounts; entries: Entry[] }
type Holiday = { date: string; name: string }
type Season = { public_id: string; name: string; start_date: string; end_date: string }
type Restriction = { public_id: string; name: string; start_date: string; end_date: string; park_scope: string; description: string | null }
type Cell = { key: string; date: string | null; number: number | null; weekday: number; week: number; column: number; inSeason: boolean }
type RestrictionBar = Restriction & { key: string; week: number; startColumn: number; endColumn: number; lane: number }

const route = useRoute()
const router = useRouter()
const person = ref<Person | null>(null)
const seasons = ref<Season[]>([]); const seasonId = ref('')
const cursor = ref(new Date(new Date().getFullYear(), new Date().getMonth(), 1))
const selectedDate = ref(''); const days = ref<Day[]>([]); const holidays = ref<Holiday[]>([]); const restrictions = ref<Restriction[]>([])
const loading = ref(true); const error = ref(''); const notice = ref(''); const actionPending = ref(false)
const weekdays = ['日', '月', '火', '水', '木', '金', '土']
const parkRows: { key: keyof ParkCounts; label: string }[] = [
  { key: 'both', label: '🏰TDL・🌍TDS' }, { key: 'land', label: '🏰TDL' },
  { key: 'sea', label: '🌍TDS' }, { key: 'undecided', label: '未定' },
]

function isoDate(value: Date) { return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}` }
function displayConnectionId(value: string) { return value.length === 8 ? `${value.slice(0, 4)}-${value.slice(4)}` : value }
const activeSeason = computed(() => seasons.value.find(value => value.public_id === seasonId.value))
const monthTitle = computed(() => `${cursor.value.getFullYear()}年${cursor.value.getMonth() + 1}月`)
const currentMonth = computed(() => isoDate(cursor.value).slice(0, 7))
const canMovePrevious = computed(() => !!activeSeason.value && currentMonth.value > activeSeason.value.start_date.slice(0, 7))
const canMoveNext = computed(() => !!activeSeason.value && currentMonth.value < activeSeason.value.end_date.slice(0, 7))
const dayMap = computed(() => new Map(days.value.map(day => [day.date, day])))
const holidayMap = computed(() => new Map(holidays.value.map(day => [day.date, day.name])))
const selectedEntries = computed(() => dayMap.value.get(selectedDate.value)?.entries ?? [])
const selectedRestrictions = computed(() => restrictions.value.filter(value => selectedDate.value >= value.start_date && selectedDate.value <= value.end_date))
const relationship = computed(() => person.value?.following && person.value?.follows_me ? '相互フォロー' : person.value?.following ? 'フォロー中' : person.value?.follows_me ? 'フォロワー' : '')
const cells = computed<Cell[]>(() => {
  const year = cursor.value.getFullYear(); const month = cursor.value.getMonth()
  const offset = new Date(year, month, 1).getDay(); const last = new Date(year, month + 1, 0).getDate()
  const total = Math.ceil((offset + last) / 7) * 7
  return Array.from({ length: total }, (_, index) => {
    const number = index - offset + 1; const position = { weekday: index % 7, week: Math.floor(index / 7), column: index % 7 + 1 }
    if (number < 1 || number > last) return { key: `blank-${index}`, date: null, number: null, inSeason: false, ...position }
    const date = isoDate(new Date(year, month, number))
    return { key: date, date, number, inSeason: dateInSeason(date), ...position }
  })
})
const restrictionBars = computed<RestrictionBar[]>(() => {
  const year = cursor.value.getFullYear(); const month = cursor.value.getMonth()
  const offset = new Date(year, month, 1).getDay(); const last = new Date(year, month + 1, 0).getDate()
  const firstDate = isoDate(new Date(year, month, 1)); const lastDate = isoDate(new Date(year, month, last))
  const segments: Omit<RestrictionBar, 'lane'>[] = []
  for (const restriction of restrictions.value) {
    const start = restriction.start_date < firstDate ? firstDate : restriction.start_date
    const end = restriction.end_date > lastDate ? lastDate : restriction.end_date
    if (start > end) continue
    let index = offset + Number(start.slice(8, 10)) - 1; const endIndex = offset + Number(end.slice(8, 10)) - 1
    while (index <= endIndex) {
      const week = Math.floor(index / 7); const segmentEnd = Math.min(endIndex, week * 7 + 6)
      segments.push({ ...restriction, key: `${restriction.public_id}-${week}`, week, startColumn: index % 7 + 1, endColumn: segmentEnd % 7 + 1 })
      index = segmentEnd + 1
    }
  }
  segments.sort((a, b) => a.week - b.week || a.startColumn - b.startColumn || a.endColumn - b.endColumn)
  const laneEnds = new Map<number, number[]>()
  return segments.map(segment => {
    const ends = laneEnds.get(segment.week) ?? []; let lane = ends.findIndex(end => end < segment.startColumn)
    if (lane < 0) lane = ends.length
    ends[lane] = segment.endColumn; laneEnds.set(segment.week, ends)
    return { ...segment, lane }
  })
})
const restrictionLanes = computed(() => {
  const lanes = new Map<number, number>()
  for (const bar of restrictionBars.value) lanes.set(bar.week, Math.max(lanes.get(bar.week) ?? 0, bar.lane + 1))
  return lanes
})
function dateInSeason(date: string) { return !!activeSeason.value && date >= activeSeason.value.start_date && date <= activeSeason.value.end_date }
function countsFor(date: string): ParkCounts { return dayMap.value.get(date)?.park_counts ?? { both: 0, land: 0, sea: 0, undecided: 0 } }
function socialUrl(service: 'x' | 'instagram', handle: string) { return service === 'x' ? `https://x.com/${handle}` : `https://www.instagram.com/${handle}/` }
function cellStyle(cell: Cell): Record<string, string> { return { gridColumn: String(cell.column), gridRow: String(cell.week + 2), '--restriction-lanes': String(restrictionLanes.value.get(cell.week) ?? 0) } }
function restrictionStyle(bar: RestrictionBar): Record<string, string> { return { gridColumn: `${bar.startColumn} / ${bar.endColumn + 1}`, gridRow: String(bar.week + 2), '--restriction-lane': String(bar.lane) } }
async function follow() {
  if (!person.value) return
  actionPending.value = true; error.value = ''
  try { await api(`/follows/${person.value.public_id}`, { method: 'POST' }); person.value.following = true; notice.value = 'フォローしました。'; await loadCalendar() }
  catch (cause) { error.value = cause instanceof ApiError ? cause.message : 'フォローできませんでした。' }
  finally { actionPending.value = false }
}
async function unfollow() {
  if (!person.value) return
  actionPending.value = true; error.value = ''
  try { await api(`/follows/${person.value.public_id}`, { method: 'DELETE' }); person.value.following = false; notice.value = 'フォローを解除しました。'; await loadCalendar() }
  catch (cause) { error.value = cause instanceof ApiError ? cause.message : '解除できませんでした。' }
  finally { actionPending.value = false }
}
async function block() {
  if (!person.value || !confirm(`${person.value.display_name}さんをブロックしますか？ 双方のフォローも解除されます。`)) return
  actionPending.value = true; error.value = ''
  try { await api(`/blocks/${person.value.public_id}`, { method: 'POST' }); await router.push('/connections') }
  catch (cause) { error.value = cause instanceof ApiError ? cause.message : 'ブロックできませんでした。'; actionPending.value = false }
}
async function loadCalendar() {
  if (!person.value || !seasonId.value) return
  try {
    const query = new URLSearchParams({ year: String(cursor.value.getFullYear()), month: String(cursor.value.getMonth() + 1), season_id: seasonId.value, person_id: person.value.connection_id })
    const result = await api<{ days: Day[]; holidays: Holiday[]; restrictions: Restriction[] }>(`/calendar?${query}`)
    days.value = result.days; holidays.value = result.holidays; restrictions.value = result.restrictions; error.value = ''
  } catch (cause) { error.value = cause instanceof ApiError ? cause.message : 'カレンダーを読み込めませんでした。' }
}
async function selectSeason() {
  if (!activeSeason.value) return
  const target = activeSeason.value.start_date
  const parsed = new Date(`${target}T00:00:00`); cursor.value = new Date(parsed.getFullYear(), parsed.getMonth(), 1); selectedDate.value = target
  await loadCalendar()
}
async function moveMonth(offset: number) {
  if ((offset < 0 && !canMovePrevious.value) || (offset > 0 && !canMoveNext.value)) return
  cursor.value = new Date(cursor.value.getFullYear(), cursor.value.getMonth() + offset, 1)
  selectedDate.value = dateInSeason(isoDate(cursor.value)) ? isoDate(cursor.value) : activeSeason.value!.start_date
  await loadCalendar()
}
onMounted(async () => {
  try {
    const publicId = String(route.params.publicId)
    const [personResult, seasonResult] = await Promise.all([api<{ person: Person }>(`/people/${encodeURIComponent(publicId)}`), api<{ seasons: Season[] }>('/seasons')])
    person.value = personResult.person; seasons.value = seasonResult.seasons; seasonId.value = seasons.value[0]?.public_id ?? ''
    if (activeSeason.value) await selectSeason()
  } catch (cause) { error.value = cause instanceof ApiError ? cause.message : '利用者を読み込めませんでした。' }
  finally { loading.value = false }
})
</script>

<template>
  <p v-if="loading" class="helper">読み込み中…</p>
  <section v-else-if="person" class="home-calendar">
    <div class="panel person-profile">
      <RouterLink to="/connections">← つながりへ戻る</RouterLink>
      <h1>{{ person.display_name }}</h1>
      <p>つながりID {{ displayConnectionId(person.connection_id) }}<template v-if="relationship"> ／ {{ relationship }}</template></p>
      <div v-if="person.x_handle || person.instagram_handle" class="person-socials">
        <a v-if="person.x_handle" :href="socialUrl('x', person.x_handle)" target="_blank" rel="noopener noreferrer">X @{{ person.x_handle }}</a>
        <a v-if="person.instagram_handle" :href="socialUrl('instagram', person.instagram_handle)" target="_blank" rel="noopener noreferrer">Instagram @{{ person.instagram_handle }}</a>
      </div>
      <p v-if="notice" class="notice">{{ notice }}</p>
      <div class="actions"><button v-if="!person.following" class="button" type="button" :disabled="actionPending" @click="follow">フォローする</button><button v-else class="button secondary" type="button" :disabled="actionPending" @click="unfollow">解除</button><button class="button danger" type="button" :disabled="actionPending" @click="block">ブロック</button></div>
    </div>
    <label v-if="seasons.length" class="field calendar-season">表示シーズン
      <select v-model="seasonId" @change="selectSeason"><option v-for="season in seasons" :key="season.public_id" :value="season.public_id">{{ season.name }}（{{ formatJapaneseDate(season.start_date) }}〜{{ formatJapaneseDate(season.end_date) }}）</option></select>
    </label>
    <p v-else class="notice">現在、表示できるシーズンがありません。</p>
    <div class="calendar-toolbar person-calendar-toolbar"><button class="calendar-nav" type="button" aria-label="前の月" :disabled="!canMovePrevious" @click="moveMonth(-1)">‹</button><h1>{{ monthTitle }}</h1><button class="calendar-nav" type="button" aria-label="次の月" :disabled="!canMoveNext" @click="moveMonth(1)">›</button></div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div class="month-calendar" aria-label="個別月間カレンダー">
      <div v-for="(weekday, index) in weekdays" :key="weekday" class="weekday" :class="{ sunday: index === 0, saturday: index === 6 }">{{ weekday }}</div>
      <template v-for="cell in cells" :key="cell.key">
        <div v-if="!cell.date" class="calendar-cell blank" :style="cellStyle(cell)" aria-hidden="true"></div>
        <button v-else class="calendar-cell" :style="cellStyle(cell)" :class="{ selected: selectedDate === cell.date, sunday: cell.weekday === 0, saturday: cell.weekday === 6, holiday: !!holidayMap.get(cell.date), 'out-of-season': !cell.inSeason }" type="button" :disabled="!cell.inSeason" @click="selectedDate=cell.date">
          <span class="day-header"><span class="day-number">{{ cell.number }}</span><span v-if="holidayMap.get(cell.date)" class="holiday-name">{{ holidayMap.get(cell.date) }}</span></span>
          <span class="day-counts"><span v-for="row in parkRows" v-show="countsFor(cell.date)[row.key] > 0" :key="row.key" class="park-count" :class="`park-${row.key}`">{{ row.label }} {{ countsFor(cell.date)[row.key] }}</span></span>
        </button>
      </template>
      <span v-for="bar in restrictionBars" :key="bar.key" class="restriction-bar" :style="restrictionStyle(bar)" :title="`${bar.name}（${formatJapaneseDate(bar.start_date)}〜${formatJapaneseDate(bar.end_date)}）`">{{ bar.name }}</span>
    </div>
    <section v-if="selectedDate" class="selected-date-panel">
      <div v-if="selectedRestrictions.length" class="selected-restrictions" role="alert"><h3>禁止・注意期間</h3><article v-for="restriction in selectedRestrictions" :key="restriction.public_id" class="selected-restriction-notice"><strong>{{ restriction.name }}</strong><p v-if="restriction.description">{{ restriction.description }}</p><small>{{ formatJapaneseDate(restriction.start_date) }}〜{{ formatJapaneseDate(restriction.end_date) }} / {{ parkScopeLabel(restriction.park_scope) }}</small></article></div>
      <h2>{{ formatJapaneseDate(selectedDate) }}の予定</h2>
      <div v-if="selectedEntries.length" class="visit-list compact"><article v-for="(entry, index) in selectedEntries" :key="`${entry.user_public_id}-${index}`" class="visit-card"><p v-if="entry.park">{{ parkLabel(entry.park) }}</p><p v-if="entry.costume">服装 {{ entry.costume }}</p><p v-if="entry.memo">{{ entry.memo }}</p></article></div>
      <p v-else class="helper">この日に公開されている予定はありません。</p>
    </section>
  </section>
  <section v-else class="panel"><p class="error">{{ error }}</p><RouterLink to="/connections">つながりへ戻る</RouterLink></section>
</template>
