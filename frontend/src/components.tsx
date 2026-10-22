import React from 'react';
import { ArrowUpRight, MapPin, ChevronDown, Check, SlidersHorizontal } from 'lucide-react';
import type { Prefs, Task } from './types';
import { statuses, interestOptions, date } from './constants';

export function Badge({ status }: { status: string }) {
  return (
    <span className={'badge ' + status}>
      <span />
      {statuses[status] || status}
    </span>
  );
}
export function PreferenceFields({
  value: v,
  onChange,
  expanded = false,
}: {
  value: Prefs;
  onChange: (v: Prefs) => void;
  expanded?: boolean;
}) {
  const change = (k: keyof Prefs, value: any) => onChange({ ...v, [k]: value });
  return (
    <details className="preferences" open={expanded || undefined}>
      <summary>
        <span>
          <SlidersHorizontal size={16} />
          更多旅行偏好
        </span>
        <span className="small-muted">
          天数、预算、兴趣
          <ChevronDown size={15} />
        </span>
      </summary>
      <div className="preferences-inner">
        <div className="form-grid">
          <Field label="旅行天数">
            <input
              type="number"
              min={1}
              max={30}
              value={v.days}
              onChange={(e) => change('days', Number(e.target.value))}
            />
          </Field>
          <Field label="同行人数">
            <input
              type="number"
              min={1}
              max={20}
              value={v.people}
              onChange={(e) => change('people', Number(e.target.value))}
            />
          </Field>
          <Field label="出行日期或月份">
            <input
              placeholder="例如：2026 年 10 月"
              value={v.date}
              onChange={(e) => change('date', e.target.value)}
            />
          </Field>
          <Field label="出发地">
            <input
              placeholder="未填写则不规划往返班次"
              value={v.origin}
              onChange={(e) => change('origin', e.target.value)}
            />
          </Field>
          <Field label="人均预算 · 人民币">
            <input
              placeholder="例如：2000–3000 元"
              value={v.budget}
              onChange={(e) => change('budget', e.target.value)}
            />
          </Field>
          <Field label="旅行节奏">
            <select value={v.pace} onChange={(e) => change('pace', e.target.value)}>
              {['轻松', '适中', '紧凑'].map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </Field>
        </div>
        <label className="check-label">
          <input
            type="checkbox"
            checked={v.include_arrival}
            onChange={(e) => change('include_arrival', e.target.checked)}
          />
          预算包含往返目的地交通
        </label>
        <div className="field">
          <label>感兴趣的体验</label>
          <div className="interest-list">
            {interestOptions.map(([name, Icon]) => (
              <button
                type="button"
                key={name}
                className={v.interests.includes(name) ? 'chosen' : ''}
                onClick={() =>
                  change(
                    'interests',
                    v.interests.includes(name)
                      ? v.interests.filter((x) => x !== name)
                      : [...v.interests, name],
                  )
                }
              >
                <Icon size={16} />
                {name}
                {v.interests.includes(name) && <Check size={13} />}
              </button>
            ))}
          </div>
        </div>
        <Field label="其他要求">
          <textarea
            placeholder="例如：不自驾、少走路、带长辈同行……"
            maxLength={2000}
            value={v.notes}
            onChange={(e) => change('notes', e.target.value)}
          />
        </Field>
        <div className="form-grid">
          <Field label="视频选择方式">
            <select value={v.selection} onChange={(e) => change('selection', e.target.value)}>
              <option value="auto">自动筛选并继续</option>
              <option value="manual">先选视频，再生成</option>
            </select>
          </Field>
          <Field label="资料获取方式">
            <select
              value={v.transcription}
              onChange={(e) => change('transcription', e.target.value)}
            >
              <option value="subtitle">优先使用合格字幕</option>
              <option value="moss">始终使用 Moss 转写</option>
            </select>
          </Field>
        </div>
        <Field label="已有视频链接（可选，每行一个）">
          <textarea
            placeholder="填写后直接处理这些视频，跳过搜索"
            value={v.links.join('\n')}
            onChange={(e) => change('links', e.target.value.split('\n'))}
          />
        </Field>
      </div>
    </details>
  );
}
export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  const id = React.useId();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {React.isValidElement(children)
        ? React.cloneElement(children as React.ReactElement<any>, { id })
        : children}
    </div>
  );
}
export function TripCard({ task: t, onOpen }: { task: Task; onOpen: () => void }) {
  return (
    <button className="trip-card" onClick={onOpen}>
      <div className="trip-card-top">
        <span className="trip-mark">
          <MapPin size={21} />
        </span>
        <ArrowUpRight size={19} />
      </div>
      <div className="trip-destination">
        {t.prefs.destination}
        <span>{t.prefs.days} DAYS</span>
      </div>
      <p>
        {t.prefs.pace}节奏 · {t.source_count || 0} 个来源
      </p>
      <div className="trip-card-bottom">
        <Badge status={t.status} />
        <small>{date(t.created)}</small>
      </div>
    </button>
  );
}
