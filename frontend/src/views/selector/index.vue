<template>
  <section class="page" data-module="selector">
    <header class="page-head">
      <div>
        <h2>抽取规则管理</h2>
        <p class="page-desc">管理各站点的 CSS/XPath 选择器和分页策略，保证抽取结果字段完整。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记选择器规则</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出抽取规则清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无抽取规则数据，可先登记选择器规则</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记选择器规则</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section class="binding-panel" data-testid="binding-panel">
      <header class="binding-head">
        <strong>站点选择器绑定记录台账</strong>
        <span class="binding-meta">由目标站点筛选数据快照生成，绑定范围不超出快照站点。</span>
        <button class="link" data-testid="binding-refresh" type="button" @click="loadBindings">刷新绑定台账</button>
      </header>
      <table class="data-table">
        <thead>
          <tr><th>绑定编号</th><th>快照编号</th><th>站点主键</th><th>站点编号</th><th>规则编号</th><th>台账状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in bindings" :key="String(item.id)" :data-testid="`binding-${item.id}`" :data-status="String(item.台账状态)">
            <td>{{ item.绑定编号 }}</td>
            <td>{{ item.快照编号 }}</td>
            <td>{{ item.站点主键 }}</td>
            <td>{{ item.站点编号 ?? '—' }}</td>
            <td>{{ item.规则编号 ?? '未匹配' }}</td>
            <td>{{ item.台账状态 }}</td>
          </tr>
          <tr v-if="!bindings.length">
            <td colspan="6" class="empty-state">尚无绑定台账，可先在采集目标页按条件筛选并快照采集目标</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条抽取规则记录</span>
      <span v-if="errorMessage" class="error-text" data-testid="error-message">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Binding = Record<string, string | number | null>

const ENDPOINT = '/api/selector'
const columns = ["规则编号", "归属站点", "列表选择器", "详情选择器", "字段选择器", "分页参数", "等待条件", "字符编码"]
const actions = ["提交校验", "启用配置", "停用配置"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const metricNames = ["选择器规则总数", "待校验选择器规则", "已启用选择器规则"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const bindings = ref<Binding[]>([])
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'selector.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('抽取规则动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '抽取规则操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('选择器规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '抽取规则列表读取失败'
  }
}

async function loadBindings() {
  try {
    const response = await request(`${ENDPOINT}/bindings?size=100`)
    if (!response.ok) return
    const payload = await response.json()
    bindings.value = payload.items ?? []
  } catch {
    bindings.value = []
  }
}

onMounted(async () => {
  await Promise.all([reload(), loadBindings()])
})
</script>

<style scoped>
.binding-panel { margin-top: 16px; }
.binding-head { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 8px; }
.binding-meta { color: #475569; font-size: 12px; }
</style>
