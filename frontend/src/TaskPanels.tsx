import { ArrowUpRight, X, Download, Copy, Printer, ExternalLink } from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Task, Guide } from './types';
import { date } from './constants';

export function GuideView({
  task,
  guide,
  onVersion,
  onCopy,
}: {
  task: Task;
  guide: Guide;
  onVersion: (version: number) => void;
  onCopy: () => void;
}) {
  return (
    <div className="guide-layout">
      <aside className="guide-toc">
        <span className="eyebrow">这份攻略</span>
        {[
          '攻略概览',
          '每日行程',
          '景点推荐',
          '美食建议',
          '住宿区域建议',
          '交通建议',
          '预算参考',
          '避坑与准备事项',
          '来源列表',
        ].map((h) => (
          <a
            href={'#section-' + h}
            key={h}
            onClick={(e) => {
              e.preventDefault();
              document.getElementById('section-' + h)?.scrollIntoView({ behavior: 'smooth' });
            }}
          >
            {h}
          </a>
        ))}
      </aside>
      <section className="guide-paper">
        <div className="guide-toolbar">
          <select
            aria-label="选择攻略版本"
            value={guide.version}
            onChange={(e) => onVersion(Number(e.target.value))}
          >
            {task.versions.map((v) => (
              <option key={v.version} value={v.version}>
                第 {v.version} 版 · {date(v.created)}
              </option>
            ))}
          </select>
          <div>
            <button title="复制攻略" aria-label="复制攻略" onClick={onCopy}>
              <Copy size={17} />
            </button>
            <a
              title="下载 Markdown"
              aria-label="下载 Markdown"
              href={'/api/tasks/' + task.id + '/export?version=' + guide.version}
            >
              <Download size={17} />
            </a>
            <button title="打印为 PDF" aria-label="打印为 PDF" onClick={() => window.print()}>
              <Printer size={17} />
            </button>
          </div>
        </div>
        <article className="markdown">
          <Markdown
            remarkPlugins={[remarkGfm]}
            components={{
              h2: ({ children }) => <h2 id={'section-' + String(children)}>{children}</h2>,
              h3: ({ children }) => {
                const text = String(children);
                return (
                  <h3 id={text.match(/^S\d+ /) ? 'source-' + text.split(' ')[0] : undefined}>
                    {children}
                  </h3>
                );
              },
              a: ({ href, children }) => {
                if (href?.startsWith('#source-')) {
                  const ref = Number(href.replace('#source-S', '')) - 1;
                  const sid = guide.content.source_ids[ref];
                  return sid ? <a href={href}>{children}</a> : <span>{children}</span>;
                }
                return (
                  <a href={href} target="_blank" rel="noreferrer">
                    {children}
                    <ExternalLink size={12} />
                  </a>
                );
              },
            }}
          >
            {guide.markdown}
          </Markdown>
        </article>
      </section>
    </div>
  );
}
export function RevisionPanels({ task }: { task: Task }) {
  return (
    <>
      {task.version_selection && (
        <section className="panel">
          <h3>版本选择</h3>
          <p>
            最新尝试：第 {task.version_selection.latest_version} 版；当前修改基准：第{' '}
            {task.version_selection.baseline_version} 版 · {task.version_selection.baseline_score}{' '}
            分
          </p>
          <p>{task.version_selection.baseline_reason}</p>
          <p className="muted">历史尝试完整保留，修改基准不代表已达标。</p>
        </section>
      )}
      {!!task.revision_issues?.length && (
        <section className="panel">
          <details>
            <summary>问题与内部证据检索</summary>
            {task.revision_issues.map((i, index) => (
              <div key={index}>
                <p>
                  {i.issue_id} · {i.description}
                </p>
                <p>
                  修改位置：{i.targets.join('、')}；证据：
                  {(
                    {
                      sufficient: '已有证据充分',
                      partial: '部分支持，须缩小安排',
                      gap: '检索后仍缺依据',
                      searching: '检索尚未完成',
                    } as Record<string, string>
                  )[i.status] || i.status}
                  ；复审：{i.review_status || '待复审'}
                </p>
              </div>
            ))}
            {task.evidence_searches?.map((r, index) => (
              <p key={index}>
                {r.issue_id}：扫描 {r.scope.items_scanned} 项，命中 {r.hits.length} 项；
                {r.complete ? '已完成' : '未完成，不触发补视频'}
              </p>
            ))}
          </details>
        </section>
      )}
      {!!task.quality_cycle?.history.length && (
        <section className="panel">
          <details>
            <summary>修改与证据缺口记录</summary>
            {task.quality_cycle.history.map((h, i) => (
              <div key={i}>
                <h3>
                  第 {h.version} 版 · {h.score} 分 · 已修改 {h.round} 轮
                </h3>
                <p>上一轮实际改动：{h.changed_sections?.join('、') || '初次分类'}</p>
                <p>本轮待修：{h.repairable.join('；') || '无'}</p>
                <p>仍缺证据：{h.evidence_gaps.join('；') || '无'}</p>
                <p>下一阶段：{h.next_phase}</p>
              </div>
            ))}
          </details>
        </section>
      )}
    </>
  );
}
export function ResourcePanel({ task }: { task: Task }) {
  return (
    <div className="detail-bottom">
      <section className="panel">
        <h3>资源限制与恢复</h3>
        {task.resources?.map((r) => (
          <p key={r.label}>
            {r.label}：已用 {r.used.toLocaleString()} / 上限 {r.limit.toLocaleString()} · 剩余{' '}
            {r.remaining.toLocaleString()}
          </p>
        ))}
        <p className="small-muted">
          实际 tokens 使用服务商返回值；服务商未返回用量的请求按预留计算。请求前按约 3 个 UTF-8 字节
          1 token
          估算输入，并预留该阶段的输出上限。剩余额度不足可能无法完成资料整理、修订和审核，补视频不保证达标。
        </p>
        <p>
          单次输出上限：{task.request_limits?.output_tokens}{' '}
          tokens（各阶段另有更低上限）；上下文预算：{task.request_limits?.context_budget}{' '}
          tokens（输入估算 + 输出预留）
        </p>
        {task.request_metrics && (
          <>
            <p>
              最近一次上下文：已用 {task.request_metrics.context_used} / 上限{' '}
              {task.request_metrics.context_limit} · 剩余{' '}
              {Math.max(0, task.request_metrics.context_limit - task.request_metrics.context_used)}
            </p>
            <p>
              最近一次输出：已用 {task.request_metrics.output_used ?? '未知 / 尚未返回'} / 上限{' '}
              {task.request_metrics.output_limit} · 剩余{' '}
              {task.request_metrics.output_used == null
                ? '未知'
                : Math.max(0, task.request_metrics.output_limit - task.request_metrics.output_used)}
            </p>
          </>
        )}
        <p>
          恢复阶段：
          {(
            {
              generate: '整理资料与路线草案',
              review: '审核已保存正文',
              classify: '区分可修复问题与证据缺口',
              revise: '利用现有资料修改',
              expand: '定向搜索补证',
              process: '处理补充视频',
              done: '本轮已结束',
            } as Record<string, string>
          )[task.quality_cycle?.phase || ''] || task.stage}
          ；本批已修改 {task.quality_cycle?.revision_round || 0} 轮。
        </p>
        {task.error && <p className="warning">当前阻塞 / 停止原因：{task.error}</p>}
        <p className="small-muted">
          设置保存后点击「继续 /
          重试」应用新限制，从已保存阶段恢复。审核重试复用正文，文字修改复用原文；未知请求仍需确认。单次用量随请求变化，以任务日志为准。
        </p>
        <h3>本次使用量</h3>
        {task.usage.length ? (
          task.usage.map((u) => (
            <div className="usage-row" key={u.provider}>
              <strong>{u.provider === 'moss' ? 'Moss 转写' : '攻略模型'}</strong>
              <span>{u.requests} 次请求</span>
              <span>
                {u.provider === 'moss'
                  ? `${(u.seconds / 60).toFixed(1)} 分钟已提交`
                  : `${u.tokens?.toLocaleString() || '未知'} tokens${u.unknown_tokens ? '（部分未知）' : ''}`}
              </span>
              <small>{u.unknown_cost ? '费用未知' : `估算 ¥${Number(u.cost).toFixed(4)}`}</small>
            </div>
          ))
        ) : (
          <p className="muted">尚未调用付费接口</p>
        )}
        <p className="small-muted">估算依据你填写的单价，实际费用以服务商账单为准。</p>
      </section>
      <section className="panel">
        <details>
          <summary>
            查看处理日志 <span className="small-muted">{task.events.length} 条</span>
          </summary>
          <div className="event-list">
            {task.events.map((e) => (
              <div key={e.id}>
                <time>{date(e.created)}</time>
                <span>{e.message}</span>
              </div>
            ))}
          </div>
        </details>
      </section>
    </div>
  );
}
export function SourceModal({ source, onClose }: { source: any; onClose: () => void }) {
  return (
    <div className="overlay" onClick={onClose}>
      <section
        className="source-modal"
        role="dialog"
        aria-modal="true"
        aria-label="视频来源详情"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-top">
          <span className="eyebrow">SOURCE NOTES</span>
          <button className="icon-btn" aria-label="关闭来源详情" onClick={onClose}>
            <X size={21} />
          </button>
        </div>
        <h2>{source.meta.title}</h2>
        <p className="muted">
          {source.meta.author} · P{source.meta.part} · {source.meta.published || '日期未知'}
        </p>
        <div className="task-actions">
          <a className="secondary" href={source.meta.url} target="_blank" rel="noreferrer">
            打开视频
            <ExternalLink size={15} />
          </a>
          <a className="secondary" href={'/api/sources/' + source.id + '/transcript?format=txt'}>
            下载 TXT
            <Download size={15} />
          </a>
        </div>
        <p className="source-explanation">
          {source.transcript?.type === 'subtitle'
            ? '以下时间范围来自字幕。'
            : '以下为 Moss 音频分片的大致位置，不是逐句时间戳。'}
        </p>
        {source.transcript?.missing?.map((r: number[], i: number) => (
          <p className="error-text" key={i}>
            缺失范围：{r[0]}–{r[1]} 秒
          </p>
        ))}
        <h3>原始资料</h3>
        <div className="transcript">
          {source.transcript?.segments.map((s: any, i: number) => (
            <div key={i}>
              <a
                href={source.meta.url + '&t=' + Math.floor(s.start)}
                target="_blank"
                rel="noreferrer"
              >
                {Math.floor(s.start)}–{Math.floor(s.end)} 秒<ArrowUpRight size={12} />
              </a>
              <p>{s.text}</p>
            </div>
          )) || <p>尚未取得文本。</p>}
        </div>
        <h3>已提取事实 · {source.facts?.facts.length || 0}</h3>
        {source.facts?.facts.map((f: any) => (
          <div className="fact" key={f.fact_id}>
            <strong>
              {f.entity_name} · {f.category}
            </strong>
            <p>{f.claim}</p>
            <blockquote>{f.evidence_excerpt}</blockquote>
          </div>
        ))}
        <details>
          <summary>原始响应与分片记录</summary>
          <pre>
            {JSON.stringify({ raw: source.transcript?.raw, chunks: source.chunks }, null, 2)}
          </pre>
        </details>
      </section>
    </div>
  );
}
