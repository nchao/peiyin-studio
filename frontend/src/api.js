async function req(url, opts = {}) {
  const r = await fetch(url, {
    headers: opts.body ? { 'Content-Type': 'application/json' } : {},
    ...opts,
  })
  if (!r.ok) {
    let msg = `HTTP ${r.status}`
    try {
      const d = await r.json()
      msg = typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail ?? d)
    } catch {
      /* 响应不是 JSON，用状态码兜底 */
    }
    throw new Error(msg)
  }
  return r.status === 204 ? null : r.json()
}

const json = (body) => ({ body: JSON.stringify(body) })

export const api = {
  authStatus: () => req('/api/auth-status'),
  login: (password) => req('/api/login', { method: 'POST', ...json({ password }) }),
  logout: () => req('/api/logout', { method: 'POST' }),

  meta: () => req('/api/meta'),

  listClones: () => req('/api/voice-clones'),
  renameClone: (id, name) =>
    req(`/api/voice-clones/${id}`, { method: 'PATCH', ...json({ name }) }),
  deleteClone: (id, force = false) =>
    req(`/api/voice-clones/${id}?force=${force}`, { method: 'DELETE' }),
  cloneSampleUrl: (id) => `/api/voice-clones/${id}/sample?t=${Date.now()}`,
  async uploadClone(file, name) {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('name', name)
    const r = await fetch('/api/voice-clones', { method: 'POST', body: fd })
    if (!r.ok) {
      let msg = `HTTP ${r.status}`
      try { const d = await r.json(); msg = d.detail ?? msg } catch { /* 忽略 */ }
      throw new Error(msg)
    }
    return r.json()
  },

  listProjects: () => req('/api/projects'),
  createProject: (b) => req('/api/projects', { method: 'POST', ...json(b) }),
  getProject: (id) => req(`/api/projects/${id}`),
  patchProject: (id, b) => req(`/api/projects/${id}`, { method: 'PATCH', ...json(b) }),
  deleteProject: (id) => req(`/api/projects/${id}`, { method: 'DELETE' }),

  preprocess: (id) => req(`/api/projects/${id}/preprocess`, { method: 'POST' }),
  ruleSplit: (id) => req(`/api/projects/${id}/split`, { method: 'POST' }),
  importSrt: (id, content) =>
    req(`/api/projects/${id}/import-srt`, { method: 'POST', ...json({ content }) }),
  replaceSegments: (id, segments) =>
    req(`/api/projects/${id}/segments`, { method: 'PUT', ...json({ segments }) }),
  patchSegment: (sid, b) => req(`/api/segments/${sid}`, { method: 'PATCH', ...json(b) }),
  synthesizeSegment: (sid) => req(`/api/segments/${sid}/synthesize`, { method: 'POST' }),

  segmentAudioUrl: (sid) => `/api/segments/${sid}/audio?t=${Date.now()}`,
  fullPreviewUrl: (id) => `/api/projects/${id}/preview?t=${Date.now()}`,
  exportUrl: (id, fmt) => `/api/projects/${id}/export?fmt=${fmt}`,
  srtUrl: (id) => `/api/projects/${id}/srt`,

  async preview(body) {
    const r = await fetch('/api/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!r.ok) {
      let msg = `HTTP ${r.status}`
      try {
        const d = await r.json()
        msg = d.detail ?? msg
      } catch { /* 忽略 */ }
      throw new Error(msg)
    }
    return URL.createObjectURL(await r.blob())
  },

  /** 合成，SSE 逐条回调。返回最后的 summary。 */
  async synthesize(id, { onlyFailed = false } = {}, onEvent) {
    const r = await fetch(
      `/api/projects/${id}/synthesize?only_failed=${onlyFailed}`,
      { method: 'POST' },
    )
    if (!r.ok) throw new Error(`合成请求失败 HTTP ${r.status}`)

    const reader = r.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    let summary = null

    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const parts = buf.split('\n\n')
      buf = parts.pop() ?? ''
      for (const part of parts) {
        const line = part.split('\n').find((l) => l.startsWith('data: '))
        if (!line) continue
        const ev = JSON.parse(line.slice(6))
        if (ev.type === 'summary') summary = ev
        onEvent?.(ev)
      }
    }
    return summary
  },
}
