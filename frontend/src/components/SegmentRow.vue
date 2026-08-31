<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({
  seg: { type: Object, required: true },
  index: { type: Number, required: true },
  voices: { type: Array, default: () => [] },
  styles: { type: Array, default: () => [] },
  clones: { type: Array, default: () => [] },
  project: { type: Object, required: true },
})
const emit = defineEmits(['patch', 'busy'])

const expanded = ref(false)   // 展开后才显示音色/语气/停顿/合成文本
const playing = ref(false)
const audioEl = ref(null)

// 段级为空表示继承项目默认值
const inheritedVoice = computed(() => props.project.default_voice)
const inheritedStyle = computed(() => props.project.default_style)

// 这一段实际生效的音色/语气（段级覆盖优先，否则继承项目默认）
const effVoice = computed(() => props.seg.voice ?? inheritedVoice.value)
// 是否克隆音色 + 显示名
const isClone = computed(() => String(effVoice.value).startsWith('clone:'))
const effVoiceLabel = computed(() => {
  if (isClone.value) {
    return props.clones.find((c) => c.voice === effVoice.value)?.name ?? '克隆(已删)'
  }
  return effVoice.value
})
const effStyleId = computed(() => props.seg.style ?? inheritedStyle.value)
const effStyleLabel = computed(
  () => props.styles.find((s) => s.id === effStyleId.value)?.label ?? effStyleId.value ?? '默认')
// 段落是否用了跟项目不同的音色/语气 —— 用来在收起态给个提示点
const overridden = computed(() => props.seg.voice != null || props.seg.style != null)

const styleLabel = effStyleLabel

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
  <div class="seg" :class="[state, { expanded }]">
    <!-- 左侧状态色条 + 序号 -->
    <div class="rail" :title="stateHint">
      <span class="idx">{{ String(index + 1).padStart(2, '0') }}</span>
    </div>

    <div class="body">
      <!-- 主行：字幕文本 -->
      <textarea
        ref="displayEl"
        class="display"
        rows="1"
        :value="seg.display_text"
        @input="autoGrow"
        @change="onDisplayInput"
      />

      <!-- 收起态的元信息条：一眼看清音色语气/时长/状态，点右侧图标操作 -->
      <div class="meta">
        <button
          class="tag-btn" :class="{ over: overridden }"
          :title="overridden ? '本段单独设了音色/语气，点开可改' : '跟随整篇设置，点开可单独改'"
          @click="expanded = !expanded"
        >
          <span class="voice-dot"
                :class="{ female: voices.find(v => v.id === effVoice)?.gender === 'female', clone: isClone }" />
          {{ effVoiceLabel }} · {{ effStyleLabel }}
          <span class="caret">{{ expanded ? '▴' : '▾' }}</span>
        </button>

        <span v-if="tags.length" class="chip accent" title="LLM 插入的音频标签">
          {{ tags.join(' ') }}
        </span>
        <span v-if="modified && !tags.length" class="chip" title="合成文本与字幕不同（改过读法）">已改读法</span>

        <div class="spacer" />

        <span v-if="stale" class="state-txt warn" :title="stateHint">● 音频过期</span>
        <span v-else-if="state === 'failed'" class="state-txt err">● 合成失败</span>
        <span v-else-if="state === 'ok'" class="state-txt ok">{{ dur || '● 已合成' }}</span>
        <span v-else class="state-txt muted">● 待合成</span>

        <button class="sm ghost play-btn" :disabled="state !== 'ok'" @click="play">
          {{ playing ? '⏸' : '▶' }}
        </button>
      </div>

      <!-- 展开区：音色 / 语气 / 停顿 / 合成文本 -->
      <div v-if="expanded" class="detail">
        <div class="detail-row">
          <label>音色</label>
          <select :value="seg.voice ?? ''" @change="patch('voice', $event.target.value || null)">
            <option value="">跟随整篇（{{ inheritedVoice }}）</option>
            <optgroup label="预置音色">
              <option v-for="v in voices" :key="v.id" :value="v.id">{{ v.label }} · {{ v.hint }}</option>
            </optgroup>
            <optgroup v-if="clones.length" label="克隆音色（付费）">
              <option v-for="c in clones" :key="c.voice" :value="c.voice">{{ c.name }}</option>
            </optgroup>
          </select>
        </div>
        <div class="detail-row">
          <label>语气</label>
          <select :value="seg.style ?? ''" @change="patch('style', $event.target.value || null)">
            <option value="">跟随整篇（{{ styleLabel }}）</option>
            <option v-for="s in styles" :key="s.id" :value="s.id">{{ s.label }}</option>
          </select>
        </div>
        <div class="detail-row">
          <label>段后停顿</label>
          <div class="pause">
            <input
              type="number" min="0" max="5000" step="100"
              :value="seg.pause_after_ms"
              @change="patch('pause_after_ms', Number($event.target.value) || 0)"
            />
            <span class="unit muted">ms</span>
          </div>
        </div>
        <div class="detail-row synth">
          <label>合成文本</label>
          <textarea
            rows="2"
            :value="seg.synth_text"
            placeholder="送 TTS 的文本，可含 [标签] 和读法改写；字幕仍用上面那行"
            @change="patch('synth_text', $event.target.value)"
          />
        </div>
      </div>

      <p v-if="seg.error_msg" class="err">{{ seg.error_msg }}</p>
    </div>

    <audio ref="audioEl" hidden />
  </div>
</template>

<style scoped>
.seg {
  display: flex;
  gap: 11px;
  padding: 11px 14px 11px 0;
  border-bottom: 1px solid var(--line-soft);
  transition: background .15s;
}
.seg:last-child { border-bottom: none; }
.seg:hover { background: rgba(255,255,255,.015); }
.seg.expanded { background: rgba(91,155,255,.04); }
.seg.failed { background: var(--err-soft); }
.seg.stale { background: var(--warn-soft); }

/* 左侧状态色条 —— 比小圆点显眼，扫一眼就知道状态分布 */
.rail {
  flex-shrink: 0; width: 34px; display: flex; align-items: flex-start; justify-content: center;
  padding-top: 10px; position: relative;
}
.rail::before {
  content: ''; position: absolute; left: 0; top: 8px; bottom: 8px; width: 3px;
  border-radius: 0 3px 3px 0; background: var(--faint);
}
.seg.ok .rail::before { background: var(--ok); }
.seg.pending .rail::before { background: var(--faint); }
.seg.failed .rail::before { background: var(--err); }
.seg.stale .rail::before { background: var(--warn); }
.idx { font-size: 11px; color: var(--faint); font-variant-numeric: tabular-nums; }
.seg.ok .idx { color: var(--muted); }

.body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px; }
.display {
  border-color: transparent;
  background: transparent;
  overflow: hidden;
  min-height: 34px;
  font-size: 14.5px;
  line-height: 1.75;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
}
.display:hover { background: var(--panel-2); }
.display:focus { background: var(--panel-2); }

/* 收起态元信息条 */
.meta { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding-left: 8px; }
.tag-btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 10px; font-size: 12px; border-radius: 999px;
  background: var(--panel-2); border: 1px solid transparent; color: var(--muted);
}
.tag-btn:hover { background: var(--panel-3); }
.tag-btn.over { color: #a9c8ff; background: var(--accent-soft); }
.voice-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); }
.voice-dot.female { background: #e58bc4; }
.voice-dot.clone { background: var(--warn); }
.caret { color: var(--faint); font-size: 10px; margin-left: 1px; }
.spacer { flex: 1; }

.state-txt { font-size: 12px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.state-txt.ok { color: var(--ok); }
.state-txt.warn { color: var(--warn); }
.state-txt.err { color: var(--err); }
.state-txt.muted { color: var(--faint); }

.play-btn { min-width: 30px; text-align: center; }

/* 展开区 */
.detail {
  margin: 2px 0 4px 8px; padding: 12px; border-radius: var(--radius-sm);
  background: var(--panel-2); display: flex; flex-direction: column; gap: 10px;
}
.detail-row { display: flex; align-items: center; gap: 10px; }
.detail-row > label { margin: 0; width: 60px; flex-shrink: 0; text-align: right; }
.detail-row select { flex: 1; font-size: 13px; }
.detail-row.synth { align-items: flex-start; }
.detail-row.synth textarea { flex: 1; font-family: ui-monospace, Menlo, monospace; font-size: 13px; }
.pause { flex: 1; display: flex; align-items: center; gap: 6px; }
.pause input { width: 90px; text-align: right; }
.unit { font-size: 12px; }

.err { margin: 2px 0 0 8px; color: var(--err); font-size: 12px; word-break: break-all; }

@media (max-width: 720px) {
  .rail { width: 24px; }
  .detail-row { flex-wrap: wrap; }
  .detail-row > label { width: auto; text-align: left; }
}
</style>
