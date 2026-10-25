"""Small seekable HTTPS reader for Rasterio's documented Python opener.

Strict Range validation prevents a server ignoring Range from downloading a full scene.
TLS verification stays enabled. Cache is per handle and bounded to 16 MiB.
"""
import io
from collections import OrderedDict
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

class RangeReader(io.RawIOBase):
    def __init__(self, url, block_size=1024*1024):
        super().__init__();self.name=url;self.mode='rb';self.pos=0;self.block_size=block_size
        self.session=requests.Session()
        self.session.mount('https://',HTTPAdapter(max_retries=Retry(total=3,backoff_factor=.5,status_forcelist=[429,500,502,503,504])))
        r=self.session.get(url,headers={'Range':'bytes=0-0'},timeout=60)
        r.raise_for_status()
        if r.status_code!=206 or not r.headers.get('Content-Range','').startswith('bytes 0-0/'):
            raise IOError('Server must honor byte ranges')
        self.size=int(r.headers['Content-Range'].split('/')[-1]);self.cache=OrderedDict()
    def readable(self):return True
    def seekable(self):return True
    def tell(self):return self.pos
    def seek(self, offset, whence=0):
        target=offset if whence==0 else self.pos+offset if whence==1 else self.size+offset
        if target<0:raise ValueError('Negative seek')
        self.pos=target;return target
    def read(self, size=-1):
        if size<0:size=self.size-self.pos
        end=min(self.pos+size,self.size);chunks=[]
        while self.pos<end:
            block=self.pos//self.block_size;start=block*self.block_size
            if block not in self.cache:
                stop=min(start+self.block_size,self.size)-1
                r=self.session.get(self.name,headers={'Range':f'bytes={start}-{stop}'},timeout=60)
                r.raise_for_status()
                if r.status_code!=206 or r.headers.get('Content-Range')!=f'bytes {start}-{stop}/{self.size}' or len(r.content)!=stop-start+1:
                    raise IOError('Invalid byte-range response')
                self.cache[block]=r.content
                if len(self.cache)>16:self.cache.popitem(last=False)
            self.cache.move_to_end(block)
            off=self.pos-start;n=min(end-self.pos,len(self.cache[block])-off)
            chunks.append(self.cache[block][off:off+n]);self.pos+=n
        return b''.join(chunks)
    def close(self):
        if hasattr(self,'session'):self.session.close()
        super().close()

def opener(path, mode='rb'):
    if mode not in ('r','rb'):raise ValueError('Read only')
    if not path.startswith('https://'):raise FileNotFoundError(path)
    return RangeReader(path)
