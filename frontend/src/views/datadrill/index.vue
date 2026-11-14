<template>
  <section class="page" data-module="datadrill">
    <header class="page-head">
      <div>
        <h2>多维下钻管理</h2>
        <p class="page-desc">下钻路径启用后只读取同模板版本的快照；按层级展开时每层显示实际维度、度量与聚合摘要。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="toggleCreate">登记下钻路径</button>
        <button class="btn" type="button" @click="exportRows">导出多维下钻清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form v-if="showCreate" class="panel" @submit.prevent="createPath">
      <div class="panel-head">
        <h3>登记下钻路径</h3>
        <span class="panel-hint">每层维度名顺序必须与关联模板当前快照的维度一致，中间层级不能为空。</span>
      </div>
      <div class="form-grid">
        <label v-for="field in createFields" :key="field">
          <span>{{ field }}</span>
          <input v-model="createForm[field]" :placeholder="placeholderFor(field)" />
        </label>
      </div>
      <div class="panel-head">
        <span class="panel-hint">业务归属默认为当前值班人；共享路径仅管理员可修改。</span>
        <label class="panel-hint">
          <input v-model="createForm.shared" type="checkbox" /> 共享路径
        </label>
      </div>
      <div class="row-actions">
        <button class="btn primary" type="submit" :disabled="!canOperate">提交登记</button>
        <button class="btn ghost" type="button" @click="showCreate = false">取消</button>
      </div>
    </form>

    <div v-if="message" class="notice" :class="messageLevel">{{ message }}</div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>路径编号/模板</span>
        <input v-model="filters.keyword" placeholder="按路径编号或关联模板检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>模板绑定</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ display(row, column) }}</td>
          <td>
            <div class="mono">{{ row['关联模板'] }} · v{{ row['关联模板版本'] || '—' }}</div>
            <div class="mono">{{ row['可展开'] ? '可展开' : '未绑定当前版本' }}</div>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :disabled="!canOperate"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" :disabled="!row['可展开']" @click="openDrill(row)">按层级展开</button>
            <button class="link" type="button" @click="openBindings(row)">历史结果</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无多维下钻数据，可先登记下钻路径</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条多维下钻记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section v-if="drill" class="panel">
      <div class="panel-head">
        <h3>层级展开 · {{ drill['路径']?.['路径编号'] }}</h3>
        <div class="row-actions">
          <button
            v-for="levelNo in drill['路径深度'] || 0"
            :key="levelNo"
            class="btn"
            type="button"
            @click="expand(drillRow!, levelNo)"
          >
            展开到第 {{ levelNo }} 层
          </button>
          <button class="btn ghost" type="button" @click="drill = null">收起</button>
        </div>
      </div>
      <div class="panel-hint">
        模板 {{ drill['模板编号'] }} · 绑定版本 v{{ drill['模板版本'] }} ·
        当前模板版本 v{{ drill['当前模板版本'] ?? '—' }} ·
        默认展开到第 {{ drill['默认展开层'] }} 层
      </div>

      <div class="level-chain">
        <span
          v-for="chip in levelChips"
          :key="chip.level"
          class="level-chip"
          :class="chip.state"
        >
          第 {{ chip.level }} 层 · {{ chip.dimension || '缺少维度' }}
        </span>
      </div>

      <div v-if="drill['ok'] === false" class="notice error">
        {{ drill['message'] }}
        <div v-if="(drill['保留上一层结果'] || []).length" class="panel-hint">
          已保留 {{ (drill['保留上一层结果'] || []).length }} 层历史结果，不会被本次失败清空。
        </div>
      </div>

      <div v-for="level in drill['层级结果'] || []" :key="level['层级']" class="level-card">
        <div class="level-card-head">
          <span>第 {{ level['层级'] }} 层 · 维度 {{ level['维度字段'] }} · 度量 {{ level['度量字段'] }}</span>
          <span class="level-meta">{{ level['行数'] }} 行 · 汇总 {{ level['汇总'] }} · {{ level['来源'] }}</span>
        </div>
        <div class="table-scroll">
          <table class="sub-table">
            <thead>
              <tr>
                <th v-for="dim in level['累计维度']" :key="dim">{{ dim }}</th>
                <th>{{ level['度量字段'] }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(item, index) in level['聚合结果'] || []" :key="index">
                <td v-for="dim in level['累计维度']" :key="dim">{{ item['维度'][dim] }}</td>
                <td>{{ item['聚合值'] }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="level-meta mono">层摘要 {{ level['摘要'] }}</div>
      </div>

      <template v-if="drill['ok'] === false">
        <div v-for="level in drill['保留上一层结果'] || []" :key="`keep-${level['层级']}`" class="level-card">
          <div class="level-card-head">
            <span>保留 · 第 {{ level['层级'] }} 层 · 维度 {{ level['维度字段'] }} · 度量 {{ level['度量字段'] }}</span>
            <span class="level-meta">{{ level['行数'] }} 行 · 汇总 {{ level['汇总'] }} · {{ level['来源'] }}</span>
          </div>
          <div class="table-scroll">
            <table class="sub-table">
              <thead>
                <tr>
                  <th v-for="dim in level['累计维度']" :key="dim">{{ dim }}</th>
                  <th>{{ level['度量字段'] }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(item, index) in level['聚合结果'] || []" :key="index">
                  <td v-for="dim in level['累计维度']" :key="dim">{{ item['维度'][dim] }}</td>
                  <td>{{ item['聚合值'] }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </template>
    </section>

    <section v-if="bindings.length" class="panel">
      <div class="panel-head">
        <h3>历史绑定与结果</h3>
        <button class="btn ghost" type="button" @click="bindings = []">收起</button>
      </div>
      <table class="data-table">
        <thead>
          <tr><th>模板版本</th><th>状态</th><th>层级维度</th><th>摘要</th><th>启用人</th><th>启用时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in bindings" :key="`${item['模板版本']}-${item['启用时间']}`">
            <td>{{ item['模板编号'] }} · v{{ item['模板版本'] }}</td>
            <td>{{ item['状态'] }}</td>
            <td>{{ (item['层级结构'] || []).map((level: Row) => level['维度字段']).join(' → ') }}</td>
            <td class="mono">{{ item['快照摘要'] }}</td>
            <td>{{ item['启用人'] }}</td>
            <td>{{ item['启用时间'] }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { postJson, readPayload, request } from '@/api/client'
import { useSessionStore } from '@/stores/session'

type Row = Record<string, any>

const ENDPOINT = '/api/datadrill'
const columns = ["路径编号", "起始维度", "下钻层级", "每层维度名", "每层度量", "路径深度", "默认展开层", "关联看板"]
const actions = ["提交路径评审", "启用路径", "关闭路径"]
const statuses = ["草稿", "路径评审中", "已启用", "已关闭"]
const createFields = ["路径编号", "关联模板", "起始维度", "每层维度名", "每层度量", "路径深度", "默认展开层", "关联看板"]

const session = useSessionStore()
const canOperate = computed(() => session.canOperate)

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const message = ref('')
const messageLevel = ref('ok')
const filters = ref<Record<string, string>>({ keyword: '', status: '' })
const showCreate = ref(false)
const createForm = ref<Record<string, any>>({ shared: false })
const drill = ref<Row | null>(null)
const drillRow = ref<Row | null>(null)
const bindings = ref<Row[]>([])

const stats = computed(() => [
  { label: '下钻路径总数', value: total.value },
  { label: '已启用路径', value: rows.value.filter((row) => row.status === '已启用').length },
  { label: '可展开路径', value: rows.value.filter((row) => row['可展开']).length },
])

const levelChips = computed(() => {
  if (!drill.value) return []
  const depth = Number(drill.value['路径深度'] || 0)
  const reached = new Map<number, string>()
  for (const level of drill.value['层级结果'] || []) {
    reached.set(Number(level['层级']), String(level['维度字段'] || ''))
  }
  for (const level of drill.value['保留上一层结果'] || []) {
    if (!reached.has(Number(level['层级']))) {
      reached.set(Number(level['层级']), String(level['维度字段'] || ''))
    }
  }
  const blocked = Number(drill.value['阻断层级'] || 0)
  return Array.from({ length: depth }, (_, index) => {
    const levelNo = index + 1
    if (levelNo === blocked) {
      return { level: levelNo, dimension: '缺少维度', state: 'blocked' }
    }
    return {
      level: levelNo,
      dimension: reached.get(levelNo) || '未展开',
      state: reached.has(levelNo) ? 'active' : '',
    }
  })
})

function placeholderFor(field: string) {
  if (field === '关联模板') return 'QUER-0002'
  if (field === '每层维度名') return '日期,渠道'
  if (field === '每层度量') return '支付金额,下单用户数'
  if (field === '路径深度') return '2'
  if (field === '默认展开层') return '2'
  return `填写${field}`
}

function display(row: Row, column: string) {
  const value = row[column]
  if (Array.isArray(value)) return value.join(',')
  return value ?? '—'
}

function notify(text: string, level: 'ok' | 'warn' | 'error' = 'ok') {
  message.value = text
  messageLevel.value = level
}

function resetFilters() {
  filters.value = { keyword: '', status: '' }
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function toggleCreate() {
  showCreate.value = !showCreate.value
  if (showCreate.value) {
    createForm.value = { shared: false, 业务归属: session.operator }
  }
}

async function createPath() {
  message.value = ''
  errorMessage.value = ''
  const values: Record<string, any> = {
    ...createForm.value,
    起始维度: createForm.value['起始维度'] || String(createForm.value['每层维度名'] || '').split(/[,，、]/)[0],
    下钻层级: createForm.value['每层维度名'],
    业务归属: createForm.value['业务归属'] || session.operator,
    共享模板: createForm.value.shared,
  }
  try {
    const payload = await postJson<any>(ENDPOINT, { values })
    if (!payload.ok) {
      notify(payload.message, 'error')
      return
    }
    notify(payload.message, 'ok')
    showCreate.value = false
    await reload()
  } catch (error) {
    notify(error instanceof Error ? error.message : '登记下钻路径失败', 'error')
  }
}

async function runAction(action: string, row: Row) {
  message.value = ''
  errorMessage.value = ''
  try {
    const payload = await postJson<any>(`${ENDPOINT}/${row.id}/actions`, { action })
    if (!payload.ok) {
      notify(payload.message, 'error')
      return
    }
    notify(payload.message, 'ok')
    await reload()
  } catch (error) {
    notify(error instanceof Error ? error.message : '多维下钻操作失败', 'error')
  }
}

async function openDrill(row: Row) {
  drillRow.value = row
  await expand(row, row['默认展开层'] || 1)
}

async function expand(row: Row, level: number) {
  drillRow.value = row
  message.value = ''
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/levels?level=${level}`)
    const payload = await readPayload(response)
    drill.value = payload
    if (payload['ok'] === false) {
      notify(payload['message'], 'warn')
    }
  } catch (error: any) {
    // 409：模板换版/层级冲突/缓存不一致，后端把结构化原因放在 detail 里。
    const payload = error?.payload
    if (payload && typeof payload === 'object') {
      drill.value = payload
      notify(payload['message'] || error.message, 'warn')
    } else {
      notify(error instanceof Error ? error.message : '下钻展开失败', 'error')
    }
  }
}

async function openBindings(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}/bindings`)
    const payload = await readPayload(response)
    bindings.value = payload.items ?? []
  } catch (error) {
    bindings.value = []
    notify(error instanceof Error ? error.message : '读取历史绑定失败', 'error')
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (filters.value.keyword) query.set('keyword', filters.value.keyword)
  if (filters.value.status) query.set('status', filters.value.status)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    const payload = await readPayload(response)
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '多维下钻列表读取失败'
  }
}

onMounted(reload)
</script>
