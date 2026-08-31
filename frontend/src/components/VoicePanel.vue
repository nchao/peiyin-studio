<script setup>
import { ref } from 'vue'
import { api } from '../api'

const props = defineProps({
  project: { type: Object, required: true },
  voices: { type: Array, default: () => [] },
  styles: { type: Array, default: () => [] },
})
const emit = defineEmits(['patch', 'toast'])

const customStyle = ref('')
const previewing = ref(false)
const audioEl = ref(null)

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
</style>
