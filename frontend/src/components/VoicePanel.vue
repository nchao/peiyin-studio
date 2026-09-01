<script setup>
import { ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({
  project: { type: Object, required: true },
  voices: { type: Array, default: () => [] },
  styles: { type: Array, default: () => [] },
  clones: { type: Array, default: () => [] },
})
const emit = defineEmits(['patch', 'toast', 'clones-changed', 'remove-clone', 'rename-clone'])

const customStyle = ref('')
const previewing = ref(false)
const audioEl = ref(null)
const sampleEl = ref(null)
const playingSample = ref(0)  // 正在试听的克隆音色 id，0 表示无

// 试听克隆音色的原始样本（不消耗合成，直接放上传的那段人声）
function playSample(id) {
  if (playingSample.value === id) {
    sampleEl.value?.pause()
    playingSample.value = 0
    return
  }
  playingSample.value = id
  const el = sampleEl.value
  el.src = api.cloneSampleUrl(id)
  el.onended = () => { playingSample.value = 0 }
  el.onerror = () => { playingSample.value = 0; emit('toast', '样本播放失败', 'err') }
  el.play().catch((e) => { playingSample.value = 0; emit('toast', `样本播放失败：${e.message}`, 'err') })
}

function renameClone(c) {
  const name = prompt('重命名克隆音色', c.name)
  if (name && name.trim() && name.trim() !== c.name) {
    emit('rename-clone', c.id, name.trim())
  }
}

// 上传克隆音色
const fileInput = ref(null)
const pendingFile = ref(null)
const cloneName = ref('')
const uploading = ref(false)

function pickFile(e) {
  const f = e.target.files?.[0]
  if (f) {
    pendingFile.value = f
    cloneName.value = f.name.replace(/\.[^.]+$/, '')  // 默认用文件名当音色名
  }
}

function cancelPick() {
  pendingFile.value = null
  cloneName.value = ''
  if (fileInput.value) fileInput.value.value = ''
}

async function submitClone() {
  if (!pendingFile.value || !cloneName.value.trim() || uploading.value) return
  uploading.value = true
  try {
    const name = cloneName.value.trim()
    const r = await api.uploadClone(pendingFile.value, name)
    const tip = r?.truncated
      ? `克隆音色「${name}」已添加（样本较长，已自动从中间截取 15s）`
      : `克隆音色「${name}」已添加`
    emit('toast', tip, 'ok')
    emit('clones-changed')          // 让父组件刷新克隆列表
    cancelPick()
  } catch (e) {
    // 失败时保留已选文件和名字，改完再传，不用重新选
    emit('toast', `上传失败：${e.message}`, 'err')
  } finally {
    uploading.value = false
  }
}

const SAMPLE = '这段声音用来试听音色和语气，你可以随时切换再试一次。'

async function tryVoice() {
  previewing.value = true
  try {
    const url = await api.preview({
      text: SAMPLE,
      voice: props.project.default_voice,
      style: customStyle.value.trim() || props.project.default_style,
      speed: props.project.default_speed ?? 1.0,
    })
    audioEl.value.src = url
    await audioEl.value.play()
  } catch (e) {
    emit('toast', `试听失败：${e.message}`, 'err')
  } finally {
    previewing.value = false
  }
}

function applyCustom() {
  const v = customStyle.value.trim()
  if (v) emit('patch', { default_style: v })
}

// 语速滑块：拖动时只更新本地显示，松手（change）才提交，避免狂发请求
const speedShown = ref(props.project.default_speed ?? 1.0)
watch(() => props.project.default_speed, (v) => { speedShown.value = v ?? 1.0 })
function onSpeedInput(e) { speedShown.value = Number(e.target.value) }
function onSpeedCommit(e) {
  const v = Number(e.target.value)
  if (v !== (props.project.default_speed ?? 1.0)) emit('patch', { default_speed: v })
}
function resetSpeed() {
  speedShown.value = 1.0
  if ((props.project.default_speed ?? 1.0) !== 1.0) emit('patch', { default_speed: 1.0 })
}
</script>

<template>
  <div class="panel">
    <div class="field">
      <label>音色（整篇默认，段落可单独覆盖）</label>
      <div class="voice-grid">
        <button
          v-for="v in voices" :key="v.id"
          class="voice"
          :class="{ on: project.default_voice === v.id, female: v.gender === 'female' }"
          @click="emit('patch', { default_voice: v.id })"
        >
          <b>{{ v.label }}</b>
          <small>{{ v.hint }}</small>
        </button>
      </div>
    </div>

    <div class="field">
      <label>我的克隆音色 <span class="paid-tag">限时免费</span></label>
      <div v-if="clones.length" class="voice-grid">
        <button
          v-for="c in clones" :key="c.id"
          class="voice clone"
          :class="{ on: project.default_voice === c.voice }"
          @click="emit('patch', { default_voice: c.voice })"
        >
          <b>{{ c.name }}</b>
          <small>{{ c.duration_ms ? (c.duration_ms/1000).toFixed(1)+'s 样本' : '克隆音色' }}</small>
          <span class="clone-acts">
            <span class="c-act" title="试听样本"
                  @click.stop="playSample(c.id)">{{ playingSample === c.id ? '⏸' : '▶' }}</span>
            <span class="c-act" title="重命名" @click.stop="renameClone(c)">✎</span>
            <span class="c-act del" title="删除此克隆音色"
                  @click.stop="emit('remove-clone', c.id, c.name)">×</span>
          </span>
        </button>
      </div>
      <p v-else class="empty-clone muted">还没有克隆音色。上传一段人声样本（3–30s，wav/mp3/m4a/flac 等），做出专属音色。</p>

      <!-- 上传入口：未选文件只显示一个按钮，选完文件展开成填名+确认的卡片 -->
      <input ref="fileInput" type="file" hidden
             accept=".wav,.mp3,.m4a,.aac,.flac,.ogg,.oga,.opus,.wma,audio/*"
             @change="pickFile" />

      <button v-if="!pendingFile" class="pick-btn" :disabled="uploading"
              @click="fileInput?.click()">
        ＋ 上传人声样本
      </button>

      <div v-else class="upload-card">
        <div class="picked" :title="pendingFile.name">📄 {{ pendingFile.name }}</div>
        <input v-model="cloneName" class="clone-name" placeholder="给音色起个名，如 孙悟空"
               :disabled="uploading" @keyup.enter="submitClone" />
        <div class="upload-acts">
          <button class="sm ghost" :disabled="uploading" @click="cancelPick">取消</button>
          <button class="sm primary" :disabled="!cloneName.trim() || uploading" @click="submitClone">
            {{ uploading ? '上传中…' : '确认上传' }}
          </button>
        </div>
      </div>
      <p class="hint2 muted">支持 wav/mp3/m4a/aac/flac/ogg/opus 等，至少 3s；超过 15s 会自动从中间截取一段。样本越干净，克隆越像。</p>
    </div>

    <div class="field">
      <label>语气（与音色独立，同一个音色可换任意语气）</label>
      <div class="style-grid">
        <button
          v-for="s in styles" :key="s.id"
          class="style"
          :class="{ on: project.default_style === s.id }"
          @click="emit('patch', { default_style: s.id }); customStyle = ''"
        >{{ s.label }}</button>
      </div>
    </div>

    <div class="field">
      <label>自定义语气词（MiMo 接受预设外的描述，空格分隔）</label>
      <div class="row">
        <input
          v-model="customStyle"
          placeholder="如：东北话 俏皮 气声"
          @keyup.enter="applyCustom"
        />
        <button class="sm" :disabled="!customStyle.trim()" @click="applyCustom">应用</button>
      </div>
      <p v-if="!styles.some(s => s.id === project.default_style)" class="hint mono">
        当前自定义：{{ project.default_style }}
      </p>
    </div>

    <div class="field">
      <label>
        语速（整篇默认，段落可单独覆盖）
        <span class="speed-val">{{ speedShown.toFixed(2) }}×</span>
        <button v-if="speedShown !== 1" class="speed-reset" title="复位到 1.0×" @click="resetSpeed">复位</button>
      </label>
      <div class="speed-row">
        <span class="speed-tick muted">慢</span>
        <input
          type="range" class="speed-slider" min="0.5" max="2" step="0.05"
          :value="speedShown" @input="onSpeedInput" @change="onSpeedCommit"
        />
        <span class="speed-tick muted">快</span>
      </div>
    </div>

    <button class="primary try" :disabled="previewing" @click="tryVoice">
      {{ previewing ? '合成中…' : '试听当前音色 + 语气' }}
    </button>
    <audio ref="audioEl" hidden />
    <audio ref="sampleEl" hidden />
  </div>
</template>

<style scoped>
.panel { padding: 4px 14px 16px; display: flex; flex-direction: column; gap: 16px; }
.field { display: flex; flex-direction: column; }

.voice-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.voice { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; padding: 10px 12px; text-align: left; border-radius: var(--radius-sm); }
.voice b { font-weight: 600; }
.voice small { font-size: 11px; color: var(--muted); }
.voice.on { border-color: var(--accent-line); background: var(--accent-soft); box-shadow: 0 0 0 3px var(--accent-soft); }
.voice.on small { color: #a8c4f5; }

.style-grid { display: flex; flex-wrap: wrap; gap: 7px; }
.style { font-size: 12px; padding: 6px 13px; border-radius: 999px; }
.style.on { border-color: var(--accent-line); background: var(--accent-soft); color: #cfe0ff; }

/* 语速滑块 */
.speed-val { color: var(--accent); font-variant-numeric: tabular-nums; margin-left: 6px; font-weight: 600; }
.speed-reset {
  font-size: 11px; padding: 1px 8px; margin-left: 8px; border-radius: 999px;
  background: var(--panel-2); color: var(--muted);
}
.speed-reset:hover { color: var(--text); }
.speed-row { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
.speed-tick { font-size: 11px; flex-shrink: 0; }
.speed-slider { flex: 1; accent-color: var(--accent); cursor: pointer; height: 4px; }

.hint { margin: 6px 0 0; font-size: 11px; color: var(--accent); }
.try { margin-top: 2px; }

/* 克隆音色 */
.paid-tag {
  display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 999px;
  background: var(--warn-soft); color: var(--warn); margin-left: 4px;
}
.voice.clone { position: relative; padding-right: 12px; }
.voice.clone.on { border-color: var(--warn); background: var(--warn-soft); box-shadow: 0 0 0 3px var(--warn-soft); }
.clone-acts {
  position: absolute; top: 4px; right: 5px; display: inline-flex; gap: 1px;
  opacity: 0; transition: opacity .12s;
}
.voice.clone:hover .clone-acts, .voice.clone.on .clone-acts { opacity: 1; }
.c-act {
  color: var(--muted); font-size: 12px; line-height: 1; padding: 3px 4px;
  border-radius: 5px; min-width: 18px; text-align: center;
}
.c-act:hover { color: var(--text); background: var(--panel-3); }
.c-act.del:hover { color: var(--err); }
.empty-clone { margin: 0 0 8px; font-size: 12px; line-height: 1.6; }

/* 上传：未选文件是一个虚线大按钮，选完展开成填名卡片 */
.pick-btn {
  width: 100%; padding: 10px; border: 1px dashed var(--line); border-radius: var(--radius-sm);
  background: var(--panel-2); color: var(--muted); font-size: 13px; cursor: pointer;
}
.pick-btn:hover:not(:disabled) { border-color: var(--accent-line); color: #a9c8ff; background: var(--accent-soft); }
.upload-card {
  display: flex; flex-direction: column; gap: 8px; padding: 10px;
  border: 1px solid var(--accent-line); border-radius: var(--radius-sm); background: var(--accent-soft);
}
.picked { font-size: 12px; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.clone-name { font-size: 13px; padding: 7px 9px; }
.upload-acts { display: flex; gap: 7px; justify-content: flex-end; }
.hint2 { margin: 8px 0 0; font-size: 11px; line-height: 1.6; }
</style>
