<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import * as echarts from 'echarts'

const question = ref('')
const answer = ref('')
const evidence = ref([])
const charts = ref([])
const sessionId = ref(null)
const runId = ref(null)
const loading = ref(false)
const error = ref('')
const chartElements = ref([])
const chartInstances = []
const resizeHandlers = []

function setChartElement(element, index) {
  if (element) chartElements.value[index] = element
}

function formatDetail(detail) {
  if (typeof detail === 'string') return detail
  return JSON.stringify(detail, null, 2)
}

async function renderCharts() {
  await nextTick()
  chartInstances.splice(0).forEach((instance) => instance.dispose())
  resizeHandlers.splice(0).forEach((handler) => window.removeEventListener('resize', handler))
  charts.value.forEach((option, index) => {
    const element = chartElements.value[index]
    if (!element) return
    const instance = echarts.init(element)
    instance.setOption(option)
    const handler = () => instance.resize()
    window.addEventListener('resize', handler)
    chartInstances.push(instance)
    resizeHandlers.push(handler)
  })
}

watch(charts, renderCharts, { deep: true })

async function ask() {
  const text = question.value.trim()
  if (!text || loading.value) return
  loading.value = true
  error.value = ''
  try {
    const response = await fetch('/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: text, session_id: sessionId.value }),
    })
    const data = await response.json()
    if (!response.ok || data.status !== 'ok') {
      throw new Error(data.reason || `请求失败 (${response.status})`)
    }
    answer.value = data.answer || ''
    evidence.value = data.evidence || []
    charts.value = data.charts || []
    sessionId.value = data.session_id || sessionId.value
    runId.value = data.run_id || null
    question.value = ''
  } catch (err) {
    error.value = err.message || '请求失败，请稍后重试'
  } finally {
    loading.value = false
  }
}

onBeforeUnmount(() => {
  chartInstances.splice(0).forEach((instance) => instance.dispose())
  resizeHandlers.splice(0).forEach((handler) => window.removeEventListener('resize', handler))
})
</script>

<template>
  <main class="page-shell">
    <header class="hero">
      <p class="eyebrow">ECOMMERCE ANALYTICS</p>
      <h1>电商经营分析助手</h1>
      <p class="subtitle">问数、归因与趋势，一次对话完成。</p>
    </header>

    <section class="ask-card" aria-label="提问">
      <textarea
        v-model="question"
        rows="3"
        placeholder="例如：近7天商品支付金额是多少？"
        :disabled="loading"
        @keydown.ctrl.enter="ask"
        @keydown.meta.enter="ask"
      />
      <div class="ask-actions">
        <span class="hint">Ctrl / ⌘ + Enter 发送</span>
        <button :disabled="loading || !question.trim()" @click="ask">
          <span v-if="loading" class="spinner" aria-hidden="true" />
          {{ loading ? '分析中…' : '发送问题' }}
        </button>
      </div>
    </section>

    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <section v-if="answer" class="result-card" aria-live="polite">
      <div class="section-heading">
        <h2>结论</h2>
        <span v-if="runId" class="run-label">{{ runId }}</span>
      </div>
      <div class="answer">{{ answer }}</div>
    </section>

    <section v-if="charts.length" class="result-card chart-card">
      <div class="section-heading">
        <h2>图表</h2>
      </div>
      <div
        v-for="(_chart, index) in charts"
        :key="index"
        :ref="(element) => setChartElement(element, index)"
        class="chart"
      />
    </section>

    <section v-if="evidence.length" class="result-card evidence-card">
      <details>
        <summary>查看依据（{{ evidence.length }} 条）</summary>
        <div class="evidence-list">
          <details v-for="item in evidence" :key="item.qn" class="evidence-item">
            <summary>
              <span class="qn">{{ item.qn }}</span>
              <span>{{ item.tool }}</span>
              <span class="rows">{{ item.n_rows }} 行</span>
            </summary>
            <pre>{{ formatDetail(item.detail) }}</pre>
          </details>
        </div>
      </details>
    </section>

    <p v-if="sessionId" class="session-label">本页已保持多轮会话</p>
  </main>
</template>
