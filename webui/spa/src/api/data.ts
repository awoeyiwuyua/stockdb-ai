// api/data.ts — 私有存储（mydb）/ 查询台接口封装。
import { getJson, postJson } from './http'

// mydb 表清单（后端已过滤上游保留表，值为表名数组）
export const getTables = () => getJson<{ tables?: string[]; [k: string]: unknown }>('/api/data/tables')
export const readData = (table: string, key?: string) =>
  getJson<Record<string, unknown>>(`/api/data/read?table=${encodeURIComponent(table)}&key=${encodeURIComponent(key ?? '')}`)
// 写入：单条 {table, key, value} 或批量 {table, items:[[k,v],...]}
export const writeData = <T = unknown>(payload: Record<string, unknown>) => postJson<T>('/api/data/write', payload)
// 查询台：直查 stockdb 任意表（t 为查询语句）；返回形态不定，消费方 payloadToRows 归一化
export const queryStockdb = (t: string) => getJson<unknown>(`/api/query?t=${encodeURIComponent(t)}`)
