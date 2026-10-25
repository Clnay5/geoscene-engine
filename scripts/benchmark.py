"""End-to-end API benchmark: real remote COG tile, warm cache, and durable ROI job."""
import argparse,json,math,platform,time
from datetime import datetime,timezone
from pathlib import Path
import numpy as np,requests

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--base',default='http://127.0.0.1:8091');args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];s=requests.Session()
    # Wuhan tile at z=13, derived from public study coordinates (not user location).
    z=13;lon=114.39;lat=30.55;x=int((lon+180)/360*2**z)
    y=int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*2**z)
    url=f'{args.base}/tiles/wuhan-green/{z}/{x}/{y}.png'
    durations=[];statuses=[]
    for i in range(11):
        t=time.perf_counter();r=s.get(url,timeout=240);r.raise_for_status()
        durations.append((time.perf_counter()-t)*1000);statuses.append(r.headers.get('X-Cache'))
        if i==0:
            (root/'docs').mkdir(exist_ok=True);(root/'docs'/'real-tile.png').write_bytes(r.content)
    payload={'dataset':'wuhan-green','bbox':[114.38,30.54,114.40,30.56]}
    start=time.perf_counter();r=s.post(args.base+'/jobs',json=payload,timeout=15);r.raise_for_status();j=r.json()
    deadline=time.monotonic()+240
    while time.monotonic()<deadline:
        q=s.get(args.base+j['status_url'],timeout=15).json()
        if q['state'] in ('succeeded','failed'):break
        time.sleep(.2)
    else:raise TimeoutError('Job did not finish')
    assert q['state']=='succeeded',q
    elapsed=(time.perf_counter()-start)*1000
    dedup=s.post(args.base+'/jobs',json=payload,timeout=15).json()
    assert dedup['deduplicated'] and dedup['id']==j['id']
    result={'generated_at':datetime.now(timezone.utc).isoformat(),'python':platform.python_version(),
            'scope':'one remote tile; 1 initial request + 10 sequential repeats, local HTTP client; no throughput/SLO claim',
            'cache_states':statuses,'tile_path':url.replace(args.base,''),'durations_ms':durations,
            'first_request_ms':durations[0],'warm_p50_ms':float(np.median(durations[1:])),
            'warm_p95_ms':float(np.percentile(durations[1:],95)),
            'job_total_ms':elapsed,'job':q,'deduplication_verified':True,
            'metrics':s.get(args.base+'/metrics').json()}
    (root/'evidence').mkdir(exist_ok=True);(root/'evidence/benchmark.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
