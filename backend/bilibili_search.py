"""Bilibili search adapter for the pinned yt-dlp runtime."""
from yt_dlp.extractor.bilibili import BilibiliBaseIE
from yt_dlp.utils import ExtractorError


def search_results(self, query):
    signer = BilibiliBaseIE(self._downloader)
    # One page contains at least our configured maximum of 10 candidates.
    params = signer._sign_wbi({'keyword': query, 'search_type': 'video', 'page': 1}, query)
    response = self._download_json(
        'https://api.bilibili.com/x/web-interface/wbi/search/type', query,
        query=params, headers={'Referer': 'https://search.bilibili.com/'},
        note='Searching Bilibili')
    if response.get('code') != 0:
        raise ExtractorError('Bilibili search rejected request; code='+str(response.get('code')), expected=True)
    for video in (response.get('data') or {}).get('result') or []:
        bvid = video.get('bvid')
        url = 'https://www.bilibili.com/video/'+bvid if bvid else video.get('arcurl')
        if url:
            yield self.url_result(url, 'BiliBili', str(video.get('aid', '')))
