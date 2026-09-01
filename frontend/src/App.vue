<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { api } from './api'
import SegmentRow from './components/SegmentRow.vue'
import VoicePanel from './components/VoicePanel.vue'

const meta = ref({ voices: [], styles: [], key_configured: true })
const clones = ref([])  // 克隆音色，全局共享
const projects = ref([])
const project = ref(null)
const segments = ref([])
const view = ref('draft') // draft | segments
const toast = reactive({ msg: '', kind: 'info' })
const busy = ref('')
const progress = reactive({ done: 0, total: 0, running: false })
const draft = ref('')
const showSidebar = ref(false)
const voiceOpen = ref(true)  // 音色定调面板展开态；进入段落视图后自动收起
const timeline = ref(false)  // 字幕时间轴模式（导入 SRT 后为真）
const srtInput = ref(null)   // 隐藏的 SRT 文件选择框

// 鉴权门禁：needLogin=true 时盖一层全屏登录框，其余界面不加载
const needLogin = ref(false)
const loginPwd = ref('')
const loginBusy = ref(false)
const loginErr = ref('')

const hasSegments = computed(() => segments.value.length > 0)
// fresh===false 表示音频与当前音色/语气不符（改过项目设置），不算已合成
const isFresh = (s) => s.status === 'ok' && s.fresh !== false
const okCount = computed(() => segments.value.filter(isFresh).length)
const staleCount = computed(
  () => segments.value.filter((s) => s.audio_hash && s.fresh === false).length)
const failCount = computed(() => segments.value.filter((s) => s.status === 'failed').length)
// 时间轴模式下音频超出字幕窗口的段数 —— 导出会顺延，提示用户精简
const overflowCount = computed(
  () => segments.value.filter((s) => (s.overflow_ms ?? 0) > 0).length)
// 待办 = 没合成的 + 失败的 + 过期的，也就是「合成全部」实际要跑的段数
const todoCount = computed(() => segments.value.length - okCount.value)
const totalDur = computed(() => {
  const ms = segments.value.reduce(
    (a, s) => a + (isFresh(s) ? (s.duration_ms ?? 0) + s.pause_after_ms : 0), 0)
  const sec = Math.round(ms / 1000)
  return `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`
})
// 有过期段时禁止导出：后端会拒，前端提前挡住并说明原因
const canExport = computed(
  () => okCount.value > 0 && staleCount.value === 0 && !progress.running)

// 当前整篇语气的显示名（自定义语气直接回显原串）
const curStyleLabel = computed(() => {
  const id = project.value?.default_style
  return meta.value.styles.find((s) => s.id === id)?.label ?? id ?? '默认'
})

// voice 值 → 显示名：克隆音色 clone:<id> 转成它的名字，预置音色原样
function voiceLabel(voice) {
  if (voice?.startsWith('clone:')) {
    const c = clones.value.find((x) => x.voice === voice)
    return c ? `${c.name}（克隆）` : '克隆音色(已删)'
  }
  return voice
}
const curVoiceLabel = computed(() => voiceLabel(project.value?.default_voice))

function say(msg, kind = 'info', sticky = false) {
  toast.msg = msg
  toast.kind = kind
  // 耗时操作的完成提示（sticky）和错误一样不自动消失，用户手动关或被下一条顶掉
  if (kind !== 'err' && !sticky) {
    setTimeout(() => { if (toast.msg === msg) toast.msg = '' }, 4000)
  }
}

async function guard(fn, label = '') {
  busy.value = label
  try {
    return await fn()
  } catch (e) {
    say(e.message, 'err')
  } finally {
    busy.value = ''
  }
}

// ---------- 加载 ----------

onMounted(async () => {
  try {
    const st = await api.authStatus()
    if (st.auth_required && !st.logged_in) {
      needLogin.value = true
      return
    }
  } catch (e) {
    say('无法连接后端：' + e.message, 'err')
    return
  }
  await bootstrap()
})

// 登录通过后加载主界面数据
async function bootstrap() {
  await guard(async () => {
    meta.value = await api.meta()
    if (!meta.value.key_configured) {
      say('未配置 MIMO_API_KEY，合成会失败。填好 .env 里的 key 再重启容器。', 'err')
    }
    clones.value = await api.listClones()
    projects.value = await api.listProjects()
    if (projects.value.length) await open(projects.value[0].id)
  })
}

async function doLogin() {
  if (!loginPwd.value) return
  loginBusy.value = true
  loginErr.value = ''
  try {
    await api.login(loginPwd.value)
    needLogin.value = false
    loginPwd.value = ''
    await bootstrap()
  } catch (e) {
    loginErr.value = e.message || '登录失败'
  } finally {
    loginBusy.value = false
  }
}

async function refreshList() {
  projects.value = await api.listProjects()
}

async function open(id) {
  await guard(async () => {
    const d = await api.getProject(id)
    project.value = d.project
    segments.value = d.segments
    timeline.value = d.timeline
    draft.value = d.project.raw_text
    view.value = d.segments.length ? 'segments' : 'draft'
    showSidebar.value = false
  })
}

async function createNew() {
  await guard(async () => {
    const p = await api.createProject({ name: '未命名 ' + new Date().toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) })
    await refreshList()
    await open(p.id)
  })
}

async function remove(id, name) {
  if (!confirm(`删除「${name}」？其音频文件也会一起清掉，不可恢复。`)) return
  await guard(async () => {
    await api.deleteProject(id)
    await refreshList()
    if (project.value?.id === id) {
      project.value = null
      segments.value = []
      if (projects.value.length) await open(projects.value[0].id)
    }
    say('已删除')
  })
}

// ---------- 稿子与分段 ----------

let saveTimer = null
watch(draft, (v) => {
  if (!project.value || v === project.value.raw_text) return
  clearTimeout(saveTimer)
  saveTimer = setTimeout(async () => {
    await api.patchProject(project.value.id, { raw_text: v })
    project.value.raw_text = v
  }, 600)
})

async function flushDraft() {
  clearTimeout(saveTimer)
  if (project.value && draft.value !== project.value.raw_text) {
    await api.patchProject(project.value.id, { raw_text: draft.value })
    project.value.raw_text = draft.value
  }
}

async function smartSplit() {
  if (timeline.value &&
      !confirm('当前是字幕时间轴模式，重新分段会丢弃字幕时间戳，退回普通拼接。确定吗？')) return
  await guard(async () => {
    await flushDraft()
    const r = await api.preprocess(project.value.id)
    segments.value = r.segments
    timeline.value = false
    view.value = 'segments'
    if (r.mode === 'rule') say(r.warning, 'warn', true)
    else say(`已分 ${r.segments.length} 段，LLM 已插入语气标签和读法改写`, 'ok', true)
  }, 'LLM 处理中…')
}

async function plainSplit() {
  if (timeline.value &&
      !confirm('当前是字幕时间轴模式，重新分段会丢弃字幕时间戳，退回普通拼接。确定吗？')) return
  await guard(async () => {
    await flushDraft()
    const r = await api.ruleSplit(project.value.id)
    segments.value = r.segments
    timeline.value = false
    view.value = 'segments'
    say(`按标点分了 ${r.segments.length} 段，未做任何文本改动`)
  }, '分段中…')
}

function pickSrt() {
  srtInput.value?.click()
}

async function onSrtFile(e) {
  const f = e.target.files?.[0]
  if (f) {
    if (segments.value.length &&
        !confirm('导入字幕会替换当前所有段落，确定继续吗？')) {
      e.target.value = ''
      return
    }
    const content = await f.text()
    await guard(async () => {
      const r = await api.importSrt(project.value.id, content)
      segments.value = r.segments
      timeline.value = true
      view.value = 'segments'
      say(`已导入 ${r.count} 条字幕，进入时间轴对齐模式`, 'ok', true)
    }, '导入字幕…')
  }
  e.target.value = ''
}

async function patchProject(fields) {
  await guard(async () => {
    project.value = await api.patchProject(project.value.id, fields)
    await refreshList()
    // 改音色/语气会让继承它的段落音频过期，重取段落拿到新的 fresh 标记
    if (hasSegments.value && ('default_voice' in fields || 'default_style' in fields)) {
      const d = await api.getProject(project.value.id)
      segments.value = d.segments
      if (staleCount.value) {
        say(`音色或语气已改，${staleCount.value} 段音频过期，需要重新合成`, 'warn')
      }
    }
  })
}

async function patchSegment(sid, fields) {
  await guard(async () => {
    const updated = await api.patchSegment(sid, fields)
    const i = segments.value.findIndex((s) => s.id === sid)
    if (i >= 0) segments.value[i] = updated
  })
}

// 单段重合成：改完一句只重跑这一句，不必整篇重来
async function resynthSegment(sid) {
  await guard(async () => {
    const r = await api.synthesizeSegment(sid)
    const i = segments.value.findIndex((s) => s.id === sid)
    if (i >= 0 && r.segment) segments.value[i] = r.segment
    if (r.result.status === 'failed') say(r.result.error || '合成失败', 'err')
  }, '合成中…')
}

async function renameClone(id, name) {
  await guard(async () => {
    await api.renameClone(id, name)
    clones.value = await api.listClones()
  })
}

async function uploadClone(file, name) {
  await guard(async () => {
    await api.uploadClone(file, name)
    clones.value = await api.listClones()
    say(`克隆音色「${name}」已添加`, 'ok')
  }, '上传克隆音色…')
}

async function removeClone(id, name) {
  await guard(async () => {
    try {
      await api.deleteClone(id, false)
    } catch (e) {
      // 被引用时后端返回 409，问用户是否强删
      if (/仍被/.test(e.message)) {
        if (!confirm(`${e.message}。仍要删除吗？`)) return
        await api.deleteClone(id, true)
      } else {
        throw e
      }
    }
    clones.value = await api.listClones()
    say(`已删除克隆音色「${name}」`)
  })
}

// ---------- 合成与导出 ----------

async function synthesize(onlyFailed = false) {
  let purgedFiles = 0
  progress.running = true
  progress.done = 0
  progress.total = onlyFailed ? todoCount.value : segments.value.length
  try {
    const summary = await api.synthesize(project.value.id, { onlyFailed }, (ev) => {
      if (ev.type === 'start') progress.total = ev.total
      if (ev.type === 'progress') {
        progress.done = ev.done
        // 即时更新只驱动进度条；status/fresh 的权威值合成后统一重取
        const i = segments.value.findIndex((s) => s.id === ev.id)
        if (i >= 0) {
          segments.value[i] = {
            ...segments.value[i],
            status: ev.status === 'failed' ? 'failed' : 'ok',
            fresh: ev.status !== 'failed',
            duration_ms: ev.duration_ms ?? segments.value[i].duration_ms,
            error_msg: ev.error ?? null,
          }
        }
      }
      if (ev.type === 'error') say(ev.message, 'err')
      if (ev.type === 'purged') purgedFiles = ev.files
    })
    // 以后端为准刷新段落，确保 fresh / duration / status 全部权威
    const d = await api.getProject(project.value.id)
    segments.value = d.segments
    if (summary) {
      const parts = [`成功 ${summary.ok}`]
      if (summary.cached) parts.push(`命中缓存 ${summary.cached}`)
      if (summary.failed) parts.push(`失败 ${summary.failed}`)
      if (purgedFiles) parts.push(`清理旧音频 ${purgedFiles}`)
      const done = summary.ok + summary.cached
      say(`合成完成：${done}/${done + summary.failed} 段 · ${parts.join(' · ')}`,
        summary.failed ? 'warn' : 'ok', true)
    }
  } catch (e) {
    say(e.message, 'err')
  } finally {
    progress.running = false
  }
}

function download(url) {
  const a = document.createElement('a')
  a.href = url
  a.click()
}

const fullPlaying = ref(false)
const fullAudio = ref(null)

async function toggleFullPlay() {
  if (fullPlaying.value) {
    fullAudio.value?.pause()
    fullPlaying.value = false
    return
  }
  fullPlaying.value = true
  try {
    const el = fullAudio.value
    el.src = api.fullPreviewUrl(project.value.id)
    el.onended = () => { fullPlaying.value = false }
    el.onerror = () => { fullPlaying.value = false; say('全篇播放失败', 'err') }
    await el.play()
  } catch (e) {
    fullPlaying.value = false
    say(`播放失败：${e.message}`, 'err')
  }
}

// 切项目时停掉正在播的音频，否则会串台
watch(() => project.value?.id, () => {
  fullAudio.value?.pause()
  fullPlaying.value = false
})

// 视图切换联动音色面板：原稿阶段要定调（展开），段落阶段专注精修（收起）
watch(view, (v) => { voiceOpen.value = v === 'draft' })
</script>

<template>
  <!-- 登录门禁：设了访问密码且未登录时盖满全屏 -->
  <div v-if="needLogin" class="login-mask">
    <form class="login-box card" @submit.prevent="doLogin">
      <h2>配音工作台</h2>
      <p class="login-hint">请输入访问密码</p>
      <input
        v-model="loginPwd" type="password" placeholder="访问密码"
        autofocus autocomplete="current-password"
      />
      <p v-if="loginErr" class="login-err">{{ loginErr }}</p>
      <button class="primary" type="submit" :disabled="loginBusy || !loginPwd">
        {{ loginBusy ? '登录中…' : '登录' }}
      </button>
    </form>
  </div>

  <div v-else class="app">
    <!-- 侧栏：项目列表 -->
    <aside class="side card" :class="{ open: showSidebar }">
      <div class="side-head">
        <b>项目</b>
        <button class="sm primary" @click="createNew">新建</button>
      </div>
      <ul class="plist">
        <li
          v-for="p in projects" :key="p.id"
          :class="{ on: project?.id === p.id }"
          @click="open(p.id)"
        >
          <div class="pmain">
            <span class="pname">{{ p.name }}</span>
            <small class="muted">{{ p.seg_count }} 段 · {{ voiceLabel(p.default_voice) }}</small>
          </div>
          <button class="sm ghost del" @click.stop="remove(p.id, p.name)">×</button>
        </li>
        <li v-if="!projects.length" class="empty muted">还没有项目，点「新建」开始</li>
      </ul>
    </aside>

    <main class="main">
      <header class="topbar">
        <button class="sm ghost burger" @click="showSidebar = !showSidebar">☰</button>
        <input
          v-if="project"
          class="title"
          :value="project.name"
          @change="patchProject({ name: $event.target.value })"
        />
        <span v-else class="title muted">选一个项目，或新建一个</span>
        <span class="muted mono stat">{{ meta.tts_model }}</span>
      </header>

      <div v-if="toast.msg" class="toast" :class="toast.kind">
        <span>{{ toast.msg }}</span>
        <button class="sm ghost" @click="toast.msg = ''">×</button>
      </div>

      <template v-if="project">
        <div class="workspace">
          <section class="editor card">
            <nav class="tabs">
              <button :class="{ on: view === 'draft' }" @click="view = 'draft'">原稿</button>
              <button :class="{ on: view === 'segments' }" :disabled="!hasSegments" @click="view = 'segments'">
                段落 <span v-if="hasSegments" class="muted">{{ segments.length }}</span>
              </button>
              <div class="spacer" />
              <template v-if="view === 'draft'">
                <button class="sm" :disabled="!!busy" @click="pickSrt" title="导入 SRT 字幕，按字幕时间轴对齐配音">导入字幕</button>
                <button class="sm" :disabled="!draft.trim() || !!busy" @click="plainSplit">按标点分段</button>
                <button class="sm primary" :disabled="!draft.trim() || !!busy" @click="smartSplit">
                  {{ busy === 'LLM 处理中…' ? 'LLM 处理中…' : '智能分段' }}
                </button>
              </template>
              <template v-else>
                <button
                  class="sm" :disabled="progress.running || !todoCount"
                  :title="failCount ? `${failCount} 段失败` : (staleCount ? `${staleCount} 段音频过期` : '')"
                  @click="synthesize(true)"
                >
                  只合成待办 {{ todoCount || '' }}
                </button>
                <button
                  class="sm" :class="todoCount ? 'primary' : ''"
                  :disabled="progress.running" @click="synthesize(false)"
                >
                  {{ progress.running ? `合成中 ${progress.done}/${progress.total}` : '全部重合成' }}
                </button>
              </template>
            </nav>

            <div v-if="view === 'draft'" class="draft-pane">
              <textarea
                v-model="draft"
                class="draft"
                placeholder="把解说稿粘进来。&#10;&#10;智能分段会调 LLM 做三件事：按语义切成 15-25 字的段、插入语气和停顿标签、把数字英文改成中文读法。字幕文本保持原文一字不改。&#10;&#10;不想动原文就点「按标点分段」。"
              />
              <p class="muted count">{{ draft.length }} 字</p>
            </div>

            <div v-else class="seg-list">
              <div v-if="progress.running" class="bar">
                <div class="bar-in" :style="{ width: (progress.done / Math.max(progress.total, 1) * 100) + '%' }" />
              </div>
              <SegmentRow
                v-for="(s, i) in segments" :key="s.id"
                :seg="s" :index="i" :voices="meta.voices" :styles="meta.styles"
                :clones="clones" :project="project" :timeline="timeline"
                @patch="patchSegment"
                @resynth="resynthSegment"
                @busy="say($event, 'err')"
              />
            </div>
          </section>

          <aside class="rightbar">
            <!-- 音色定调：写稿/未合成阶段的主角，合成完折叠成一条 -->
            <section class="card side-sec">
              <button class="sec-head" @click="voiceOpen = !voiceOpen">
                <span class="sec-title">🎙 音色 · 语气</span>
                <span class="sec-sub muted">
                  {{ curVoiceLabel }} · {{ curStyleLabel }}
                  <span class="caret">{{ voiceOpen ? '▴' : '▾' }}</span>
                </span>
              </button>
              <VoicePanel
                v-show="voiceOpen"
                :project="project" :voices="meta.voices" :styles="meta.styles"
                :clones="clones"
                @patch="patchProject" @toast="say"
                @upload-clone="uploadClone" @remove-clone="removeClone"
                @rename-clone="renameClone"
              />
            </section>

            <!-- 合成与导出：段落阶段的主角 -->
            <section class="card export">
              <div class="progress-ring">
                <div class="ring-num"><b>{{ okCount }}</b><span class="muted">/ {{ segments.length }}</span></div>
                <div class="ring-label muted">段已合成 · {{ totalDur }}</div>
                <div class="mini-bar">
                  <div class="mini-in" :style="{ width: (okCount / Math.max(segments.length, 1) * 100) + '%' }" />
                </div>
              </div>

              <p v-if="timeline" class="tl-note">
                🎬 字幕时间轴模式 · 音轨按字幕起始时间对齐
              </p>
              <p v-if="staleCount" class="stale-note">
                {{ staleCount }} 段音频与当前音色/语气不符，重新合成后才能导出
              </p>
              <p v-else-if="failCount" class="stale-note err">
                {{ failCount }} 段合成失败，可点「只合成待办」重试
              </p>
              <p v-if="overflowCount" class="stale-note">
                {{ overflowCount }} 段音频超出字幕时长，导出会顺延后续段，建议精简文本或调快语速
              </p>

              <button class="wide" :disabled="!canExport" @click="toggleFullPlay">
                {{ fullPlaying ? '⏸ 停止' : '▶ 试听全篇' }}
              </button>
              <button class="primary wide" :disabled="!canExport" @click="download(api.exportUrl(project.id, 'mp3'))">
                ⬇ 导出 MP3
              </button>
              <div class="row">
                <button class="wide" :disabled="!canExport" @click="download(api.exportUrl(project.id, 'wav'))">WAV</button>
                <button class="wide" :disabled="!canExport" @click="download(api.srtUrl(project.id))">SRT 字幕</button>
              </div>
              <p class="muted tip">
                <template v-if="timeline">字幕导出用导入的原始时间轴，与视频画面严格对齐。</template>
                <template v-else>字幕用「原稿」文本，不含 [标签]；时间轴按段级对齐，切段越细越准。</template>
              </p>
            </section>
          </aside>
        </div>
      </template>

      <div v-else class="blank muted">
        <p>左边选一个项目，或者新建一个开始配音。</p>
      </div>

      <audio ref="fullAudio" hidden />
      <input ref="srtInput" type="file" accept=".srt,text/plain" hidden @change="onSrtFile" />
    </main>
  </div>
</template>

<style scoped>
.app { display: flex; height: 100vh; gap: 12px; padding: 12px; }

/* 登录门禁 */
.login-mask { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center; padding: 20px; }
.login-box { width: 320px; max-width: 100%; padding: 28px 24px; display: flex; flex-direction: column; gap: 12px; }
.login-box h2 { margin: 0; text-align: center; }
.login-hint { margin: 0; text-align: center; color: var(--muted, #8a94a6); font-size: 14px; }
.login-box input { padding: 10px 12px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel-2); font-size: 15px; }
.login-box button { padding: 10px; font-size: 15px; }
.login-err { margin: 0; color: #ff6b6b; font-size: 13px; text-align: center; }

/* 侧栏 */
.side { width: 232px; flex-shrink: 0; display: flex; flex-direction: column; overflow: hidden; }
.side-head { display: flex; align-items: center; justify-content: space-between; padding: 12px; border-bottom: 1px solid var(--line); }
.plist { list-style: none; margin: 0; padding: 6px; overflow-y: auto; flex: 1; }
.plist li { display: flex; align-items: center; gap: 6px; padding: 8px 9px; border-radius: 8px; cursor: pointer; }
.plist li:hover { background: var(--panel-2); }
.plist li.on { background: rgba(76, 141, 255, .14); }
.pmain { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.pname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pmain small { font-size: 11px; }
.del { opacity: 0; color: var(--muted); padding: 2px 6px; }
.plist li:hover .del { opacity: 1; }
.del:hover { color: var(--err); }
.empty { cursor: default; font-size: 12px; padding: 14px 9px !important; }
.empty:hover { background: none !important; }

/* 主区 */
.main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 12px; }
.topbar { display: flex; align-items: center; gap: 10px; padding: 2px 4px; }
.burger { display: none; }
.title { border: none; background: transparent; font-size: 18px; font-weight: 600; padding: 5px 8px; border-radius: 8px; }
.title:hover { background: var(--panel-2); }
.title:focus { background: var(--panel-2); box-shadow: 0 0 0 3px var(--accent-soft); }
.stat { font-size: 11px; flex-shrink: 0; padding: 3px 9px; border-radius: 999px; background: var(--panel); border: 1px solid var(--line-soft); }

.toast { display: flex; align-items: center; gap: 10px; padding: 10px 14px; border-radius: var(--radius-sm); font-size: 13px; white-space: pre-wrap; border: 1px solid transparent; }
.toast span { flex: 1; }
.toast.info { background: var(--panel-2); }
.toast.ok { background: var(--ok-soft); color: #9ae8c4; border-color: rgba(69,209,154,.25); }
.toast.warn { background: var(--warn-soft); color: #f0cf94; border-color: rgba(230,173,74,.25); }
.toast.err { background: var(--err-soft); color: #f7b0b0; border-color: rgba(244,112,112,.25); }

.workspace { flex: 1; min-height: 0; display: flex; gap: 12px; }
.editor { flex: 1; min-width: 0; display: flex; flex-direction: column; overflow: hidden; }
.tabs { display: flex; align-items: center; gap: 6px; padding: 10px 12px; border-bottom: 1px solid var(--line-soft); }
.tabs > button:not(.sm) { background: transparent; border-color: transparent; font-weight: 500; padding: 6px 14px; }
.tabs > button:not(.sm):hover { background: var(--panel-2); }
.tabs > button.on { background: var(--accent-soft); border-color: transparent; color: #a9c8ff; }
.spacer { flex: 1; }

.draft-pane { flex: 1; display: flex; flex-direction: column; padding: 11px; min-height: 0; }
.draft { flex: 1; resize: none; font-size: 15px; line-height: 1.9; border-color: transparent; }
.count { margin: 7px 2px 0; font-size: 11px; text-align: right; }

.seg-list { flex: 1; overflow-y: auto; position: relative; }
.bar { position: sticky; top: 0; height: 2px; background: var(--line); z-index: 2; }
.bar-in { height: 100%; background: var(--accent); transition: width .2s; }

.rightbar { width: 290px; flex-shrink: 0; display: flex; flex-direction: column; gap: 12px; overflow-y: auto; }

/* 可折叠的音色定调区 */
.side-sec { overflow: hidden; }
.sec-head {
  width: 100%; display: flex; align-items: center; justify-content: space-between;
  padding: 12px 14px; background: transparent; border: none; border-radius: 0; gap: 8px;
}
.sec-head:hover { background: var(--panel-2); }
.sec-title { font-weight: 600; font-size: 14px; }
.sec-sub { font-size: 12px; display: inline-flex; align-items: center; gap: 5px; }
.sec-sub .caret { color: var(--faint); font-size: 10px; }

.export { padding: 16px; display: flex; flex-direction: column; gap: 10px; }
.progress-ring { text-align: center; padding: 4px 0 6px; }
.ring-num { font-size: 15px; }
.ring-num b { font-size: 30px; font-weight: 700; color: var(--ok); }
.ring-label { font-size: 12px; margin-top: 2px; }
.mini-bar { height: 5px; border-radius: 3px; background: var(--panel-3); margin-top: 10px; overflow: hidden; }
.mini-in { height: 100%; background: linear-gradient(90deg, var(--ok), #5be0ac); border-radius: 3px; transition: width .3s; }
.export .row { gap: 8px; }
.export .wide { width: 100%; }
.export .row > button { flex: 1; }
.tip { margin: 4px 0 0; font-size: 11px; line-height: 1.6; }
.stale-note {
  margin: 0; padding: 8px 11px; border-radius: var(--radius-sm); font-size: 12px; line-height: 1.5;
  background: var(--warn-soft); color: #f0cf94;
}
.stale-note.err { background: var(--err-soft); color: #f7b0b0; }
.tl-note {
  margin: 0; padding: 8px 11px; border-radius: var(--radius-sm); font-size: 12px;
  background: var(--accent-soft); color: #a9c8ff;
}

.blank { flex: 1; display: grid; place-items: center; }

/* 移动端 */
@media (max-width: 900px) {
  .app { padding: 8px; gap: 8px; height: auto; min-height: 100vh; }
  .burger { display: block; }
  .side {
    position: fixed; inset: 8px auto 8px 8px; z-index: 20; width: 250px;
    transform: translateX(-115%); transition: transform .2s; box-shadow: 0 8px 32px rgba(0,0,0,.5);
  }
  .side.open { transform: none; }

  /* 竖排后整页滚动，各区块按内容自然展开 —— 嵌套 flex 高度在小屏会塌成一行 */
  .main { min-height: 0; }
  .workspace { flex-direction: column; min-height: 0; flex: none; }
  .editor { overflow: visible; }
  .seg-list { overflow: visible; }
  .draft-pane { min-height: 46vh; }
  .draft { font-size: 16px; } /* iOS 16px 以下会自动放大页面 */
  .rightbar { width: auto; flex-shrink: 1; overflow: visible; }
  .tabs { flex-wrap: wrap; }
}
</style>
