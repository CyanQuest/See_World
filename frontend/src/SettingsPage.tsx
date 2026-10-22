import React, { useEffect, useState } from 'react';
import {
  Sparkles,
  ChevronDown,
  Check,
  Loader2,
  AlertCircle,
  ShieldCheck,
  SlidersHorizontal,
} from 'lucide-react';
import type { Config } from './types';
import { api } from './api';
import { Field } from './components';

import settingLimits from './settingsLimits.json';
const limits: Record<string, { min: number; max: number | null; integer: boolean }> = settingLimits;
export function SettingsPage({
  config,
  onSave,
  onError,
}: {
  config: Config;
  onSave: (v: Config) => Promise<void>;
  onError: (message: string) => void;
}) {
  const [form, setForm] = useState<Config>({ ...config, moss_key: '', llm_key: '', cookies: '' }),
    [saving, setSaving] = useState(false),
    [saved, setSaved] = useState(false),
    [health, setHealth] = useState<any>(null),
    [testProvider, setTestProvider] = useState(''),
    [saveError, setSaveError] = useState('');
  useEffect(() => {
    api('/health')
      .then(setHealth)
      .catch((e) => onError(e.message));
  }, []);
  const edit = (key: string, v: any) => {
    setForm({ ...form, [key]: v });
    setSaved(false);
    setSaveError('');
  };
  const save = async () => {
    setSaved(false);
    setSaveError('');
    setSaving(true);
    try {
      for (const [key, label] of numberFields) {
        const b = limits[key],
          v = form[key];
        if (
          b &&
          (typeof v !== 'number' ||
            !Number.isFinite(v) ||
            v < b.min ||
            (b.max !== null && v > b.max) ||
            (b.integer && !Number.isInteger(v)))
        )
          throw Error(`${label}：请输入${b.integer ? '整数，' : ''}${b.min}–${b.max ?? '不限'}`);
      }
      const patch = Object.fromEntries(
        Object.entries(form).filter(([k]) => !k.endsWith('_configured')),
      );
      await onSave(patch);
      setForm({ ...form, moss_key: '', llm_key: '', cookies: '' });
      setSaved(true);
    } catch (e) {
      setSaveError((e as Error).message);
      onError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };
  const input = (key: string, label: string, type = 'text', placeholder = '') => (
    <Field
      label={
        label + (limits[key] ? `（允许 ${limits[key].min}–${limits[key].max ?? '不限'}）` : '')
      }
    >
      <input
        type={type}
        min={limits[key]?.min}
        max={limits[key]?.max ?? undefined}
        step={type === 'number' ? (limits[key]?.integer ? 1 : 'any') : undefined}
        value={(form[key] ?? '') as string}
        placeholder={placeholder}
        autoComplete={type === 'password' ? 'new-password' : 'off'}
        onChange={(e) =>
          edit(
            key,
            type === 'number'
              ? e.target.value === ''
                ? null
                : Number(e.target.value)
              : e.target.value,
          )
        }
      />
    </Field>
  );
  const numberFields = [
    ['search_count', '每组候选视频数'],
    ['search_groups', '搜索词组数（住行玩吃至少 4 组）'],
    ['max_videos', '首批视频数（实际最多5个）'],
    ['max_minutes', '总时长上限（分钟）'],
    ['max_video_minutes', '单视频上限（分钟）'],
    ['recent_months', '优先近几个月资料'],
    ['transcribe_concurrency', '转写并发数'],
    ['chunk_seconds', '分片时长（秒）'],
    ['overlap_seconds', '分片重叠（秒）'],
    ['chunk_mb', '分片大小上限（MB）'],
    ['subtitle_chars', '字幕有效字符下限'],
    ['subtitle_coverage', '字幕覆盖率下限（0–1）'],
    ['moss_timeout', 'Moss 响应超时（秒）'],
    ['llm_timeout', '攻略模型响应超时（秒）'],
    ['context_budget', '模型上下文预算（tokens）'],
    ['output_tokens', '单次输出 token 上限'],
    ['revision_rounds', '每批文字修改轮数（0–2）'],
    ['max_requests', '每任务请求上限'],
    ['max_llm_tokens', '每任务 token 上限'],
    ['max_audio_minutes', '提交音频上限（分钟）'],
  ];
  return (
    <>
      <div className="page-heading compact">
        <div className="eyebrow">MAKE IT YOURS</div>
        <h1>连接与设置</h1>
        <p>两组服务分别配置，完整密钥只会留在本机后端。</p>
      </div>
      <div className="settings-grid">
        <section className="panel">
          <div className="settings-title">
            <span className="service-icon">M</span>
            <div>
              <h2>Moss 语音转写</h2>
              <p>固定使用 moss-transcribe-1.0</p>
            </div>
            <span className={'config-dot ' + (config.moss_key_configured ? 'ready' : '')} />
          </div>
          {input('moss_base', '服务 Base URL', 'url', 'https://api.mosi.cn')}
          {input(
            'moss_key',
            'API Key',
            'password',
            config.moss_key_configured ? '已保存 · 留空保留原密钥' : '粘贴 Moss API Key',
          )}
          <p className="small-muted">只有需要语音转写时才会调用此服务。</p>
          <button className="secondary" disabled={saving} onClick={() => setTestProvider('moss')}>
            <ShieldCheck size={16} />
            测试已保存的连接
          </button>
        </section>
        <section className="panel">
          <div className="settings-title">
            <span className="service-icon llm">
              <Sparkles size={21} />
            </span>
            <div>
              <h2>攻略生成模型</h2>
              <p>兼容 Chat Completions 接口</p>
            </div>
            <span className={'config-dot ' + (config.llm_key_configured ? 'ready' : '')} />
          </div>
          {input('llm_provider', '服务商名称')}
          {input('llm_base', '服务 Base URL', 'url', 'https://你的服务地址/v1')}
          {input('llm_model', '模型名称', 'text', '填写服务商提供的准确模型名')}
          {input(
            'llm_key',
            'API Key',
            'password',
            config.llm_key_configured ? '已保存 · 留空保留原密钥' : '粘贴攻略模型 API Key',
          )}
          <button className="secondary" disabled={saving} onClick={() => setTestProvider('llm')}>
            <ShieldCheck size={16} />
            测试已保存的连接
          </button>
        </section>
      </div>
      <section className="panel">
        <h2>Bilibili 登录状态</h2>
        <p className="muted">
          部分字幕或视频需要登录。仅在需要时导入你主动导出的 Netscape 格式 Cookie，应用只保留
          Bilibili 域。
        </p>
        <div className="cookie-row">
          <input
            aria-label="导入 Cookie 文件"
            type="file"
            accept=".txt"
            onChange={async (e) => {
              const file = e.target.files?.[0];
              if (file) {
                if (file.size > 1000000) {
                  onError('Cookie 文件不能超过 1 MB');
                  return;
                }
                edit('cookies', await file.text());
              }
            }}
          />
          <span className="small-muted">
            {form.cookies
              ? '已选择新文件，保存后生效'
              : config.cookies_configured
                ? '已保存登录状态'
                : '尚未导入'}
          </span>
          {config.cookies_configured && (
            <button
              className="quiet danger"
              onClick={async () => {
                try {
                  await api('/settings/credentials/cookies', 'DELETE');
                  await onSave({});
                  setForm({ ...form, cookies: '' });
                } catch (e) {
                  onError((e as Error).message);
                }
              }}
            >
              清除
            </button>
          )}
        </div>
      </section>
      <section className="panel">
        <details>
          <summary>
            <span>
              <SlidersHorizontal size={18} />
              资源限制与处理选项
            </span>
            <ChevronDown size={18} />
          </summary>
          <div className="form-grid three">
            {numberFields.map(([k, label]) => (
              <React.Fragment key={k}>{input(k, label, 'number')}</React.Fragment>
            ))}
          </div>
          <label className="check-label">
            <input
              type="checkbox"
              checked={!!form.retain_media}
              onChange={(e) => edit('retain_media', e.target.checked)}
            />
            保留下载媒体
          </label>
          <p className="small-muted">
            默认：成功任务的媒体 24 小时后清理，失败任务保留 7 天；原始文本与攻略持续保留。
          </p>
        </details>
      </section>
      <section className="panel">
        <h2>费用估算</h2>
        <p className="muted">选填，统一按人民币单价估算。未填写时显示“费用未知”。</p>
        <div className="form-grid">
          {input('moss_price_minute', 'Moss · 元 / 音频分钟', 'number')}
          {input('llm_price_million', '攻略模型 · 元 / 百万 tokens（统一单价）', 'number')}
        </div>
        <p className="small-muted">
          输入与输出分别计价的服务，请以账单为准。本应用按请求数、音频时长和 token
          限制资源，不把估算当作账单。
        </p>
      </section>
      <section className="panel">
        <h2>本机环境</h2>
        {health ? (
          <div className="health-grid">
            <span>
              <Check size={16} />
              Python {health.python}
            </span>
            <span>
              <Check size={16} />
              yt-dlp {health.yt_dlp}
            </span>
            <span>
              {health.ffmpeg ? <Check size={16} /> : <AlertCircle size={16} />}FFmpeg{' '}
              {health.ffmpeg ? '可用' : '未安装'}
            </span>
            <span>数据占用 {(health.data_bytes / 1024 / 1024).toFixed(1)} MB</span>
          </div>
        ) : (
          <p>正在检查…</p>
        )}
        <p className="small-muted">{health?.probe_mode} · 数据库及文本储存在应用 data 目录。</p>
      </section>
      <div className="save-bar">
        <span
          role={saveError ? 'alert' : 'status'}
          className={saveError ? 'error-text' : undefined}
        >
          {saveError ? (
            <>保存失败：{saveError}</>
          ) : saved ? (
            <>
              <Check size={16} />
              设置已保存
            </>
          ) : (
            <>密钥不写入攻略、导出文件或浏览器存储</>
          )}
        </span>
        <button className="primary" disabled={saving} onClick={save}>
          {saving ? <Loader2 className="spin" size={17} /> : <Check size={17} />}保存设置
        </button>
      </div>
      {testProvider && (
        <div className="overlay">
          <section className="confirm-dialog" role="dialog" aria-modal="true">
            <h2>测试已保存的连接</h2>
            <p>
              会实际调用{testProvider === 'moss' ? ' Moss 接口，上传 1 秒静音样本' : '攻略模型接口'}
              ，可能产生少量费用。请先保存你的设置。
            </p>
            <div className="task-actions">
              <button className="secondary" disabled={saving} onClick={() => setTestProvider('')}>
                返回
              </button>
              <button
                className="primary"
                disabled={saving}
                onClick={async () => {
                  setSaving(true);
                  try {
                    const r = await api('/settings/test', 'POST', {
                      provider: testProvider,
                      acknowledge_cost: true,
                    });
                    onError(r.message);
                  } catch (e) {
                    onError((e as Error).message);
                  } finally {
                    setSaving(false);
                    setTestProvider('');
                  }
                }}
              >
                {saving ? <Loader2 size={16} className="spin" /> : '开始测试'}
              </button>
            </div>
          </section>
        </div>
      )}
    </>
  );
}
