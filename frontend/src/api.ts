let csrf = '';
export async function api<T = any>(path: string, method = 'GET', body?: unknown): Promise<T> {
  if (!csrf) csrf = (await fetch('/api/session').then((r) => r.json())).csrf;
  const r = await fetch('/api' + path, {
    method,
    headers: {
      'Content-Type': 'application/json',
      'X-Travel-CSRF': csrf,
      'Idempotency-Key': crypto.randomUUID(),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let value;
    try {
      value = await r.json();
    } catch {
      throw Error('服务暂时不可用，请确认应用已启动');
    }
    throw Error(typeof value.detail === 'string' ? value.detail : '输入不符合要求，请检查字段范围');
  }
  return r.json();
}
