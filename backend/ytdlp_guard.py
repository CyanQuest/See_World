"""Pinned yt-dlp adapter with HTTPS/public Bilibili-only network requests."""
import socket
import ipaddress
import yt_dlp
from yt_dlp.networking.common import RequestDirector
from yt_dlp.networking._urllib import RedirectHandler
from .security import public_url
from yt_dlp.extractor.bilibili import BiliBiliSearchIE
from .bilibili_search import search_results

BiliBiliSearchIE._search_results = search_results

_original_send=RequestDirector.send

def guarded_send(self,request):
    public_url(request.url,media=True)
    return _original_send(self,request)
RequestDirector.send=guarded_send
_original_redirect=RedirectHandler.redirect_request

def guarded_redirect(self,req,fp,code,msg,headers,newurl):
    public_url(newurl,media=True)
    return _original_redirect(self,req,fp,code,msg,headers,newurl)
RedirectHandler.redirect_request=guarded_redirect

# Check DNS answers at the actual connect step as well (DNS rebinding guard).
_original_dns=socket.getaddrinfo

def guarded_dns(*args,**kwargs):
    result=_original_dns(*args,**kwargs)
    if any(not ipaddress.ip_address(item[4][0]).is_global for item in result):
        raise ValueError('拒绝访问非公网地址')
    return result
socket.getaddrinfo=guarded_dns

_original_build=yt_dlp.YoutubeDL.build_request_director

def guarded_build(self,handlers,preferences=None):
    return _original_build(self,[h for h in handlers if h.RH_KEY=='Urllib'],preferences)
yt_dlp.YoutubeDL.build_request_director=guarded_build

if __name__=='__main__':
    import sys
    yt_dlp.main(['--use-extractors','BiliBili.*','--proxy','','--compat-options','prefer-legacy-http-handler',*sys.argv[1:]])
