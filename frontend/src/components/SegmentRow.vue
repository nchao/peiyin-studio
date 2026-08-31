<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({
  seg: { type: Object, required: true },
  index: { type: Number, required: true },
  voices: { type: Array, default: () => [] },
  styles: { type: Array, default: () => [] },
  project: { type: Object, required: true },
})
const emit = defineEmits(['patch', 'busy'])

const showSynth = ref(false)
const playing = ref(false)
const audioEl = ref(null)

// 段级为空表示继承项目默认值
const inheritedVoice = computed(() => props.project.default_voice)
const inheritedStyle = computed(() => props.project.default_style)

const styleLabel = computed(() => {
  const id = props.seg.style ?? inheritedStyle.value
  return props.styles.find((s) => s.id === id)?.label ?? id ?? '无'
})

const dur = computed(() =>
  props.seg.duration_ms ? (props.seg.duration_ms / 1000).toFixed(1) + 's' : '',
)

// 有音频但与当前音色/语气不符 —— 改过项目设置，需要重新合成
const stale = computed(() => !!props.seg.audio_hash && props.seg.fresh === false)

const state = computed(() => {
  if (props.seg.status === 'failed') return 'failed'
  if (stale.value) return 'stale'
  return props.seg.status
})

const stateHint = computed(() => ({
  ok: '已合成',
  pending: '待合成',
  failed: '合成失败',
  stale: '音色或语气已改，音频过期，需重新合成',
}[state.value]))

// synth_text 与 display_text 不同 → LLM 动过读法或插了标签
const modified = computed(() => props.seg.synth_text !== props.seg.display_text)
const tags = computed(() => props.seg.synth_text.match(/\[[^\]]{1,8}\]/g) ?? [])

// 文本框跟着内容长高，不留空白也不出滚动条
function fit(el) {
  if (!el) return
  el.style.height = 'auto'
  el.style.height = el.scrollHeight + 'px'
}
function autoGrow(e) {
  fit(e.target)
}

const displayEl = ref(null)
onMounted(() => fit(displayEl.value))
watch(() => props.seg.display_text, () => nextTick(() => fit(displayEl.value)))

function patch(field, value) {
  if (value === props.seg[field]) return
  emit('patch', props.seg.id, { [field]: value })
}

function onDisplayInput(e) {
  const v = e.target.value
  // 改字幕文本时，若之前没单独改过合成文本，两者同步
  emit('patch', props.seg.id, modified.value ? { display_text: v } : { display_text: v, synth_text: v })
}

async function play() {
  if (playing.value) {
    audioEl.value?.pause()
    playing.value = false
    return
  }
  playing.value = true
  try {
    const el = audioEl.value
    el.src = api.segmentAudioUrl(props.seg.id)
    el.onended = () => { playing.value = false }
    await el.play()
  } catch (e) {
    playing.value = false
    emit('busy', `试听失败：${e.message}`)
  }
}
</script>

<template>
  <div class="seg" :class="state">
    <div class="gutter">
      <span class="idx mono">{{ String(index + 1).padStart(2, '0') }}</span>
      <span class="dot" :title="stateHint" />
    </div>

    <div class="body">
      <textarea
        ref="displayEl"
        class="display"
        rows="1"
        :value="seg.display_text"
        @input="autoGrow"
        @change="onDisplayInput"
      />

      <div v-if="showSynth" class="synth-box">
        <label>合成文本（送 TTS，可含 [标签] 和读法改写；字幕用上面那行）</label>
        <textarea
          rows="2"
          :value="seg.synth_text"
          @change="patch('synth_text', $event.target.value)"
        />
      </div>

      <div class="ctrls">
        <select :value="seg.voice ?? ''" @change="patch('voice', $event.target.value || null)">
          <option value="">音色：跟随（{{ inheritedVoice }}）</option>
          <option v-for="v in voices" :key="v.id" :value="v.id">
            {{ v.label }} · {{ v.hint }}
          </option>
        </select>

        <select :value="seg.style ?? ''" @change="patch('style', $event.target.value || null)">
          <option value="">语气：跟随（{{ styleLabel }}）</option>
          <option v-for="s in styles" :key="s.id" :value="s.id">{{ s.label }}</option>
        </select>

        <div class="pause">
          <input
            type="number" min="0" max="5000" step="100"
            :value="seg.pause_after_ms"
            @change="patch('pause_after_ms', Number($event.target.value) || 0)"
          />
          <span class="unit muted">ms 停顿</span>
        </div>

        <div class="spacer" />

        <span v-if="tags.length" class="tagchips mono" :title="'插入的音频标签'">
          {{ tags.join(' ') }}
        </span>
        <span v-if="stale" class="stale-tag" :title="stateHint">音频过期</span>
        <span v-else-if="dur" class="muted mono">{{ dur }}</span>

        <button class="sm ghost" :class="{ on: showSynth }" @click="showSynth = !showSynth">
          {{ showSynth ? '收起' : (modified ? '合成文本*' : '合成文本') }}
        </button>
        <button class="sm ghost" :disabled="state !== 'ok'" @click="play">
          {{ playing ? '停止' : '试听' }}
        </button>
      </div>

      <p v-if="seg.error_msg" class="err">{{ seg.error_msg }}</p>
    </div>

    <audio ref="audioEl" hidden />
  </div>
</template>

<style scoped>
.seg {
  display: flex;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
}
.seg:last-child { border-bottom: none; }
.seg.failed { background: rgba(242, 109, 109, .06); }
.seg.stale { background: rgba(224, 163, 62, .05); }

.gutter { display: flex; flex-direction: column; align-items: center; gap: 6px; padding-top: 6px; }
.idx { font-size: 11px; color: var(--muted); }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
.seg.ok .dot { background: var(--ok); }
.seg.pending .dot { background: var(--warn); }
.seg.failed .dot { background: var(--err); }
.seg.stale .dot { background: var(--warn); box-shadow: 0 0 0 3px rgba(224,163,62,.18); }

.body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 7px; }
.display {
  border-color: transparent;
  background: var(--panel-2);
  overflow: hidden;
  min-height: 36px;
}
.synth-box textarea { font-family: ui-monospace, Menlo, monospace; font-size: 13px; }

.ctrls { display: flex; gap: 7px; align-items: center; flex-wrap: wrap; }
.ctrls select { width: auto; min-width: 130px; max-width: 190px; font-size: 12px; padding: 4px 24px 4px 8px; }
.pause { display: flex; align-items: center; gap: 4px; }
.pause input { width: 68px; font-size: 12px; padding: 4px 6px; text-align: right; }
.unit { font-size: 11px; }
.spacer { flex: 1; }
.tagchips { font-size: 11px; color: var(--accent); }
.stale-tag { font-size: 11px; color: var(--warn); white-space: nowrap; }
button.on { border-color: var(--accent); color: var(--accent); }
.err { margin: 0; color: var(--err); font-size: 12px; word-break: break-all; }

@media (max-width: 720px) {
  .ctrls select { min-width: 0; flex: 1 1 45%; max-width: none; }
  .spacer { display: none; }
}
</style>
