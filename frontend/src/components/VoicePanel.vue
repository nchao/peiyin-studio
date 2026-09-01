<script setup>
import { ref } from 'vue'
import { api } from '../api'

const props = defineProps({
  project: { type: Object, required: true },
  voices: { type: Array, default: () => [] },
  styles: { type: Array, default: () => [] },
  clones: { type: Array, default: () => [] },
})
const emit = defineEmits(['patch', 'toast', 'upload-clone', 'remove-clone'])

const customStyle = ref('')
const previewing = ref(false)
const audioEl = ref(null)

// 上传克隆音色
const fileInput = ref(null)
const pendingFile = ref(null)
const cloneName = ref('')

function pickFile(e) {
  const f = e.target.files?.[0]
  if (f) { pendingFile.value = f; if (!cloneName.value) cloneName.value = f.name.replace(/\.[^.]+$/, '') }
}
function submitClone() {
  if (!pendingFile.value || !cloneName.value.trim()) return
  emit('upload-clone', pendingFile.value, cloneName.value.trim())
  pendingFile.value = null
  cloneName.value = ''
  if (fileInput.value) fileInput.value.value = ''
}

const SAMPLE = '这段声音用来试听音色和语气，你可以随时切换再试一次。'

async function tryVoice() {
  previewing.value = true
  try {
    const url = await api.preview({
      text: SAMPLE,
      voice: props.project.default_voice,
      style: customStyle.value.trim() || props.project.default_style,
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
          <span class="del-clone" title="删除此克隆音色"
                @click.stop="emit('remove-clone', c.id, c.name)">×</span>
        </button>
      </div>
      <p v-else class="empty-clone muted">还没有克隆音色。上传一段人声样本（3–30s），做出专属音色。</p>

      <!-- 上传入口 -->
      <div class="upload">
        <input ref="fileInput" type="file" accept=".wav,.mp3,audio/wav,audio/mpeg"
               class="file-in" @change="pickFile" />
        <template v-if="pendingFile">
          <input v-model="cloneName" class="clone-name" placeholder="给音色起个名，如 孙悟空"
                 @keyup.enter="submitClone" />
          <button class="sm primary" :disabled="!cloneName.trim()" @click="submitClone">上传</button>
        </template>
      </div>
      <p class="hint2 muted">克隆音色走 voiceclone 模型（MiMo 当前限时免费，后续可能收费）。样本越干净，克隆越像。</p>
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

    <button class="primary try" :disabled="previewing" @click="tryVoice">
      {{ previewing ? '合成中…' : '试听当前音色 + 语气' }}
    </button>
    <audio ref="audioEl" hidden />
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

.hint { margin: 6px 0 0; font-size: 11px; color: var(--accent); }
.try { margin-top: 2px; }

/* 克隆音色 */
.paid-tag {
  display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 999px;
  background: var(--warn-soft); color: var(--warn); margin-left: 4px;
}
.voice.clone { position: relative; }
.voice.clone.on { border-color: var(--warn); background: var(--warn-soft); box-shadow: 0 0 0 3px var(--warn-soft); }
.del-clone {
  position: absolute; top: 4px; right: 6px; color: var(--faint); font-size: 14px;
  line-height: 1; padding: 2px 4px; border-radius: 5px;
}
.del-clone:hover { color: var(--err); background: var(--panel-3); }
.empty-clone { margin: 0 0 8px; font-size: 12px; line-height: 1.6; }
.upload { display: flex; gap: 7px; align-items: center; flex-wrap: wrap; }
.file-in { font-size: 12px; flex: 1; min-width: 0; }
.file-in::file-selector-button {
  font: inherit; font-size: 12px; padding: 5px 10px; margin-right: 8px;
  border-radius: 7px; border: 1px solid var(--line); background: var(--panel-2);
  color: var(--text); cursor: pointer;
}
.clone-name { flex: 1; min-width: 120px; font-size: 13px; }
.hint2 { margin: 8px 0 0; font-size: 11px; line-height: 1.6; }
</style>
