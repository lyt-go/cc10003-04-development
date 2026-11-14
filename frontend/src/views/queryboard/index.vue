<template>
  <section class="page" data-module="queryboard">
    <header class="page-head">
      <div>
        <h2>聚合查询管理</h2>
        <p class="page-desc">定义预置的聚合查询模板；提交模板验证时固化维度、度量与允许的下钻层级，生成可复用的聚合结果摘要。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="toggleCreate">登记查询模板</button>
        <button class="btn" type="button" @click="exportRows">导出聚合查询清单</button>
        <button class="btn" type="button" @click="loadAudit">刷新查询影响记录</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form v-if="showCreate" class="panel" @submit.prevent="createTemplate">
      <div class="panel-head">
        <h3>登记查询模板</h3>
        <span class="panel-hint">维度按「日期,渠道」这样的顺序填写；顺序即下钻层级顺序。</span>
      </div>
      <div class="form-grid">
        <label v-for="field in createFields" :key="field">
          <span>{{ field }}</span>
          <input v-model="createForm[field]" :placeholder="placeholderFor(field)" />
        </label>
      </div>
      <div class="panel-head">
        <span class="panel-hint">业务归属默认为当前值班人；勾选共享后仅管理员可修改。</span>
        <label class="panel-hint">
          <input v-model="createForm.shared" type="checkbox" /> 共享模板
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
        <span>模板编号/指标</span>
        <input v-model="filters.keyword" placeholder="按模板编号或指标名称检索" />
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
          <th>快照边界</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ display(row, column) }}</td>
          <td>
            <div class="mono" v-if="row['当前快照版本']">
              {{ row['版本标识'] }} · 层级 {{ row['允许的下钻层级'] }} ·
              {{ row['可下钻'] ? '可下钻' : '未启用' }}
            </div>
            <div class="mono" v-else>尚未固化快照</div>
            <div class="mono" v-if="row['待启用快照版本']">待启用 v{{ row['待启用快照版本'] }}</div>
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
            <button class="link" type="button" @click="openSnapshot(row)">查看快照</button>
            <button class="link" type="button" @click="openVersions(row)">版本历史</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无聚合查询数据，可先登记查询模板</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条聚合查询记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section v-if="snapshot" class="panel">
      <div class="panel-head">
        <h3>模板快照 · {{ snapshot['版本标识'] }}</h3>
        <button class="btn ghost" type="button" @click="snapshot = null">收起</button>
      </div>
      <div class="panel-hint">
        维度字段：{{ (snapshot['维度字段'] || []).join(' → ') }} ·
        度量字段：{{ (snapshot['度量字段'] || []).join('、') }} ·
        允许的下钻层级：{{ snapshot['允许的下钻层级'] }} ·
        摘要：<span class="mono">{{ snapshot['摘要校验值'] }}</span>
      </div>
      <div v-for="level in snapshot['层级摘要'] || []" :key="level['层级']" class="level-card">
        <div class="level-card-head">
          <span>第 {{ level['层级'] }} 层 · 维度 {{ level['维度字段'] }} · 度量 {{ level['度量字段'] }}</span>
          <span class="level-meta">{{ level['行数'] }} 行 · 汇总 {{ level['汇总'] }}</span>
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
    </section>

    <section v-if="versions.length" class="panel">
      <div class="panel-head">
        <h3>快照版本历史</h3>
        <button class="btn ghost" type="button" @click="versions = []">收起</button>
      </div>
      <table class="data-table">
        <thead>
          <tr><th>版本</th><th>状态</th><th>维度</th><th>度量</th><th>层级</th><th>摘要</th><th>生成时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="version in versions" :key="String(version['模板版本'])">
            <td>{{ version['版本标识'] }}</td>
            <td>{{ version['状态'] }}</td>
            <td>{{ (version['维度字段'] || []).join(' → ') }}</td>
            <td>{{ (version['度量字段'] || []).join('、') }}</td>
            <td>{{ version['允许的下钻层级'] }}</td>
            <td class="mono">{{ version['摘要校验值'] }}</td>
            <td>{{ version['生成时间'] }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h3>查询影响记录</h3>
        <span class="panel-hint">模板验证、启用、展开与拒绝都可核对，不进入运营概览。</span>
      </div>
      <table class="data-table">
        <thead>
          <tr><th>时间</th><th>动作</th><th>结果</th><th>模板</th><th>版本</th><th>路径</th><th>层级</th><th>操作人</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="record in auditRows" :key="String(record.id)">
            <td>{{ record['时间'] }}</td>
            <td>{{ record['动作'] }}</td>
            <td>{{ record['结果'] }}</td>
            <td>{{ record['模板编号'] ?? '—' }}</td>
            <td>{{ record['模板版本'] ?? '—' }}</td>
            <td>{{ record['路径编号'] ?? '—' }}</td>
            <td>{{ record['层级'] ?? '—' }}</td>
            <td>{{ record['操作人'] ?? '—' }}</td>
            <td>{{ record['说明'] }}</td>
          </tr>
          <tr v-if="!auditRows.length">
            <td colspan="9" class="empty-state">暂无查询影响记录</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { ApiError, postJson, readPayload, request } from '@/api/client'
import { useSessionStore } from '@/stores/session'

type Row = Record<string, any>

const ENDPOINT = '/api/queryboard'
const columns = ["模板编号", "指标名称", "维度字段", "度量字段", "排序规则", "分页上限", "缓存秒数", "数据源"]
const actions = ["提交模板验证", "启用模板", "废弃模板"]
const statuses = ["草稿", "模板验证中", "已启用", "已废弃"]
const createFields = ["模板编号", "指标名称", "维度字段", "度量字段", "排序规则", "分页上限", "缓存秒数", "数据源"]

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
const snapshot = ref<Row | null>(null)
const versions = ref<Row[]>([])
const auditRows = ref<Row[]>([])

const stats = computed(() => [
  { label: '查询模板总数', value: total.value },
  { label: '已启用模板', value: rows.value.filter((row) => row.status === '已启用').length },
  { label: '可下钻模板', value: rows.value.filter((row) => row['可下钻']).length },
])

function placeholderFor(field: string) {
  if (field === '维度字段') return '日期,渠道'
  if (field === '度量字段') return '成交额,订单数'
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

async function createTemplate() {
  errorMessage.value = ''
  message.value = ''
  try {
    const payload = await postJson<any>(ENDPOINT, {
      values: {
        ...createForm.value,
        业务归属: createForm.value['业务归属'] || session.operator,
        共享模板: createForm.value.shared,
      },
    })
    if (!payload.ok) {
      notify(payload.message, 'error')
      return
    }
    notify(payload.message, 'ok')
    showCreate.value = false
    await reload()
  } catch (error) {
    notify(error instanceof Error ? error.message : '登记查询模板失败', 'error')
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  message.value = ''
  try {
    const payload = await postJson<any>(`${ENDPOINT}/${row.id}/actions`, { action })
    if (!payload.ok) {
      notify(payload.message, 'error')
      return
    }
    notify(payload.message, 'ok')
    await reload()
    await loadAudit()
  } catch (error) {
    notify(error instanceof Error ? error.message : '聚合查询操作失败', 'error')
  }
}

async function openSnapshot(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/snapshot?include_results=true`)
    snapshot.value = await readPayload(response)
  } catch (error) {
    snapshot.value = null
    notify(error instanceof Error ? error.message : '读取模板快照失败', 'error')
  }
}

async function openVersions(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}/versions`)
    const payload = await readPayload(response)
    versions.value = payload.items ?? []
  } catch (error) {
    versions.value = []
    notify(error instanceof Error ? error.message : '读取版本历史失败', 'error')
  }
}

async function loadAudit() {
  try {
    const response = await request(`${ENDPOINT}/audit?size=30`)
    const payload = await readPayload(response)
    auditRows.value = payload.items ?? []
  } catch (error) {
    auditRows.value = []
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
    errorMessage.value = error instanceof Error ? error.message : '聚合查询列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadAudit()
})
</script>
