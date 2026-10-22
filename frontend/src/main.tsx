import { useEffect, useState, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import {
  ArrowUpRight,
  ArrowRight,
  Plus,
  Compass,
  MapPin,
  Clock3,
  BookOpen,
  Settings2,
  Sparkles,
  X,
  FileText,
  Loader2,
  AlertCircle,
  RotateCcw,
  Trash2,
  CircleStop,
  ShieldCheck,
  SlidersHorizontal,
  Video,
  Menu,
  ChevronRight,
  Footprints,
  KeyRound,
} from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import './style.css';
import type { Prefs, Task, Config, Guide } from './types';
import { defaults, statuses, active, date, duration } from './constants';
import { api } from './api';
import { Badge, PreferenceFields, TripCard } from './components';
import { SettingsPage } from './SettingsPage';
import { GuideView, RevisionPanels, ResourcePanel, SourceModal } from './TaskPanels';

function App() {
  const actionLock = useRef(false);
  const [page, setPage] = useState('new'),
    [tasks, setTasks] = useState<Task[]>([]),
    [config, setConfig] = useState<Config | null>(null),
    [task, setTask] = useState<Task | null>(null),
    [prefs, setPrefs] = useState<Prefs>(defaults),
    [busy, setBusy] = useState(false),
    [notice, setNotice] = useState(''),
    [mobile, setMobile] = useState(false),
    [guide, setGuide] = useState<Guide | null>(null),
    [source, setSource] = useState<any>(null),
    [selected, setSelected] = useState<string[]>([]),
    [linkText, setLinkText] = useState(''),
    [version, setVersion] = useState(0),
    [confirmDelete, setConfirmDelete] = useState<string | null>(null),
    [retryConfirm, setRetryConfirm] = useState(false),
    [regenerate, setRegenerate] = useState(false);
  const refresh = async () => {
    const [list, settings] = await Promise.all([api<Task[]>('/tasks'), api<Config>('/settings')]);
    setTasks(list);
    setConfig(settings);
  };
  useEffect(() => {
    refresh().catch((e) => setNotice(e.message));
    const hash = location.hash.slice(1);
    if (hash.startsWith('task/')) openTask(hash.slice(5));
    else if (['new', 'history', 'settings'].includes(hash)) setPage(hash);
  }, []);
  const running = !!task && (active.includes(task.status) || !!task.owner);
  useEffect(() => {
    if (!task || page !== 'task' || !running) return;
    let alive = true,
      lastEvent = task.events[0]?.id;
    const timer = setInterval(async () => {
      try {
        const t = await api<Task>('/tasks/' + task.id);
        const progressed = t.events[0]?.id !== lastEvent || t.status !== task.status;
        lastEvent = t.events[0]?.id;
        const next =
          t.versions.length &&
          (guide === null ||
            (version === 0 && (t.versions[0].version > guide.version || progressed)))
            ? await api<Guide>('/tasks/' + t.id + '/guides/' + version)
            : null;
        if (!alive) return;
        if (t.status === 'selecting' && task.status !== 'selecting')
          setSelected(t.sources.filter((s) => s.selected).map((s) => s.id));
        setTask(t);
        setTasks((list) =>
          list.map((x) =>
            x.id === t.id
              ? {
                  ...x,
                  status: t.status,
                  stage: t.stage,
                  updated: t.updated,
                  source_count: t.sources.filter((s) => s.has_transcript).length,
                  version: t.versions[0]?.version,
                }
              : x,
          ),
        );
        if (next) {
          setGuide(next);
          refresh();
        }
      } catch (e) {
        if (alive) setNotice((e as Error).message);
      }
    }, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [task?.id, task?.status, running, page, guide?.version, version]);
  const action = async (fn: () => Promise<void>) => {
    if (actionLock.current) return;
    actionLock.current = true;
    setBusy(true);
    setNotice('');
    try {
      await fn();
    } catch (e) {
      setNotice((e as Error).message);
    } finally {
      setBusy(false);
      actionLock.current = false;
    }
  };
  const go = (p: string) => {
    setPage(p);
    setMobile(false);
    setNotice('');
    location.hash = p;
    refresh().catch((e) => setNotice(e.message));
  };
  const openTask = async (id: string) => {
    setGuide(null);
    setVersion(0);
    setRegenerate(false);
    setPage('task');
    location.hash = 'task/' + id;
    try {
      const t = await api<Task>('/tasks/' + id);
      setTask(t);
      setSelected(t.sources.filter((s) => s.selected).map((s) => s.id));
      if (t.versions.length) setGuide(await api('/tasks/' + id + '/guides/0'));
    } catch (e) {
      setNotice((e as Error).message);
    }
  };
  const create = () =>
    action(async () => {
      const t = await api<{ id: string }>('/tasks', 'POST', {
        ...prefs,
        links: prefs.links.map((x) => x.trim()).filter(Boolean),
      });
      await refresh();
      await openTask(t.id);
    });
  const viewSource = (sid: string) =>
    action(async () =>
      setSource(await api('/sources/' + sid + '/transcript').then((v) => ({ ...v, id: sid }))),
    );
  const reload = async () => {
    if (task) setTask(await api('/tasks/' + task.id));
    await refresh();
  };
  return (
    <div className="app">
      <aside className={'sidebar ' + (mobile ? 'mobile-open' : '')}>
        <a className="brand" href="#new" onClick={() => go('new')}>
          <span className="brand-icon">
            <Compass size={25} />
          </span>
          <span>
            见世界<small>JIAN SHI JIE</small>
          </span>
        </a>
        <div className="sidebar-label">你的旅行工作台</div>
        <nav>
          {(
            [
              ['new', '新建攻略', Plus],
              ['history', '我的攻略', BookOpen],
              ['settings', '连接与设置', Settings2],
            ] as const
          ).map(([key, label, Icon]) => (
            <button
              key={key as string}
              className={page === key ? 'nav active' : 'nav'}
              onClick={() => go(key as string)}
            >
              <Icon size={19} />
              <span>{label as string}</span>
              {key === 'new' && <span className="nav-shortcut">＋</span>}
            </button>
          ))}
        </nav>
        <div className="sidebar-label recent-label">
          最近的计划 <span>{tasks.length}</span>
        </div>
        <div className="recent-list">
          {tasks.slice(0, 5).map((t) => (
            <button
              key={t.id}
              className={'recent ' + (task?.id === t.id && page === 'task' ? 'selected' : '')}
              onClick={() => openTask(t.id)}
            >
              <MapPin size={15} />
              <span>
                {t.prefs.destination}
                <small>
                  {t.prefs.days} 天 · {statuses[t.status]}
                </small>
              </span>
              <ChevronRight size={13} />
            </button>
          ))}
          {!tasks.length && (
            <p className="sidebar-empty">
              第一份旅行计划
              <br />
              从一个目的地开始。
            </p>
          )}
        </div>
        <div className="local-note">
          <ShieldCheck size={18} />
          <div>
            只属于你的旅行资料<small>本机运行 · 密钥加密保存</small>
          </div>
          <span className="live-dot" />
        </div>
      </aside>
      <div className="main-wrap">
        <header>
          <button
            className="mobile-menu icon-btn"
            aria-label="展开导航"
            onClick={() => setMobile(!mobile)}
          >
            <Menu size={22} />
          </button>
          <div className="breadcrumb">
            工作台 <ChevronRight size={14} />
            <span>
              {page === 'new'
                ? '新建攻略'
                : page === 'history'
                  ? '我的攻略'
                  : page === 'settings'
                    ? '连接与设置'
                    : task?.prefs.destination || '任务详情'}
            </span>
          </div>
          <span className="local-pill">
            <span /> 本机工作空间
          </span>
        </header>
        <main>
          {notice && (
            <div className="notice" role="alert">
              <AlertCircle size={19} />
              <span>{notice}</span>
              <button className="icon-btn" aria-label="关闭提示" onClick={() => setNotice('')}>
                <X size={16} />
              </button>
            </div>
          )}
          {page === 'new' && (
            <>
              <div className="page-heading">
                <div className="eyebrow">YOUR NEXT CHAPTER</div>
                <h1>
                  下一站，想去哪里？<span className="heading-star">✳</span>
                </h1>
                <p>把 B 站里的旅行经验，整理成一份有据可查的出行计划。</p>
              </div>
              <section className="planner-card">
                <div className="card-top">
                  <span>
                    <span className="step-dot">01</span> 从一个目的地开始
                  </span>
                  <span className="small-muted">新建旅行计划</span>
                </div>
                <label className="destination-label" htmlFor="destination">
                  旅行目的地
                </label>
                <div className="destination-box">
                  <MapPin size={26} />
                  <input
                    autoFocus
                    id="destination"
                    placeholder="城市、地区，或一个想去的地方"
                    value={prefs.destination}
                    maxLength={100}
                    onChange={(e) => setPrefs({ ...prefs, destination: e.target.value })}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && prefs.destination.trim()) create();
                    }}
                  />
                </div>
                <div className="suggestions">
                  <span>从这里找灵感</span>
                  {['成都', '京都', '大理', '泉州'].map((city) => (
                    <button onClick={() => setPrefs({ ...prefs, destination: city })} key={city}>
                      {city}
                      <ArrowUpRight size={12} />
                    </button>
                  ))}
                </div>
                <PreferenceFields value={prefs} onChange={setPrefs} />
                <div className="planner-bottom">
                  <div className="default-summary">
                    <Clock3 size={16} />
                    <span>{prefs.days} 天</span>
                    <span className="dot">·</span>
                    <span>{prefs.people} 人</span>
                    <span className="dot">·</span>
                    <span>{prefs.pace}节奏</span>
                  </div>
                  <button
                    className="primary"
                    disabled={busy || !prefs.destination.trim()}
                    onClick={create}
                  >
                    {busy ? <Loader2 className="spin" size={18} /> : <Sparkles size={18} />}
                    生成旅行攻略
                    <ArrowRight size={18} />
                  </button>
                </div>
              </section>
              {config && !config.llm_key_configured && (
                <div className="setup-note">
                  <span className="setup-icon">
                    <KeyRound size={20} />
                  </span>
                  <div>
                    <strong>出发之前，连接你的模型</strong>
                    <p>填写攻略模型与 Moss 转写服务。现在也可以先搜索、选择视频。</p>
                  </div>
                  <button onClick={() => go('settings')}>
                    前往设置
                    <ArrowRight size={16} />
                  </button>
                </div>
              )}
              <div className="process-grid">
                {(
                  [
                    [Video, '真实的视频来源', '自动寻找 Bilibili 攻略，也可以亲自选择视频。'],
                    [FileText, '每一条建议，有出处', '优先使用字幕，需要时转写；保留原文与来源。'],
                    [Footprints, '按你的节奏出发', '结合天数和兴趣编排，未知信息清楚标注。'],
                  ] as const
                ).map(([Icon, title, description], i) => (
                  <div className="process-card" key={i}>
                    <div className="process-icon">
                      <Icon size={21} />
                      <span>0{i + 1}</span>
                    </div>
                    <h3>{title as string}</h3>
                    <p>{description as string}</p>
                  </div>
                ))}
              </div>
              <div className="section-title">
                <h2>你的旅行手记</h2>
                {tasks.length > 0 && (
                  <button onClick={() => go('history')}>
                    查看全部 <ArrowRight size={15} />
                  </button>
                )}
              </div>
              {tasks.length ? (
                <div className="trip-grid">
                  {tasks.slice(0, 3).map((t) => (
                    <TripCard key={t.id} task={t} onOpen={() => openTask(t.id)} />
                  ))}
                </div>
              ) : (
                <div className="empty-inline">
                  <BookOpen size={25} />
                  <div>
                    <strong>还没有旅行计划</strong>
                    <span>生成的攻略会保存在这里，随时回来继续整理。</span>
                  </div>
                </div>
              )}
              <p className="page-footnote">
                来源中的票价、营业时间和预约规则，请在出行前再次核实。
              </p>
            </>
          )}
          {page === 'history' && (
            <>
              <div className="title-row">
                <div>
                  <div className="eyebrow">YOUR TRAVEL JOURNAL</div>
                  <h1>我的攻略</h1>
                  <p>保留每次出发的灵感，也保留每一版计划。</p>
                </div>
                <button className="primary" onClick={() => go('new')}>
                  <Plus size={18} />
                  新建攻略
                </button>
              </div>
              {tasks.length ? (
                <div className="trip-grid history">
                  {tasks.map((t) => (
                    <div key={t.id}>
                      <TripCard task={t} onOpen={() => openTask(t.id)} />
                      {!active.includes(t.status) && (
                        <button
                          className="quiet danger delete-link"
                          onClick={() => setConfirmDelete(t.id)}
                        >
                          <Trash2 size={14} />
                          删除计划
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="empty-large">
                  <BookOpen size={40} />
                  <h2>旅程尚未开始</h2>
                  <p>创建第一份攻略，把散落的视频经验带上路。</p>
                  <button className="primary" onClick={() => go('new')}>
                    规划一次旅行
                    <ArrowRight size={16} />
                  </button>
                </div>
              )}
            </>
          )}
          {page === 'settings' && config && (
            <SettingsPage
              config={config}
              onSave={async (patch) => {
                await api('/settings', 'PUT', patch);
                await refresh();
              }}
              onError={setNotice}
            />
          )}
          {page === 'task' && task && (
            <>
              <div className="title-row">
                <div>
                  <div className="eyebrow">TRIP WORKSPACE</div>
                  <h1>
                    {task.prefs.destination}
                    <span className="title-days">{task.prefs.days} 日计划</span>
                  </h1>
                  <p>
                    {task.prefs.people} 人 · {task.prefs.pace}节奏 · 创建于 {date(task.created)}
                  </p>
                </div>
                <Badge status={task.status} />
              </div>
              {task.error && (
                <div className={'notice ' + (task.status === 'cancelled' ? 'neutral' : '')}>
                  <AlertCircle size={19} />
                  <span>{task.error}</span>
                  {task.status === 'needs_attention' && (
                    <button className="text-link" onClick={() => go('settings')}>
                      检查设置
                    </button>
                  )}
                </div>
              )}
              {!guide && (
                <section className="task-progress panel">
                  <div className="title-row">
                    <div>
                      <h2>{task.stage}</h2>
                      <p>
                        已获得 {task.sources.filter((s) => s.has_transcript).length}/
                        {task.sources.filter((s) => s.selected).length} 个视频的文本 · 最后更新{' '}
                        {date(task.updated)}
                      </p>
                    </div>
                    {active.includes(task.status) && <Loader2 className="spin" size={23} />}
                  </div>
                  <div className="stage-track">
                    {['搜索视频', '筛选视频', '获取文本', '整理资料', '生成攻略', '检查结果'].map(
                      (s, i) => (
                        <div key={s} className={task.stage === s ? 'current' : ''}>
                          <span>{i + 1}</span>
                          <small>{s}</small>
                        </div>
                      ),
                    )}
                  </div>
                  {task.chunks.length > 0 && (
                    <p className="muted">
                      音频分片：{task.chunks.filter((c) => c.status === 'completed').length} 成功 /{' '}
                      {task.chunks.filter((c) => ['failed', 'unknown'].includes(c.status)).length}{' '}
                      失败或未知 / 共 {task.chunks.length} 段
                    </p>
                  )}
                </section>
              )}
              <div className="task-actions">
                {active.includes(task.status) || task.status === 'selecting' ? (
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={() =>
                      action(async () => {
                        await api('/tasks/' + task.id + '/cancel', 'POST');
                        await reload();
                      })
                    }
                  >
                    <CircleStop size={16} />
                    取消任务
                  </button>
                ) : (
                  <>
                    {['failed', 'cancelled', 'needs_attention'].includes(task.status) && (
                      <button
                        className="secondary"
                        disabled={busy || !!task.owner}
                        onClick={() => {
                          if (task.unknown_requests) setRetryConfirm(true);
                          else
                            action(async () => {
                              await api('/tasks/' + task.id + '/retry', 'POST', {});
                              await reload();
                            });
                        }}
                      >
                        <RotateCcw size={16} />
                        继续 / 重试
                      </button>
                    )}
                    {guide?.content.quality_review && !guide.content.quality_review.passed && (
                      <button
                        className="secondary"
                        disabled={busy || !!task.owner}
                        onClick={() =>
                          action(async () => {
                            await api('/tasks/' + task.id + '/revise', 'POST');
                            await reload();
                          })
                        }
                      >
                        <RotateCcw size={16} />
                        利用现有资料修改
                      </button>
                    )}
                    {task.sources.some((s) => s.has_transcript) && (
                      <button
                        className="secondary"
                        disabled={busy || !!task.owner}
                        onClick={() => {
                          setPrefs(task.prefs);
                          setRegenerate(!regenerate);
                        }}
                      >
                        <SlidersHorizontal size={16} />
                        调整偏好，生成新版
                      </button>
                    )}
                  </>
                )}
                {busy && <Loader2 size={18} className="spin" />}
              </div>
              {regenerate && (
                <div className="panel">
                  <h2>调整下一版计划</h2>
                  <p className="muted">复用已保存的原始资料，旧版攻略会保留。</p>
                  <PreferenceFields value={prefs} onChange={setPrefs} expanded />
                  <button
                    className="primary"
                    disabled={busy}
                    onClick={() =>
                      action(async () => {
                        await api('/tasks/' + task.id + '/regenerate', 'POST', prefs);
                        setRegenerate(false);
                        setGuide(null);
                        setVersion(0);
                        await reload();
                      })
                    }
                  >
                    生成新版本
                    <ArrowRight size={16} />
                  </button>
                </div>
              )}
              {guide && (
                <div className="panel">
                  <h2>
                    {guide.content.quality_review
                      ? `GLM 自评 ${guide.content.quality_review.score} / 100 · ${guide.content.quality_review.passed ? '达标' : '未达标'}`
                      : '此版本尚未完成质量审核'}
                  </h2>
                  <p>
                    {active.includes(task.status)
                      ? `正在规划路线、修改或审核；确有证据缺口才补资料，累计最多处理 ${task.video_limits.total} 个视频。`
                      : guide.content.quality_review?.stop_reason ||
                        '审核门槛：至少 85 分，且无关键错误。'}
                  </p>
                  {guide.content.quality_review && (
                    <p className="muted">
                      累计处理 {guide.content.quality_review.processed_videos} /{' '}
                      {task.video_limits.total} 个视频；详细评分和修改意见见文末。评分为 GLM 自评。
                    </p>
                  )}
                </div>
              )}
              {guide && (
                <GuideView
                  task={task}
                  guide={guide}
                  onVersion={(v) =>
                    action(async () => {
                      setVersion(v);
                      setGuide(await api('/tasks/' + task.id + '/guides/' + v));
                    })
                  }
                  onCopy={() =>
                    action(async () => {
                      await navigator.clipboard.writeText(guide.markdown);
                      setNotice('攻略已复制');
                    })
                  }
                />
              )}
              {guide?.review_markdown && (
                <section className="panel review-appendix">
                  <details>
                    <summary>GLM 审核附录（独立于正文）</summary>
                    <Markdown remarkPlugins={[remarkGfm]}>{guide.review_markdown}</Markdown>
                  </details>
                </section>
              )}
              <section className="sources-section">
                <div className="section-title">
                  <h2>
                    {task.status === 'selecting' ? '选择用于攻略的视频' : '来源与处理记录'}{' '}
                    <span className="count">{task.sources.length}</span>
                  </h2>
                  <span className="small-muted">
                    {task.status === 'selecting'
                      ? `每批最多 ${Math.min(task.video_limits.batch, Number(config?.max_videos || task.video_limits.batch))} 条 / 共 ${config?.max_minutes} 分钟`
                      : '保留视频原文与处理状态'}
                  </span>
                </div>
                {task.status === 'selecting' && (
                  <div className="selection-bar">
                    <span>
                      已选 {selected.length} 个 ·{' '}
                      {duration(
                        task.sources
                          .filter((s) => selected.includes(s.id))
                          .reduce((a, s) => a + (s.meta.duration || 0), 0),
                      )}
                    </span>
                    <button
                      className="primary"
                      disabled={busy || !selected.length}
                      onClick={() =>
                        action(async () => {
                          await api('/tasks/' + task.id + '/selection', 'POST', {
                            source_ids: selected,
                          });
                          await reload();
                        })
                      }
                    >
                      使用这些视频
                      <ArrowRight size={16} />
                    </button>
                  </div>
                )}
                <div className="source-list">
                  {task.sources.map((s, i) => (
                    <div
                      key={s.id}
                      className={
                        'source-card ' +
                        (selected.includes(s.id) && task.status === 'selecting' ? 'checked' : '')
                      }
                    >
                      {task.status === 'selecting' ? (
                        <input
                          type="checkbox"
                          aria-label={'选择 ' + s.meta.title}
                          checked={selected.includes(s.id)}
                          onChange={(e) =>
                            setSelected(
                              e.target.checked
                                ? [...selected, s.id]
                                : selected.filter((x) => x !== s.id),
                            )
                          }
                        />
                      ) : (
                        <span className="source-number">{String(i + 1).padStart(2, '0')}</span>
                      )}
                      <div className="source-body">
                        <a
                          href={s.meta.url}
                          target="_blank"
                          rel="noreferrer"
                          className="source-title"
                        >
                          {s.meta.title}
                          <ArrowUpRight size={15} />
                        </a>
                        <p>
                          {s.meta.author} <span>·</span> {s.meta.published || '发布日期未知'}{' '}
                          <span>·</span> {duration(s.meta.duration)} <span>·</span> P{s.meta.part}
                        </p>
                        <small>{s.reason}</small>
                        {s.error && <p className="error-text">{s.error}</p>}
                        <details className="chunk-details">
                          <summary>处理详情</summary>
                          {task.chunks
                            .filter((c) => c.source_id === s.id)
                            .map((c) => (
                              <div key={c.id}>
                                {Math.round(c.start)}–{Math.round(c.end)} 秒：
                                {statuses[c.status] || c.status} {c.error}
                              </div>
                            ))}
                          {!task.chunks.some((c) => c.source_id === s.id) && (
                            <span>暂无音频分片；字幕成功时无需转写。</span>
                          )}
                        </details>
                      </div>
                      <div className="source-right">
                        <Badge status={s.status} />
                        {s.has_transcript && (
                          <button className="text-link" onClick={() => viewSource(s.id)}>
                            查看原文
                            <ChevronRight size={14} />
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
                {!active.includes(task.status) && !task.owner && (
                  <div className="panel add-links">
                    <label htmlFor="links">补充 Bilibili 视频链接</label>
                    <p className="small-muted">
                      支持标准 BV / AV 链接，每行一个；分 P 请保留 ?p= 参数。
                    </p>
                    <div>
                      <textarea
                        id="links"
                        placeholder="https://www.bilibili.com/video/BV…"
                        value={linkText}
                        onChange={(e) => setLinkText(e.target.value)}
                      />
                      <button
                        className="secondary"
                        disabled={busy || !linkText.trim()}
                        onClick={() =>
                          action(async () => {
                            const r = await api('/tasks/' + task.id + '/sources', 'POST', {
                              links: linkText
                                .split('\n')
                                .map((x) => x.trim())
                                .filter(Boolean),
                            });
                            if (r.errors.length) setNotice(r.errors.join('；'));
                            if (r.added.length) setLinkText('');
                            await reload();
                          })
                        }
                      >
                        {busy ? <Loader2 className="spin" size={16} /> : <Plus size={16} />}添加视频
                      </button>
                    </div>
                  </div>
                )}
              </section>
              <RevisionPanels task={task} />
              <ResourcePanel task={task} />
            </>
          )}
        </main>
        <footer>
          <span>见世界</span>
          <span>把经验带上路。</span>
          <span>LOCAL EDITION / 01</span>
        </footer>
      </div>
      {source && <SourceModal source={source} onClose={() => setSource(null)} />}
      {(confirmDelete || retryConfirm) && (
        <div className="overlay">
          <section className="confirm-dialog" role="dialog" aria-modal="true">
            <AlertCircle size={30} />
            <h2>{confirmDelete ? '删除这份旅行计划？' : '继续可能重复计费'}</h2>
            <p>
              {confirmDelete
                ? '将删除此任务的攻略版本、原始文本与下载文件，无法撤销。'
                : '此前的外部请求结果未知。请先检查服务商记录；确认后，仅重试尚未完成的部分。'}
            </p>
            <div className="task-actions">
              <button
                className="secondary"
                onClick={() => {
                  setConfirmDelete(null);
                  setRetryConfirm(false);
                }}
              >
                返回
              </button>
              <button
                className="primary"
                disabled={busy}
                onClick={() =>
                  action(async () => {
                    if (confirmDelete) {
                      await api('/tasks/' + confirmDelete, 'DELETE');
                      setConfirmDelete(null);
                      go('history');
                    } else if (task) {
                      await api('/tasks/' + task.id + '/retry', 'POST', {
                        acknowledge_unknown: true,
                      });
                      setRetryConfirm(false);
                      await reload();
                    }
                  })
                }
              >
                {confirmDelete ? '确认删除' : '理解并继续'}
              </button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
createRoot(document.getElementById('root')!).render(<App />);
