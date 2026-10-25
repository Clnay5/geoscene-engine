import os,json,hashlib,threading
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from typing import Annotated
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, field_validator
from .cache import TileCache
from .jobs import JobStore, QueueFull
from .raster import render_tile, statistics, tile_bounds

ROOT=Path(__file__).resolve().parents[1]
ASSETS=Path(__file__).resolve().parent/'assets'
class StatsRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    dataset:str
    bbox:tuple[float,float,float,float]
    @field_validator('bbox')
    @classmethod
    def check_bbox(cls,b):
        if not(-180<=b[0]<b[2]<=180 and -85<=b[1]<b[3]<=85):raise ValueError('Ordered WGS84 rectangle required')
        if b[2]-b[0]>.2 or b[3]-b[1]>.2:raise ValueError('Each ROI side must be <=0.2 degree')
        return b

def create_app(registry=None,runtime=None,tile_fn=render_tile,stats_fn=statistics,start_worker=True):
    registry=registry or json.loads(Path(os.environ.get('GEOSCENE_REGISTRY',ASSETS/'datasets.json')).read_text())
    runtime=Path(runtime or os.environ.get('GEOSCENE_RUNTIME',Path.cwd()/'runtime'));runtime.mkdir(exist_ok=True,parents=True)
    cache=TileCache();slots=threading.BoundedSemaphore(4)
    def lookup(name):
        if name not in registry:raise HTTPException(404,'Unknown registered dataset')
        return registry[name]
    def compute(payload):
        entry=registry[payload['dataset']]
        if entry['revision']!=payload['revision']:raise ValueError('Source revision changed since job admission')
        return stats_fn(entry,payload['bbox'])
    @asynccontextmanager
    async def lifespan(app):
        store=JobStore(runtime/'jobs.sqlite',compute);app.state.jobs=store
        if start_worker:store.start()
        yield
        store.close()
    app=FastAPI(title='GeoScene Engine',version='0.1.0',lifespan=lifespan)
    app.state.cache=cache
    @app.get('/health')
    def health():return {'status':'ok','mode':'single-process bounded prototype','tile_cache':cache.info()}
    @app.get('/datasets')
    def datasets():return {k:{a:b for a,b in v.items() if a!='url'} for k,v in registry.items()}
    @app.get('/tiles/{name}/{z}/{x}/{y}.png')
    def tile(name:str,z:int,x:int,y:int,request:Request):
        entry=lookup(name)
        try:tile_bounds(z,x,y)
        except ValueError as e:raise HTTPException(422,str(e))
        start=perf_counter()
        if not slots.acquire(blocking=False):raise HTTPException(429,'Tile concurrency limit reached',headers={'Retry-After':'1'})
        try:
            key=(name,entry['revision'],entry['scale'],entry['offset'],tuple(entry['display_range']),z,x,y)
            png,hit=cache.get_or_compute(key,lambda:tile_fn(entry,z,x,y))
        except Exception as e:raise HTTPException(502,f'Raster source failed: {type(e).__name__}')
        finally:slots.release()
        etag='"'+hashlib.sha256(png).hexdigest()+'"'
        headers={'ETag':etag,'Cache-Control':'public,max-age=300','X-Cache':'HIT' if hit else 'MISS',
                 'Server-Timing':f'tile;dur={(perf_counter()-start)*1000:.2f}'}
        if request.headers.get('If-None-Match')==etag:return Response(status_code=304,headers=headers)
        return Response(png,media_type='image/png',headers=headers)
    @app.post('/jobs',status_code=202)
    def submit(body:StatsRequest):
        entry=lookup(body.dataset);payload=body.model_dump(mode='json');payload['revision']=entry['revision']
        try:job,dedup=app.state.jobs.submit(payload)
        except QueueFull:raise HTTPException(429,'Statistics queue full',headers={'Retry-After':'5'})
        return {**job,'deduplicated':dedup,'status_url':'/jobs/'+job['id']}
    @app.get('/jobs/{job_id}')
    def job(job_id:str):
        r=app.state.jobs.get(job_id)
        if r is None:raise HTTPException(404,'Unknown job')
        return r
    @app.get('/metrics')
    def metrics():return {'tile_cache':cache.info(),'maximum_parallel_tiles':4,'maximum_pending_statistics':16,
                          'note':'In-process counters reset on restart; not an SLO or distributed metric.'}
    app.mount('/viewer',StaticFiles(directory=ASSETS/'web',html=True),name='viewer')
    return app

app=create_app()
