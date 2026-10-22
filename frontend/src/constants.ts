import { Leaf, UtensilsCrossed, Camera, Landmark, ShoppingBag, Baby } from 'lucide-react';
import type { Prefs } from './types';

export const defaults: Prefs = {
  destination: '',
  days: 3,
  people: 1,
  date: '',
  origin: '',
  budget: '',
  include_arrival: false,
  interests: [],
  pace: '适中',
  notes: '',
  selection: 'auto',
  transcription: 'subtitle',
  links: [],
};
export const statuses: Record<string, string> = {
  queued: '排队中',
  searching: '寻找资料',
  selecting: '待选视频',
  processing: '整理来源',
  generating: '编排攻略',
  completed: '已完成',
  completed_with_warnings: '已完成 · 有待核实项',
  failed: '处理失败',
  cancelled: '已取消',
  needs_attention: '需要处理',
  candidate: '候选视频',
  partial: '文本不完整',
  pending: '等待处理',
  submitting: '正在转写',
  unknown: '结果未知',
};
export const active = ['queued', 'searching', 'processing', 'generating'];
export const interestOptions = [
  ['美食', UtensilsCrossed],
  ['自然', Leaf],
  ['人文', Landmark],
  ['摄影', Camera],
  ['购物', ShoppingBag],
  ['亲子', Baby],
] as const;
export const date = (t: number) =>
  new Date(t * 1000).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
export const duration = (s: number | null) =>
  s ? `${Math.floor(Math.round(s) / 60)} 分 ${Math.round(s) % 60} 秒` : '时长未知';
