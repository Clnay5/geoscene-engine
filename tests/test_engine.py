import io,time,threading
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pytest,rasterio
from rasterio.transform import from_origin,from_bounds
from fastapi.testclient import TestClient
from PIL import Image
from geoscene_engine.cache import TileCache
from geoscene_engine.jobs import JobStore,QueueFull
from geoscene_engine.api import create_app
from geoscene_engine.raster import statistics,render_tile,tile_bounds

REG={'test':{'url':'not-read','revision':'v1','scale':.0001,'offset':0,'display_range':[0,.3]}}

def test_cache_singleflight_and_lru():
    cache=TileCache(capacity=1);calls=[]
    def compute():calls.append(1);time.sleep(.03);return b'ok'
    with ThreadPoolExecutor(max_workers=8) as pool:
        result=list(pool.map(lambda _:cache.get_or_compute('a',compute),range(8)))
    assert len(calls)==1 and sum(x[1] for x in result)==7
    cache.get_or_compute('b',lambda:b'new')
    cache.get_or_compute('a',compute)
    assert len(calls)==2 and cache.info()['entries']==1

def test_cache_expiry_and_exception_retry():
    cache=TileCache(ttl=0)
    cache.get_or_compute('a',lambda:1)
    assert cache.get_or_compute('a',lambda:2)==(2,False)
    def fail():raise IOError('source')
    with pytest.raises(IOError):cache.get_or_compute('x',fail)
    assert cache.get_or_compute('x',lambda:3)==(3,False)

def test_job_transaction_admission_and_dedup(tmp_path):
    q=JobStore(tmp_path/'jobs.db',lambda x:x,max_pending=2)
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs=list(pool.map(lambda _:q.submit({'a':1}),range(8)))
    assert len({x[0]['id'] for x in jobs})==1 and sum(x[1] for x in jobs)==7
    q.submit({'a':2})
    with pytest.raises(QueueFull):q.submit({'a':3})
    assert q.work_once();q.submit({'a':3})

def test_restart_requeues_interrupted_work_and_failures(tmp_path):
    path=tmp_path/'jobs.db';q=JobStore(path,lambda x:x)
    j,_=q.submit({'a':1})
    with q.connect() as c:c.execute("UPDATE jobs SET state='running'")
    def fail(x):raise ValueError('invalid ROI')
    recovered=JobStore(path,fail)
    assert recovered.get(j['id'])['state']=='queued'
    recovered.work_once();result=recovered.get(j['id'])
    assert result['state']=='failed' and 'invalid ROI' in result['error']

def test_api_etag_cache_and_no_arbitrary_source(tmp_path):
    calls=[]
    def tile(*args):calls.append(args);return b'png-fixture'
    app=create_app(REG,tmp_path,tile_fn=tile,stats_fn=lambda *_:{'count':2})
    with TestClient(app) as c:
        assert c.get('/viewer/').status_code==200
        assert 'url' not in c.get('/datasets').json()['test']
        r=c.get('/tiles/test/1/1/1.png');assert r.status_code==200 and r.headers['x-cache']=='MISS'
        r2=c.get('/tiles/test/1/1/1.png',headers={'If-None-Match':r.headers['etag']})
        assert r2.status_code==304 and r2.headers['x-cache']=='HIT' and len(calls)==1
        assert c.get('/tiles/unknown/1/1/1.png').status_code==404
        assert c.get('/tiles/test/1/2/0.png').status_code==422
        assert c.post('/jobs',json={'dataset':'test','bbox':[0,0,.01,.01],'url':'http://127.0.0.1'}).status_code==422
        assert c.post('/jobs',json={'dataset':'test','bbox':[1,0,0,1]}).status_code==422
        assert c.post('/jobs',json={'dataset':'test','bbox':[0,0,1,1]}).status_code==422
        j=c.post('/jobs',json={'dataset':'test','bbox':[0,0,.01,.01]}).json()
        for _ in range(50):
            status=c.get(j['status_url']).json()
            if status['state']=='succeeded':break
            time.sleep(.01)
        assert status['result']['count']==2
        assert c.post('/jobs',json={'dataset':'test','bbox':[0,0,.01,.01]}).json()['deduplicated']

def test_queue_backpressure_endpoint(tmp_path):
    app=create_app(REG,tmp_path,start_worker=False)
    with TestClient(app) as c:
        for i in range(16):assert c.post('/jobs',json={'dataset':'test','bbox':[i/1000,0,i/1000+.001,.001]}).status_code==202
        assert c.post('/jobs',json={'dataset':'test','bbox':[.02,0,.021,.001]}).status_code==429

def test_native_stats_nodata_scaling_and_population_std(tmp_path):
    p=tmp_path/'raster.tif'
    a=np.array([[0,1000],[2000,3000]],dtype='uint16')
    with rasterio.open(p,'w',driver='GTiff',height=2,width=2,count=1,dtype='uint16',
                       crs='EPSG:4326',transform=from_origin(0,.02,.01,.01),nodata=0) as d:d.write(a,1)
    r=statistics({**REG['test'],'url':str(p)},[0,0,.02,.02])
    assert r['count']==3 and r['nodata_pixels']==1
    assert r['mean']==pytest.approx(.2) and r['std_population']==pytest.approx(np.std([.1,.2,.3]))

def test_real_gdal_tile_png_and_transparent_nodata(tmp_path):
    p=tmp_path/'tile.tif';bounds=tile_bounds(1,1,1)
    a=np.full((256,256),1500,dtype='uint16');a[:64]=0
    with rasterio.open(p,'w',driver='GTiff',height=256,width=256,count=1,dtype='uint16',
                       crs='EPSG:3857',transform=from_bounds(*bounds,256,256),nodata=0) as d:d.write(a,1)
    png=render_tile({**REG['test'],'url':str(p)},1,1,1)
    image=np.array(Image.open(io.BytesIO(png)))
    assert image.shape==(256,256,4) and image[0,0,3]==0 and image[128,128,3]==255
