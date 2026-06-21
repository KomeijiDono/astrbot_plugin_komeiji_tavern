import { createApp, ref, computed, onMounted } from 'vue/dist/vue.esm-bundler.js'
import './style.css'

const API = '/api/plug/astrbot_plugin_komeiji_tavern/v1'

const request = async (path, options = {}) => {
  const bridge = window.AstrBotPluginPage
  if (bridge) {
    await bridge.ready()
    const endpoint = 'v1/' + path.replace(/^\/+/, '').replace(/\?.*$/, '')
    const params = Object.fromEntries(new URLSearchParams(path.includes('?') ? path.split('?')[1] : ''))
    const data = (options.method || 'GET') === 'GET'
      ? await bridge.apiGet(endpoint, params)
      : await bridge.apiPost(endpoint, options.body ? JSON.parse(options.body) : {})
    return { data }
  }
  const response = await fetch(API + path, { headers: { 'Content-Type': 'application/json' }, ...options })
  const payload = await response.json()
  if (!response.ok || payload.status !== 'ok') throw new Error(payload.message || '请求失败')
  return payload
}

const post = (path, data) => request(path, { method: 'POST', body: JSON.stringify(data) })

const labels = {
  character: '角色卡',
  preset: '提示词预设',
  lorebook: '世界书',
  persona: '用户设定',
  character_group: '角色组',
  material: '创作素材',
}

const tabs = [
  ['home', '开始'],
  ['character', '角色卡'],
  ['preset', '提示词预设'],
  ['lorebook', '世界书'],
  ['persona', '用户设定'],
  ['bindings', '绑定管理'],
  ['debug', '调试器'],
  ['help', '使用说明'],
]

const blocks = [
  ['main', '主提示词', 0],
  ['world_before', '世界书（角色前）', 10],
  ['character', '角色描述', 15],
  ['personality', '角色性格', 20],
  ['scenario', '场景', 25],
  ['persona', '用户设定', 30],
  ['world_after', '世界书（角色后）', 35],
  ['author_note', '作者注', 40],
  ['summary', '摘要', 50],
  ['examples', '示例消息', 60],
  ['memory', '向量记忆', 70],
  ['post_history', '历史后指令', 5],
].map(x => ({
  identifier: x[0],
  name: x[1],
  priority: x[2],
  enabled: true,
  role: 'system',
  position: x[0] === 'examples' ? 'examples' : x[0] === 'post_history' ? 'depth' : 'system',
  depth: 0,
}))

const newEntry = () => ({
  uid: crypto.randomUUID(),
  comment: '新条目',
  key: [],
  keysecondary: [],
  content: '',
  constant: false,
  disable: false,
  selective: false,
  selectiveLogic: 0,
  position: 1,
  depth: 4,
  role: 'system',
  order: 100,
  probability: 100,
  useProbability: true,
  sticky: 0,
  cooldown: 0,
  delay: 0,
  outletName: '',
})

createApp({
  setup() {
    const tab = ref('home')
    const overview = ref({ counts: {}, tasks: [] })
    const documents = ref([])
    const bindings = ref([])
    const personas = ref([])
    const conversations = ref([])
    const selected = ref(null)
    const error = ref('')
    const notice = ref('')
    const busy = ref(false)
    const file = ref(null)
    const advanced = ref('')
    const sQuery = ref('')
    const sOpen = ref(false)
    const sFocused = ref(false)
    const dQuery = ref('')
    const dOpen = ref(false)
    const dFocused = ref(false)

    const binding = ref({ scope_type: 'session', scope_id: '', kind: 'character', target_id: '', priority: 0 })
    const debug = ref({ session_id: '', persona_id: '', prompt: '', system_prompt: '', mode: 'normal', quiet_prompt: '', seed: 1 })
    const debugResult = ref(null)

    const docsForTab = computed(() => documents.value.filter(x => x.kind === tab.value))
    const bindDocs = computed(() => documents.value.filter(x => x.kind === binding.value.kind))
    const card = computed(() => selected.value?.data?.data && typeof selected.value.data.data === 'object' ? selected.value.data.data : selected.value?.data || {})
    const entries = computed(() => { const x = selected.value?.data?.entries || []; return Array.isArray(x) ? x : Object.values(x) })

    const sFiltered = computed(() => {
      const q = sQuery.value.toLowerCase()
      return conversations.value.filter(x => !q || (x.title + ' ' + x.id + ' ' + x.platform).toLowerCase().includes(q))
    })

    const sessionOptions = computed(() => {
      const byId = new Map(conversations.value.map(x => [x.id, x]))
      for (const b of bindings.value.filter(x => x.scope_type === 'session')) {
        if (!byId.has(b.scope_id)) {
          const p = b.scope_id.split(':', 3)
          byId.set(b.scope_id, { id: b.scope_id, title: '已绑定会话 · ' + (p[1] || '会话') + ' · ' + (p[2] || b.scope_id), platform: p[0] || '', persona_id: '', source: 'binding' })
        }
      }
      return Array.from(byId.values())
    })

    const dFiltered = computed(() => {
      const q = dQuery.value.toLowerCase()
      return sessionOptions.value.filter(x => !q || (x.title + ' ' + x.id + ' ' + x.platform).toLowerCase().includes(q))
    })

    const sessionDisplay = computed({
      get() {
        if (sFocused.value) return sQuery.value
        const c = conversations.value.find(x => x.id === binding.value.scope_id)
        return c ? c.title + ' · ' + c.platform : (binding.value.scope_id || '')
      },
      set(v) { sQuery.value = v; binding.value.scope_id = v },
    })

    const debugDisplay = computed({
      get() {
        if (dFocused.value) return dQuery.value
        const c = sessionOptions.value.find(x => x.id === debug.value.session_id)
        return c ? c.title + ' · ' + c.platform : (debug.value.session_id || '')
      },
      set(v) { dQuery.value = v; debug.value.session_id = v; selectConversation() },
    })

    const clear = () => { error.value = ''; notice.value = '' }

    const load = async () => {
      try {
        const x = await Promise.all([
          request('/overview'),
          request('/documents'),
          request('/bindings'),
          request('/catalog/personas'),
          request('/catalog/conversations?page_size=100'),
        ])
        overview.value = x[0].data
        documents.value = x[1].data
        bindings.value = x[2].data
        personas.value = x[3].data
        conversations.value = x[4].data.items || []
        if (!debug.value.session_id) {
          const bound = bindings.value.find(b => b.scope_type === 'session')
          if (bound) { debug.value.session_id = bound.scope_id; selectConversation() }
        }
        const warnings = x[4].data.warnings || []
        if (warnings.length) notice.value = warnings.join('；')
      } catch (e) {
        error.value = e.message
      }
    }

    const choose = d => {
      selected.value = JSON.parse(JSON.stringify(d))
      if (d.kind === 'lorebook' && !Array.isArray(selected.value.data.entries)) {
        selected.value.data.entries = Object.values(selected.value.data.entries || {})
      }
      advanced.value = JSON.stringify(selected.value.data, null, 2)
      clear()
    }

    const createDoc = kind => {
      const t = {
        character: { data: { name: '新角色', description: '', personality: '', scenario: '', first_mes: '', mes_example: '', system_prompt: '', post_history_instructions: '' } },
        preset: { main_prompt: '{{original_system}}', post_history_instructions: '', allow_character_main_override: false, allow_character_phi_override: true, blocks: JSON.parse(JSON.stringify(blocks)) },
        lorebook: { entries: [] },
        persona: { content: '' },
      }
      choose({ kind, name: '新' + labels[kind], data: t[kind] })
      tab.value = kind
    }

    const save = async () => {
      busy.value = true; clear()
      try {
        const check = await post('/documents/validate', selected.value)
        if (!check.data.valid) throw new Error(check.data.errors.join('；'))
        selected.value.data = check.data.normalized
        const out = await post('/documents', selected.value)
        await load()
        choose(documents.value.find(x => x.id === out.data.id))
        notice.value = '已保存。'
      } catch (e) {
        error.value = e.message
      } finally {
        busy.value = false
      }
    }

    const remove = async () => {
      if (!selected.value?.id || !confirm('确定删除"' + selected.value.name + '"吗？')) return
      await post('/documents/delete', { id: selected.value.id })
      selected.value = null
      await load()
    }

    const duplicate = async () => {
      const out = await post('/documents/duplicate', { id: selected.value.id })
      await load()
      choose(documents.value.find(x => x.id === out.data.id))
    }

    const applyAdvanced = () => {
      try {
        selected.value.data = JSON.parse(advanced.value)
        notice.value = '高级 JSON 已应用，仍需保存。'
      } catch (e) {
        error.value = 'JSON 格式错误：' + e.message
      }
    }

    const importData = async () => {
      if (!file.value) return
      busy.value = true; clear()
      try {
        const binary = file.value.name.toLowerCase().endsWith('.png')
        const content = binary ? '' : await file.value.text()
        const base64 = binary ? await new Promise((ok, fail) => {
          const r = new FileReader()
          r.onload = () => ok(String(r.result).split(',')[1])
          r.onerror = fail
          r.readAsDataURL(file.value)
        }) : ''
        const pre = await post('/import/preview', { content, base64, file_name: file.value.name })
        const info = pre.data.preview
        if (!confirm('识别为' + (labels[info.kind] || info.kind) + '"' + info.name + '"，共 ' + info.count + ' 项。确认导入？')) return
        const out = await post('/import/commit', { parsed: pre.data.parsed, file_name: file.value.name })
        await load()
        binding.value.kind = out.data.kind
        binding.value.target_id = out.data.id
        tab.value = 'bindings'
        notice.value = '导入完成。请选择目标并绑定，当前尚未影响任何会话。'
      } catch (e) {
        error.value = e.message
      } finally {
        busy.value = false
      }
    }

    const updateScope = () => {
      binding.value.scope_id = binding.value.scope_type === 'global' ? '*' : ''
      sQuery.value = ''
      sOpen.value = false
    }

    const addBinding = async () => {
      clear()
      if (!binding.value.target_id || !binding.value.scope_id) { error.value = '请选择资料和绑定目标。'; return }
      await post('/bindings', binding.value)
      await load()
      notice.value = '绑定已生效。'
    }

    const unbind = async i => { await post('/bindings/delete', i); await load() }

    const scopeName = i =>
      i.scope_type === 'global' ? '全局'
      : i.scope_type === 'persona' ? 'Persona：' + i.scope_id
      : i.scope_type === 'session' ? '会话：' + i.scope_id
      : i.scope_type + '：' + i.scope_id

    const move = (i, n) => {
      const b = selected.value.data.blocks
      const j = i + n
      if (j >= 0 && j < b.length) [b[i], b[j]] = [b[j], b[i]]
    }

    const addBlock = () => selected.value.data.blocks.push({
      identifier: 'custom_' + Date.now(),
      name: '自定义提示词',
      content: '',
      enabled: true,
      role: 'system',
      position: 'system',
      depth: 0,
      priority: 50,
    })

    const keyText = v => Array.isArray(v) ? v.join(', ') : String(v || '')
    const setKeys = (e, f, v) => e[f] = v.split(',').map(x => x.trim()).filter(Boolean)

    const selectConversation = () => {
      debug.value.persona_id = conversations.value.find(x => x.id === debug.value.session_id)?.persona_id || ''
    }

    const pickSession = c => { binding.value.scope_id = c.id; sQuery.value = ''; sOpen.value = false }
    const onSFocus = () => { sFocused.value = true; sOpen.value = true }
    const onSBlur = () => { sFocused.value = false; setTimeout(() => sOpen.value = false, 150) }
    const pickDebug = c => { debug.value.session_id = c.id; selectConversation(); dQuery.value = ''; dOpen.value = false }
    const onDFocus = () => { dFocused.value = true; dOpen.value = true }
    const onDBlur = () => { dFocused.value = false; setTimeout(() => dOpen.value = false, 150) }

    const simulate = async () => {
      clear()
      if (!debug.value.session_id && bindings.value.some(b => b.scope_type === 'session')) {
        error.value = '当前资料绑定在具体会话上。请先选择会话，否则模拟只会使用全局绑定。'
        return
      }
      busy.value = true
      try {
        debugResult.value = (await post('/simulate', debug.value)).data
      } catch (e) {
        error.value = e.message
      } finally {
        busy.value = false
      }
    }

    const actual = async () => {
      clear()
      try {
        debugResult.value = (await request('/preview/' + encodeURIComponent(debug.value.session_id))).data
      } catch (e) {
        error.value = '该会话还没有真实请求预览，请先发送消息或使用只读模拟。'
      }
    }

    onMounted(load)

    return {
      tabs, labels, tab, overview, bindings, personas, selected, error, notice, busy,
      advanced, binding, debug, debugResult, docsForTab, bindDocs, card, entries,
      sessionOptions, sFiltered, dFiltered, sessionDisplay, debugDisplay,
      sOpen, dOpen, pickSession, onSFocus, onSBlur, pickDebug, onDFocus, onDBlur,
      choose, createDoc, save, remove, duplicate, applyAdvanced, importData,
      setFile: e => file.value = e.target.files[0],
      updateScope, addBinding, unbind, scopeName, move, addBlock, keyText, setKeys,
      addEntry: () => selected.value.data.entries.push(newEntry()),
      selectConversation, simulate, actual,
    }
  },
  template: `
<div class="shell">
  <aside class="nav">
    <div class="brand">
      <small>ASTRBOT 角色扮演工作台</small>
      <h1>Komeiji's<br>Tavern</h1>
      <span>v0.3.4</span>
    </div>
    <button v-for="t in tabs" :class="{active:tab===t[0]}" @click="tab=t[0];selected=null">{{t[1]}}</button>
  </aside>
  <main>
    <header>
      <div><h2>{{tabs.find(x=>x[0]===tab)?.[1]}}</h2><p>创建或导入 → 编辑 → 绑定 → 扫描测试 → 检查 messages[]</p></div>
      <label class="import"><input type="file" accept=".json,.yaml,.yml,.png,.txt,.md" @change="setFile"><button @click="importData" :disabled="busy">解析并导入</button></label>
    </header>
    <div v-if="error" class="alert error">{{error}}</div>
    <div v-if="notice" class="alert ok">{{notice}}</div>
    <section v-if="tab==='home'" class="home">
      <div class="hero">
        <h3>{{overview.ready?'核心配置已就绪':'从这里开始'}}</h3>
        <p>资料只有绑定后才会参与模型请求。先准备角色，再绑定到 AstrBot 会话，最后确认最终提示词。</p>
        <div class="steps">
          <button @click="createDoc('character')"><b>1</b>创建角色</button>
          <button @click="tab='bindings'"><b>2</b>绑定会话</button>
          <button @click="tab='debug'"><b>3</b>检查请求</button>
        </div>
      </div>
      <div class="cards">
        <div class="metric" v-for="(v,k) in labels"><strong>{{overview.counts?.[k]||0}}</strong><span>{{v}}</span></div>
      </div>
      <div class="panel"><h3>待完成</h3><p v-if="!overview.tasks?.length">没有必须处理的事项。</p><ul><li v-for="x in overview.tasks">{{x}}</li></ul></div>
    </section>
    <section v-else-if="['character','preset','lorebook','persona'].includes(tab)" class="workspace">
      <div class="library">
        <button class="primary" @click="createDoc(tab)">新建{{labels[tab]}}</button>
        <button v-for="d in docsForTab" :class="['doc',{active:selected?.id===d.id}]" @click="choose(d)"><b>{{d.name}}</b><small>{{new Date(d.updated_at*1000).toLocaleString()}}</small></button>
        <p v-if="!docsForTab.length" class="muted">还没有{{labels[tab]}}。</p>
      </div>
      <article v-if="selected" class="editor">
        <div class="editor-title">
          <input class="title-input" v-model="selected.name">
          <div>
            <button v-if="selected.id" @click="duplicate">复制</button>
            <button v-if="selected.id" class="danger" @click="remove">删除</button>
            <button class="primary" @click="save">保存</button>
          </div>
        </div>
        <template v-if="tab==='character'">
          <div class="grid"><label>角色名称<input v-model="card.name"></label><label>开场白<textarea v-model="card.first_mes"></textarea></label></div>
          <label>角色描述<textarea v-model="card.description"></textarea></label>
          <div class="grid"><label>性格<textarea v-model="card.personality"></textarea></label><label>场景<textarea v-model="card.scenario"></textarea></label></div>
          <label>示例对话<textarea v-model="card.mes_example"></textarea></label>
          <div class="grid"><label>角色 Main Prompt<textarea v-model="card.system_prompt"></textarea></label><label>历史后指令（PHI）<textarea v-model="card.post_history_instructions"></textarea></label></div>
        </template>
        <template v-if="tab==='preset'">
          <div class="grid"><label>主提示词<textarea v-model="selected.data.main_prompt"></textarea></label><label>预设 PHI<textarea v-model="selected.data.post_history_instructions"></textarea></label></div>
          <div class="checks">
            <label><input type="checkbox" v-model="selected.data.allow_character_main_override">允许角色覆盖主提示词</label>
            <label><input type="checkbox" v-model="selected.data.allow_character_phi_override">允许角色覆盖 PHI</label>
          </div>
          <div class="block" v-for="(b,i) in selected.data.blocks">
            <div class="block-head">
              <input type="checkbox" v-model="b.enabled"><input v-model="b.name">
              <button @click="move(i,-1)">↑</button><button @click="move(i,1)">↓</button>
              <button class="danger" @click="selected.data.blocks.splice(i,1)">×</button>
            </div>
            <div class="inline">
              <label>角色<select v-model="b.role"><option>system</option><option>user</option><option>assistant</option></select></label>
              <label>位置<select v-model="b.position"><option value="system">系统提示词</option><option value="examples">示例区</option><option value="depth">聊天深度</option></select></label>
              <label>深度<input type="number" v-model.number="b.depth"></label>
              <label>裁剪优先级<input type="number" v-model.number="b.priority"></label>
            </div>
            <textarea v-if="b.identifier.startsWith('custom_')" v-model="b.content"></textarea>
            <small>标识：{{b.identifier}}</small>
          </div>
          <button @click="addBlock">添加自定义块</button>
        </template>
        <template v-if="tab==='lorebook'">
          <div class="entry" v-for="(e,i) in entries">
            <div class="entry-head">
              <input v-model="e.comment"><label><input type="checkbox" v-model="e.constant">常驻</label>
              <label><input type="checkbox" v-model="e.disable">禁用</label>
              <button class="danger" @click="selected.data.entries.splice(i,1)">删除</button>
            </div>
            <div class="grid">
              <label>主关键词<input :value="keyText(e.key)" @input="setKeys(e,'key',$event.target.value)"></label>
              <label>次关键词<input :value="keyText(e.keysecondary)" @input="setKeys(e,'keysecondary',$event.target.value)"></label>
            </div>
            <div class="inline">
              <label><input type="checkbox" v-model="e.selective">次关键词</label>
              <label>逻辑<select v-model.number="e.selectiveLogic"><option :value="0">且任一</option><option :value="3">且全部</option><option :value="2">且无</option><option :value="1">且非全部</option></select></label>
              <label>位置<select v-model.number="e.position"><option :value="0">角色前</option><option :value="1">角色后</option><option :value="2">作者注顶部</option><option :value="3">作者注底部</option><option :value="4">聊天深度</option><option :value="5">示例顶部</option><option :value="6">示例底部</option><option :value="7">Outlet</option></select></label>
              <label>角色<select v-model="e.role"><option>system</option><option>user</option><option>assistant</option></select></label>
            </div>
            <div class="inline">
              <label>深度<input type="number" v-model.number="e.depth"></label>
              <label>顺序<input type="number" v-model.number="e.order"></label>
              <label>概率<input type="number" min="0" max="100" v-model.number="e.probability"></label>
              <label>Sticky<input type="number" v-model.number="e.sticky"></label>
              <label>Cooldown<input type="number" v-model.number="e.cooldown"></label>
              <label>Delay<input type="number" v-model.number="e.delay"></label>
            </div>
            <label>注入内容<textarea v-model="e.content"></textarea></label>
          </div>
          <button @click="addEntry">添加条目</button>
        </template>
        <template v-if="tab==='persona'"><label>用户设定内容<textarea class="tall" v-model="selected.data.content"></textarea></label></template>
        <details class="advanced">
          <summary>高级 JSON（保留未知扩展字段）</summary>
          <div><button @click="advanced=JSON.stringify(selected.data,null,2)">从表单刷新</button><button @click="applyAdvanced">应用 JSON</button></div>
          <textarea class="json" v-model="advanced"></textarea>
        </details>
      </article>
      <article v-else class="empty"><h3>选择一项开始编辑</h3></article>
    </section>
    <section v-else-if="tab==='bindings'" class="stack">
      <div class="panel">
        <h3>新增绑定</h3>
        <p>单选资源按"会话 → Persona → 用户 → 群组 → 全局"覆盖；世界书按作用域叠加。</p>
        <div class="binding-form">
          <label>资料类型<select v-model="binding.kind"><option v-for="(v,k) in labels" :value="k">{{v}}</option></select></label>
          <label>资料<select v-model="binding.target_id"><option value="">请选择</option><option v-for="d in bindDocs" :value="d.id">{{d.name}}</option></select></label>
          <label>范围<select v-model="binding.scope_type" @change="updateScope"><option value="session">具体会话</option><option value="persona">AstrBot Persona</option><option value="global">全局</option></select></label>
          <label v-if="binding.scope_type==='session'" class="combo">会话
            <input v-model="sessionDisplay" @focus="onSFocus" @blur="onSBlur" placeholder="搜索或输入会话 ID">
            <div class="combo-panel" v-if="sOpen">
              <div v-for="c in sFiltered" class="combo-option" @mousedown.prevent="pickSession(c)">{{c.title}} · {{c.platform}}</div>
              <div v-if="!sFiltered.length" class="combo-option muted">无匹配，可直接输入 ID 绑定</div>
            </div>
          </label>
          <label v-if="binding.scope_type==='persona'">Persona<select v-model="binding.scope_id"><option value="">请选择</option><option v-for="p in personas" :value="p.id">{{p.name}}</option></select></label>
          <button class="primary" @click="addBinding">确认绑定</button>
        </div>
      </div>
      <div class="panel">
        <h3>当前绑定</h3>
        <table><tr><th>范围</th><th>类型</th><th>资料</th><th></th></tr>
          <tr v-for="i in bindings"><td>{{scopeName(i)}}</td><td>{{labels[i.kind]||i.kind}}</td><td>{{i.target_name}}</td><td><button class="danger" @click="unbind(i)">移除</button></td></tr>
        </table>
      </div>
    </section>
    <section v-else-if="tab==='debug'" class="debug">
      <div class="panel controls">
        <h3>请求调试器</h3>
        <label class="combo">会话
          <input v-model="debugDisplay" @focus="onDFocus" @blur="onDBlur" placeholder="搜索或输入会话 ID">
          <div class="combo-panel" v-if="dOpen">
            <div v-for="c in dFiltered" class="combo-option" @mousedown.prevent="pickDebug(c)">{{c.title}} · {{c.platform}}</div>
            <div v-if="!dFiltered.length" class="combo-option muted">无匹配，可直接输入 ID</div>
          </div>
        </label>
        <label>模式<select v-model="debug.mode"><option value="normal">普通生成</option><option value="continue">Continue</option><option value="impersonate">Impersonate</option><option value="quiet">Quiet Prompt</option></select></label>
        <label v-if="debug.mode==='quiet'">Quiet Prompt<input v-model="debug.quiet_prompt"></label>
        <label>本次用户消息<textarea v-model="debug.prompt"></textarea></label>
        <label>原始 AstrBot System Prompt<textarea v-model="debug.system_prompt"></textarea></label>
        <div class="actions"><button class="primary" @click="simulate">只读模拟</button><button @click="actual" :disabled="!debug.session_id">最近真实请求</button></div>
        <p class="muted">模拟不会推进 Sticky、Cooldown、Delay 或轮次。未选会话时只解析全局绑定。</p>
      </div>
      <div class="panel result" v-if="debugResult">
        <div class="result-head"><h3>最终 messages[]</h3><span>Token 为近似估算</span></div>
        <div class="effective" v-if="debugResult.effective">
          <b>当前有效配置</b>
          <span>预设：{{debugResult.effective.single?.preset?.name||'无'}}</span>
          <span>角色：{{debugResult.effective.single?.character?.name||'无'}}</span>
          <span>世界书：{{debugResult.effective.additive?.lorebook?.map(x=>x.name).join('、')||'无'}}</span>
        </div>
        <div class="alert error" v-for="w in debugResult.warnings">{{w}}</div>
        <div class="message" v-for="(m,i) in debugResult.messages"><b>{{i}} · {{m.role}}</b><pre>{{m.content}}</pre></div>
        <details open><summary>提示词块（{{debugResult.blocks?.length||0}}）</summary>
          <table><tr v-for="b in debugResult.blocks"><td>{{b.name}}</td><td>{{b.role}} / {{b.position}} / depth {{b.depth}}</td><td>≈ {{b.tokens}}</td><td>{{b.source}}</td></tr></table>
        </details>
        <details><summary>世界书激活（{{debugResult.activated?.length||0}}）</summary>
          <div class="activation" v-for="a in debugResult.activated"><b>{{a.name||a.uid}}</b> · {{a.reason}} · 递归 {{a.step}}<pre>{{a.content}}</pre></div>
        </details>
        <details><summary>裁剪、警告与 Outlet</summary><pre>{{JSON.stringify({dropped:debugResult.dropped,warnings:debugResult.warnings,outlets:debugResult.outlets},null,2)}}</pre></details>
      </div>
    </section>
    <section v-else class="help panel">
      <h3>推荐使用顺序</h3>
      <ol>
        <li>创建或导入角色卡。</li>
        <li>按需要调整提示词预设块。</li>
        <li>创建世界书条目，填写关键词和注入位置。</li>
        <li>将角色与世界书绑定到 Persona 或具体会话。</li>
        <li>在调试器确认最终 messages[]。</li>
      </ol>
      <h3>生命周期</h3>
      <p><b>Sticky</b> 激活后保持若干轮；<b>Cooldown</b> 在保持结束后阻止再次触发；<b>Delay</b> 让条目延迟启用。</p>
      <h3>命令</h3>
      <pre>/tavern status
/tavern preview
/tavern reset
/tavern continue [补充要求]
/tavern impersonate [补充要求]
/tavern quiet [静默提示词]</pre>
    </section>
  </main>
</div>`
}).mount('#app')
