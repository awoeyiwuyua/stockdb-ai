// use-mydb.js — 私有存储域状态机（0.10.18 自 views/OpsMydb.vue 迁入）。
//
// 与 use-sync 等的差别：返回 reactive 对象而非散装 refs——三个面板组件
// （ReadPanel/WritePanel/QueryConsole）需共享同一实例（tables 等状态跨组件），
// reactive 包裹后模板里 db.tables 直接是值、v-model 可写，props 传一个对象即可。
import { ref, computed, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getTables, readData, writeData, queryStockdb } from '../api/data.js'

const MAX_ROWS = 500 // 查询结果最大展示行数

// 任意值 → 展示文本：对象/数组 JSON 化，避免表格里出现 [object Object]
function displayValue(v) {
  if (v === null || v === undefined) return 'null'
  if (typeof v === 'object') {
    try {
      return JSON.stringify(v)
    } catch {
      return String(v)
    }
  }
  return String(v)
}

// 单条读取的 value 美化展示（pre 块缩进排版）
function pretty(v) {
  if (v === null || v === undefined) return 'null'
  if (typeof v === 'object') {
    try {
      return JSON.stringify(v, null, 2)
    } catch {
      return String(v)
    }
  }
  return String(v)
}

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

// 多个对象的所有字段名取并集（保持出现顺序、去重）
function unionKeys(items) {
  const set = new Set()
  items.forEach((item) => Object.keys(item).forEach((k) => set.add(k)))
  return [...set]
}

// 查询结果结构归一化：后端返回的 JSON 形态不确定（数组 / 对象 / 对象里套对象），
// 统一折叠成"列名数组 + 行数组"再喂给 el-table，什么形态都能表格化。
function payloadToRows(data) {
  if (data === null || data === undefined) return { columns: [], rows: [] }
  // 1) 数组：对象数组 → 各字段当列；标量数组 → 单列「值」
  if (Array.isArray(data)) {
    if (!data.length) return { columns: [], rows: [] }
    if (isPlainObject(data[0])) {
      const cols = unionKeys(data)
      return { columns: cols, rows: data.map((item) => cols.map((c) => displayValue(item[c]))) }
    }
    return { columns: ['值'], rows: data.map((v) => [displayValue(v)]) }
  }
  // 2) 普通对象：值全是对象 → 展开成「键 + 各字段列」；否则两列「键 / 值」
  if (isPlainObject(data)) {
    const entries = Object.entries(data)
    if (!entries.length) return { columns: [], rows: [] }
    const allObj = entries.every(([, v]) => isPlainObject(v))
    if (allObj) {
      const cols = unionKeys(entries.map(([, v]) => v))
      return { columns: ['键', ...cols], rows: entries.map(([k, v]) => [k, ...cols.map((c) => displayValue(v[c]))]) }
    }
    return { columns: ['键', '值'], rows: entries.map(([k, v]) => [k, displayValue(v)]) }
  }
  // 3) 其它（数字/字符串）：单行单列
  return { columns: ['结果'], rows: [[displayValue(data)]] }
}

// 解析 value 文本：能解析成 JSON 就按 JSON 存（对象/数字/布尔），否则按原始字符串存
function parseSingleValue(text) {
  const t = (text || '').trim()
  if (!t) return ''
  try {
    return JSON.parse(t)
  } catch {
    return t
  }
}

// 解析批量 JSON：支持 [[k,v],...] 或 {items:[[k,v],...]} 两种写法
function parseBatchPayload(text) {
  const t = (text || '').trim()
  if (!t) throw new Error('请输入批量 JSON')
  const obj = JSON.parse(t)
  const items = Array.isArray(obj) ? obj : obj && Array.isArray(obj.items) ? obj.items : null
  if (!items) throw new Error('批量格式需为 [["k",值],...] 或 {"items":[["k",值],...]}')
  if (!items.length) throw new Error('批量 items 不能为空')
  return items.map((pair) => [String(pair[0]), pair[1]])
}

export function useMydb() {
  // ---- 表清单 ----
  const tables = ref([]) // 自定义表名数组（后端已过滤上游保留表）
  const tablesLoading = ref(false)
  const tablesError = ref(null)

  // ---- 读取表单 ----
  const selTable = ref('') // 当前选中的表（读取区）
  const readKey = ref('') // 读取的 key，留空 = 列出全部
  const reading = ref(false)
  const readResult = ref(null) // {table,key,value} 或 {table,keys,values}
  const readError = ref(null)

  // ---- 写入表单 ----
  const writeMode = ref('single') // single | batch
  const writeTable = ref('')
  const writeKey = ref('')
  const writeValue = ref('')
  const batchPayload = ref('')
  const writing = ref(false)

  // ---- 查询台 ----
  const queryInput = ref('')
  const querying = ref(false)
  const queryDone = ref(false) // 是否执行过查询（区分"没查"和"查了但空"）
  const queryError = ref(null)
  const queryColumns = ref([]) // 动态列名
  const queryRows = ref([]) // 原始行（数组数组），表格数据在 computed 里转换

  // ---- 派生 ----
  // 读取结果两种形态：single = 有 value 字段；list = 有 keys 字段
  const readSingle = computed(() => (readResult.value && 'value' in readResult.value ? readResult.value : null))
  const readList = computed(() => (readResult.value && 'keys' in readResult.value ? readResult.value : null))
  // 全键列表 → 表格行（值统一转成展示文本，避免表格渲染 [object Object]）
  const readListRows = computed(() => {
    const v = readResult.value
    if (!v || !Array.isArray(v.keys)) return []
    return v.keys.map((k) => ({ key: k, val: displayValue(v.values?.[k]) }))
  })
  // 查询行（数组数组）→ el-table 要的对象数组（列名作 key）
  const queryTableData = computed(() =>
    queryRows.value.slice(0, MAX_ROWS).map((r) =>
      Object.fromEntries(queryColumns.value.map((col, i) => [col, r[i]]))
    )
  )
  const queryTruncated = computed(() => queryRows.value.length > MAX_ROWS)

  // ---- 表清单 ----
  // 拉表清单；手动刷新时转圈，轮询静默。保留当前选中表（还在清单里就不换）
  async function loadTables(manual = false) {
    if (manual) tablesLoading.value = true
    try {
      const r = await getTables()
      tables.value = r?.tables || []
      tablesError.value = null
      if (!tables.value.includes(selTable.value)) selTable.value = tables.value[0] || ''
    } catch (e) {
      tablesError.value = e?.message || '表清单接口不可用'
    } finally {
      tablesLoading.value = false
    }
  }

  // ---- 读取 ----
  // key 留空 = 读整表（后端返回 keys+values），填 key = 读单条
  async function doRead() {
    if (!selTable.value) {
      ElMessage.warning('请先选择表')
      return
    }
    reading.value = true
    readError.value = null
    try {
      readResult.value = await readData(selTable.value, readKey.value.trim())
    } catch (e) {
      readResult.value = null
      readError.value = e?.message || '读取失败'
      ElMessage.error(readError.value) // 后端 error 文案统一走 ElMessage
    } finally {
      reading.value = false
    }
  }

  // ---- 写入（危险操作：ElMessageBox.confirm 二次确认，取消直接 return）----
  async function doWrite() {
    const table = writeTable.value.trim()
    if (!table) {
      ElMessage.warning('请输入表名')
      return
    }
    let payload
    try {
      payload = writeMode.value === 'batch'
        ? { table, items: parseBatchPayload(batchPayload.value) }
        : { table, key: writeKey.value.trim(), value: parseSingleValue(writeValue.value) }
    } catch (e) {
      ElMessage.warning(e.message) // 客户端格式校验失败，还没发请求
      return
    }
    if (writeMode.value === 'single' && !payload.key) {
      ElMessage.warning('请输入 key')
      return
    }
    const confirmText = writeMode.value === 'batch'
      ? `确定向表「${table}」批量写入 ${payload.items.length} 条？写入后可用读取功能验证。`
      : `确定写入「${table}:${payload.key}」？写入后可用读取功能验证。`
    try {
      await ElMessageBox.confirm(confirmText, '确认写入', {
        type: 'warning',
        confirmButtonText: '写入',
        cancelButtonText: '取消',
      })
    } catch {
      return // 用户取消
    }
    writing.value = true
    try {
      const r = await writeData(payload)
      ElMessage.success(r?.msg || `已写入 ${r?.written ?? 0} 条`)
      await loadTables() // 表可能是新建的，刷新清单
    } catch (e) {
      ElMessage.error(e?.message || '写入失败') // 后端 400/501/500 的 error 文案
    } finally {
      writing.value = false
    }
  }

  // ---- 查询台 ----
  async function doQuery() {
    const t = queryInput.value.trim()
    if (!t) {
      ElMessage.warning('请输入查询表名（如 股票代码）')
      return
    }
    querying.value = true
    queryError.value = null
    try {
      const data = await queryStockdb(t)
      // 把任意 JSON 结构统一转成 {columns, rows}（数组数组）
      const { columns, rows } = payloadToRows(data)
      queryColumns.value = columns
      queryRows.value = rows
      queryDone.value = true
    } catch (e) {
      queryError.value = e?.message || '查询失败'
      queryDone.value = true
      queryColumns.value = []
      queryRows.value = []
      ElMessage.error(queryError.value)
    } finally {
      querying.value = false
    }
  }

  // reactive 包裹：refs 自动解包，props.db.tables 模板直接读值、v-model 可写
  return reactive({
    MAX_ROWS,
    tables, tablesLoading, tablesError,
    selTable, readKey, reading, readResult, readError,
    readSingle, readList, readListRows,
    writeMode, writeTable, writeKey, writeValue, batchPayload, writing,
    queryInput, querying, queryDone, queryError, queryColumns, queryRows,
    queryTableData, queryTruncated,
    loadTables, doRead, doWrite, doQuery,
    pretty,
  })
}
