import { createApp, ref, computed, onMounted, watch } from 'vue/dist/vue.esm-bundler.js'
import './style.css'

const API = '/api/plug/astrbot_plugin_komeiji_tavern/v1'

const request = async (path, options = {}) => {
  const bridge = window.AstrBotPluginPage
  if (bridge) {
    await bridge.ready()
    const endpointPath = path.replace(/^\/+/, '').replace(/\?.*$/, '')
      .split('/')
      .map(segment => {
        try { return decodeURIComponent(segment) } catch { return segment }
      })
      .join('/')
    const endpoint = 'v1/' + endpointPath
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

const saveBlob = (blob, filename) => {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

const saveDownload = payload => {
  const bytes = Uint8Array.from(atob(payload.base64), char => char.charCodeAt(0))
  saveBlob(new Blob([bytes], { type: payload.mime || 'application/octet-stream' }), payload.filename || 'export.bin')
}

const saveJson = (value, filename) => {
  saveBlob(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json;charset=utf-8' }), filename)
}

const downloadLocationHint = '文件由浏览器下载，通常保存在系统“下载”文件夹；如果浏览器开启了“每次询问保存位置”，则保存到你选择的位置。'

const labels = {
  character: '角色卡',
  preset: '提示词预设',
  lorebook: '世界书',
  persona: '用户设定',
  character_group: '角色组',
  material: '创作素材',
  quick_reply: '快捷回复',
}

const tabs = [
  ['home', '开始'],
  ['game', '当前游戏'],
  ['character', '角色卡'],
  ['character_group', '角色组'],
  ['preset', '提示词预设'],
  ['lorebook', '世界书'],
  ['material', '创作素材'],
  ['persona', '用户设定'],
  ['quick_reply', '快捷回复'],
  ['bindings', '绑定管理'],
  ['campaigns', '战役状态'],
  ['memories', '长期记忆'],
  ['metrics', '运行仪表盘'],
  ['archive', '分支树'],
  ['debug', '调试器'],
  ['help', '使用说明'],
]

const navGroups = [
  ['开始游戏', ['game']],
  ['创作资料', ['home', 'character', 'character_group', 'preset', 'lorebook', 'material', 'persona', 'quick_reply']],
  ['生效与调试', ['bindings', 'debug', 'metrics']],
  ['长期系统', ['campaigns', 'memories', 'archive']],
  ['帮助', ['help']],
].map(group => ({
  name: group[0],
  items: group[1].map(key => tabs.find(item => item[0] === key)).filter(Boolean),
}))

const tabMeta = {
  home: { icon: '🌌', subtitle: '向导与就绪总览' },
  game: { icon: '🎮', subtitle: '开局、存档与状态中枢' },
  character: { icon: '🎴', subtitle: '角色档案编构' },
  character_group: { icon: '👥', subtitle: '多角色协奏' },
  preset: { icon: '⚡', subtitle: '提示词积木装配' },
  lorebook: { icon: '📖', subtitle: '世界规则检索' },
  material: { icon: '🎨', subtitle: '创作素材灵感包' },
  persona: { icon: '🧙', subtitle: '观测者档案' },
  quick_reply: { icon: '💬', subtitle: '快捷回复宏' },
  bindings: { icon: '🔗', subtitle: '会话与资料映射' },
  campaigns: { icon: '🧭', subtitle: '多世界战役与权威状态' },
  memories: { icon: '🧠', subtitle: '长期神经记忆' },
  metrics: { icon: '📊', subtitle: 'Token 与运行指标' },
  archive: { icon: '🌳', subtitle: '多元分支快照' },
  debug: { icon: '🔬', subtitle: '请求推演沙盒' },
  help: { icon: '📜', subtitle: '操作秘术手册' },
}

const twistSeeds = [
  '让当前场景突然出现一个只有角色本人能感知到的旧日约定，并要求 TA 用行动而非解释回应。',
  '把一条长期记忆变成当轮剧情的暗线证据，但不要直接揭露，只让角色通过细节试探。',
  '让世界书中的地点规则短暂失效三分钟，观察角色如何维持人设与秩序。',
  '让用户收到一封来自未来分支线的短消息，内容必须与当前会话的情绪核心相冲突。',
  '让角色误以为自己刚刚重复经历过这一轮对话，并在回复里留下一个温柔的破绽。',
  '把一个普通物件升级为剧情锚点：它记录了上一条 assistant 回复中没说出口的真实意图。',
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

const newQuickReply = () => ({
  id: crypto.randomUUID(),
  label: '新快捷回复',
  alias: '',
  content: '',
  mode: 'normal',
  enabled: true,
  append_input: true,
  order: 100,
})

const defaultQuickReplies = () => [
  { id: crypto.randomUUID(), label: '继续剧情', alias: 'continue', content: '继续上一条助手回复，从中断处自然衔接。推进当前场景，避免复述已有内容，也不要替用户决定行动或台词。', mode: 'continue', enabled: true, append_input: true, order: 10 },
  { id: crypto.randomUUID(), label: '丰富描写', alias: 'detail', content: '结合当前上下文继续回应，并加强动作、神态、环境与感官细节。保持人物性格和既有设定，不要无故改变剧情事实。', mode: 'normal', enabled: true, append_input: true, order: 20 },
  { id: crypto.randomUUID(), label: '代写我的回复', alias: 'reply', content: '根据当前对话和用户设定，拟写一条自然的下一步用户消息。保持用户的口吻，只输出可直接发送的消息正文。', mode: 'impersonate', enabled: true, append_input: true, order: 30 },
  { id: crypto.randomUUID(), label: '润色重写', alias: 'polish', content: '润色并重写上一条助手回复，保留原意和事实，提高语言自然度、画面感与节奏。只输出重写后的正文。', mode: 'normal', enabled: true, append_input: true, order: 40 },
  { id: crypto.randomUUID(), label: '总结当前剧情', alias: 'summary', content: '总结截至目前的剧情进展、人物关系、重要信息、当前状态和未解决事项。不要续写剧情，不要添加上下文中不存在的信息。', mode: 'normal', enabled: true, append_input: true, order: 50 },
  { id: crypto.randomUUID(), label: '严格保持角色', alias: 'incharacter', content: '本轮必须严格遵守角色卡、世界书与既有剧情事实，保持角色口吻和行为逻辑，不要跳出角色解释。', mode: 'quiet', enabled: true, append_input: true, order: 60 },
]

const newEntry = kind => ({
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
  vectorized: kind === 'material',
  extensions: { category: '', description: '' },
})

const readThemePreference = () => {
  try {
    const stored = window.localStorage?.getItem('komeiji-tavern-theme')
    if (stored === 'light' || stored === 'dark') return stored
  } catch {
  }
  try {
    const requested = new URLSearchParams(window.location.search).get('theme')
    return requested === 'light' ? 'light' : requested === 'dark' ? 'dark' : null
  } catch {
    return null
  }
}

const writeThemePreference = value => {
  try {
    window.localStorage?.setItem('komeiji-tavern-theme', value)
  } catch {
    // AstrBot hosts plugin pages in a sandboxed frame, where storage may be unavailable.
  }
}

createApp({
  setup() {
    const storedTheme = readThemePreference()
    const theme = ref(storedTheme === 'light' ? 'light' : 'dark')
    const tab = ref('home')
    const overview = ref({ counts: {}, tasks: [] })
    const documents = ref([])
    const bindings = ref([])
    const memories = ref([])
    const campaigns = ref([])
    const campaignChanges = ref([])
    const rpPacks = ref([])
    const stateTemplates = ref({})
    const currentGame = ref({ campaign: null, pack: null, changes: [] })
    const worldbookReports = ref([])
    const metrics = ref({ items: [], totals: {}, providers: {} })
    const personas = ref([])
    const conversations = ref([])
    const selected = ref(null)
    const pendingDeleteId = ref('')
    const error = ref('')
    const notice = ref('')
    let noticeTimer = null
    const busy = ref(false)
    const file = ref(null)
    const fileLabel = computed(() => file.value?.name || '未选择文件')
    const advanced = ref('')
    const sQuery = ref('')
    const sOpen = ref(false)
    const sFocused = ref(false)
    const dQuery = ref('')
    const dOpen = ref(false)
    const dFocused = ref(false)
    const entryQuery = ref('')
    const entryFilter = ref('all')
    const openEntryUid = ref('')
    const memoryQuery = ref('')
    const memoryStatusFilter = ref('')
    const selectedMemoryIds = ref([])
    const pendingMemoryDeleteId = ref('')
    const pendingImport = ref(null)
    const metricDays = ref(7)
    const retrievalTest = ref({ text: '' })
    const retrievalStats = ref(null)
    const retrievalResult = ref(null)
    const archive = ref({ session_id: '', nodes: [], selected: null, branch_name: '' })
    const playDraft = ref({ prompt: '', mode: 'normal', quiet_prompt: '', branch_name: '' })
    const playResult = ref(null)
    const continueVisibleCount = ref(1)
    const selectedQuickReplyId = ref('')
    const twistOpen = ref(false)
    const twistText = ref(twistSeeds[0])

    const binding = ref({ scope_type: 'session', scope_id: '', kind: 'character', target_id: '', priority: 0 })
    const newMemoryDraft = () => ({ id: '', scope_type: 'session', scope_id: '', category: 'status', content: '', enabled: true, status: 'active', importance: 1, source_type: 'manual', source_ref: '', expires_at: 0 })
    const memoryDraft = ref(newMemoryDraft())
    const newCampaignDraft = () => ({
      id: '', name: '新战役', world_id: '', ruleset_id: '', description: '', rule_prompt: '',
      state_schema: { location: '当前位置', time: '当前时间', condition: '角色状态', inventory: '物品清单', quests: '任务', clues: '线索', relationships: '人物关系' },
      state_data: { location: '', time: '', condition: {}, inventory: [], quests: [], clues: [], relationships: {} },
      settings: { state_tracking_enabled: true, state_extract_interval: 1, state_apply_mode: 'pending' }, archived: false,
    })
    const campaignDraft = ref(newCampaignDraft())
    const campaignSessionId = ref('')
    const campaignSchemaJson = ref(JSON.stringify(campaignDraft.value.state_schema, null, 2))
    const campaignStateJson = ref(JSON.stringify(campaignDraft.value.state_data, null, 2))
    const newGameDraft = ref({ pack_id: '', session_id: '', name: '', template_id: '', archive_current: true, conversation_mode: 'new' })
    const debug = ref({ session_id: '', persona_id: '', prompt: '', system_prompt: '', mode: 'normal', quiet_prompt: '', seed: 1 })
    const debugResult = ref(null)
    const toggleTheme = () => {
      theme.value = theme.value === 'dark' ? 'light' : 'dark'
      writeThemePreference(theme.value)
    }
    const formatTimestamp = value => value ? new Date(Number(value) * 1000).toLocaleString() : '无'

    const docsForTab = computed(() => documents.value.filter(x => x.kind === tab.value))
    const bindDocs = computed(() => documents.value.filter(x => x.kind === binding.value.kind))
    const characterDocs = computed(() => documents.value.filter(x => x.kind === 'character'))
    const card = computed(() => selected.value?.data?.data && typeof selected.value.data.data === 'object' ? selected.value.data.data : selected.value?.data || {})
    const entries = computed(() => { const x = selected.value?.data?.entries || []; return Array.isArray(x) ? x : Object.values(x) })
    const filteredEntries = computed(() => {
      const q = entryQuery.value.toLowerCase().trim()
      return entries.value.filter(item => {
        const category = item.extensions?.category || item.category || item.group || ''
        const haystack = [item.comment, item.content, category, keyText(item.key), keyText(item.keysecondary)].join(' ').toLowerCase()
        const matchedQuery = !q || haystack.includes(q)
        const matchedFilter = entryFilter.value === 'all'
          || (entryFilter.value === 'enabled' && !item.disable)
          || (entryFilter.value === 'disabled' && item.disable)
          || (entryFilter.value === 'vectorized' && item.vectorized)
          || (entryFilter.value === 'constant' && item.constant)
          || (entryFilter.value === 'no_keys' && !keyText(item.key))
        return matchedQuery && matchedFilter
      })
    })
    const groupMembers = computed(() => (selected.value?.data?.members || []).map(id => characterDocs.value.find(x => x.id === id)).filter(Boolean))
    const availableGroupMembers = computed(() => characterDocs.value.filter(x => !(selected.value?.data?.members || []).includes(x.id)))
    const quickReplies = computed(() => Array.isArray(selected.value?.data?.items) ? selected.value.data.items.slice().sort((a, b) => Number(a.order || 0) - Number(b.order || 0)) : [])

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
    const filteredMemories = computed(() => {
      const q = memoryQuery.value.toLowerCase()
      const status = memoryStatusFilter.value
      return memories.value.filter(x => {
        const itemStatus = x.status || (x.enabled ? 'active' : 'archived')
        const matchedStatus = !status || itemStatus === status
        const matchedQuery = !q || (x.content + ' ' + x.category + ' ' + x.scope_id + ' ' + itemStatus + ' ' + x.source_type).toLowerCase().includes(q)
        return matchedStatus && matchedQuery
      })
    })
    const visibleMemoryIds = computed(() => filteredMemories.value.map(x => x.id))
    const allVisibleMemoriesSelected = computed(() => visibleMemoryIds.value.length > 0 && visibleMemoryIds.value.every(id => selectedMemoryIds.value.includes(id)))
    const memoryGroups = computed(() => {
      const order = ['campaign', 'conversation', 'world', 'ruleset', 'session', 'user', 'group', 'persona', 'global']
      return order.map(scope => ({
        scope,
        title: scope === 'campaign' ? '战役记忆' : scope === 'conversation' ? 'AstrBot 对话记忆' : scope === 'world' ? '世界记忆' : scope === 'ruleset' ? '规则集记忆' : scope === 'session' ? '会话记忆' : scope === 'user' ? '用户记忆' : scope === 'group' ? '群组记忆' : scope === 'persona' ? 'Persona 记忆' : '全局记忆',
        items: filteredMemories.value.filter(item => item.scope_type === scope),
      })).filter(group => group.items.length)
    })
    const metricItems = computed(() => metrics.value.items || [])
    const metricTotals = computed(() => metrics.value.totals || {})
    const metricProviders = computed(() => Object.entries(metrics.value.providers || {}).sort((a, b) => b[1] - a[1]))
    const maxMetricTokens = computed(() => Math.max(1, ...metricItems.value.map(x => Number(x.prompt_tokens || 0))))
    const currentTabMeta = computed(() => tabMeta[tab.value] || { icon: '?', subtitle: '' })
    const documentCount = kind => overview.value.counts?.[kind] ?? documents.value.filter(x => x.kind === kind).length
    const tabCount = key => {
      if (labels[key]) return documentCount(key)
      if (key === 'bindings') return bindings.value.length
      if (key === 'campaigns') return campaigns.value.length
      if (key === 'memories') return memories.value.length
      if (key === 'metrics') return metricTotals.value.requests || 0
      if (key === 'archive') return archive.value.nodes.length
      return null
    }
    const activeSingleCount = computed(() => ['preset', 'character', 'character_group', 'persona'].filter(kind => bindingSummary.value.single[kind]).length)
    const additiveBindingCount = computed(() => (bindingSummary.value.additive.lorebook?.length || 0) + (bindingSummary.value.additive.material?.length || 0) + (bindingSummary.value.additive.quick_reply?.length || 0))
    const enabledMemoryCount = computed(() => memories.value.filter(x => x.enabled && (x.status || 'active') === 'active').length)
    const promptAssembly = computed(() => {
      const blocks = selected.value?.data?.blocks
      if (!Array.isArray(blocks)) return []
      return blocks.filter(x => x.enabled).map(x => ({
        name: x.name || x.identifier || '未命名提示块',
        role: x.role || 'system',
        position: x.position || 'system',
        depth: x.depth ?? 0,
        priority: x.priority ?? 0,
        content: x.content || '',
      }))
    })
    const archiveTree = computed(() => {
      const byParent = new Map()
      const ordered = archive.value.nodes.slice().reverse()
      const knownIds = new Set(ordered.map(node => node.id))
      for (const node of ordered) {
        let parent = node.parent_id || ''
        if (node.parent_history_continuous === false) {
          parent = ''
        }
        node._effective_parent_id = parent
        if (!byParent.has(parent)) byParent.set(parent, [])
        byParent.get(parent).push(node)
      }
      const out = []
      const visited = new Set()
      const visit = (parent, lane, segment) => {
        const children = byParent.get(parent) || []
        children.forEach((node, index) => {
          if (visited.has(node.id)) return
          visited.add(node.id)
          // A normal continuation stays in the same lane. Only additional
          // children of one parent represent a real branch and move right.
          const nodeLane = lane + (index === 0 ? 0 : 1)
          out.push({ ...node, depth: nodeLane, segment, is_root: false })
          visit(node.id, nodeLane, segment)
        })
      }
      const roots = ordered.filter(node => !node._effective_parent_id || !knownIds.has(node._effective_parent_id))
      roots.forEach((node, index) => {
        if (visited.has(node.id)) return
        visited.add(node.id)
        const segment = index + 1
        out.push({ ...node, depth: 0, segment, is_root: true })
        visit(node.id, 0, segment)
      })
      return out
    })
    const archiveSegments = computed(() => {
      const groups = new Map()
      for (const node of archiveTree.value) {
        if (!groups.has(node.segment)) groups.set(node.segment, [])
        groups.get(node.segment).push(node)
      }
      return Array.from(groups.entries())
        .map(([segment, nodes]) => ({
          segment,
          nodes,
          root: nodes.find(node => node.is_root) || nodes[0],
          latest_at: Math.max(...nodes.map(node => Number(node.created_at || 0))),
        }))
        .sort((left, right) => right.latest_at - left.latest_at)
        .map((item, index) => ({ ...item, current: index === 0 }))
    })
    const activeContinueSegment = computed(() => archiveSegments.value.find(item => item.current) || null)
    const sortedContinueNodes = computed(() => (activeContinueSegment.value?.nodes || archiveTree.value).slice().sort((left, right) => {
      const turnDelta = Number(right.turn_index || 0) - Number(left.turn_index || 0)
      if (turnDelta) return turnDelta
      return Number(right.created_at || 0) - Number(left.created_at || 0)
    }))
    const continueTimeline = computed(() => sortedContinueNodes.value.slice(0, continueVisibleCount.value))
    const hiddenContinueCount = computed(() => Math.max(0, sortedContinueNodes.value.length - continueTimeline.value.length))
    const expandContinueNodes = () => {
      continueVisibleCount.value = Math.min(sortedContinueNodes.value.length, continueVisibleCount.value + 10)
    }
    const collapseContinueNodes = () => {
      continueVisibleCount.value = 1
    }
    const latestContinueNode = computed(() => sortedContinueNodes.value[0] || null)
    const selectedContinueNode = computed(() => archive.value.selected || latestContinueNode.value)
    const pendingCurrentChanges = computed(() => (currentGame.value.changes || []).filter(item => item.status === 'pending'))
    const continueHits = computed(() => {
      const preview = playResult.value?.preview || {}
      const retrieval = preview.retrieval?.matches || []
      const memory = preview.memory?.matches || []
      return { retrieval, memory }
    })
    const compactStateRows = computed(() => currentStateRows.value.slice(0, 10))
    const homeGuide = computed(() => {
      const counts = overview.value.counts || {}
      const hasPresetBinding = bindings.value.some(x => x.kind === 'preset')
      const hasCharacterBinding = bindings.value.some(x => x.kind === 'character' || x.kind === 'character_group')
      const hasAdditiveBinding = bindings.value.some(x => x.kind === 'lorebook' || x.kind === 'material')
      const hasSession = Boolean(debug.value.session_id || bindings.value.some(x => x.scope_type === 'session'))
      return [
        { title: '准备角色', state: counts.character || counts.character_group ? 'ready' : 'todo', detail: `角色卡 ${counts.character || 0} · 角色组 ${counts.character_group || 0}`, action: '创建角色', tab: 'character', create: 'character' },
        { title: '选择预设', state: hasPresetBinding ? 'ready' : (counts.preset ? 'warn' : 'todo'), detail: hasPresetBinding ? '已有提示词预设绑定' : `可用预设 ${counts.preset || 0}，尚未绑定`, action: counts.preset ? '去绑定' : '新建预设', tab: counts.preset ? 'bindings' : 'preset', create: counts.preset ? '' : 'preset' },
        { title: '绑定主角', state: hasCharacterBinding ? 'ready' : 'todo', detail: hasCharacterBinding ? '角色或角色组已进入生效链路' : '还没有角色绑定到会话、Persona 或全局', action: '打开绑定管理', tab: 'bindings' },
        { title: '补充设定', state: hasAdditiveBinding ? 'ready' : 'idle', detail: `世界书 ${counts.lorebook || 0} · 创作素材 ${counts.material || 0}`, action: '导入或编辑素材', tab: counts.material ? 'material' : 'lorebook' },
        { title: '长期状态', state: memories.value.some(x => x.enabled && (x.status || 'active') === 'active') ? 'ready' : 'idle', detail: `长期记忆 ${memories.value.length} 条 · 分支节点 ${archive.value.nodes.length} 个`, action: '管理记忆', tab: 'memories' },
        { title: '请求验证', state: hasSession ? 'ready' : 'warn', detail: hasSession ? '已有会话可用于模拟或读取最近请求' : '建议先选择或绑定一个具体会话', action: '打开调试器', tab: 'debug' },
      ]
    })
    const bindingTargetTitle = computed(() => {
      const c = sessionOptions.value.find(x => x.id === debug.value.session_id)
      if (c) return c.title + ' · ' + c.platform
      if (debug.value.session_id) return debug.value.session_id
      return '未选择会话，仅显示全局与 Persona 绑定'
    })
    const bindingsForTarget = computed(() => {
      const sessionId = debug.value.session_id || ''
      const personaId = debug.value.persona_id || ''
      const campaign = campaigns.value.find(value => value.session_ids?.includes(sessionId))
      return bindings.value.filter(item => {
        if (item.scope_type === 'global') return true
        if (item.scope_type === 'session') return sessionId && item.scope_id === sessionId
        if (item.scope_type === 'persona') return personaId && item.scope_id === personaId
        if (item.scope_type === 'campaign') return campaign && item.scope_id === campaign.id
        if (item.scope_type === 'world') return campaign?.world_id && item.scope_id === campaign.world_id
        if (item.scope_type === 'ruleset') return campaign?.ruleset_id && item.scope_id === campaign.ruleset_id
        return false
      })
    })
    const bindingSummary = computed(() => {
      const singleKinds = ['preset', 'character', 'character_group', 'persona']
      const additiveKinds = ['lorebook', 'material', 'quick_reply']
      const rank = { global: 1, persona: 2, world: 3, ruleset: 4, campaign: 5, session: 6 }
      const out = { single: {}, additive: {} }
      for (const kind of singleKinds) {
        out.single[kind] = bindingsForTarget.value
          .filter(item => item.kind === kind)
          .sort((a, b) => (rank[b.scope_type] || 0) - (rank[a.scope_type] || 0) || Number(b.priority || 0) - Number(a.priority || 0))[0] || null
      }
      for (const kind of additiveKinds) out.additive[kind] = bindingsForTarget.value.filter(item => item.kind === kind)
      return out
    })
    const debugSummary = computed(() => {
      const result = debugResult.value || {}
      const effective = result.effective || {}
      const single = effective.single || {}
      const additive = effective.additive || {}
      const messages = Array.isArray(result.messages) ? result.messages : []
      const tokens = messages.reduce((sum, item) => sum + Math.ceil(String(item.content || '').length / 4), 0)
      return [
        { label: '预设', value: single.preset?.name || '未绑定', state: single.preset ? 'ready' : 'warn' },
        { label: '本轮角色', value: result.character_selection?.character?.card_name || result.character_selection?.character?.name || single.character?.name || '未绑定', state: (result.character_selection?.character || single.character) ? 'ready' : 'warn' },
        { label: '角色选择', value: result.character_selection?.reason || '无角色组状态', state: result.character_selection ? 'ready' : 'idle' },
        { label: '世界书', value: String((result.activated || []).length || (additive.lorebook || []).length || 0) + ' 条', state: ((result.activated || []).length || (additive.lorebook || []).length) ? 'ready' : 'idle' },
        { label: '创作素材', value: String(result.retrieval?.matches?.length || 0) + ' 条', state: result.retrieval?.matches?.length ? 'ready' : 'idle' },
        { label: '长期记忆', value: String(result.memory?.injected_count || result.memory?.matches?.length || 0) + ' 条', state: (result.memory?.injected_count || result.memory?.matches?.length) ? 'ready' : 'idle' },
        { label: '自动摘要', value: result.summary?.included ? '已注入' : result.summary?.enabled ? '已启用' : '未启用', state: result.summary?.included ? 'ready' : result.summary?.enabled ? 'idle' : 'warn' },
        { label: '消息与 Token', value: messages.length + ' 条 / ≈ ' + tokens, state: messages.length ? 'ready' : 'warn' },
        { label: '警告', value: String((result.warnings || []).length) + ' 个', state: (result.warnings || []).length ? 'warn' : 'ready' },
      ]
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
    const dismissNotice = () => { notice.value = '' }

    watch(notice, value => {
      if (noticeTimer) clearTimeout(noticeTimer)
      if (!value) return
      noticeTimer = setTimeout(() => {
        if (notice.value === value) notice.value = ''
      }, 4200)
    })

    const load = async () => {
      try {
        const x = await Promise.all([
          request('/overview'),
          request('/documents'),
          request('/bindings'),
          request('/catalog/personas'),
          request('/catalog/conversations?page_size=100'),
          request('/memories?limit=300'),
          request('/metrics?days=' + encodeURIComponent(metricDays.value) + '&limit=1000'),
          request('/campaigns'),
          request('/rp-packs'),
          request('/state-templates'),
        ])
        overview.value = x[0].data
        documents.value = x[1].data
        bindings.value = x[2].data
        personas.value = x[3].data
        conversations.value = x[4].data.items || []
        memories.value = x[5].data
        selectedMemoryIds.value = selectedMemoryIds.value.filter(id => memories.value.some(item => item.id === id))
        metrics.value = x[6].data
        campaigns.value = x[7].data
        rpPacks.value = x[8].data
        stateTemplates.value = x[9].data
        if (!debug.value.session_id) {
          const bound = bindings.value.find(b => b.scope_type === 'session')
          if (bound) { debug.value.session_id = bound.scope_id; selectConversation() }
        }
        if (!archive.value.session_id) archive.value.session_id = debug.value.session_id
        if (!newGameDraft.value.session_id) newGameDraft.value.session_id = debug.value.session_id
        if (!newGameDraft.value.pack_id && rpPacks.value.length) newGameDraft.value.pack_id = rpPacks.value[0].id
        const warnings = x[4].data.warnings || []
        if (warnings.length) notice.value = warnings.join('；')
        await refreshCurrentGame()
        await refreshArchive()
      } catch (e) {
        error.value = e.message
      }
    }

    const choose = d => {
      selected.value = JSON.parse(JSON.stringify(d))
      pendingDeleteId.value = ''
      if (d.kind === 'quick_reply' && !Array.isArray(selected.value.data.items)) selected.value.data.items = []
      if (['lorebook', 'material'].includes(d.kind) && !Array.isArray(selected.value.data.entries)) {
        selected.value.data.entries = Object.values(selected.value.data.entries || {})
      }
      if (['lorebook', 'material'].includes(d.kind)) {
        selected.value.data.entries = (selected.value.data.entries || []).map(e => ({
          ...e,
          extensions: { category: '', description: '', ...(e.extensions || {}) },
        }))
      }
      advanced.value = JSON.stringify(selected.value.data, null, 2)
      clear()
    }

    const createDoc = kind => {
      const t = {
        character: { data: { name: '新角色', description: '', personality: '', scenario: '', first_mes: '', mes_example: '', system_prompt: '', post_history_instructions: '' } },
        character_group: { members: [], selection: 'round_robin' },
        preset: { main_prompt: '{{original_system}}', post_history_instructions: '', allow_character_main_override: false, allow_character_phi_override: true, blocks: JSON.parse(JSON.stringify(blocks)) },
        lorebook: { entries: [] },
        material: { entries: [] },
        persona: { content: '' },
        quick_reply: { items: defaultQuickReplies() },
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
      if (!selected.value?.id) return
      if (pendingDeleteId.value !== selected.value.id) {
        pendingDeleteId.value = selected.value.id
        notice.value = '再次点击删除以确认删除“' + selected.value.name + '”。'
        error.value = ''
        return
      }
      busy.value = true; clear()
      try {
        const out = await post('/documents/delete', { id: selected.value.id })
        if (!out.data?.deleted) throw new Error('资料不存在或已经被删除')
        selected.value = null
        pendingDeleteId.value = ''
        await load()
        notice.value = '已删除。'
      } catch (e) {
        error.value = e.message || '删除失败。'
      } finally {
        busy.value = false
      }
    }

    const duplicate = async () => {
      const out = await post('/documents/duplicate', { id: selected.value.id })
      await load()
      choose(documents.value.find(x => x.id === out.data.id))
    }

    const runDownload = async action => {
      busy.value = true; clear()
      try {
        const payload = (await action()).data
        saveDownload(payload)
        notice.value = `已开始下载 ${payload.filename || '导出文件'}。${downloadLocationHint}`
      } catch (e) {
        error.value = e.message || '导出失败。'
      } finally {
        busy.value = false
      }
    }

    const exportSelected = () => runDownload(() => post('/export/document', { id: selected.value?.id }))
    const exportKind = () => runDownload(() => post('/export/archive', {
      kinds: [tab.value], name: 'komeiji-tavern-' + tab.value,
    }))
    const exportAll = () => runDownload(() => post('/export/archive', { name: 'komeiji-tavern-all' }))

    const exportMessages = () => {
      if (!Array.isArray(debugResult.value?.messages)) { error.value = '当前没有可导出的 messages[]。'; return }
      const suffix = debug.value.session_id ? '-' + debug.value.session_id.replace(/[^a-zA-Z0-9_-]/g, '_') : ''
      const filename = 'messages' + suffix + '.json'
      saveJson(debugResult.value.messages, filename)
      notice.value = `已开始下载 ${filename}。${downloadLocationHint}`
    }

    const backupSession = () => {
      if (!debug.value.session_id) { error.value = '请先选择会话。'; return }
      return runDownload(() => post('/session/' + encodeURIComponent(debug.value.session_id) + '/backup', {}))
    }

    const refreshArchive = async () => {
      clear()
      const query = archive.value.session_id ? '?session_id=' + encodeURIComponent(archive.value.session_id) : ''
      archive.value.nodes = (await request('/archive' + query)).data
      continueVisibleCount.value = 1
      if (archive.value.selected && !archive.value.nodes.some(x => x.id === archive.value.selected.id)) {
        archive.value.selected = null
      }
    }

    const selectArchiveNode = async node => {
      clear()
      archive.value.selected = (await request('/archive/' + encodeURIComponent(node.id))).data
      archive.value.branch_name = archive.value.selected.branch_name || ''
    }

    const renameArchiveNode = async () => {
      if (!archive.value.selected) return
      await post('/archive/' + encodeURIComponent(archive.value.selected.id) + '/rename', {
        title: archive.value.selected.title,
        branch_name: archive.value.selected.branch_name,
      })
      await refreshArchive()
      archive.value.selected = (await request('/archive/' + encodeURIComponent(archive.value.selected.id))).data
      notice.value = '节点名称已更新。'
    }

    const branchFromArchiveNode = async () => {
      if (!archive.value.selected) return
      await post('/archive/' + encodeURIComponent(archive.value.selected.id) + '/branch', {
        session_id: archive.value.session_id || archive.value.selected.session_id,
        branch_name: archive.value.branch_name,
      })
      notice.value = '已设置从该节点继续；下一次真实对话会保存为新分支。'
    }

    const exportArchive = () => runDownload(() => post('/archive/export', { session_id: archive.value.session_id }))

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
        if (pendingImport.value?.file_name === file.value.name) {
          const out = await post('/import/commit', { parsed: pendingImport.value.parsed, file_name: file.value.name })
          pendingImport.value = null
          await load()
          binding.value.kind = out.data.kind
          binding.value.target_id = out.data.id
          tab.value = 'bindings'
          notice.value = '导入完成。请选择目标并绑定，当前尚未影响任何会话。'
          return
        }
        const lowerName = file.value.name.toLowerCase()
        if (['.db', '.sqlite', '.sqlite3'].some(suffix => lowerName.endsWith(suffix))) {
          const base64 = await new Promise((ok, fail) => {
            const r = new FileReader()
            r.onload = () => ok(String(r.result).split(',')[1])
            r.onerror = fail
            r.readAsDataURL(file.value)
          })
          const out = await post('/import/sqlite', { base64, file_name: file.value.name })
          await load()
          binding.value.kind = 'material'
          binding.value.target_id = out.data.id
          tab.value = 'bindings'
          notice.value = 'SQLite 导入完成：知识库素材，共 ' + out.data.count + ' 条。请选择目标并绑定。'
          return
        }

        const binary = lowerName.endsWith('.png')
        const content = binary ? '' : await file.value.text()
        const base64 = binary ? await new Promise((ok, fail) => {
          const r = new FileReader()
          r.onload = () => ok(String(r.result).split(',')[1])
          r.onerror = fail
          r.readAsDataURL(file.value)
        }) : ''
        const pre = await post('/import/preview', { content, base64, file_name: file.value.name })
        const info = pre.data.preview
        pendingImport.value = { file_name: file.value.name, parsed: pre.data.parsed, preview: info }
        notice.value = '识别为' + (labels[info.kind] || info.kind) + '“' + info.name + '”，共 ' + info.count + ' 项。再次点击“确认导入”完成写入。'
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

    const updateMemoryScope = () => {
      memoryDraft.value.scope_id = memoryDraft.value.scope_type === 'global' ? '*' : ''
    }

    const resetMemoryDraft = () => {
      memoryDraft.value = { ...newMemoryDraft(), scope_id: debug.value.session_id || '' }
    }

    const saveMemory = async () => {
      clear()
      if (!memoryDraft.value.content.trim()) { error.value = '请填写记忆内容。'; return }
      if (!memoryDraft.value.scope_id.trim()) { error.value = '请填写记忆作用域 ID。'; return }
      const out = await post('/memories', memoryDraft.value)
      await load()
      resetMemoryDraft()
      notice.value = out.data.warning || '长期记忆已保存。'
    }

    const editMemory = item => {
      memoryDraft.value = {
        id: item.id,
        scope_type: item.scope_type,
        scope_id: item.scope_id,
        category: item.category,
        content: item.content,
        enabled: item.enabled,
        status: item.status || (item.enabled ? 'active' : 'archived'),
        importance: Number(item.importance || 1),
        source_type: item.source_type || 'manual',
        source_ref: item.source_ref || '',
        expires_at: Number(item.expires_at || 0),
      }
      tab.value = 'memories'
    }

    const toggleMemory = async item => {
      await post('/memories/' + encodeURIComponent(item.id) + '/toggle', { enabled: !item.enabled })
      await load()
    }

    const toggleAllVisibleMemories = () => {
      if (allVisibleMemoriesSelected.value) {
        selectedMemoryIds.value = selectedMemoryIds.value.filter(id => !visibleMemoryIds.value.includes(id))
      } else {
        selectedMemoryIds.value = Array.from(new Set([...selectedMemoryIds.value, ...visibleMemoryIds.value]))
      }
    }

    const updateSelectedMemoryStatus = async status => {
      clear()
      if (!selectedMemoryIds.value.length) { error.value = '请先选择长期记忆。'; return }
      const out = await post('/memories/status', { ids: selectedMemoryIds.value, status })
      selectedMemoryIds.value = []
      await load()
      notice.value = '已更新 ' + out.data.updated + ' 条长期记忆。'
    }

    const deleteMemory = async item => {
      if (pendingMemoryDeleteId.value !== item.id) {
        pendingMemoryDeleteId.value = item.id
        notice.value = '再次点击删除以确认删除这条长期记忆。'
        error.value = ''
        return
      }
      await post('/memories/' + encodeURIComponent(item.id) + '/delete', {})
      pendingMemoryDeleteId.value = ''
      await load()
      notice.value = '长期记忆已删除。'
    }

    const editCampaign = async item => {
      campaignDraft.value = JSON.parse(JSON.stringify(item))
      campaignSchemaJson.value = JSON.stringify(campaignDraft.value.state_schema || {}, null, 2)
      campaignStateJson.value = JSON.stringify(campaignDraft.value.state_data || {}, null, 2)
      campaignSessionId.value = item.session_ids?.[0] || debug.value.session_id || ''
      campaignChanges.value = (await request('/campaigns/' + encodeURIComponent(item.id) + '/changes?limit=200')).data
    }

    const resetCampaignDraft = () => {
      campaignDraft.value = newCampaignDraft()
      campaignSchemaJson.value = JSON.stringify(campaignDraft.value.state_schema, null, 2)
      campaignStateJson.value = JSON.stringify(campaignDraft.value.state_data, null, 2)
      campaignSessionId.value = debug.value.session_id || ''
      campaignChanges.value = []
    }

    const saveCampaign = async () => {
      clear()
      if (!campaignDraft.value.name.trim()) { error.value = '请填写战役名称。'; return }
      try {
        campaignDraft.value.state_schema = JSON.parse(campaignSchemaJson.value || '{}')
        campaignDraft.value.state_data = JSON.parse(campaignStateJson.value || '{}')
      } catch (e) { error.value = '状态字段说明或当前状态不是合法 JSON：' + e.message; return }
      const out = await post('/campaigns', campaignDraft.value)
      campaignDraft.value = JSON.parse(JSON.stringify(out.data))
      if (campaignSessionId.value.trim()) {
        await post('/campaigns/' + encodeURIComponent(out.data.id) + '/sessions', { session_id: campaignSessionId.value.trim() })
      }
      await load()
      await editCampaign(campaigns.value.find(item => item.id === out.data.id) || out.data)
      notice.value = '战役与状态已保存。'
    }

    const deleteCampaign = async item => {
      if (pendingDeleteId.value !== item.id) {
        pendingDeleteId.value = item.id
        notice.value = '再次点击删除以确认；战役状态与候选变更将一并删除。'
        return
      }
      await post('/campaigns/' + encodeURIComponent(item.id) + '/delete', {})
      pendingDeleteId.value = ''
      resetCampaignDraft()
      await load()
    }

    const bindCampaignSession = async () => {
      if (!campaignDraft.value.id || !campaignSessionId.value.trim()) { error.value = '请先保存战役并填写会话 ID。'; return }
      await post('/campaigns/' + encodeURIComponent(campaignDraft.value.id) + '/sessions', { session_id: campaignSessionId.value.trim() })
      await load()
      await editCampaign(campaigns.value.find(item => item.id === campaignDraft.value.id))
      notice.value = '会话已绑定到该战役。'
    }

    const unbindCampaignSession = async sessionId => {
      await post('/campaigns/sessions/unbind', { session_id: sessionId })
      await load()
      await editCampaign(campaigns.value.find(item => item.id === campaignDraft.value.id))
    }

    const resolveCampaignChange = async (change, action) => {
      await post('/campaigns/' + encodeURIComponent(campaignDraft.value.id) + '/changes/' + encodeURIComponent(change.id), { action })
      await load()
      await editCampaign(campaigns.value.find(item => item.id === campaignDraft.value.id))
      notice.value = action === 'apply' ? '候选状态变更已应用。' : '候选状态变更已拒绝。'
    }

    const refreshCurrentGame = async () => {
      const sessionId = debug.value.session_id || newGameDraft.value.session_id || ''
      if (!sessionId) { currentGame.value = { campaign: null, pack: null, changes: [] }; return }
      currentGame.value = (await request('/game/current?session_id=' + encodeURIComponent(sessionId))).data
    }

    const refreshPlayableSession = async () => {
      const sessionId = debug.value.session_id || newGameDraft.value.session_id || ''
      newGameDraft.value.session_id = sessionId
      archive.value.session_id = sessionId
      await refreshCurrentGame()
      await refreshArchive()
    }

    const clearSelectedContinueNode = () => {
      archive.value.selected = null
      archive.value.branch_name = ''
      playDraft.value.branch_name = ''
    }

    const previewPlayPrompt = async () => {
      clear()
      const sessionId = debug.value.session_id || newGameDraft.value.session_id || ''
      if (!sessionId) { error.value = '请先选择当前会话。'; return }
      debug.value.session_id = sessionId
      debug.value.prompt = playDraft.value.prompt || 'Continue.'
      debug.value.mode = playDraft.value.mode
      debug.value.quiet_prompt = playDraft.value.quiet_prompt
      await simulate()
      tab.value = 'debug'
    }

    const playTurn = async () => {
      clear()
      const sessionId = debug.value.session_id || newGameDraft.value.session_id || ''
      if (!sessionId) { error.value = '请先选择当前会话。'; return }
      if (!String(playDraft.value.prompt || '').trim()) { error.value = '请输入玩家行动或续写要求。'; return }
      busy.value = true
      try {
        const out = await post('/game/play', {
          session_id: sessionId,
          prompt: playDraft.value.prompt,
          mode: playDraft.value.mode,
          quiet_prompt: playDraft.value.quiet_prompt,
          branch_node_id: archive.value.selected?.id || '',
          branch_name: playDraft.value.branch_name || archive.value.branch_name || '',
        })
        playResult.value = out.data
        currentGame.value = { campaign: out.data.campaign, pack: currentGame.value.pack, changes: out.data.changes || [] }
        if (out.data.reply) playDraft.value.prompt = ''
        await refreshArchive()
        if (out.data.node_id) archive.value.selected = (await request('/archive/' + encodeURIComponent(out.data.node_id))).data
        notice.value = out.data.conversation_synced ? '已在网页完成一轮并同步到 AstrBot conversation。' : '已在网页完成一轮并保存到插件分支树。'
      } catch (e) {
        error.value = e.message
      } finally {
        busy.value = false
      }
    }

    const createPackFromCurrent = async () => {
      const campaign = currentGame.value.campaign
      if (!campaign) { error.value = '当前会话没有战役。'; return }
      const out = await post('/rp-packs/from-campaign', { campaign_id: campaign.id, name: campaign.name + '整合包' })
      await load()
      newGameDraft.value.pack_id = out.data.id
      notice.value = '已从当前战役创建 RP 整合包。以后可一键重开。'
    }

    const deleteRpPack = async item => {
      await post('/rp-packs/' + encodeURIComponent(item.id) + '/delete', {})
      await load()
    }

    const startNewGame = async () => {
      clear()
      if (!newGameDraft.value.pack_id || !newGameDraft.value.session_id) { error.value = '请选择整合包和会话。'; return }
      busy.value = true
      try {
        const out = await post('/game/new', newGameDraft.value)
        debug.value.session_id = newGameDraft.value.session_id
        await load()
        notice.value = '新游戏“' + out.data.campaign.name + '”已创建；旧战役已归档。'
      } catch (e) { error.value = e.message } finally { busy.value = false }
    }

    const flattenState = (value, prefix = '') => {
      const rows = []
      if (Array.isArray(value)) return [{ path: prefix, type: 'array', value: value.join(', ') }]
      if (value !== null && typeof value === 'object') {
        for (const [key, child] of Object.entries(value)) rows.push(...flattenState(child, prefix ? prefix + '.' + key : key))
        return rows
      }
      return prefix ? [{ path: prefix, type: value === null ? 'null' : typeof value, value: value ?? '' }] : rows
    }
    const currentStateRows = computed(() => flattenState(currentGame.value.campaign?.state_data || {}))
    const setCurrentStateValue = (row, raw) => {
      const campaign = currentGame.value.campaign
      if (!campaign) return
      const state = JSON.parse(JSON.stringify(campaign.state_data || {}))
      const parts = row.path.split('.')
      let cursor = state
      for (const part of parts.slice(0, -1)) cursor = cursor[part]
      let value = raw
      if (row.type === 'number') value = Number(raw)
      else if (row.type === 'boolean') value = raw === true || raw === 'true'
      else if (row.type === 'array') value = String(raw).split(',').map(item => item.trim()).filter(Boolean)
      else if (row.type === 'null') value = raw || null
      cursor[parts.at(-1)] = value
      campaign.state_data = state
    }
    const saveCurrentState = async () => {
      if (!currentGame.value.campaign) return
      await post('/campaigns', currentGame.value.campaign)
      await refreshCurrentGame()
      notice.value = '权威状态已保存。'
    }

    const analyzeCurrentWorldbooks = async () => {
      worldbookReports.value = []
      const campaign = currentGame.value.campaign
      if (!campaign) return
      const ids = bindings.value.filter(item => item.scope_type === 'campaign' && item.scope_id === campaign.id && item.kind === 'lorebook').map(item => item.target_id)
      for (const id of ids) worldbookReports.value.push((await request('/worldbooks/' + encodeURIComponent(id) + '/analyze')).data)
    }
    const riskLabel = value => value === 'low' ? '低风险' : value === 'medium' ? '中风险' : '高风险'

    const refreshMetrics = async () => {
      clear()
      metrics.value = (await request('/metrics?days=' + encodeURIComponent(metricDays.value) + '&limit=1000')).data
    }

    const refreshRetrievalStats = async () => {
      clear()
      const query = debug.value.session_id ? '?session_id=' + encodeURIComponent(debug.value.session_id) : ''
      retrievalStats.value = (await request('/retrieval/stats' + query)).data
    }

    const runRetrievalTest = async () => {
      clear()
      if (!retrievalTest.value.text.trim()) { error.value = '请填写检索测试文本。'; return }
      busy.value = true
      try {
        retrievalResult.value = (await post('/retrieval/test', {
          text: retrievalTest.value.text,
          session_id: debug.value.session_id,
          persona_id: debug.value.persona_id,
        })).data
      } catch (e) {
        error.value = e.message
      } finally {
        busy.value = false
      }
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
      : i.scope_type === 'campaign' ? '战役：' + i.scope_id
      : i.scope_type === 'world' ? '世界：' + i.scope_id
      : i.scope_type === 'ruleset' ? '规则集：' + i.scope_id
      : i.scope_type === 'session' ? '会话：' + i.scope_id
      : i.scope_type + '：' + i.scope_id

    const move = (i, n) => {
      const b = selected.value.data.blocks
      const j = i + n
      if (j >= 0 && j < b.length) [b[i], b[j]] = [b[j], b[i]]
    }

    const moveMember = (i, n) => {
      const b = selected.value.data.members
      const j = i + n
      if (j >= 0 && j < b.length) [b[i], b[j]] = [b[j], b[i]]
    }

    const addMember = id => {
      if (!id) return
      selected.value.data.members = [...(selected.value.data.members || []), id]
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

    const addQuickReply = () => {
      const item = newQuickReply()
      selected.value.data.items = [...(selected.value.data.items || []), item]
      selectedQuickReplyId.value = item.id
    }

    const removeQuickReply = item => {
      selected.value.data.items = (selected.value.data.items || []).filter(x => x !== item)
      if (selectedQuickReplyId.value === item.id) selectedQuickReplyId.value = ''
    }

    const applyQuickReply = item => {
      const content = String(item.content || '').trim()
      const existing = String(debug.value.prompt || '').trim()
      debug.value.prompt = item.append_input && existing ? content + '\n\n' + existing : content
      debug.value.mode = item.mode || 'normal'
      debug.value.quiet_prompt = item.mode === 'quiet' ? content : ''
      tab.value = 'debug'
    }

    const openTwist = () => {
      twistOpen.value = true
      twistText.value = twistSeeds[Math.floor(Math.random() * twistSeeds.length)]
    }

    const rerollTwist = () => {
      const next = twistSeeds[Math.floor(Math.random() * twistSeeds.length)]
      twistText.value = next === twistText.value ? twistSeeds[(twistSeeds.indexOf(next) + 1) % twistSeeds.length] : next
    }

    const useTwist = () => {
      const existing = String(debug.value.prompt || '').trim()
      const content = '第三只眼灵感：' + twistText.value
      debug.value.prompt = existing ? existing + '\n\n' + content : content
      twistOpen.value = false
      tab.value = 'debug'
    }

    const addEntry = () => {
      const item = newEntry(tab.value)
      selected.value.data.entries.push(item)
      openEntryUid.value = item.uid
    }

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
        error.value = e.message || '读取真实请求预览失败。'
      }
    }

    onMounted(() => {
      load()
    })

    return {
      tabs, navGroups, labels, tab, tabMeta, currentTabMeta, tabCount, activeSingleCount, additiveBindingCount, enabledMemoryCount,
      overview, documents, bindings, personas, selected, pendingDeleteId, pendingImport, pendingMemoryDeleteId, error, notice, busy,
      dismissNotice,
      theme, toggleTheme,
      fileLabel,
      downloadLocationHint, saveJson,
      advanced, binding, memoryDraft, memoryQuery, memoryStatusFilter, selectedMemoryIds,
      memories, filteredMemories, memoryGroups, allVisibleMemoriesSelected,
      campaigns, campaignDraft, campaignChanges, campaignSessionId, campaignSchemaJson, campaignStateJson,
      rpPacks, stateTemplates, currentGame, worldbookReports, newGameDraft, currentStateRows,
      metricDays, metrics, metricItems, metricTotals, metricProviders, maxMetricTokens,
      retrievalTest, retrievalStats, retrievalResult,
      archive, archiveTree, archiveSegments, homeGuide, selectedQuickReplyId, promptAssembly, twistOpen, twistText,
      playDraft, playResult, continueVisibleCount, hiddenContinueCount, sortedContinueNodes, continueTimeline, latestContinueNode, selectedContinueNode, pendingCurrentChanges, activeContinueSegment, continueHits, compactStateRows,
      debug, debugResult, debugSummary, docsForTab, bindDocs, characterDocs, card, entries, filteredEntries, quickReplies,
      groupMembers, availableGroupMembers,
      sessionOptions, sFiltered, dFiltered, sessionDisplay, debugDisplay, bindingTargetTitle, bindingsForTarget, bindingSummary,
      entryQuery, entryFilter, openEntryUid,
      sOpen, dOpen, pickSession, onSFocus, onSBlur, pickDebug, onDFocus, onDBlur,
      choose, createDoc, save, remove, duplicate, applyAdvanced, importData,
      exportSelected, exportKind, exportAll, exportMessages, backupSession,
      refreshArchive, selectArchiveNode, renameArchiveNode, branchFromArchiveNode, exportArchive,
      setFile: e => { file.value = e.target.files[0]; pendingImport.value = null },
      updateScope, addBinding, unbind, scopeName, updateMemoryScope, resetMemoryDraft,
      saveMemory, editMemory, toggleMemory, toggleAllVisibleMemories,
      updateSelectedMemoryStatus, deleteMemory, refreshMetrics,
      editCampaign, resetCampaignDraft, saveCampaign, deleteCampaign,
      bindCampaignSession, unbindCampaignSession, resolveCampaignChange,
      refreshCurrentGame, refreshPlayableSession, expandContinueNodes, collapseContinueNodes, clearSelectedContinueNode, previewPlayPrompt, playTurn, createPackFromCurrent, deleteRpPack, startNewGame,
      setCurrentStateValue, saveCurrentState, analyzeCurrentWorldbooks, riskLabel,
      refreshRetrievalStats, runRetrievalTest,
      move, moveMember, addMember, addBlock, addEntry, addQuickReply, removeQuickReply, applyQuickReply,
      openTwist, rerollTwist, useTwist, keyText, setKeys,
      selectConversation, simulate, actual, formatTimestamp,
    }
  },
  template: `
<div class="shell" :class="'theme-' + theme">
  <aside class="nav">
    <div class="brand">
      <button class="third-eye" type="button" @click="openTwist" title="唤醒第三只眼灵感助手"><span>眼</span></button>
      <small>ASTRBOT 角色扮演神经引擎</small>
      <h1>Komeiji's<br>Tavern</h1>
      <span v-if="overview?.version">v{{overview.version}} · 深渊星穹</span>
    </div>
    <div class="nav-scroll">
      <div class="nav-group" v-for="group in navGroups">
        <strong>{{group.name}}</strong>
        <button v-for="t in group.items" :class="{active:tab===t[0]}" @click="tab=t[0];selected=null">
          <span><i>{{tabMeta[t[0]]?.icon || '·'}}</i>{{t[1]}}</span>
          <em v-if="tabCount(t[0]) !== null">{{tabCount(t[0])}}</em>
        </button>
      </div>
    </div>
    <div class="nav-footer">
      <button class="theme-switch" :class="'is-' + theme" type="button" :aria-label="theme==='dark'?'切换到浅色':'切换到深色'" @click="toggleTheme">
        <span class="theme-switch-track"><span class="theme-switch-thumb"></span></span>
        <span class="theme-switch-label">{{theme==='dark'?'深渊星穹':'日光酒馆'}}</span>
      </button>
    </div>
  </aside>
  <main>
    <Transition name="toast-slide">
      <div v-if="notice" class="toast notice-toast" @click="dismissNotice">
        <span>✓</span>
        <p>{{notice}}</p>
      </div>
    </Transition>
    <header>
      <div class="page-title">
        <span class="page-glyph">{{currentTabMeta.icon}}</span>
        <div><h2>{{tabs.find(x=>x[0]===tab)?.[1]}}<span>{{currentTabMeta.subtitle}}</span></h2></div>
      </div>
      <div class="header-actions">
        <div class="status-strip">
          <span><i></i>神经链接：同步</span>
          <span>会话 {{sessionOptions.length}}</span>
          <span>绑定 {{bindings.length}}</span>
        </div>
        <div class="import toolbar-group">
          <label class="file-picker"><input type="file" accept=".json,.yaml,.yml,.png,.txt,.md,.db,.sqlite,.sqlite3" @change="setFile"><span>选择文件</span><b>{{fileLabel}}</b></label>
          <button class="toolbar-button" @click="importData" :disabled="busy">{{pendingImport?'确认导入':'解析并导入'}}</button>
        </div>
        <details class="export-menu">
          <summary>导出</summary>
          <div class="export-popover">
            <strong>导出内容</strong>
            <button v-if="['character','character_group','preset','lorebook','material','persona','quick_reply'].includes(tab) && selected?.id" @click="exportSelected" :disabled="busy">当前资料 JSON</button>
            <button v-if="['character','character_group','preset','lorebook','material','persona','quick_reply'].includes(tab)" @click="exportKind" :disabled="busy || !docsForTab.length">当前类别 ZIP</button>
            <button v-if="tab==='home' || ['character','character_group','preset','lorebook','material','persona','quick_reply'].includes(tab)" @click="exportAll" :disabled="busy || !documents.length">全部资料 ZIP</button>
            <button v-if="tab==='debug'" @click="exportMessages" :disabled="busy || !debugResult?.messages">当前 messages[] JSON</button>
            <button v-if="tab==='debug'" @click="backupSession" :disabled="busy || !debug.session_id">当前会话备份 ZIP</button>
            <button v-if="tab==='archive'" @click="exportArchive" :disabled="busy || !archive.nodes.length">当前分支树 JSON</button>
            <p v-if="!['home','character','character_group','preset','lorebook','material','persona','quick_reply','debug','archive'].includes(tab)" class="muted">当前页面没有可导出的内容。</p>
            <small>{{downloadLocationHint}}</small>
          </div>
        </details>
      </div>
    </header>
    <div v-if="twistOpen" class="modal-backdrop" @click.self="twistOpen=false">
      <div class="twist-modal">
        <div class="result-head"><h3>第三只眼灵感助手</h3><button @click="twistOpen=false">关闭</button></div>
        <p class="muted">随机生成一段可直接送入调试器的剧情变奏，不会自动保存或影响资料。</p>
        <blockquote>{{twistText}}</blockquote>
        <div class="actions"><button @click="rerollTwist">再次无意识转动</button><button class="primary" @click="useTwist">送入调试器</button></div>
      </div>
    </div>
    <div v-if="error" class="alert error">{{error}}</div>
    <section v-if="tab==='home'" class="home">
      <div class="home-hero-grid">
        <div class="hero">
          <small>ASTRBOT v{{overview.version || 'LOCAL'}} ENGINE LOADED</small>
          <h3>{{overview.ready?'欢迎来到星系潜意识酒馆':'从一个可验证的角色会话开始'}}</h3>
          <p>这里是 AI 角色扮演与长期记忆编织的中枢。资料只有进入绑定链路后才会参与模型请求，调试器会展示最终 messages[]、检索命中与 Token 近似估算。</p>
          <div class="steps">
            <button class="primary" @click="tab='debug'"><b>测</b>进入神经调试器试玩</button>
            <button @click="openTwist"><b>眼</b>唤醒第三只眼灵感</button>
          </div>
        </div>
        <div class="system-card">
          <div class="result-head compact"><h3>当前引擎快照</h3><span>LIVE</span></div>
          <div class="system-line"><span>资料总量</span><b>{{documents.length}}</b></div>
          <div class="system-line"><span>有效主链</span><b>{{activeSingleCount}} / 4</b></div>
          <div class="system-line"><span>叠加注入</span><b>{{additiveBindingCount}}</b></div>
          <div class="system-line"><span>活跃记忆</span><b>{{enabledMemoryCount}}</b></div>
        </div>
      </div>
      <div class="ops-grid">
        <div class="ops-card pink"><span>资料总量</span><b>{{documents.length}}</b><small>角色、预设、世界书、素材与快捷回复</small></div>
        <div class="ops-card cyan"><span>有效主链</span><b>{{activeSingleCount}} / 4</b><small>预设、角色、角色组、Persona</small></div>
        <div class="ops-card gold"><span>叠加注入</span><b>{{additiveBindingCount}}</b><small>世界书、创作素材、快捷回复绑定</small></div>
        <div class="ops-card purple"><span>活跃记忆</span><b>{{enabledMemoryCount}}</b><small>active 且启用的长期记忆</small></div>
      </div>
      <div class="guide-grid">
        <div v-for="item in homeGuide" :class="['guide-card', item.state]">
          <span>{{item.state==='ready'?'已就绪':item.state==='warn'?'需确认':item.state==='todo'?'待处理':'可选'}}</span>
          <h3>{{item.title}}</h3>
          <p>{{item.detail}}</p>
          <button @click="item.create ? createDoc(item.create) : tab=item.tab">{{item.action}}</button>
        </div>
      </div>
      <div class="panel">
        <h3>知识库 SQLite 素材导入</h3>
        <p>顶部“解析并导入”支持 .db / .sqlite / .sqlite3。系统会自动识别兼容的知识库表，并导入为创作素材。</p>
      </div>
      <div class="cards">
        <div class="metric" v-for="(v,k) in labels"><strong>{{overview.counts?.[k]||0}}</strong><span>{{v}}</span></div>
      </div>
      <div class="panel"><h3>待完成</h3><p v-if="!overview.tasks?.length">没有必须处理的事项。</p><ul><li v-for="x in overview.tasks">{{x}}</li></ul></div>
    </section>
    <section v-else-if="tab==='game'" class="stack game-hub">
      <div class="panel game-toolbar">
        <label>当前会话<select v-model="debug.session_id" @change="refreshPlayableSession"><option value="">请选择</option><option v-for="session in sessionOptions" :value="session.id">{{session.title}} · {{session.platform}}</option></select></label>
        <button @click="refreshPlayableSession">刷新</button>
        <button v-if="currentGame.campaign && !currentGame.pack" @click="createPackFromCurrent">将当前战役保存为整合包</button>
      </div>

      <div v-if="currentGame.campaign" class="hero game-hero">
        <small>{{currentGame.pack ? 'RP PACK · '+currentGame.pack.name : 'CUSTOM CAMPAIGN'}}</small>
        <h3>{{currentGame.campaign.name}}</h3>
        <p>{{currentGame.campaign.description}}</p>
        <div class="steps"><button @click="tab='archive'">查看存档树</button><button @click="tab='campaigns';editCampaign(currentGame.campaign)">高级战役设置</button></div>
      </div>
      <div v-else class="panel empty"><h3>这个会话还没有游戏</h3><p>从下方选择一个 RP 整合包即可一键开局。</p></div>

      <div v-if="currentGame.campaign" class="panel continue-board">
        <div class="result-head">
          <div><h3>网页接着玩</h3><p class="muted">像酒馆一样在这里直接输入玩家行动；系统会使用当前绑定、世界书、长期记忆、战役状态和选中的剧情分支。</p></div>
          <div class="actions"><button @click="tab='archive'">完整存档树</button><button @click="clearSelectedContinueNode" :disabled="!archive.selected">回到当前最新</button><button @click="previewPlayPrompt" :disabled="busy">预览本轮 Prompt</button></div>
        </div>
        <div class="summary-grid continue-stats">
          <div class="summary-card ready"><span>当前战役</span><b>{{currentGame.campaign.name}}</b></div>
          <div class="summary-card"><span>当前会话段</span><b>{{activeContinueSegment ? activeContinueSegment.nodes.length + ' 个节点' : '暂无节点'}}</b></div>
          <div class="summary-card"><span>最新轮次</span><b>{{latestContinueNode ? latestContinueNode.turn_index : '—'}}</b></div>
          <div class="summary-card" :class="{warn:pendingCurrentChanges.length}"><span>待确认状态</span><b>{{pendingCurrentChanges.length}}</b></div>
        </div>
        <div class="continue-layout">
          <div class="continue-map">
            <button v-for="node in continueTimeline" :key="node.id" :class="['continue-node',{active:archive.selected?.id===node.id,latest:latestContinueNode?.id===node.id}]" @click="selectArchiveNode(node)" :style="{paddingLeft: (18 + Math.min(node.depth || 0, 5) * 14) + 'px'}">
              <span>{{node.branch_name || '主线'}}</span>
              <b>{{node.title || '未命名节点'}}</b>
              <small>第 {{node.turn_index}} 轮 · {{formatTimestamp(node.created_at)}}</small>
            </button>
            <button v-if="hiddenContinueCount" class="ghost compact" @click="expandContinueNodes">继续展开旧节点（还剩 {{hiddenContinueCount}}）</button>
            <button v-if="continueVisibleCount > 1" class="ghost compact" @click="collapseContinueNodes">折叠旧节点</button>
            <p v-if="!continueTimeline.length" class="muted">还没有自动归档节点。完成一次真实 RP 后，这里会出现可继续的剧情路线。</p>
          </div>
          <div class="play-console">
            <div class="play-transcript">
              <article v-if="archive.selected?.assistant_text" class="chat-bubble assistant"><small>选中存档 · {{archive.selected.branch_name || '主线'}} · 第 {{archive.selected.turn_index}} 轮</small><pre>{{archive.selected.assistant_text}}</pre></article>
              <article v-else-if="selectedContinueNode" class="chat-bubble assistant"><small>最新节点 · {{selectedContinueNode.branch_name || '主线'}} · 第 {{selectedContinueNode.turn_index}} 轮</small><p>选择左侧节点可查看回复；不选节点时将从当前会话继续。</p></article>
              <article v-if="playResult?.reply" class="chat-bubble assistant live"><small>刚生成 · {{playResult.provider_id || 'current provider'}} · {{playResult.conversation_synced ? '已同步 AstrBot' : '插件分支树'}}</small><pre>{{playResult.reply}}</pre></article>
              <p v-if="!archive.selected?.assistant_text && !playResult?.reply" class="muted">这里会显示选中存档或刚生成的回复。输入玩家行动后，网页会直接完成一轮 RP 并自动保存新节点。</p>
            </div>

            <label>玩家行动 / 续写要求<textarea v-model="playDraft.prompt" placeholder="例如：我推开门，压低声音问她刚才听见了什么。"></textarea></label><p class="muted play-note">{{archive.selected ? '将从选中节点另起/续写分支。' : '未选节点时会接当前最新剧情。'}}</p>
            <div class="play-controls">
              <label>模式<select v-model="playDraft.mode"><option value="normal">普通生成</option><option value="continue">继续上一段</option><option value="impersonate">代写玩家</option><option value="quiet">静默提示</option></select></label>
              <label>分支名<input v-model="playDraft.branch_name" placeholder="可选：if线 / 重开 / 主线"></label>
              <button class="primary" @click="playTurn" :disabled="busy">{{busy ? '生成中…' : '在网页里继续'}}</button>
            </div>
            <label v-if="playDraft.mode==='quiet'">静默提示<textarea v-model="playDraft.quiet_prompt" placeholder="仅作为本轮约束，不直接作为玩家台词。"></textarea></label>
          </div>
          <aside class="play-hud">
            <div><h4>状态 HUD</h4><div class="state-pills"><span v-for="row in compactStateRows"><b>{{row.path}}</b>{{row.value || '—'}}</span></div></div>
            <div><h4>命中可视化</h4><p class="muted" v-if="!continueHits.retrieval.length && !continueHits.memory.length">生成后显示本轮世界书/素材检索与长期记忆命中。</p><div class="hit-chip" v-for="hit in continueHits.retrieval.slice(0,6)">世界书 · {{hit.name || hit.uid}}</div><div class="hit-chip memory" v-for="hit in continueHits.memory.slice(0,6)">记忆 · {{hit.category}} · {{hit.content}}</div></div>
            <div><h4>待确认状态</h4><div v-for="change in pendingCurrentChanges.slice(0,4)" class="mini-change"><b>{{riskLabel(change.risk_level)}} · 第 {{change.source_turn}} 轮</b><small>{{change.reason}}</small><div class="actions"><button @click="resolveCampaignChange(change,'apply');refreshCurrentGame()">应用</button><button @click="resolveCampaignChange(change,'reject');refreshCurrentGame()">拒绝</button></div></div><p v-if="!pendingCurrentChanges.length" class="muted">暂无待确认补丁。</p></div>
          </aside>
        </div>
      </div>

      <div class="panel">
        <div class="result-head"><div><h3>新游戏向导</h3><p class="muted">自动归档旧战役、初始化状态、绑定全部资料并新建 AstrBot conversation。</p></div><button class="primary" @click="startNewGame" :disabled="busy">开始新游戏</button></div>
        <div class="grid">
          <label>RP整合包<select v-model="newGameDraft.pack_id"><option value="">请选择</option><option v-for="pack in rpPacks" :value="pack.id">{{pack.name}}</option></select></label>
          <label>游戏名称<input v-model="newGameDraft.name" placeholder="留空使用整合包名称"></label>
          <label>状态模板<select v-model="newGameDraft.template_id"><option value="">使用整合包初始状态</option><option v-for="(template,id) in stateTemplates" :value="id">{{template.name}}</option></select></label>
          <label>目标会话<select v-model="newGameDraft.session_id"><option value="">请选择</option><option v-for="session in sessionOptions" :value="session.id">{{session.title}} · {{session.platform}}</option></select></label>
          <label>AstrBot处理<select v-model="newGameDraft.conversation_mode"><option value="new">新建 conversation（推荐）</option><option value="clear">清空当前 conversation</option></select></label>
          <label class="check-field"><input type="checkbox" v-model="newGameDraft.archive_current">归档当前战役</label>
        </div>
        <p v-if="!rpPacks.length" class="muted">还没有整合包。如果当前会话已有战役，点击页面顶部“将当前战役保存为整合包”。</p>
        <div class="activation" v-for="pack in rpPacks"><div><b>{{pack.name}}</b><small>{{pack.description}}</small></div><button class="danger" @click="deleteRpPack(pack)">删除整合包</button></div>
      </div>

      <div v-if="currentGame.campaign" class="panel">
        <div class="result-head"><div><h3>当前权威状态</h3><p class="muted">普通字段直接编辑；复杂结构仍可在高级战役设置中维护。</p></div><button class="primary" @click="saveCurrentState">保存状态</button></div>
        <div class="state-form">
          <label v-for="row in currentStateRows"><span>{{row.path}}</span>
            <select v-if="row.type==='boolean'" :value="String(row.value)" @change="setCurrentStateValue(row,$event.target.value)"><option value="true">是</option><option value="false">否</option></select>
            <input v-else :type="row.type==='number'?'number':'text'" :value="row.value" @input="setCurrentStateValue(row,$event.target.value)">
          </label>
        </div>
      </div>

      <div v-if="currentGame.campaign" class="panel">
        <div class="result-head"><div><h3>状态变更</h3><p class="muted">低风险可自动应用；资源、伤势、积分与关键任务仍需确认。</p></div><span>{{currentGame.changes.filter(item=>item.status==='pending').length}} 待确认</span></div>
        <div class="memory-card" v-for="change in currentGame.changes.slice(0,20)">
          <div class="result-head"><b>{{riskLabel(change.risk_level)}} · {{change.status}}</b><small>第 {{change.source_turn}} 轮</small></div>
          <p>{{change.reason}}</p><pre>{{JSON.stringify(change.patch,null,2)}}</pre>
          <div class="actions"><button v-if="change.status==='pending'" class="primary" @click="resolveCampaignChange(change,'apply');refreshCurrentGame()">应用</button><button v-if="change.status==='pending'" @click="resolveCampaignChange(change,'reject');refreshCurrentGame()">拒绝</button><button v-if="change.status==='applied'" @click="resolveCampaignChange(change,'undo');refreshCurrentGame()">撤销</button></div>
        </div>
      </div>

      <div v-if="currentGame.campaign" class="panel">
        <div class="result-head"><div><h3>世界书体检</h3><p class="muted">检查错误分隔符、无法命中、过大常驻和宽泛关键词。</p></div><button @click="analyzeCurrentWorldbooks">开始体检</button></div>
        <div v-for="report in worldbookReports" class="binding-stack"><h4>{{report.name}} · {{report.entry_count}} 条</h4><div v-for="issue in report.issues" :class="['activation',issue.level]"><b>{{issue.entry}}</b><span>{{issue.message}}</span></div><p v-if="!report.issues.length" class="muted">没有发现明显问题。</p></div>
      </div>
    </section>
    <section v-else-if="['character','character_group','preset','lorebook','material','persona','quick_reply'].includes(tab)" class="workspace">
      <div class="library">
        <div class="library-actions">
          <button class="primary" @click="createDoc(tab)">新建{{labels[tab]}}</button>
        </div>
        <button v-for="d in docsForTab" :class="['doc',{active:selected?.id===d.id}]" @click="choose(d)"><b>{{d.name}}</b><small>{{new Date(d.updated_at*1000).toLocaleString()}}</small></button>
        <p v-if="!docsForTab.length" class="muted">还没有{{labels[tab]}}。</p>
      </div>
      <article v-if="selected" class="editor">
        <div class="editor-title">
          <input class="title-input" v-model="selected.name">
          <div>
            <button v-if="selected.id" @click="duplicate">复制</button>
            <button v-if="selected.id" class="danger" @click="remove">{{pendingDeleteId===selected.id?'确认删除':'删除'}}</button>
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
        <template v-if="tab==='character_group'">
          <div class="grid">
            <label>选择策略<select v-model="selected.data.selection"><option value="round_robin">round_robin：未点名时自动轮询</option><option value="manual">manual：不自动推进</option></select></label>
            <label>添加成员<select @change="addMember($event.target.value); $event.target.value=''">
              <option value="">请选择角色卡</option>
              <option v-for="d in availableGroupMembers" :value="d.id">{{d.name}}</option>
            </select></label>
          </div>
          <p class="muted">真实请求会优先匹配用户消息中点名的成员；未点名时按下方顺序选择。</p>
          <div class="block" v-for="(m,i) in groupMembers">
            <div class="block-head">
              <b>{{i+1}}. {{m.name}}</b>
              <button @click="moveMember(i,-1)">↑</button><button @click="moveMember(i,1)">↓</button>
              <button class="danger" @click="selected.data.members.splice(i,1)">移除</button>
            </div>
            <small>{{m.id}}</small>
          </div>
          <p v-if="!groupMembers.length" class="muted">还没有成员，请先创建角色卡再添加到角色组。</p>
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
          <div class="assembly-preview" v-if="promptAssembly.length">
            <div class="result-head compact"><h3>实时提示词装配骨架</h3><span>仅展示已启用块</span></div>
            <div class="assembly-row" v-for="b in promptAssembly">
              <b>[{{b.role}} / {{b.position}} / D:{{b.depth}}]</b>
              <span>{{b.name}} · 优先级 {{b.priority}}</span>
              <p>{{(b.content || '由插件运行时动态生成').slice(0,120)}}</p>
            </div>
          </div>
        </template>
        <template v-if="tab==='lorebook' || tab==='material'">
          <div class="entry-toolbar">
            <input v-model="entryQuery" placeholder="搜索标题、关键词、分类或内容">
            <select v-model="entryFilter"><option value="all">全部条目</option><option value="enabled">只看启用</option><option value="disabled">只看禁用</option><option value="vectorized">只看向量化</option><option value="constant">只看常驻</option><option value="no_keys">无主关键词</option></select>
            <button @click="openEntryUid=''">全部收起</button>
            <button class="primary" @click="addEntry">添加条目</button>
          </div>
          <p class="muted">显示 {{filteredEntries.length}} / {{entries.length}} 条。点击条目卡片展开编辑；保存后会自动重建检索索引。</p>
          <div class="entry-card" v-for="e in filteredEntries" :class="{open:openEntryUid===e.uid, disabled:e.disable}">
            <div class="entry-summary" @click="openEntryUid = openEntryUid===e.uid ? '' : e.uid">
              <div>
                <b>{{e.comment || '未命名条目'}}</b>
                <p>{{(e.extensions?.description || e.content || '没有内容').slice(0,120)}}</p>
              </div>
              <div class="entry-tags">
                <span v-if="tab==='material'">{{e.extensions?.category || '未分类'}}</span>
                <span>{{keyText(e.key) || '无主关键词'}}</span>
                <span v-if="e.vectorized">向量化</span>
                <span v-if="e.constant">常驻</span>
                <span v-if="e.disable">禁用</span>
              </div>
            </div>
            <div class="entry-detail" v-if="openEntryUid===e.uid">
              <div class="entry-head">
                <input v-model="e.comment"><label><input type="checkbox" v-model="e.constant">常驻</label>
                <label><input type="checkbox" v-model="e.disable">禁用</label>
                <label><input type="checkbox" v-model="e.vectorized">向量化</label>
                <button class="danger" @click="selected.data.entries.splice(entries.indexOf(e),1)">删除</button>
              </div>
              <div class="grid" v-if="tab==='material'">
                <label>分类<input v-model="e.extensions.category"></label>
                <label>描述<input v-model="e.extensions.description"></label>
              </div>
              <div class="grid">
                <label>主关键词<input :value="keyText(e.key)" @input="setKeys(e,'key',$event.target.value)"></label>
                <label>次关键词<input :value="keyText(e.keysecondary)" @input="setKeys(e,'keysecondary',$event.target.value)"></label>
              </div>
              <label>注入内容<textarea v-model="e.content"></textarea></label>
              <details class="advanced entry-advanced">
                <summary>高级触发设置</summary>
                <div class="inline">
                  <label><input type="checkbox" v-model="e.selective">启用次关键词逻辑</label>
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
              </details>
            </div>
          </div>
          <p v-if="!filteredEntries.length" class="muted">没有匹配当前筛选条件的条目。</p>
        </template>
        <template v-if="tab==='persona'"><label>用户设定内容<textarea class="tall" v-model="selected.data.content"></textarea></label></template>
        <template v-if="tab==='quick_reply'">
          <div class="panel">
            <h3>快捷回复怎么用</h3>
            <p>快捷回复是可复用的 AI 提示词模板，不会把固定文字原样发送。保存后请在“绑定管理”中绑定到全局、Persona 或具体会话。</p>
            <p><code>/tavern qr list</code> 查看当前可用项；使用 <code>/tavern qr 编号</code>、<code>/tavern qr 别名</code> 或 <code>/tavern qr 名称</code> 触发。命令末尾可以追加本次要求，例如 <code>/tavern qr detail 更侧重环境气氛</code>。</p>
          </div>
          <div class="entry-toolbar">
            <button class="primary" @click="addQuickReply">新增快捷回复</button>
            <span class="muted">保存并绑定后，可通过聊天命令或调试器使用。</span>
          </div>
          <div class="block quick-reply-block" v-for="item in quickReplies" :class="{inactive:!item.enabled}">
            <div class="block-head">
              <label><input type="checkbox" v-model="item.enabled">启用</label>
              <input v-model="item.label" placeholder="快捷回复名称">
              <button @click="selectedQuickReplyId = selectedQuickReplyId===item.id ? '' : item.id">{{selectedQuickReplyId===item.id?'收起':'编辑'}}</button>
              <button @click="applyQuickReply(item)">在调试器中使用</button>
              <button class="danger" @click="removeQuickReply(item)">删除</button>
            </div>
            <div class="inline">
              <label>命令别名<input v-model="item.alias" placeholder="例如 continue（不要带空格）"></label>
              <label>生成模式<select v-model="item.mode"><option value="normal">普通生成</option><option value="continue">继续生成</option><option value="impersonate">代写用户回复</option><option value="quiet">静默提示词</option></select></label>
              <label>排序<input type="number" v-model.number="item.order"></label>
              <label class="check-field"><input type="checkbox" v-model="item.append_input">拼接命令后的补充文本</label>
            </div>
            <label v-if="selectedQuickReplyId===item.id">提示词内容<textarea v-model="item.content" placeholder="例如：继续当前剧情，加强动作与环境描写，不要替用户决定行动。"></textarea></label>
            <p v-else class="muted">{{(item.content || '暂无内容').slice(0,160)}}</p>
          </div>
          <p v-if="!quickReplies.length" class="muted">还没有快捷回复，点击上方按钮新增。</p>
        </template>
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
        <div class="result-head">
          <div>
            <h3>当前目标的有效配置</h3>
            <p class="muted">{{bindingTargetTitle}}</p>
          </div>
          <button @click="tab='debug'">去调试器验证</button>
        </div>
        <label class="combo">用于查看生效结果的会话
          <input v-model="debugDisplay" @focus="onDFocus" @blur="onDBlur" placeholder="搜索或输入会话 ID">
          <div class="combo-panel" v-if="dOpen">
            <div v-for="c in dFiltered" class="combo-option" @mousedown.prevent="pickDebug(c)">{{c.title}} · {{c.platform}}</div>
            <div v-if="!dFiltered.length" class="combo-option muted">无匹配，可直接输入 ID</div>
          </div>
        </label>
        <div class="binding-summary">
          <div class="binding-slot" v-for="kind in ['preset','character','character_group','persona']">
            <span>{{labels[kind]}}</span>
            <b>{{bindingSummary.single[kind]?.target_name || '未生效'}}</b>
            <small>{{bindingSummary.single[kind] ? scopeName(bindingSummary.single[kind]) : '没有匹配当前目标的绑定'}}</small>
          </div>
        </div>
        <div class="grid">
          <div class="binding-stack">
            <h4>叠加世界书</h4>
            <div class="activation" v-for="i in bindingSummary.additive.lorebook"><b>{{i.target_name}}</b><small> · {{scopeName(i)}}</small></div>
            <p v-if="!bindingSummary.additive.lorebook.length" class="muted">当前目标没有叠加世界书。</p>
          </div>
          <div class="binding-stack">
            <h4>叠加创作素材</h4>
            <div class="activation" v-for="i in bindingSummary.additive.material"><b>{{i.target_name}}</b><small> · {{scopeName(i)}}</small></div>
            <p v-if="!bindingSummary.additive.material.length" class="muted">当前目标没有叠加创作素材。</p>
          </div>
        </div>
      </div>
      <div class="panel">
        <h3>新增绑定</h3>
        <p>角色、角色组、预设、用户设定按“会话 → 战役 → 规则集 → 世界 → Persona → 全局”覆盖；世界书和创作素材会按作用域叠加。</p>
        <div class="binding-form">
          <label>资料类型<select v-model="binding.kind"><option v-for="(v,k) in labels" :value="k">{{v}}</option></select></label>
          <label>资料<select v-model="binding.target_id"><option value="">请选择</option><option v-for="d in bindDocs" :value="d.id">{{d.name}}</option></select></label>
          <label>范围<select v-model="binding.scope_type" @change="updateScope"><option value="session">具体会话</option><option value="campaign">战役</option><option value="world">世界</option><option value="ruleset">规则集</option><option value="persona">AstrBot Persona</option><option value="global">全局</option></select></label>
          <label v-if="binding.scope_type==='session'" class="combo">会话
            <input v-model="sessionDisplay" @focus="onSFocus" @blur="onSBlur" placeholder="搜索或输入会话 ID">
            <div class="combo-panel" v-if="sOpen">
              <div v-for="c in sFiltered" class="combo-option" @mousedown.prevent="pickSession(c)">{{c.title}} · {{c.platform}}</div>
              <div v-if="!sFiltered.length" class="combo-option muted">无匹配，可直接输入 ID 绑定</div>
            </div>
          </label>
          <label v-if="binding.scope_type==='persona'">Persona<select v-model="binding.scope_id"><option value="">请选择</option><option v-for="p in personas" :value="p.id">{{p.name}}</option></select></label>
          <label v-if="binding.scope_type==='campaign'">战役<select v-model="binding.scope_id"><option value="">请选择</option><option v-for="c in campaigns" :value="c.id">{{c.name}}</option></select></label>
          <label v-if="binding.scope_type==='world'">世界标识<input v-model="binding.scope_id" placeholder="与战役 world_id 一致"></label>
          <label v-if="binding.scope_type==='ruleset'">规则集标识<input v-model="binding.scope_id" placeholder="与战役 ruleset_id 一致"></label>
          <button class="primary" @click="addBinding">确认绑定</button>
        </div>
      </div>
      <div class="panel">
        <h3>全部绑定记录</h3>
        <table><tr><th>范围</th><th>类型</th><th>资料</th><th></th></tr>
          <tr v-for="i in bindings"><td>{{scopeName(i)}}</td><td>{{labels[i.kind]||i.kind}}</td><td>{{i.target_name}}</td><td><button class="danger" @click="unbind(i)">移除</button></td></tr>
        </table>
      </div>
    </section>
    <section v-else-if="tab==='campaigns'" class="split">
      <aside class="list panel">
        <div class="result-head"><h3>战役</h3><button @click="resetCampaignDraft">新建</button></div>
        <button v-for="item in campaigns" class="list-item" :class="{active:campaignDraft.id===item.id}" @click="editCampaign(item)">
          <b>{{item.name}}</b><small>{{item.world_id || '独立世界'}} · {{item.session_ids?.length || 0}} 个会话</small>
        </button>
        <p v-if="!campaigns.length" class="muted">创建第一局战役后，长期记忆和状态便不再依赖某个聊天窗口。</p>
      </aside>
      <article class="stack">
        <div class="panel">
          <div class="result-head"><div><h3>{{campaignDraft.id ? '编辑战役' : '新建战役'}}</h3><p class="muted">世界、规则、存档状态和聊天会话彼此独立组合。</p></div><div class="actions"><button class="primary" @click="saveCampaign">保存</button><button v-if="campaignDraft.id" class="danger" @click="deleteCampaign(campaignDraft)">{{pendingDeleteId===campaignDraft.id?'确认删除':'删除'}}</button></div></div>
          <div class="grid">
            <label>战役名称<input v-model="campaignDraft.name"></label>
            <label>世界标识<input v-model="campaignDraft.world_id" placeholder="例如 zombie-city"></label>
            <label>规则集标识<input v-model="campaignDraft.ruleset_id" placeholder="例如 survival-lite"></label>
          </div>
          <label>战役简介<textarea v-model="campaignDraft.description" placeholder="只写本局稳定前提，不写流水剧情"></textarea></label>
          <label>规则提示<textarea v-model="campaignDraft.rule_prompt" placeholder="例如资源守恒、不可替玩家决定行动、伤势恢复规则"></textarea></label>
          <div class="grid">
            <label>状态提取间隔（轮）<input type="number" min="1" v-model.number="campaignDraft.settings.state_extract_interval"></label>
            <label>状态应用方式<select v-model="campaignDraft.settings.state_apply_mode"><option value="pending">待确认（推荐）</option><option value="auto">自动应用</option></select></label>
            <label class="check-field"><input type="checkbox" v-model="campaignDraft.settings.state_tracking_enabled">启用 LLM 状态提议</label>
          </div>
          <div class="grid">
            <label>状态字段说明 JSON<textarea class="json" v-model="campaignSchemaJson"></textarea></label>
            <label>当前权威状态 JSON<textarea class="json" v-model="campaignStateJson"></textarea></label>
          </div>
        </div>
        <div class="panel" v-if="campaignDraft.id">
          <h3>绑定聊天会话</h3>
          <div class="inline"><label>会话 ID<input v-model="campaignSessionId" placeholder="default:GroupMessage:..."></label><button @click="bindCampaignSession">绑定 / 移动到本战役</button></div>
          <div class="activation" v-for="sid in campaignDraft.session_ids"><b>{{sid}}</b><button class="danger" @click="unbindCampaignSession(sid)">解绑</button></div>
        </div>
        <div class="panel" v-if="campaignDraft.id">
          <div class="result-head"><div><h3>状态变更审核</h3><p class="muted">LLM 只提出补丁；确认后才改变权威状态。</p></div><span>{{campaignChanges.filter(x=>x.status==='pending').length}} 条待处理</span></div>
          <div class="memory-card" v-for="change in campaignChanges">
            <div class="result-head"><b>第 {{change.source_turn}} 轮 · {{change.status}}</b><small>{{formatTimestamp(change.created_at)}}</small></div>
            <p>{{change.reason || '无说明'}}</p><pre>{{JSON.stringify(change.patch,null,2)}}</pre>
            <div class="actions" v-if="change.status==='pending'"><button class="primary" @click="resolveCampaignChange(change,'apply')">应用</button><button @click="resolveCampaignChange(change,'reject')">拒绝</button></div>
          </div>
          <p v-if="!campaignChanges.length" class="muted">尚无状态候选。绑定会话并完成一轮 RP 后会在这里出现。</p>
        </div>
      </article>
    </section>
    <section v-else-if="tab==='memories'" class="stack">
      <div class="panel">
        <h3>{{memoryDraft.id?'编辑长期记忆':'新增长期记忆'}}</h3>
        <p>自动提取的记忆会出现在这里。只有 active 且启用的记忆会注入 Prompt；pending 记忆需要确认后才会参与检索。</p>
        <div class="binding-form">
          <label>作用域<select v-model="memoryDraft.scope_type" @change="updateMemoryScope"><option value="campaign">战役</option><option value="conversation">AstrBot 对话</option><option value="world">世界</option><option value="ruleset">规则集</option><option value="session">会话</option><option value="user">用户</option><option value="group">群组</option><option value="persona">Persona</option><option value="global">全局</option></select></label>
          <label>作用域 ID<input v-model="memoryDraft.scope_id" placeholder="会话 ID / 用户 ID / *"></label>
          <label>分类<select v-model="memoryDraft.category"><option value="preference">用户偏好</option><option value="relationship">角色关系</option><option value="plot">剧情节点</option><option value="status">长期状态</option></select></label>
          <label>状态<select v-model="memoryDraft.status"><option value="active">active</option><option value="pending">pending</option><option value="archived">archived</option><option value="rejected">rejected</option></select></label>
          <label>重要度<input type="number" min="0" step="0.1" v-model.number="memoryDraft.importance"></label>
          <label>来源<input v-model="memoryDraft.source_type" placeholder="manual / auto_extract"></label>
          <label class="check-field"><input type="checkbox" v-model="memoryDraft.enabled">启用</label>
        </div>
        <label>记忆内容<textarea v-model="memoryDraft.content" placeholder="一条具体、可长期复用的事实。"></textarea></label>
        <div class="actions"><button class="primary" @click="saveMemory">保存记忆</button><button @click="resetMemoryDraft">清空表单</button></div>
      </div>
      <div class="panel">
        <div class="result-head">
          <h3>长期记忆列表</h3>
          <div class="actions">
            <select v-model="memoryStatusFilter"><option value="">全部状态</option><option value="pending">pending</option><option value="active">active</option><option value="archived">archived</option><option value="rejected">rejected</option></select>
            <input v-model="memoryQuery" placeholder="搜索内容、分类或作用域">
          </div>
        </div>
        <div class="actions"><button @click="toggleAllVisibleMemories">{{allVisibleMemoriesSelected?'取消全选':'全选当前列表'}}</button><button @click="updateSelectedMemoryStatus('active')" :disabled="!selectedMemoryIds.length">确认为 active</button><button @click="updateSelectedMemoryStatus('rejected')" :disabled="!selectedMemoryIds.length">拒绝</button><button @click="updateSelectedMemoryStatus('archived')" :disabled="!selectedMemoryIds.length">归档</button><span class="muted">已选择 {{selectedMemoryIds.length}} 条</span></div>
        <div v-for="group in memoryGroups" class="memory-group">
          <h4>{{group.title}} <span>{{group.items.length}} 条</span></h4>
          <div class="memory-card" v-for="m in group.items" :class="{inactive:!m.enabled || ['archived','rejected'].includes(m.status)}">
            <label class="memory-check"><input type="checkbox" :value="m.id" v-model="selectedMemoryIds"></label>
            <div>
              <div class="memory-meta">
                <span>{{m.status || (m.enabled?'active':'archived')}}</span>
                <span>{{m.category}}</span>
                <span>重要度 {{Number(m.importance || 1).toFixed(1)}}</span>
                <span>{{m.source_type || 'manual'}}</span>
              </div>
              <p>{{m.content}}</p>
              <small>{{m.scope_type}}: {{m.scope_id}} · {{formatTimestamp(m.updated_at)}}</small>
            </div>
            <div class="actions"><button @click="editMemory(m)">编辑</button><button @click="toggleMemory(m)">{{m.enabled?'禁用':'启用'}}</button><button class="danger" @click="deleteMemory(m)">{{pendingMemoryDeleteId===m.id?'确认删除':'删除'}}</button></div>
          </div>
        </div>
        <p v-if="!filteredMemories.length" class="muted">还没有长期记忆。</p>
      </div>
    </section>
    <section v-else-if="tab==='metrics'" class="stack">
      <div class="panel">
        <div class="result-head">
          <h3>运行仪表盘</h3>
          <div class="actions"><label>时间范围<select v-model.number="metricDays"><option :value="1">1 天</option><option :value="7">7 天</option><option :value="30">30 天</option></select></label><button @click="refreshMetrics">刷新</button></div>
        </div>
        <div class="cards">
          <div class="metric"><strong>{{metricTotals.requests||0}}</strong><span>请求数</span></div>
          <div class="metric"><strong>{{metricTotals.prompt_tokens||0}}</strong><span>估算 Token</span></div>
          <div class="metric"><strong>{{metricTotals.avg_duration_ms||0}}ms</strong><span>平均耗时</span></div>
          <div class="metric"><strong>{{metricTotals.worldbook_hits||0}}</strong><span>世界书命中</span></div>
          <div class="metric"><strong>{{metricTotals.memory_hits||0}}</strong><span>记忆命中</span></div>
          <div class="metric"><strong>{{metricTotals.summary_generated||0}} / {{metricTotals.summary_failed||0}}</strong><span>摘要生成 / 失败</span></div>
        </div>
      </div>
      <div class="grid">
        <div class="panel">
          <h3>Provider 调用量</h3>
          <div v-for="p in metricProviders" class="bar-row"><span>{{p[0]}}</span><b>{{p[1]}}</b></div>
          <p v-if="!metricProviders.length" class="muted">暂无 Provider 数据。</p>
        </div>
        <div class="panel">
          <h3>Token 趋势</h3>
          <div v-for="m in metricItems.slice(-30)" class="spark-row">
            <span>{{formatTimestamp(m.created_at)}}</span>
            <div class="spark"><i :style="{width: Math.max(4, Number(m.prompt_tokens||0) / maxMetricTokens * 100) + '%'}"></i></div>
            <b>{{m.prompt_tokens}}</b>
          </div>
          <p v-if="!metricItems.length" class="muted">暂无运行指标。真实请求后会自动记录；只读模拟不会写入。</p>
        </div>
      </div>
      <div class="panel">
        <h3>最近请求</h3>
        <table><tr><th>时间</th><th>会话</th><th>Provider</th><th>模式</th><th>Token</th><th>耗时</th><th>世界书</th><th>记忆</th><th>警告</th></tr>
          <tr v-for="m in metricItems.slice().reverse().slice(0,50)"><td>{{formatTimestamp(m.created_at)}}</td><td>{{m.session_id}}</td><td>{{m.provider_id||'unknown'}}</td><td>{{m.mode}}</td><td>{{m.prompt_tokens}}</td><td>{{m.duration_ms}}ms</td><td>{{m.worldbook_hits}}</td><td>{{m.memory_hits}}</td><td>{{m.warning_count}}</td></tr>
        </table>
      </div>
    </section>
    <section v-else-if="tab==='archive'" class="workspace">
      <div class="library">
        <div class="library-actions">
          <button class="primary" @click="refreshArchive">刷新分支树</button>
        </div>
        <label>会话
          <select v-model="archive.session_id" @change="refreshArchive">
            <option value="">全部会话</option>
            <option v-for="session in sessionOptions" :value="session.id">{{session.title}} · {{session.platform}}</option>
          </select>
        </label>
        <details v-for="segment in archiveSegments" :key="segment.segment" class="archive-segment" :open="segment.current">
          <summary>
            <span>{{segment.current ? '当前会话' : '已归档'}}</span>
            <small>{{segment.root?.title || '未命名会话段'}} · {{segment.nodes.length}} 个节点</small>
          </summary>
          <button v-for="n in segment.nodes" :key="n.id" :class="['doc','archive-node',{active:archive.selected?.id===n.id}]" @click="selectArchiveNode(n)" :style="{paddingLeft: (16 + Math.min(n.depth, 5) * 18) + 'px'}">
            <b>{{n.title||'（无标题）'}}</b>
            <small>轮次 {{n.turn_index}} · {{n.branch_name||'主线'}} · {{formatTimestamp(n.created_at)}}</small>
            <small>{{n.id}}</small>
          </button>
        </details>
        <p v-if="!archive.nodes.length" class="muted">还没有归档节点。真实请求完成后会自动保存。</p>
      </div>
      <article v-if="archive.selected" class="editor">
        <div class="editor-title">
          <input class="title-input" v-model="archive.selected.title" placeholder="节点标题">
          <button class="primary" @click="renameArchiveNode">保存名称</button>
        </div>
        <div class="grid">
          <label>分支名<input v-model="archive.selected.branch_name" placeholder="主线 / if线 / 存档名"></label>
          <label>从此继续的新分支名<input v-model="archive.branch_name" placeholder="留空继承主线"></label>
        </div>
        <div class="actions"><button @click="branchFromArchiveNode">从此节点继续</button><button @click="saveJson(archive.selected,'story-node-'+archive.selected.id+'.json')">导出该节点 JSON</button></div>
        <div class="summary-grid archive-summary">
          <div class="summary-card ready"><span>分支</span><b>{{archive.selected.branch_name||'主线'}}</b></div>
          <div class="summary-card ready"><span>轮次</span><b>{{archive.selected.turn_index}}</b></div>
          <div class="summary-card ready"><span>消息数</span><b>{{archive.selected.message_count}}</b></div>
          <div class="summary-card"><span>父节点</span><b>{{archive.selected.parent_id||'无'}}</b></div>
          <div class="summary-card"><span>会话</span><b>{{archive.selected.session_id}}</b></div>
          <div class="summary-card"><span>创建时间</span><b>{{formatTimestamp(archive.selected.created_at)}}</b></div>
        </div>
        <details open><summary>Assistant 回复</summary><pre>{{archive.selected.assistant_text||'（空）'}}</pre></details>
        <details open><summary>请求 messages[]（{{archive.selected.request_messages?.length||0}}）</summary>
          <div class="message" v-for="(m,i) in archive.selected.request_messages"><b>{{i}} · {{m.role}}</b><pre>{{m.content}}</pre></div>
        </details>
        <details><summary>绑定快照</summary><pre>{{JSON.stringify(archive.selected.bindings_snapshot,null,2)}}</pre></details>
        <details><summary>检索与记忆命中</summary><pre>{{JSON.stringify({retrieval:archive.selected.retrieval_snapshot,memory:archive.selected.memory_snapshot},null,2)}}</pre></details>
        <details><summary>会话状态快照</summary><pre>{{JSON.stringify(archive.selected.state_snapshot,null,2)}}</pre></details>
      </article>
      <article v-else class="empty"><h3>选择一个节点查看快照</h3><p>这里保存真实请求的 prompt、messages、绑定、检索命中和最终回复。</p></article>
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
        <div class="result-head">
          <h3>本轮结论</h3>
          <span>先看结论，再展开细节</span>
        </div>
        <div class="summary-grid">
          <div v-for="item in debugSummary" :class="['summary-card', item.state]">
            <span>{{item.label}}</span>
            <b>{{item.value}}</b>
          </div>
        </div>
        <div class="alert error" v-for="w in debugResult.warnings">{{w}}</div>
        <div class="result-head compact">
          <h3>最终 messages[]</h3>
          <span>Token 为近似估算</span>
        </div>
        <div class="effective" v-if="debugResult.effective">
          <b>当前有效配置</b>
          <span>预设：{{debugResult.effective.single?.preset?.name||'无'}}</span>
          <span>角色：{{debugResult.effective.single?.character?.name||'无'}}</span>
          <span>角色组：{{debugResult.effective.single?.character_group?.name||'无'}}</span>
          <span>本轮实际角色：{{debugResult.character_selection?.character?.card_name||debugResult.character_selection?.character?.name||'无'}}</span>
          <span>选择原因：{{debugResult.character_selection?.reason||'无'}}</span>
          <span>世界书：{{debugResult.effective.additive?.lorebook?.map(x=>x.name).join('、')||'无'}}</span>
        </div>
        <details v-if="debugResult.character_selection"><summary>角色组切换状态</summary>
          <div class="effective">
            <span>绑定组：{{debugResult.character_selection.group?.name||'无'}}</span>
            <span>策略：{{debugResult.character_selection.group?.selection||'无'}}</span>
            <span>当前下标：{{debugResult.character_selection.index ?? '无'}}</span>
            <span>下轮下标：{{debugResult.character_selection.next_index ?? '无'}}</span>
            <span>手动锁定：{{debugResult.character_selection.forced?'是':'否'}}</span>
          </div>
          <div class="activation" v-for="m in debugResult.character_selection.members"><b>{{m.card_name||m.name}}</b><small> · {{m.id}}</small></div>
        </details>
        <details v-if="debugResult.summary" open><summary>自动摘要状态</summary>
          <div class="effective">
            <span>状态：{{debugResult.summary.enabled?'已启用':'未启用'}}</span>
            <span>来源：{{debugResult.summary.source}}</span>
            <span>已覆盖：{{debugResult.summary.covered_messages||0}} 条</span>
            <span>待处理：{{debugResult.summary.pending_messages||0}} 条</span>
            <span>本轮生成：{{debugResult.summary.generated_this_request?'是':'否'}}</span>
            <span>将触发：{{debugResult.summary.would_generate?'是':'否'}}</span>
            <span>Provider：{{debugResult.summary.provider_id||'未指定'}}</span>
            <span>已注入：{{debugResult.summary.included?'是':'否'}}</span>
            <span>更新时间：{{formatTimestamp(debugResult.summary.updated_at)}}</span>
          </div>
          <pre v-if="debugResult.summary.content">{{debugResult.summary.content}}</pre>
          <div class="alert error" v-if="debugResult.summary.error">{{debugResult.summary.error}}</div>
        </details>
        <details v-if="debugResult.retrieval" open><summary>混合检索状态</summary>
          <div class="effective">
            <span>状态：{{debugResult.retrieval.enabled?'已启用':'未启用'}}</span>
            <span>模式：{{debugResult.retrieval.mode}}</span>
            <span>FTS 可用：{{debugResult.retrieval.fts_available?'是':'否'}}</span>
            <span>候选召回：{{debugResult.retrieval.candidate_count}}</span>
            <span>最终上限：{{debugResult.retrieval.top_k}}</span>
            <span>命中条目：{{debugResult.retrieval.matches?.length||0}}</span>
          </div>
          <div v-if="debugResult.retrieval.matches?.length" class="activation" v-for="m in debugResult.retrieval.matches">
            <b>{{m.name||m.uid}}</b> · {{m.reason}}<span v-if="m.scanner_reason"> / {{m.scanner_reason}}</span> · 分数 {{m.score?.toFixed(3)}}
          </div>
        </details>
        <details v-if="debugResult.memory" open><summary>长期记忆注入</summary>
          <div class="effective">
            <span>状态：{{debugResult.memory.enabled?'已启用':'未启用'}}</span>
            <span>作用域：{{debugResult.memory.scopes?.map(x=>x.join(':')).join('、')||'无'}}</span>
            <span>注入条数：{{debugResult.memory.injected_count||0}}</span>
          </div>
          <pre v-if="debugResult.memory.query">{{debugResult.memory.query}}</pre>
          <div v-if="debugResult.memory.matches?.length" class="activation" v-for="m in debugResult.memory.matches">
            <b>{{m.category||'memory'}}</b> · {{m.scope_type}}:{{m.scope_id}} · 分数 {{m.score?.toFixed(3)}} · 重要度 {{Number(m.importance||1).toFixed(1)}} · {{m.status||'active'}} · {{m.source_type||'manual'}}
            <pre>{{m.content}}</pre>
          </div>
          <p v-if="!debugResult.memory.matches?.length" class="muted">本轮没有注入长期记忆。</p>
        </details>
        <details open><summary>检索测试与统计</summary>
          <div class="binding-form">
            <label>测试文本<textarea v-model="retrievalTest.text" placeholder="输入一段用户消息，测试 keyword/vector/hybrid 召回"></textarea></label>
            <button class="primary" @click="runRetrievalTest" :disabled="busy">测试检索</button>
            <button @click="refreshRetrievalStats">刷新统计</button>
          </div>
          <div class="effective" v-if="retrievalStats">
            <span>模式：{{retrievalStats.mode}}</span>
            <span>向量：{{retrievalStats.vector_enabled?'启用':'禁用'}}</span>
            <span>FTS：{{retrievalStats.fts_available?'可用':'不可用'}}</span>
            <span>总日志：{{retrievalStats.total_retrieval_logs||0}}</span>
            <span>当前会话日志：{{retrievalStats.session_retrieval_logs||0}}</span>
            <span>命中过的条目：{{retrievalStats.entries_with_matches||0}}</span>
          </div>
          <div v-if="retrievalStats?.top_matched_entries?.length">
            <h4>高频命中</h4>
            <div class="activation" v-for="m in retrievalStats.top_matched_entries"><b>{{m.name||'未命名'}}</b> · {{m.match_count}} 次</div>
          </div>
          <div v-if="retrievalResult">
            <h4>测试结果：{{retrievalResult.matches?.length||0}} 条</h4>
            <div class="effective"><span>模式：{{retrievalResult.mode}}</span><span>FTS：{{retrievalResult.fts_available?'可用':'不可用'}}</span><span>候选：{{retrievalResult.candidate_count}}</span><span>上限：{{retrievalResult.top_k}}</span></div>
            <div class="activation" v-for="m in retrievalResult.matches">
              <b>{{m.name||m.uid}}</b> · {{m.score?.toFixed(3)}} · {{m.category||'未分类'}}<small v-if="m.document_name"> · {{m.document_name}}</small>
              <pre>{{m.content}}</pre>
            </div>
          </div>
        </details>
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
      <h3>快捷回复</h3>
      <p>快捷回复是保存好的 AI 提示词模板。插件首次运行时会创建并全局绑定“默认快捷回复”，包含继续剧情、丰富描写、代写回复、润色重写、剧情总结和严格保持角色。</p>
      <ol>
        <li>进入“快捷回复”页面编辑模板，填写名称、命令别名、生成模式和提示词。</li>
        <li>新建的快捷回复集需要前往“绑定管理”，绑定到全局、Persona 或具体会话；默认套装已经全局绑定。</li>
        <li>发送 <code>/tavern qr list</code> 查看可用项。</li>
        <li>发送 <code>/tavern qr &lt;编号/别名/名称&gt; [补充文本]</code> 触发生成，例如 <code>/tavern qr detail 更侧重环境气氛</code>。</li>
      </ol>
      <p><b>普通生成</b>正常回复；<b>继续生成</b>续写上一条助手消息；<b>代写用户回复</b>替你起草下一句话；<b>静默提示词</b>把模板作为本轮额外约束。</p>
      <h3>命令</h3>
      <pre>/tavern status
/tavern preview
/tavern reset
/tavern continue [补充要求]
/tavern impersonate [补充要求]
/tavern quiet [静默提示词]
/tavern qr list
/tavern qr &lt;编号/别名/名称&gt; [补充文本]
/tavern character status
/tavern character next
/tavern character use <角色名>
/tavern retrieval test <文本>
/tavern retrieval stats</pre>
    </section>
  </main>
</div>`
}).mount('#app')
