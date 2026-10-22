import ipaddress
import re
import socket
from urllib.parse import urlparse, parse_qs, urljoin
import httpx

class Attention(Exception): pass
class DeferredRetry(Attention): pass
class UnknownOutcome(Attention): pass  # a paid request may have been billed
class Cancelled(Exception): pass
class InvalidOutput(Exception): pass

def canonical_video(value):
    value = value.strip()
    if re.fullmatch(r'(BV[0-9A-Za-z]{10}|av[0-9]+)', value): value = 'https://www.bilibili.com/video/' + value
    parsed = urlparse(value)
    if parsed.scheme != 'https' or parsed.hostname not in ('www.bilibili.com', 'bilibili.com', 'm.bilibili.com') or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError('仅接受 https://www.bilibili.com/video/BV… 或 av… 标准视频链接')
    match = re.fullmatch(r'/video/(BV[0-9A-Za-z]{10}|av[0-9]+)/?', parsed.path)
    if not match: raise ValueError('视频路径无效，请复制标准 BV/AV 链接')
    try: part = int(parse_qs(parsed.query).get('p', ['1'])[0])
    except ValueError: raise ValueError('分 P 必须为数字')
    if not 1 <= part <= 1000: raise ValueError('分 P 超出范围')
    return f'https://www.bilibili.com/video/{match[1]}?p={part}'

def public_url(value, media=False):
    p = urlparse(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password:
        raise ValueError('服务和媒体地址必须为不含用户信息的 HTTPS 地址')
    if media and not any(p.hostname == d or p.hostname.endswith('.'+d) for d in ('bilibili.com', 'bilivideo.com', 'bilivideo.cn', 'hdslb.com', 'biliapi.net', 'biliapi.com')):
        raise ValueError('拒绝非 Bilibili 媒体或字幕域名')
    try: addresses = socket.getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror: raise ValueError('无法解析服务地址，请检查网络和域名')
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('拒绝访问本机、私网或保留地址')
    return value

def get_public(value):
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        for _ in range(5):
            public_url(value, media=True)
            response = client.get(value, headers={'Referer': 'https://www.bilibili.com/'})
            if response.is_redirect:
                value = urljoin(value, response.headers['location'])
                continue
            response.raise_for_status()
            if len(response.content) > 10_000_000: raise ValueError('字幕文件过大')
            return response.text
    raise ValueError('字幕重定向次数过多')

def filter_cookies(content):
    lines = ['# Netscape HTTP Cookie File']
    for line in content.splitlines():
        if not line.strip() or (line.startswith('#') and not line.startswith('#HttpOnly_')): continue
        fields = line.split('\t')
        if len(fields) != 7: raise ValueError('Cookie 必须为 Netscape 文件格式')
        domain = fields[0].removeprefix('#HttpOnly_').lstrip('.')
        if domain == 'bilibili.com' or domain.endswith('.bilibili.com'): lines.append(line)
    if len(lines) == 1: raise ValueError('文件中没有 Bilibili Cookie')
    return '\n'.join(lines) + '\n'

def safe_error(error):
    if isinstance(error, (Attention, ValueError, InvalidOutput)): return str(error)[:500]
    if isinstance(error, httpx.HTTPStatusError): return f'外部请求失败（HTTP {error.response.status_code}）'
    return '处理失败（' + type(error).__name__ + '），请检查网络、视频权限或服务设置后重试'
