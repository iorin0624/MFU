<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, api } from '@/lib/api'
import { formatJapaneseDate } from '@/lib/date'

type Entry = { user_public_id: string; display_name: string; park?: string; costume?: string; memo?: string }
type ParkCounts = { both: number; land: number; sea: number; undecided: number }
type Day = { date: string; count: number; park_counts: ParkCounts; entries: Entry[] }
type Holiday = { date: string; name: string }
type Season = { public_id: string; name: string; start_date: string; end_date: string }
type Restriction = { public_id: string; name: string; start_date: string; end_date: string }
type RestrictionBar = Restriction & { key: string; week: number; startColumn: number; endColumn: number; lane: number }
type Cell = { key: string; date: string | null; number: number | null; weekday: number; week: number; column: number; inSeason: boolean }

const route = useRoute()
const router = useRouter()
const today = new Date()
const requestedDate = typeof route.query.date === 'string' ? route.query.date : ''
const parsedDate = /^\d{4}-\d{2}-\d{2}$/.test(requestedDate) ? new Date(`${requestedDate}T00:00:00`) : today
const initialDate = Number.isNaN(parsedDate.getTime()) ? today : parsedDate
const cursor = ref(new Date(initialDate.getFullYear(), initialDate.getMonth(), 1))
const selectedDate = ref(isoDate(initialDate))
const days = ref<Day[]>([])
const seasons = ref<Season[]>([])
const selectedSeasonId = ref(typeof route.query.season === 'string' ? route.query.season : '')
const holidays = ref<Holiday[]>([])
const restrictions = ref<Restriction[]>([])
const authenticated = ref(false)
const loading = ref(true)
const error = ref('')
const weekdays = ['日', '月', '火', '水', '木', '金', '土']
const parkRows: { key: keyof ParkCounts; label: string }[] = [
  { key: 'both', label: '両方' }, { key: 'land', label: 'TDL' },
  { key: 'sea', label: 'TDS' }, { key: 'undecided', label: '未定' },
]

function isoDate(value: Date) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`
}
const monthTitle = computed(() => `${cursor.value.getFullYear()}年${cursor.value.getMonth() + 1}月`)
const activeSeason = computed(() => seasons.value.find(season => season.public_id === selectedSeasonId.value))
const currentMonth = computed(() => isoDate(cursor.value).slice(0, 7))
const canMovePrevious = computed(() => !!activeSeason.value && currentMonth.value > activeSeason.value.start_date.slice(0, 7))
const canMoveNext = computed(() => !!activeSeason.value && currentMonth.value < activeSeason.value.end_date.slice(0, 7))
const dayMap = computed(() => new Map(days.value.map(day => [day.date, day])))
const holidayMap = computed(() => new Map(holidays.value.map(holiday => [holiday.date, holiday.name])))
const selectedDay = computed(() => selectedDate.value ? dayMap.value.get(selectedDate.value) : undefined)
const selectedEntries = computed(() => selectedDay.value?.entries ?? [])
const selectedDateInSeason = computed(() => dateInSeason(selectedDate.value))
const todayInActiveSeason = computed(() => dateInSeason(isoDate(today)))
const addLink = computed(() => ({
  path: '/visits',
  query: { date: selectedDate.value, season: selectedSeasonId.value, return: 'calendar' },
}))
const calendarMetrics = computed(() => {
  const year = cursor.value.getFullYear(); const month = cursor.value.getMonth()
  const offset = new Date(year, month, 1).getDay()
  const last = new Date(year, month + 1, 0).getDate()
  return { year, month, offset, last, total: Math.ceil((offset + last) / 7) * 7 }
})
const cells = computed<Cell[]>(() => {
  const { year, month, offset, last, total } = calendarMetrics.value
  return Array.from({ length: total }, (_, index) => {
    const number = index - offset + 1
    const position = { weekday: index % 7, week: Math.floor(index / 7), column: index % 7 + 1 }
    if (number < 1 || number > last) return { key: `blank-${index}`, date: null, number: null, inSeason: false, ...position }
    const value = new Date(year, month, number)
    const valueText = isoDate(value)
    return { key: valueText, date: valueText, number, inSeason: dateInSeason(valueText), ...position }
  })
})
const restrictionBars = computed<RestrictionBar[]>(() => {
  const { year, month, offset, last } = calendarMetrics.value
  const firstDate = isoDate(new Date(year, month, 1))
  const lastDate = isoDate(new Date(year, month, last))
  const segments: Omit<RestrictionBar, 'lane'>[] = []
  for (const restriction of restrictions.value) {
    const start = restriction.start_date < firstDate ? firstDate : restriction.start_date
    const end = restriction.end_date > lastDate ? lastDate : restriction.end_date
    if (start > end) continue
    let index = offset + Number(start.slice(8, 10)) - 1
    const endIndex = offset + Number(end.slice(8, 10)) - 1
    while (index <= endIndex) {
      const week = Math.floor(index / 7)
      const segmentEnd = Math.min(endIndex, week * 7 + 6)
      segments.push({
        ...restriction,
        key: `${restriction.public_id}-${week}`,
        week,
        startColumn: index % 7 + 1,
        endColumn: segmentEnd % 7 + 1,
      })
      index = segmentEnd + 1
    }
  }
  segments.sort((a, b) => a.week - b.week || a.startColumn - b.startColumn || a.endColumn - b.endColumn)
  const laneEnds = new Map<number, number[]>()
  return segments.map(segment => {
    const ends = laneEnds.get(segment.week) ?? []
    let lane = ends.findIndex(endColumn => endColumn < segment.startColumn)
    if (lane < 0) lane = ends.length
    ends[lane] = segment.endColumn
    laneEnds.set(segment.week, ends)
    return { ...segment, lane }
  })
})
const restrictionLanes = computed(() => {
  const lanes = new Map<number, number>()
  for (const bar of restrictionBars.value) lanes.set(bar.week, Math.max(lanes.get(bar.week) ?? 0, bar.lane + 1))
  return lanes
})

function countsFor(date: string): ParkCounts {
  return dayMap.value.get(date)?.park_counts ?? { both: 0, land: 0, sea: 0, undecided: 0 }
}
function isToday(date: string) { return date === isoDate(today) }
function holidayFor(date: string) { return holidayMap.value.get(date) }
function dateInSeason(value: string) {
  const season = activeSeason.value
  return !!season && value >= season.start_date && value <= season.end_date
}
function cellStyle(cell: Cell): Record<string, string> {
  return {
    gridColumn: String(cell.column), gridRow: String(cell.week + 2),
    '--restriction-lanes': String(restrictionLanes.value.get(cell.week) ?? 0),
  }
}
function restrictionStyle(bar: RestrictionBar): Record<string, string> {
  return {
    gridColumn: `${bar.startColumn} / ${bar.endColumn + 1}`,
    gridRow: String(bar.week + 2), '--restriction-lane': String(bar.lane),
  }
}
function parkLabel(park?: string) {
  return ({ both: '両方', land: 'TDL', sea: 'TDS', undecided: '未定' } as Record<string, string>)[park ?? ''] ?? '未定'
}
async function loadCalendar() {
  if (!selectedSeasonId.value) return
  const year = String(cursor.value.getFullYear()); const month = String(cursor.value.getMonth() + 1)
  try {
    const query = new URLSearchParams({ year, month, season_id: selectedSeasonId.value })
    const result = await api<{ days: Day[]; holidays: Holiday[]; restrictions: Restriction[] }>(`/calendar?${query}`)
    days.value = result.days; holidays.value = result.holidays; restrictions.value = result.restrictions; error.value = ''
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : 'カレンダーを読み込めませんでした。'
  }
}
async function moveMonth(offset: number) {
  if ((offset < 0 && !canMovePrevious.value) || (offset > 0 && !canMoveNext.value)) return
  cursor.value = new Date(cursor.value.getFullYear(), cursor.value.getMonth() + offset, 1)
  const first = isoDate(cursor.value)
  const last = isoDate(new Date(cursor.value.getFullYear(), cursor.value.getMonth() + 1, 0))
  selectedDate.value = first < activeSeason.value!.start_date
    ? activeSeason.value!.start_date
    : last > activeSeason.value!.end_date ? activeSeason.value!.end_date : first
  await router.replace({ path: '/', query: { date: selectedDate.value, season: selectedSeasonId.value } }); await loadCalendar()
}
async function selectDate(date: string) {
  if (!dateInSeason(date)) return
  selectedDate.value = date
  await router.replace({ path: '/', query: { date, season: selectedSeasonId.value } })
}
async function goToday() {
  const target = dateInSeason(isoDate(today)) ? isoDate(today) : activeSeason.value!.start_date
  const parsed = new Date(`${target}T00:00:00`)
  cursor.value = new Date(parsed.getFullYear(), parsed.getMonth(), 1)
  await selectDate(target); await loadCalendar()
}
async function changeSeason() {
  const season = activeSeason.value
  if (!season) return
  const target = dateInSeason(selectedDate.value)
    ? selectedDate.value
    : dateInSeason(isoDate(today)) ? isoDate(today) : season.start_date
  const parsed = new Date(`${target}T00:00:00`)
  cursor.value = new Date(parsed.getFullYear(), parsed.getMonth(), 1)
  selectedDate.value = target
  await router.replace({ path: '/', query: { date: target, season: season.public_id } })
  await loadCalendar()
}
onMounted(async () => {
  try {
    await api('/auth/me'); authenticated.value = true
    seasons.value = (await api<{ seasons: Season[] }>('/seasons')).seasons
    const requestedSeason = seasons.value.find(season => season.public_id === selectedSeasonId.value)
    const matchingSeason = seasons.value.find(season => dateInRange(selectedDate.value, season))
    selectedSeasonId.value = (requestedSeason ?? matchingSeason ?? seasons.value[0])?.public_id ?? ''
    if (activeSeason.value && !dateInSeason(selectedDate.value)) selectedDate.value = activeSeason.value.start_date
    if (activeSeason.value) {
      const parsed = new Date(`${selectedDate.value}T00:00:00`)
      cursor.value = new Date(parsed.getFullYear(), parsed.getMonth(), 1)
      await router.replace({ path: '/', query: { date: selectedDate.value, season: selectedSeasonId.value } })
      await loadCalendar()
    }
  } catch (cause) {
    if (!(cause instanceof ApiError && cause.status === 401)) {
      error.value = cause instanceof ApiError ? cause.message : '読み込めませんでした。'
    }
  } finally { loading.value = false }
})
function dateInRange(value: string, season: Season) {
  return value >= season.start_date && value <= season.end_date
}
</script>

<template>
  <p v-if="loading" class="helper">読み込み中…</p>
  <section v-else-if="authenticated" class="home-calendar">
    <label v-if="seasons.length" class="field calendar-season">表示シーズン
      <select v-model="selectedSeasonId" @change="changeSeason">
        <option v-for="season in seasons" :key="season.public_id" :value="season.public_id">{{ season.name }}（{{ formatJapaneseDate(season.start_date) }}〜{{ formatJapaneseDate(season.end_date) }}）</option>
      </select>
    </label>
    <p v-else class="notice">現在、表示できるシーズンがありません。</p>
    <div class="calendar-toolbar">
      <button class="calendar-nav" type="button" aria-label="前の月" :disabled="!canMovePrevious" @click="moveMonth(-1)">‹</button>
      <h1>{{ monthTitle }}</h1>
      <button class="calendar-nav" type="button" aria-label="次の月" :disabled="!canMoveNext" @click="moveMonth(1)">›</button>
      <RouterLink v-if="selectedDateInSeason" class="calendar-add" :to="addLink" aria-label="選択日に予定を追加">＋</RouterLink>
      <span v-else class="calendar-add disabled" aria-hidden="true">＋</span>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div class="month-calendar" aria-label="月間カレンダー">
      <div v-for="(weekday, index) in weekdays" :key="weekday" class="weekday" :class="{ sunday: index === 0, saturday: index === 6 }">{{ weekday }}</div>
      <template v-for="cell in cells" :key="cell.key">
        <div v-if="!cell.date" class="calendar-cell blank" :style="cellStyle(cell)" aria-hidden="true"></div>
        <button v-else class="calendar-cell" :style="cellStyle(cell)" :class="{ selected: selectedDate === cell.date, today: isToday(cell.date), sunday: cell.weekday === 0, saturday: cell.weekday === 6, holiday: !!holidayFor(cell.date), 'out-of-season': !cell.inSeason }" type="button" :disabled="!cell.inSeason" @click="selectDate(cell.date)">
          <span class="day-header"><span class="day-number">{{ cell.number }}</span><span v-if="holidayFor(cell.date)" class="holiday-name">{{ holidayFor(cell.date) }}</span></span>
          <span class="day-counts">
            <span v-for="row in parkRows" v-show="countsFor(cell.date)[row.key] > 0" :key="row.key" class="park-count" :class="`park-${row.key}`">{{ row.label }} {{ countsFor(cell.date)[row.key] }}</span>
          </span>
        </button>
      </template>
      <span v-for="bar in restrictionBars" :key="bar.key" class="restriction-bar" :style="restrictionStyle(bar)" :title="`${bar.name}（${formatJapaneseDate(bar.start_date)}〜${formatJapaneseDate(bar.end_date)}）`">{{ bar.name }}</span>
    </div>
    <div class="calendar-footer"><button class="button secondary" type="button" @click="goToday">{{ todayInActiveSeason ? '今日' : 'シーズン開始日' }}</button></div>

    <section v-if="selectedDateInSeason" class="selected-date-panel">
      <div class="selected-date-heading"><h2>{{ formatJapaneseDate(selectedDate) }}の予定</h2><RouterLink class="button" :to="addLink">この日に予定を追加</RouterLink></div>
      <div v-if="selectedEntries.length" class="visit-list compact">
        <article v-for="(entry, index) in selectedEntries" :key="`${entry.user_public_id}-${index}`" class="visit-card">
          <h3>{{ entry.display_name }}</h3><p>{{ parkLabel(entry.park) }}</p>
          <p v-if="entry.costume">服装 {{ entry.costume }}</p><p v-if="entry.memo">{{ entry.memo }}</p>
        </article>
      </div>
      <p v-else class="helper">この日に表示できる予定はありません。</p>
    </section>
  </section>

  <section v-else class="welcome">
    <p class="eyebrow">INPA</p><h1>インパーク予定を、気軽に共有。</h1>
    <p>プロフィールと予定を登録し、公開範囲を選んで共有できます。</p>
    <div class="actions"><RouterLink class="button" to="/register">予定を登録</RouterLink><RouterLink class="button secondary" to="/login">ログイン</RouterLink></div>
  </section>
</template>
