import io, math, hashlib
from contextlib import contextmanager
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.transform import from_bounds
from rasterio.windows import from_bounds as window_from_bounds, Window
from rasterio.warp import transform_bounds
from PIL import Image
from .rangeio import opener

@contextmanager
def dataset(entry):
    opts={'opener':opener} if entry['url'].startswith('https://') else {}
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR'):
        with rasterio.open(entry['url'],**opts) as src:yield src

def tile_bounds(z,x,y):
    if not 0<=z<=18 or not 0<=x<2**z or not 0<=y<2**z:raise ValueError('Invalid WebMercator tile coordinate')
    half=20037508.342789244;size=2*half/(2**z)
    return -half+x*size,half-(y+1)*size,-half+(x+1)*size,half-y*size

def render_tile(entry,z,x,y):
    bounds=tile_bounds(z,x,y);transform=from_bounds(*bounds,256,256)
    with dataset(entry) as src:
        # Warp directly to exactly one tile. No global Mercator raster allocation.
        with WarpedVRT(src,crs='EPSG:3857',transform=transform,width=256,height=256,
                       nodata=-9999, dtype='float32',resampling=Resampling.bilinear) as vrt:
            arr=vrt.read(1,masked=True)
    values=arr.filled(np.nan)*entry['scale']+entry['offset']
    valid=np.isfinite(values)
    lo,hi=entry['display_range'];norm=np.clip((np.nan_to_num(values)-lo)/(hi-lo),0,1)
    rgba=np.zeros((256,256,4),dtype='uint8')
    rgba[:,:,0]=(norm*210+15).astype('uint8');rgba[:,:,1]=(norm*220+24).astype('uint8')
    rgba[:,:,2]=(norm*170+30).astype('uint8');rgba[:,:,3]=valid.astype('uint8')*255
    stream=io.BytesIO();Image.fromarray(rgba).save(stream,format='PNG')
    return stream.getvalue()

def statistics(entry,bbox):
    """Native pixel-center rectangle sample. No approximate overviews or cloud mask."""
    with dataset(entry) as src:
        bounds=transform_bounds('EPSG:4326',src.crs,*bbox,densify_pts=21)
        win=window_from_bounds(*bounds,src.transform)
        x0=max(0,math.ceil(win.col_off-.5));y0=max(0,math.ceil(win.row_off-.5))
        x1=min(src.width,math.ceil(win.col_off+win.width-.5));y1=min(src.height,math.ceil(win.row_off+win.height-.5))
        count=max(0,x1-x0)*max(0,y1-y0)
        if count==0:raise ValueError('No intersecting native pixels')
        if count>1_000_000:raise ValueError('ROI exceeds one million native pixels; use a smaller rectangle')
        arr=src.read(1,window=Window(x0,y0,x1-x0,y1-y0),masked=True)
        values=arr.compressed().astype('float64')*entry['scale']+entry['offset']
        values=values[np.isfinite(values)]
        if values.size==0:raise ValueError('All pixels are nodata')
        return {'count':int(values.size),'total_pixels':count,'nodata_pixels':count-int(values.size),
                'min':float(values.min()),'max':float(values.max()),'mean':float(values.mean()),
                'std_population':float(values.std()),'p05':float(np.percentile(values,5)),
                'p50':float(np.percentile(values,50)),'p95':float(np.percentile(values,95)),
                'crs':str(src.crs),'window':[x0,y0,x1-x0,y1-y0],
                'pixel_centers_envelope_wgs84':list(bbox),
                'source_revision':entry['revision'],'cloud_mask_applied':False,
                'method':'native-resolution projected envelope, pixel centers; no resampling',
                'valid_values_sha256':hashlib.sha256(values.tobytes()).hexdigest()}
